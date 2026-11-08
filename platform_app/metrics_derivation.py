"""Deterministic metric semantics; Prometheus scrapes never create observations."""

from datetime import datetime, timezone
import math

REGISTRY = {
    "connections": ("identity_status", "Threads_connected", "connections", "gauge"),
    "threads_running": ("identity_status", "Threads_running", "threads", "gauge"),
    "qps": ("identity_status", "Questions", "statements/second", "rate"),
    "explicit_commit_rollback_per_second": (
        "identity_status",
        ("Com_commit", "Com_rollback"),
        "statements/second",
        "rate",
    ),
    "buffer_pool_hit_ratio": (
        "identity_status",
        ("Innodb_buffer_pool_read_requests", "Innodb_buffer_pool_reads"),
        "ratio",
        "ratio",
    ),
    "bytes_received_per_second": ("identity_status", "Bytes_received", "bytes/second", "rate"),
    "bytes_sent_per_second": ("identity_status", "Bytes_sent", "bytes/second", "rate"),
    "innodb_reads_per_second": (
        "identity_status",
        "Innodb_data_reads",
        "operations/second",
        "rate",
    ),
    "innodb_writes_per_second": (
        "identity_status",
        "Innodb_data_writes",
        "operations/second",
        "rate",
    ),
    "lock_waits": ("runtime", "lock_waits", "waits", "gauge"),
    "transactions": ("runtime", "transactions", "transactions", "gauge"),
    "host_load1": ("host", "load1", "load", "gauge"),
    "host_memory_available_bytes": ("host", "memory_available_bytes", "bytes", "gauge"),
    "host_cpu_busy_ratio": ("host", ("cpu_total_ticks", "cpu_idle_ticks"), "ratio", "cpu_ratio"),
}


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _timestamp(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def source_is_fresh(source, now, interval_seconds, freshness_multiplier=1.5):
    if source.get("availability") != "available" or interval_seconds <= 0:
        return False
    try:
        age = (_timestamp(now) - _timestamp(source["observed_at"])).total_seconds()
        return 0 <= age <= interval_seconds * freshness_multiplier
    except (KeyError, TypeError, ValueError):
        return False


def _window(previous, current, interval_seconds):
    if previous is None:
        return None, ["first_sample"]
    reasons = []
    for field, reason in [
        ("instance_id", "instance_changed"),
        ("server_uuid", "identity_changed"),
        ("owner_collector", "owner_changed"),
        ("collector_boot_id", "collector_restarted"),
        ("counter_epoch", "counter_epoch_changed"),
    ]:
        if previous.get(field) != current.get(field) or current.get(field) is None:
            reasons.append(reason)
    if previous.get("uptime_seconds") is not None and current.get("uptime_seconds") is not None:
        if current["uptime_seconds"] < previous["uptime_seconds"]:
            reasons.append("server_restarted")
    for item in (previous, current):
        if (
            item.get("failed_since_previous")
            or item.get("clock_jump")
            or item.get("availability", "available") != "available"
        ):
            reasons.append("source_gap")
    if ("source_epoch" in previous or "source_epoch" in current) and previous.get(
        "source_epoch"
    ) != current.get("source_epoch"):
        reasons.append("source_epoch_changed")
    try:
        elapsed = (current["observed_monotonic_ns"] - previous["observed_monotonic_ns"]) / 1e9
        if not interval_seconds * 0.5 <= elapsed <= interval_seconds * 2:
            reasons.append("invalid_interval")
        if previous.get("uptime_seconds") is not None and current.get("uptime_seconds") is not None:
            if abs((current["uptime_seconds"] - previous["uptime_seconds"]) - elapsed) > 2:
                reasons.append("uptime_epoch_changed")
        # UTC and monotonic intervals should agree. Modest wall-clock jitter is tolerated.
        wall = (
            _timestamp(current["observed_at"]) - _timestamp(previous["observed_at"])
        ).total_seconds()
        if abs(wall - elapsed) > 5:
            reasons.append("clock_jump")
    except (KeyError, TypeError, ValueError):
        elapsed = None
        reasons.append("invalid_interval")
    return elapsed, list(dict.fromkeys(reasons))


def derive(previous, current, interval_seconds=30):
    elapsed, reasons = _window(previous, current, interval_seconds)
    owner = current.get("owner_collector", current.get("collector", "identity_status"))
    raw = current.get("raw_values", {})
    prior = (previous or {}).get("raw_values", {})
    values, missing, metric_validity = {}, [], {}
    for name, (metric_owner, fields, unit, kind) in REGISTRY.items():
        if metric_owner != owner:
            continue
        fields = fields if isinstance(fields, tuple) else (fields,)
        now = [_number(raw.get(field)) for field in fields]
        metric_validity[name] = {"validity": "unknown", "reasons": list(reasons)}
        if kind == "gauge":
            values[name] = now[0]
            metric_validity[name] = {
                "validity": "valid" if now[0] is not None else "unknown",
                "reasons": [] if now[0] is not None else ["missing_gauge"],
            }
            continue
        before = [_number(prior.get(field)) for field in fields]
        values[name] = None
        if reasons:
            continue
        if any(value is None for value in now + before):
            missing.append("missing_counter")
            metric_validity[name]["reasons"] = ["missing_counter"]
            continue
        delta = [x - y for x, y in zip(now, before)]
        if any(value < 0 for value in delta):
            missing.append("counter_decreased")
            metric_validity[name]["reasons"] = ["counter_decreased"]
            continue
        if kind == "rate":
            values[name] = sum(delta) / elapsed
        elif kind in ("ratio", "cpu_ratio") and delta[0] > 0:
            ratio = 1 - delta[1] / delta[0]
            if 0 <= ratio <= 1:
                values[name] = ratio
            else:
                missing.append("invalid_ratio")
                metric_validity[name]["reasons"] = ["invalid_ratio"]
        else:
            missing.append("zero_denominator")
            metric_validity[name]["reasons"] = ["zero_denominator"]
        if values[name] is not None:
            metric_validity[name] = {"validity": "valid", "reasons": []}
    all_reasons = list(dict.fromkeys(reasons + missing))
    return {
        "values": values,
        "validity": "valid" if not all_reasons else "unknown",
        "window_seconds": elapsed,
        "reasons": all_reasons,
        "derivation_version": "1",
        "owner_collector": owner,
        "window_validity": "valid" if not reasons else "unknown",
        "metric_validity": metric_validity,
    }


def derive_digest_window(previous, current, interval_seconds=60):
    elapsed, reasons = _window(previous, current, interval_seconds)
    prior_rows = {
        (r.get("SCHEMA_NAME"), r.get("DIGEST")): r
        for r in (previous or {}).get("raw_values", {}).get("rows", [])
    }
    rows = []
    for value in current.get("raw_values", {}).get("rows", []):
        row = dict(value)
        before = prior_rows.get((row.get("SCHEMA_NAME"), row.get("DIGEST")))
        row.update(count_delta=None, total_time_seconds_delta=None, executions_per_second=None)
        row_reasons = list(reasons)
        if not before:
            row_reasons.append("new_or_evicted_digest")
        if not row_reasons:
            count = _number(value.get("COUNT_STAR"))
            old_count = _number(before.get("COUNT_STAR"))
            total = _number(value.get("SUM_TIMER_WAIT"))
            old_total = _number(before.get("SUM_TIMER_WAIT"))
            if None in (count, old_count, total, old_total):
                row_reasons.append("missing_counter")
            elif count < old_count or total < old_total:
                row_reasons.append("digest_reset")
            else:
                row.update(
                    count_delta=count - old_count,
                    total_time_seconds_delta=(total - old_total) / 1e12,
                    executions_per_second=(count - old_count) / elapsed,
                )
        row["window_validity"] = "valid" if not row_reasons else "unknown"
        row["window_reasons"] = row_reasons
        rows.append(row)
    return {
        "rows": rows,
        "window_seconds": elapsed,
        "reasons": reasons,
        "coverage": {"complete": False, "reason": "bounded_top_n_and_digest_eviction_possible"},
    }
