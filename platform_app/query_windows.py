"""Bounded query-counter differences across every actual sampled endpoint.

Only enrich rows already authorized/redacted by the caller. Raw historical
digests never create result rows. Every adjacent pair must remain comparable;
first/last subtraction alone would hide an intermediate reset or eviction.
"""

from __future__ import annotations

import math
from sqlalchemy import select

from .common import PlatformError, iso
from .evidence import previous_frame_dict
from .metrics_derivation import derive_digest_window
from .models import CollectionRun, CollectionSchedule, Evidence, Instance, MetricFrame

MAX_FRAMES = 1000
# Match the existing UTC/monotonic derivation tolerance. Real source/frame
# observations are independently timestamped and may differ by milliseconds.
SOURCE_CLOCK_TOLERANCE_SECONDS = 5


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _counter(value):
    # COUNT_STAR and picosecond counters are JSON integers. Preserve integer
    # precision rather than rounding a large cumulative counter through float.
    return value if type(value) is int and value >= 0 else None


def _interval(value):
    value = _number(value)
    return value if value is not None and 0 < value <= 86400 else None


def _key(row):
    return row.get("schema_name"), row.get("digest")


def _raw_key(row):
    return row.get("SCHEMA_NAME"), row.get("DIGEST")


def _limit():
    raise PlatformError(
        503,
        "query_window_limit",
        "所选区间的摘要观察超过有界处理上限，请缩小时间范围。",
        {"maximum_observations": MAX_FRAMES},
    )


def apply_query_windows(db, instance_id, start, end, items):
    """Mutate only supplied visible rows; return safe observation/quality metadata."""
    keys = {_key(item) for item in items}
    for item in items:
        timer = _number(item.get("sum_timer_wait"))
        item.update(
            schema_name=item.get("schema_name"),
            window_count=None,
            total_time_ms=None,
            avg_time_ms=None,
            window_rows_examined=None,
            window_validity="unknown",
            window_reasons=[],
            cumulative_count=item.get("count_star"),
            cumulative_time_ms=timer / 1e9 if timer is not None else None,
            rows_examined=item.get("sum_rows_examined"),
        )
    frames = list(
        db.scalars(
            select(MetricFrame)
            .where(
                MetricFrame.instance_id == instance_id,
                MetricFrame.collector == "statement_digest",
                MetricFrame.observed_at >= start,
                MetricFrame.observed_at < end,
            )
            .order_by(MetricFrame.observed_at, MetricFrame.run_id)
            .limit(MAX_FRAMES + 1)
        )
    )
    if len(frames) > MAX_FRAMES:
        _limit()
    # Include unavailable source observations without a MetricFrame as explicit
    # gaps, including a failed attempt between two otherwise good endpoints.
    evidence = list(
        db.scalars(
            select(Evidence)
            .where(
                Evidence.instance_id == instance_id,
                Evidence.source == "mysql.statement_digest",
                Evidence.observed_at >= start,
                Evidence.observed_at < end,
            )
            .order_by(Evidence.observed_at, Evidence.id)
            .limit(MAX_FRAMES + 1)
        )
    )
    if len(evidence) > MAX_FRAMES:
        _limit()
    by_run = {}
    for source in evidence:
        by_run.setdefault(source.run_id, []).append(source)
    # A frame anchored just inside a requested edge can have its actual source
    # observation just outside it. Fetch that correspondence, then mark the edge
    # unknown rather than silently relabeling a source time as a frame time.
    missing = [f.run_id for f in frames if f.run_id not in by_run]
    if missing:
        outside = list(
            db.scalars(
                select(Evidence)
                .where(
                    Evidence.instance_id == instance_id,
                    Evidence.source == "mysql.statement_digest",
                    Evidence.run_id.in_(missing),
                )
                .limit(MAX_FRAMES + 1)
            )
        )
        if len(outside) > MAX_FRAMES:
            _limit()
        for source in outside:
            by_run.setdefault(source.run_id, []).append(source)
    instance = db.get(Instance, instance_id)
    schedule = db.scalar(
        select(CollectionSchedule).where(
            CollectionSchedule.instance_id == instance_id,
            CollectionSchedule.collector == "statement_digest",
        )
    )
    configured = _interval(schedule.interval_seconds) if schedule else None
    global_reasons = []
    sources = []
    intervals = []
    prepared = []
    raw_rows = []

    def gap(reason):
        if reason not in global_reasons:
            global_reasons.append(reason)

    if len(frames) < 2:
        gap("insufficient_observations")
    for frame in frames:
        candidates = by_run.get(frame.run_id, [])
        source = candidates[0] if len(candidates) == 1 else None
        sources.append(source)
        if not candidates:
            gap("source_evidence_missing")
        elif len(candidates) != 1:
            gap("ambiguous_source_evidence")
        if source:
            coverage = source.coverage or {}
            if source.source_mode != "live":
                gap("source_not_live")
            if coverage.get("availability") != "available":
                gap("source_unavailable")
            if source.truncated or coverage.get("truncated"):
                gap("source_truncated")
            if coverage.get("gaps"):
                gap("source_coverage_gap")
            if not start <= source.observed_at < end:
                gap("source_observation_outside_window")
            if (
                abs((source.observed_at - frame.observed_at).total_seconds())
                > SOURCE_CLOCK_TOLERANCE_SECONDS
            ):
                gap("source_frame_clock_skew")
            if source.server_uuid != frame.server_uuid:
                gap("source_identity_mismatch")
            measured = _interval(coverage.get("interval_seconds"))
            if measured is None and configured is None:
                gap("interval_unknown")
            intervals.append(measured or configured)
        else:
            intervals.append(configured)
        if (
            not instance
            or not instance.expected_server_uuid
            or frame.server_uuid != instance.expected_server_uuid
        ):
            gap("identity_changed")
        if not frame.collector_boot_id or frame.source_epoch is None:
            gap("epoch_unknown")
        reasons = (frame.derived_values or {}).get("rate_reasons", [])
        value = previous_frame_dict(frame, failed_since_previous="source_gap" in reasons)
        rows = [
            row
            for row in (frame.raw_values or {}).get("rows", [])
            if isinstance(row, dict) and _raw_key(row) in keys
        ]
        # Do not mutate ORM JSON or let historical raw data add output keys.
        value["raw_values"] = {"rows": rows}
        value["availability"] = (
            "available"
            if source and (source.coverage or {}).get("availability") == "available"
            else "unknown"
        )
        prepared.append(value)
        raw_rows.append({_raw_key(row): row for row in rows})
    if frames:
        first_source, last_source = sources[0], sources[-1]
        first_time = first_source.observed_at if first_source else frames[0].observed_at
        last_time = last_source.observed_at if last_source else frames[-1].observed_at
        first_interval = intervals[0] or configured
        last_interval = intervals[-1] or configured
        if first_interval is None or (first_time - start).total_seconds() > 2 * first_interval:
            gap("history_unavailable")
        if last_interval is None or (end - last_time).total_seconds() > 2 * last_interval:
            gap("window_tail_unavailable")
        # A failed source attempt can exist without a frame. A present, good
        # source wins independently over another source's partial run status.
        frame_ids = {f.run_id for f in frames}
        for source in evidence:
            if source.source_mode == "live" and source.run_id not in frame_ids:
                gap("source_frame_missing")
                if (source.coverage or {}).get("availability") != "available" or source.truncated:
                    gap("source_gap")
        failed = list(
            db.scalars(
                select(CollectionRun)
                .where(
                    CollectionRun.instance_id == instance_id,
                    CollectionRun.collector == "statement_digest",
                    CollectionRun.status == "failed",
                    CollectionRun.finished_at >= frames[0].observed_at,
                    CollectionRun.finished_at <= frames[-1].observed_at,
                )
                .limit(MAX_FRAMES + 1)
            )
        )
        if len(failed) > MAX_FRAMES:
            _limit()
        if any(
            run.id not in frame_ids
            and not any(
                (ev.coverage or {}).get("availability") == "available"
                for ev in by_run.get(run.id, [])
            )
            for run in failed
        ):
            gap("source_gap")
    else:
        first_time = last_time = None
    totals = {
        key: {"count": 0, "timer": 0, "examined": 0, "examined_known": True, "reasons": []}
        for key in keys
    }

    def row_gap(key, reason):
        if reason not in totals[key]["reasons"]:
            totals[key]["reasons"].append(reason)

    for key in keys:
        if any(key not in rows for rows in raw_rows):
            row_gap(key, "digest_missing")
    for index in range(1, len(prepared)):
        interval = (
            max(value for value in intervals[index - 1 : index + 1] if value is not None)
            if any(value is not None for value in intervals[index - 1 : index + 1])
            else 60
        )
        if sources[index - 1] and sources[index]:
            source_elapsed = (
                sources[index].observed_at - sources[index - 1].observed_at
            ).total_seconds()
            monotonic_elapsed = (
                frames[index].observed_monotonic_ns - frames[index - 1].observed_monotonic_ns
            ) / 1e9
            if not interval * 0.5 <= source_elapsed <= interval * 2:
                gap("invalid_source_interval")
            if abs(source_elapsed - monotonic_elapsed) > SOURCE_CLOCK_TOLERANCE_SECONDS:
                gap("source_clock_jump")
        derived = derive_digest_window(
            prepared[index - 1], prepared[index], interval_seconds=interval
        )
        for reason in derived["reasons"]:
            gap(reason)
        derived_rows = {_raw_key(row): row for row in derived["rows"]}
        for key in keys:
            previous = raw_rows[index - 1].get(key)
            current = raw_rows[index].get(key)
            pair = derived_rows.get(key)
            if previous is None or current is None:
                continue
            if not pair or pair.get("window_validity") != "valid":
                for reason in (pair or {}).get("window_reasons", ["invalid_pair"]):
                    row_gap(key, reason)
                continue
            count, old_count = (
                _counter(current.get("COUNT_STAR")),
                _counter(previous.get("COUNT_STAR")),
            )
            timer, old_timer = (
                _counter(current.get("SUM_TIMER_WAIT")),
                _counter(previous.get("SUM_TIMER_WAIT")),
            )
            if None in (count, old_count, timer, old_timer):
                row_gap(key, "missing_counter")
                continue
            if count < old_count or timer < old_timer:
                row_gap(key, "digest_reset")
                continue
            totals[key]["count"] += count - old_count
            totals[key]["timer"] += timer - old_timer
            examined, old_examined = (
                _counter(current.get("SUM_ROWS_EXAMINED")),
                _counter(previous.get("SUM_ROWS_EXAMINED")),
            )
            if examined is None or old_examined is None:
                totals[key]["examined_known"] = False
            elif examined < old_examined:
                row_gap(key, "digest_reset")
            else:
                totals[key]["examined"] += examined - old_examined
    for item in items:
        total = totals[_key(item)]
        reasons = list(dict.fromkeys(global_reasons + total["reasons"]))
        item["window_reasons"] = reasons
        if not reasons:
            item.update(
                window_validity="valid",
                window_count=total["count"],
                total_time_ms=total["timer"] / 1e9,
                avg_time_ms=total["timer"] / 1e9 / total["count"] if total["count"] else None,
                window_rows_examined=total["examined"] if total["examined_known"] else None,
            )
    aggregate = list(
        dict.fromkeys(
            global_reasons + [reason for total in totals.values() for reason in total["reasons"]]
        )
    )
    refs = []
    if frames:
        for index in sorted({0, len(frames) - 1}):
            source = sources[index]
            refs.append(
                {
                    "id": source.id if source else None,
                    "run_id": frames[index].run_id,
                    "observed_at": iso(source.observed_at) if source else None,
                    "frame_observed_at": iso(frames[index].observed_at),
                }
            )
    return {
        "requested_from": iso(start),
        "requested_to": iso(end),
        "comparison_from": iso(first_time),
        "comparison_to": iso(last_time),
        "observations": len(frames),
        "comparison_evidence_refs": refs,
        "coverage": {
            "complete": len(frames) >= 2 and not aggregate,
            "gaps": aggregate,
            "scope": "sampled_endpoints",
            "requested_boundaries_exact": False,
            "maximum_observations": MAX_FRAMES,
            "history_bounded": True,
            "candidate_scope": "only_digest_keys_visible_in_latest_authorized_snapshot",
            "notes": [
                "Values cover sampled endpoints, not exact requested boundaries.",
                "Bounded digest candidates may be evicted; missing rows invalidate their window.",
            ],
        },
    }
