"""Versioned metric registry and fixed, scope-bound Prometheus range queries."""

from __future__ import annotations

from dataclasses import dataclass
from bisect import bisect_right
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import math
import hashlib
import json
import time
from uuid import UUID

import httpx
from sqlalchemy import select

from .access import scoped_instance
from .common import PlatformError, iso
from .config import load_config
from .db import utcnow
from .metrics_derivation import REGISTRY, derive
from .models import CollectionSchedule, MetricFrame


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    owner_collector: str
    source_field: str
    unit: str
    metric_type: str
    prometheus_name: str
    freshness_multiplier: float = 1.5
    derivation_version: str = "1"


METRIC_TEMPLATES = {}
for _name, (_owner, _field, _unit, _kind) in REGISTRY.items():
    _public = "questions_per_second" if _name == "qps" else _name
    _prom = "tdx_" + (_public if _owner == "host" else "db_" + _public)
    METRIC_TEMPLATES[_public] = MetricDefinition(_public, _owner, _name, _unit, _kind, _prom)
DEFAULT_INTERVALS = {
    "identity_status": 30,
    "runtime": 30,
    "host": 30,
    "replication": 60,
    "statement_digest": 60,
    "schema_config": 300,
    "error_log": 60,
    "slow_log": 60,
}


def timestamp(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


def prometheus_range(config, selector, start, end, step, *, timeout_seconds=8, max_bytes=4_000_000):
    try:
        deadline = time.monotonic() + timeout_seconds
        with httpx.Client(
            timeout=timeout_seconds, follow_redirects=False, trust_env=False
        ) as client:
            with client.stream(
                "GET",
                config.prometheus_url.rstrip("/") + "/api/v1/query_range",
                params={"query": selector, "start": start, "end": end, "step": step},
            ) as response:
                if response.status_code != 200:
                    raise ValueError("prometheus_failed")
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise ValueError("prometheus_deadline_exceeded")
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("prometheus_response_budget")
                    chunks.append(chunk)
        result = json.loads(b"".join(chunks))
        if (
            result.get("status") != "success"
            or result.get("data", {}).get("resultType") != "matrix"
        ):
            raise ValueError("unexpected_result")
        return result["data"]["result"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise PlatformError(
            503, "prometheus_unavailable", "Prometheus 历史查询不可用；未用控制库帧代替时序。"
        ) from None


def materialize_metric_window(
    config,
    instance_ids,
    from_time,
    to_time,
    *,
    max_points=10000,
    max_bytes=2 * 1024 * 1024,
    max_series=192,
    timeout_seconds=8,
):
    """Freeze registry metrics after the caller authorizes every source asset.

    This internal helper has no identity bypass or implicit configuration load.
    The trusted owner supplies its already-authorized UUIDs. Returned labels are
    discarded; only numeric values, canonical owners/templates and UTC remain.
    All budgets apply to the whole snapshot, including diagnostic query points.
    """
    try:
        ids = sorted(set(instance_ids))
        if not 1 <= len(ids) <= 5 or any(not isinstance(i, str) or str(UUID(i)) != i for i in ids):
            raise ValueError()
        lower, upper = math.ceil(timestamp(from_time) * 1000) / 1000, timestamp(to_time)
        if lower >= upper or upper - lower > 14 * 86400 or upper > timestamp(utcnow()) + 60:
            raise ValueError()
        for value, ceiling in (
            (max_points, 10000),
            (max_bytes, 2 * 1024 * 1024),
            (max_series, 192),
            (timeout_seconds, 8),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
                raise ValueError()
        if max_bytes < 4096:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise PlatformError(
            422, "invalid_metric_materialization", "证据时序窗口、实例范围或冻结预算无效。"
        ) from None
    diagnostics = (
        "tdx_collector_up",
        "tdx_collector_last_success_timestamp_seconds",
        "tdx_collector_sample_age_seconds",
        "tdx_collector_freshness_limit_seconds",
        "tdx_collector_rate_valid",
        "tdx_collector_last_attempt_timestamp_seconds",
    )
    definitions = sorted(METRIC_TEMPLATES.values(), key=lambda d: d.metric_id)
    owners = sorted({d.owner_collector for d in definitions})
    expected_series = len(ids) * (len(definitions) + len(owners) * len(diagnostics))
    coverage = {
        "materialized": False,
        "complete": False,
        "source": "prometheus_resampled",
        "reason_codes": [],
        "instance_count": len(ids),
        "series_count": 0,
        "point_count": 0,
        "valid_point_count": 0,
        "window": {"from": iso(from_time), "to": iso(to_time)},
    }

    def stopped(reason):
        return {"items": [], "coverage": {**coverage, "reason_codes": [reason]}}

    if expected_series > max_series or expected_series > max_points:
        return stopped("metric_materialization_budget_exceeded")
    per_series = max_points // expected_series
    step = max(15, math.ceil((upper - lower) / per_series))
    names = sorted({d.prometheus_name for d in definitions} | set(diagnostics))
    selector = (
        '{__name__=~"'
        + "|".join(names)
        + '",instance_id=~"'
        + "|".join(ids)
        + '",collector=~"'
        + "|".join(owners)
        + '"}'
    )
    try:
        rows = prometheus_range(
            config,
            selector,
            lower,
            (math.ceil(upper * 1000) - 1) / 1000,
            step,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
        )
    except PlatformError:
        return stopped("prometheus_unavailable")
    if not isinstance(rows, list) or len(rows) > max_series:
        return stopped("metric_materialization_series_limit")
    grouped = {}
    duplicates = set()
    reasons = set()
    sample_count = 0
    definition_owners = {d.prometheus_name: d.owner_collector for d in definitions}
    try:
        for row in rows:
            labels = row.get("metric", {})
            name = labels.get("__name__")
            identifier = labels.get("instance_id")
            owner = labels.get("collector")
            if (
                identifier not in ids
                or owner not in owners
                or name not in names
                or (name in definition_owners and definition_owners[name] != owner)
            ):
                reasons.add("unexpected_series_omitted")
                continue
            values = row.get("values", [])
            if not isinstance(values, list):
                raise ValueError()
            sample_count += len(values)
            if sample_count > max_points:
                return stopped("metric_materialization_point_limit")
            key = (identifier, owner, name)
            if key in grouped:
                duplicates.add(key)
                reasons.add("ambiguous_duplicate_series")
            else:
                grouped[key] = values
    except (ValueError, TypeError, AttributeError):
        return stopped("invalid_prometheus_materialization")
    for key in duplicates:
        grouped.pop(key, None)
    items = []
    size = 4096
    frozen_at = iso(utcnow())
    valid_count = 0
    point_count = 0
    for identifier in ids:
        for definition in definitions:
            series = {
                name: values
                for (asset, owner, name), values in grouped.items()
                if asset == identifier and owner == definition.owner_collector
            }
            try:
                masked = mask_series(series, lower, upper, step, definition)
            except (ValueError, TypeError, OverflowError):
                return stopped("invalid_prometheus_materialization")
            item = {
                "schema_version": "1",
                "instance_id": identifier,
                "template": definition.metric_id,
                "owner_collector": definition.owner_collector,
                "unit": definition.unit,
                "source": "prometheus_resampled",
                "window_start": iso(from_time),
                "window_end": iso(to_time),
                "frozen_at": frozen_at,
                "effective_step_seconds": step,
                **masked,
            }
            encoded = json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            if size + len(encoded) + 85 > max_bytes:
                reasons.add("metric_materialization_byte_limit")
                continue
            size += len(encoded) + 85
            item["payload_sha256"] = hashlib.sha256(encoded).hexdigest()
            items.append(item)
            point_count += len(masked["points"])
            valid_count += sum(p["value"] is not None for p in masked["points"])
            reasons.update(masked["coverage"]["reason_codes"])
    coverage.update(
        materialized=True,
        complete=bool(items) and not reasons,
        reason_codes=sorted(reasons),
        series_count=len(rows),
        point_count=point_count,
        valid_point_count=valid_count,
        effective_step_seconds=step,
    )
    return {"items": items, "coverage": coverage}


def mask_series(series, start, end, step, definition):
    mapped = {
        name: {round(float(t) * 1000): finite(v) for t, v in samples}
        for name, samples in series.items()
    }
    points = []
    gaps = []
    last_observed = None
    for index in range(int(math.ceil((end - start) / step))):
        point = start + index * step
        if point >= end:
            break
        key = round(point * 1000)
        raw = mapped.get(definition.prometheus_name, {}).get(key)
        up = mapped.get("tdx_collector_up", {}).get(key)
        last = mapped.get("tdx_collector_last_success_timestamp_seconds", {}).get(key)
        age = mapped.get("tdx_collector_sample_age_seconds", {}).get(key)
        freshness = mapped.get("tdx_collector_freshness_limit_seconds", {}).get(key)
        rate = mapped.get("tdx_collector_rate_valid", {}).get(key)
        reason = "valid"
        if up is None or last is None or age is None or freshness is None:
            reason = "unknown_gap"
        elif freshness <= 0 or point - last < -0.001 or age < 0:
            reason = "invalid_observation_time"
        elif point - last > freshness or age > freshness:
            reason = "stale"
        elif up != 1:
            reason = "source_unavailable"
        elif definition.metric_type != "gauge" and rate != 1:
            reason = "invalid_rate_window"
        elif raw is None:
            reason = "unknown_gap"
        value = raw if reason == "valid" else None
        points.append(
            {
                "time": iso(datetime.fromtimestamp(point, timezone.utc)),
                "value": value,
                "validity": reason,
            }
        )
        if last is not None and last <= point and (last_observed is None or last > last_observed):
            last_observed = last
        if reason != "valid":
            interval = {
                "from": iso(datetime.fromtimestamp(point, timezone.utc)),
                "to": iso(datetime.fromtimestamp(min(end, point + step), timezone.utc)),
                "reason_code": reason,
            }
            if gaps and gaps[-1]["reason_code"] == reason and gaps[-1]["to"] == interval["from"]:
                gaps[-1]["to"] = interval["to"]
            else:
                gaps.append(interval)
    return {
        "points": points,
        "gaps": gaps,
        "last_observed_at": iso(datetime.fromtimestamp(last_observed, timezone.utc))
        if last_observed is not None
        else None,
        "coverage": {
            "complete": bool(points) and not gaps,
            "reason_codes": list(dict.fromkeys(gap["reason_code"] for gap in gaps)),
        },
    }


def query_template(actor, instance_id, template, from_time, to_time, step=30, config=None):
    instance = scoped_instance(actor, instance_id)
    if instance.connection_mode == "zabbix":
        from .monitoring import metric_charts
        charts = metric_charts(actor.db, instance, from_time, to_time, config or actor.db.info.get("platform_config") or load_config())
        match = next((c for c in charts if c["template"] == template), None)
        if match is None:
            raise PlatformError(422, "unknown_metric_template", "指标未绑定到该监控对象。")
        return match
    definition = METRIC_TEMPLATES.get(template)
    if definition is None:
        raise PlatformError(422, "unknown_metric_template", "指标模板不在服务端注册表中。")
    if (
        from_time >= to_time
        or to_time - from_time > timedelta(days=14)
        or to_time > utcnow() + timedelta(minutes=1)
    ):
        raise PlatformError(422, "invalid_metric_window", "图表窗口须递增且不超过14天。")
    if not isinstance(step, (int, float)) or not math.isfinite(step) or step <= 0:
        raise PlatformError(422, "invalid_metric_step", "图表步长须为正数。")
    # Prometheus evaluates on a millisecond grid. The supplied UTC values may
    # contain microseconds; exact float lookup otherwise masks every real point.
    # Round the start inward, preserving the requested half-open time boundary.
    lower, upper = math.ceil(timestamp(from_time) * 1000) / 1000, timestamp(to_time)
    effective = max(15, int(math.ceil(step)), int(math.ceil((upper - lower) / 2000)))
    names = [
        definition.prometheus_name,
        "tdx_collector_up",
        "tdx_collector_last_success_timestamp_seconds",
        "tdx_collector_sample_age_seconds",
        "tdx_collector_freshness_limit_seconds",
        "tdx_collector_rate_valid",
    ]
    # Both resource and owner come from authorized database/registry entries.
    selector = (
        '{__name__=~"'
        + "|".join(names)
        + '",instance_id="'
        + instance_id
        + '",collector="'
        + definition.owner_collector
        + '"}'
    )
    data = prometheus_range(
        config or load_config(), selector, lower, (math.ceil(upper * 1000) - 1) / 1000, effective
    )
    series = {row.get("metric", {}).get("__name__"): row.get("values", []) for row in data}
    result = mask_series(series, lower, upper, effective, definition)
    schedule = actor.db.scalar(
        select(CollectionSchedule).where(
            CollectionSchedule.instance_id == instance_id,
            CollectionSchedule.collector == definition.owner_collector,
        )
    )
    interval = (
        schedule.interval_seconds if schedule else DEFAULT_INTERVALS[definition.owner_collector]
    )
    # Exact invalid-rate reasons come only from retained source frames, never
    # from guessed interpretations of absent Prometheus samples.
    frames = list(
        actor.db.scalars(
            select(MetricFrame)
            .where(
                MetricFrame.instance_id == instance_id,
                MetricFrame.collector == definition.owner_collector,
                MetricFrame.observed_at >= from_time - timedelta(seconds=interval * 2),
                MetricFrame.observed_at < to_time,
            )
            .order_by(MetricFrame.observed_at.desc())
            .limit(4000)
        )
    )
    frames.sort(key=lambda f: f.observed_at)
    times = [timestamp(f.observed_at) for f in frames]
    for point in result["points"]:
        if point["validity"] not in ("unknown_gap", "invalid_rate_window"):
            continue
        moment = datetime.fromisoformat(point["time"].replace("Z", "+00:00")).timestamp()
        index = bisect_right(times, moment) - 1
        reason = "unknown_gap"
        if index >= 0 and moment - times[index] <= interval * 1.5:
            data = frames[index].derived_values or {}
            details = (data.get("metric_validity") or {}).get(definition.source_field) or {}
            reasons = details.get("reasons") or data.get("rate_reasons") or []
            if details.get("validity") != "valid" and reasons:
                candidate = reasons[0]
                if isinstance(candidate, str) and candidate in {
                    "first_sample",
                    "instance_changed",
                    "identity_changed",
                    "owner_changed",
                    "collector_restarted",
                    "counter_epoch_changed",
                    "server_restarted",
                    "source_gap",
                    "source_epoch_changed",
                    "invalid_interval",
                    "uptime_epoch_changed",
                    "clock_jump",
                    "missing_gauge",
                    "missing_counter",
                    "counter_decreased",
                    "invalid_ratio",
                    "zero_denominator",
                }:
                    reason = candidate
        point["validity"] = reason
    gaps = []
    for index, point in enumerate(result["points"]):
        if point["value"] is not None:
            continue
        right = (
            result["points"][index + 1]["time"]
            if index + 1 < len(result["points"])
            else iso(to_time)
        )
        if (
            gaps
            and gaps[-1]["reason_code"] == point["validity"]
            and gaps[-1]["to"] == point["time"]
        ):
            gaps[-1]["to"] = right
        else:
            gaps.append({"from": point["time"], "to": right, "reason_code": point["validity"]})
    result["gaps"] = gaps
    result["coverage"] = {
        "complete": bool(result["points"]) and not gaps,
        "reason_codes": list(dict.fromkeys(g["reason_code"] for g in gaps)),
    }
    return {
        "instance_id": instance_id,
        "template": template,
        "owner_collector": definition.owner_collector,
        "unit": definition.unit,
        "source": "prometheus_resampled",
        "collection_interval_seconds": interval,
        "effective_step_seconds": effective,
        "requested_step_seconds": step,
        **result,
    }


# External registry entries remain numeric and retain their logical permission owner.
for _key, _owner, _unit in (
    ("host_cpu_percent", "host", "%"),
    ("host_disk_read_bytes_per_second", "host", "Bps"),
    ("host_disk_write_bytes_per_second", "host", "Bps"),
    ("probe_latency_ms", "host", "ms"),
    ("probe_availability", "host", ""),
):
    METRIC_TEMPLATES[_key] = MetricDefinition(_key, _owner, _key, _unit, "external", "tdx_" + _key)


def _required_sample_indices(points):
    """Keep observed endpoints, extrema and both sides of every invalid run.

    A sampled null without its neighbouring observations (or the opposite)
    could connect a line across an unobserved interval. If these mandatory
    observations alone exceed the budget, the series must be explicitly
    omitted rather than made to look continuously observed.
    """
    if not points:
        return set()
    required = {0, len(points) - 1}
    valid = []
    for index, point in enumerate(points):
        value = finite(point.get('value')) if point.get('validity') == 'valid' else None
        if value is not None:
            valid.append((index, value))
        else:
            # Keep transition boundaries, not every interior null in a long gap.
            previous_valid = index > 0 and points[index - 1].get('validity') == 'valid' and finite(points[index - 1].get('value')) is not None
            next_valid = index + 1 < len(points) and points[index + 1].get('validity') == 'valid' and finite(points[index + 1].get('value')) is not None
            previous_reason_changed = index > 0 and points[index - 1].get('validity') != point.get('validity')
            next_reason_changed = index + 1 < len(points) and points[index + 1].get('validity') != point.get('validity')
            if previous_valid or previous_reason_changed or index == 0:
                required.update((max(0, index - 1), index))
            if next_valid or next_reason_changed or index == len(points) - 1:
                required.update((index, min(len(points) - 1, index + 1)))
    if valid:
        required.add(min(valid, key=lambda value: (value[1], value[0]))[0])
        required.add(max(valid, key=lambda value: (value[1], -value[0]))[0])
    return required


def _sample_original_points(points, required, count):
    if count >= len(points):
        return points
    selected = set(required)
    remaining = count - len(selected)
    # Equal index buckets keep local extrema throughout the window. Every
    # retained point is an original observation, never an average or fake zero.
    bucket_count = remaining // 2
    if bucket_count:
        for bucket in range(bucket_count):
            left = bucket * len(points) // bucket_count
            right = (bucket + 1) * len(points) // bucket_count
            candidates = [(index, finite(points[index].get('value'))) for index in range(left, right)
                          if index not in selected and points[index].get('validity') == 'valid']
            candidates = [(index, value) for index, value in candidates if value is not None]
            if candidates:
                selected.add(min(candidates, key=lambda value: (value[1], value[0]))[0])
                selected.add(max(candidates, key=lambda value: (value[1], -value[0]))[0])
    remaining = count - len(selected)
    if remaining:
        candidates = [index for index in range(len(points)) if index not in selected]
        # Fill unused bucket slots uniformly and deterministically.
        selected.update(candidates[(2 * index + 1) * len(candidates) // (2 * remaining)] for index in range(remaining))
    return [points[index] for index in sorted(selected)]


def _allocate_source_points(entries, point_limit):
    counts = [len(entry['required']) for entry in entries]
    remaining = point_limit - sum(counts)
    active = [index for index, entry in enumerate(entries) if counts[index] < len(entry['points'])]
    while active and remaining:
        share = max(1, remaining // len(active))
        for index in active:
            added = min(share, len(entries[index]['points']) - counts[index], remaining)
            counts[index] += added
            remaining -= added
            if not remaining:
                break
        active = [index for index in active if counts[index] < len(entries[index]['points'])]
    return counts


def _frozen_source_item(entry, count, reasons):
    item = deepcopy(entry['base'])
    original = entry['points']
    item['points'] = _sample_original_points(original, entry['required'], count)
    if len(item['points']) < len(original):
        sampling = {
            'method': 'original_point_bucket_minmax',
            'original_point_count': len(original),
            'retained_point_count': len(item['points']),
            'original_valid_point_count': sum(p.get('validity') == 'valid' and finite(p.get('value')) is not None for p in original),
            'retained_valid_point_count': sum(p.get('validity') == 'valid' and finite(p.get('value')) is not None for p in item['points']),
            'minimum_required_point_count': len(entry['required']),
            'endpoints_preserved': True,
            'gap_boundaries_preserved': True,
        }
        coverage = item.setdefault('coverage', {})
        coverage.update(complete=False, truncated=True, sampling=sampling)
        coverage['reason_codes'] = sorted(set(coverage.get('reason_codes', [])) | reasons)
        item.setdefault('provenance', {})['sampling'] = dict(sampling)
    # Recompute after sampling, naming and registered relationships. A digest
    # copied from a larger pre-budget payload no longer identifies this window.
    item.pop('payload_sha256', None)
    encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    item['payload_sha256'] = hashlib.sha256(encoded).hexdigest()
    return item


def _bound_source_windows(result, *, point_limit=10000, byte_limit=2_000_000, item_limit=192):
    """Share one point/byte/item budget without dropping every fitting curve."""
    original_items = result['items']
    original_point_count = sum(len(item.get('points', [])) for item in original_items)
    coverage = deepcopy(result['coverage'])
    reasons = set(coverage.get('reason_codes', []))
    omitted = []

    def omit(entry, reason):
        item = entry['base']
        omitted.append({key: str(item.get(key, ''))[:120] for key in ('instance_id', 'template', 'id')} | {'reason': reason})
        reasons.add(reason)

    grouped = {}
    for item in original_items:
        # Interleave sources so one large instance cannot consume every slot.
        grouped.setdefault(item['instance_id'], []).append(deepcopy(item))
    for items in grouped.values():
        items.sort(key=lambda item: (str(item.get('id', '')), str(item.get('template', '')), str((item.get('provenance') or {}).get('itemid', ''))))
    ordered = [items[index] for index in range(max((len(v) for v in grouped.values()), default=0))
               for _, items in sorted(grouped.items()) if index < len(items)]
    entries = []
    for item in ordered:
        points = item.pop('points', [])
        entry = {'base': item, 'points': points, 'required': _required_sample_indices(points)}
        if len(entries) >= item_limit:
            omit(entry, 'source_window_item_budget')
        elif len(entry['required']) > point_limit:
            omit(entry, 'source_window_gap_budget')
        else:
            entries.append(entry)
    while sum(len(entry['required']) for entry in entries) > point_limit:
        # Dense gaps cannot be represented by joining them. Retain the other
        # series, rather than replacing missing intervals with interpolations.
        entry = max(entries, key=lambda value: len(value['required']))
        entries.remove(entry)
        omit(entry, 'source_window_gap_budget')

    def build(counts):
        items = [_frozen_source_item(entry, count, reasons) for entry, count in zip(entries, counts)]
        adjusted = bool(omitted) or any(len(item['points']) < len(entry['points']) for item, entry in zip(items, entries))
        current = {
            **coverage,
            'materialized': bool(items) or (not original_items and coverage.get('materialized') is True),
            'complete': coverage.get('complete') is True and not adjusted,
            'reason_codes': sorted(reasons),
            'series_count': len(items),
            'point_count': sum(len(item['points']) for item in items),
            'valid_point_count': sum(p.get('validity') == 'valid' and finite(p.get('value')) is not None for item in items for p in item['points']),
            'sampling': {
                'method': 'original_point_bucket_minmax' if adjusted else 'none',
                'original_series_count': len(original_items),
                'retained_series_count': len(items),
                'original_point_count': original_point_count,
                'retained_point_count': sum(len(item['points']) for item in items),
                'omitted_series_count': len(omitted),
                'omitted_series': omitted[:item_limit],
                'omitted_series_list_truncated': len(omitted) > item_limit,
            },
        }
        return {'items': items, 'coverage': current}

    # Omit only metadata that cannot fit even with mandatory observations.
    # Usually all metadata fits and only observations need to be sampled.
    for entry in list(entries):
        minimal = _frozen_source_item(entry, len(entry['required']), reasons)
        if len(json.dumps(minimal, ensure_ascii=False).encode()) + 4096 > byte_limit:
            entries.remove(entry)
            omit(entry, 'source_window_byte_budget')
    counts = _allocate_source_points(entries, point_limit)
    if any(count < len(entry['points']) for count, entry in zip(counts, entries)):
        reasons.add('source_window_point_budget')
    bounded = build(counts)
    while len(json.dumps(bounded, ensure_ascii=False).encode()) > byte_limit:
        reasons.add('source_window_byte_budget')
        reduced = [len(entry['required']) + (count - len(entry['required'])) // 2 for entry, count in zip(entries, counts)]
        if reduced != counts:
            counts = reduced
        elif entries:
            # Combined metadata can exceed the byte budget even when every
            # individual series fits. Omission is explicit and local.
            index = max(range(len(entries)), key=lambda i: len(json.dumps(_frozen_source_item(entries[i], counts[i], reasons), ensure_ascii=False).encode()))
            omit(entries.pop(index), 'source_window_byte_budget')
            counts.pop(index)
        else:
            break
        bounded = build(counts)
    return bounded


def materialize_source_windows(db, config, instance_ids, start, end):
    """One bounded snapshot contract, provider provenance retained per item."""
    from .models import Instance
    from .monitoring import metric_charts
    external, direct = [], []
    for instance in db.scalars(select(Instance).where(Instance.id.in_(instance_ids)).order_by(Instance.id)):
        (external if instance.connection_mode == "zabbix" else direct).append(instance)
    result = materialize_metric_window(config, [r.id for r in direct], start, end) if direct else {"items": [], "coverage": {"materialized": True, "complete": True, "reason_codes": []}}
    for instance in external:
        try:
            charts = metric_charts(db, instance, start, end, config)
        except PlatformError as exc:
            charts = []
            result["coverage"]["reason_codes"].append(exc.detail["code"])
        from .monitoring import mapping_for
        mapping=mapping_for(db,instance.id)
        relationships=[]
        if mapping and (mapping.relationships or {}).get("parent_hostid"):
            from .models import MonitoringMapping
            parent=db.scalar(select(MonitoringMapping).where(MonitoringMapping.source_id==mapping.source_id, MonitoringMapping.hostid==mapping.relationships["parent_hostid"], MonitoringMapping.instance_id.in_(instance_ids)))
            if parent: relationships=[{"from":parent.instance_id,"to":instance.id,"kind":"registered","evidence_ids":[],"relationship":"监控配置声明的关联，不代表因果","provenance":"zabbix.host.tags.sl.parent"}]
        for chart in charts: chart["registered_relationships"]=[*chart.get("registered_relationships", []), *relationships]
        result["items"].extend(charts)
        if not charts or any(not c["coverage"]["complete"] for c in charts):
            result["coverage"]["complete"] = False
            result["coverage"]["reason_codes"].append("external_history_partial")
    names={instance.id:instance.name for instance in external+direct}
    for item in result["items"]: item["object_name"]=names.get(item.get("instance_id"), "unknown")
    result["coverage"]["reason_codes"] = sorted(set(result["coverage"]["reason_codes"]))
    return _bound_source_windows(result)
