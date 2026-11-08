"""Versioned metric registry and fixed, scope-bound Prometheus range queries."""

from __future__ import annotations

from dataclasses import dataclass
from bisect import bisect_right
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
    scoped_instance(actor, instance_id)
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
