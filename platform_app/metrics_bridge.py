"""Read-only control-DB bridge; failures never return cached business values."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import threading

from sqlalchemy import select

from .db import utcnow
from .metrics import DEFAULT_INTERVALS, METRIC_TEMPLATES, finite, timestamp
from .models import CollectionRun, CollectionSchedule, Evidence, Instance, MetricFrame


def source_health(db, instance, collector, now=None):
    now = now or utcnow()
    schedule = db.scalar(
        select(CollectionSchedule).where(
            CollectionSchedule.instance_id == instance.id, CollectionSchedule.collector == collector
        )
    )
    interval = schedule.interval_seconds if schedule else DEFAULT_INTERVALS.get(collector, 60)
    frame = db.scalar(
        select(MetricFrame)
        .where(MetricFrame.instance_id == instance.id, MetricFrame.collector == collector)
        .order_by(MetricFrame.observed_at.desc(), MetricFrame.run_id.desc())
        .limit(1)
    )
    attempt = db.scalar(
        select(CollectionRun)
        .where(CollectionRun.instance_id == instance.id, CollectionRun.collector == collector)
        .order_by(CollectionRun.started_at.desc(), CollectionRun.id.desc())
        .limit(1)
    )
    in_progress = bool(attempt and attempt.status == "running")
    completed = attempt
    if in_progress:
        completed = db.scalar(
            select(CollectionRun)
            .where(
                CollectionRun.instance_id == instance.id,
                CollectionRun.collector == collector,
                CollectionRun.status != "running",
            )
            .order_by(CollectionRun.started_at.desc(), CollectionRun.id.desc())
            .limit(1)
        )
    evidence_query = (
        select(Evidence)
        .join(CollectionRun, Evidence.run_id == CollectionRun.id)
        .where(
            Evidence.instance_id == instance.id,
            CollectionRun.collector == collector,
            CollectionRun.status.in_(["succeeded", "partial"]),
            Evidence.source_mode == "live",
        )
    )
    # Identity observations accompanying a replication probe do not establish
    # replication applicability; only its owned observation does.
    if collector == "replication":
        evidence_query = evidence_query.where(Evidence.source == "mysql.replication")
    evidence = db.scalar(
        evidence_query.order_by(Evidence.observed_at.desc(), Evidence.id.desc()).limit(1)
    )
    observed = evidence.observed_at if evidence else None
    enabled = (
        instance.enabled
        and instance.lifecycle_state == "Active"
        and (schedule is None or schedule.enabled)
    )
    age = (now - observed).total_seconds() if observed else None
    fresh = age is not None and 0 <= age <= interval * 1.5
    availability = (
        (evidence.coverage or {}).get("availability", "unknown") if evidence else "unknown"
    )
    success = bool(
        completed
        and completed.status in ("succeeded", "partial")
        and evidence
        and evidence.run_id == completed.id
        and availability in ("available", "degraded")
    )
    revision_ok = bool(
        attempt
        and completed
        and attempt.config_revision == instance.config_revision
        and completed.config_revision == instance.config_revision
        and (schedule is None or schedule.config_revision == instance.config_revision)
    )
    identity_ok = frame is None or frame.server_uuid == instance.expected_server_uuid
    up = bool(enabled and fresh and success and revision_ok and identity_ok)
    failed = bool(completed and completed.status not in ("succeeded", "partial"))
    if up:
        reason = "valid"
    elif not enabled:
        reason = "disabled"
    elif failed:
        reason = "latest_attempt_failed"
    elif evidence is None:
        reason = "collecting" if in_progress else "no_observation"
    elif not revision_ok:
        reason = "configuration_changed"
    elif not identity_ok:
        reason = "identity_mismatch"
    elif not fresh:
        reason = "stale"
    elif availability not in ("available", "degraded"):
        reason = availability
    else:
        reason = "no_observation"
    return {
        "collector": collector,
        "up": up,
        "enabled": bool(enabled),
        "interval_seconds": interval,
        "freshness_limit_seconds": interval * 1.5,
        "sample_age_seconds": age,
        "last_attempt_at": attempt.started_at if attempt else None,
        "last_success_at": observed,
        "frame": frame if evidence and frame and evidence.run_id == frame.run_id else None,
        "latest_attempt": attempt,
        "availability": availability,
        "attempt_in_progress": in_progress,
        "latest_attempt_status": attempt.status if attempt else None,
        "reason_code": reason,
    }


def render_metrics(session_factory):
    lines = ["# DB Evidence metric registry v1; gauges are real observations, not scrape deltas."]

    def emit(name, value, instance_id, collector=None):
        number = finite(value)
        if number is None:
            return
        labels = f'instance_id="{instance_id}"' + (f',collector="{collector}"' if collector else "")
        lines.append(f"{name}{{{labels}}} {number}")

    with session_factory() as db:
        for instance in db.scalars(select(Instance).order_by(Instance.id)):
            emit(
                "tdx_instance_enabled",
                int(instance.enabled and instance.lifecycle_state == "Active"),
                instance.id,
            )
            owners = set(
                db.scalars(
                    select(CollectionSchedule.collector).where(
                        CollectionSchedule.instance_id == instance.id
                    )
                )
            )
            owners.update(definition.owner_collector for definition in METRIC_TEMPLATES.values())
            for owner in sorted(owners.intersection(DEFAULT_INTERVALS)):
                state = source_health(db, instance, owner)
                emit("tdx_collector_up", int(state["up"]), instance.id, owner)
                emit(
                    "tdx_collector_freshness_limit_seconds",
                    state["freshness_limit_seconds"],
                    instance.id,
                    owner,
                )
                if state["last_attempt_at"]:
                    emit(
                        "tdx_collector_last_attempt_timestamp_seconds",
                        timestamp(state["last_attempt_at"]),
                        instance.id,
                        owner,
                    )
                if state["last_success_at"]:
                    emit(
                        "tdx_collector_last_success_timestamp_seconds",
                        timestamp(state["last_success_at"]),
                        instance.id,
                        owner,
                    )
                    emit(
                        "tdx_collector_sample_age_seconds",
                        state["sample_age_seconds"],
                        instance.id,
                        owner,
                    )
                frame = state["frame"]
                data = (frame.derived_values or {}) if frame else {}
                values = data.get("values", data)
                validity = data.get("metric_validity", {})
                rate_valid = bool(
                    state["up"]
                    and (
                        data.get("window_validity", frame.rate_validity if frame else "unknown")
                        == "valid"
                    )
                )
                emit("tdx_collector_rate_valid", int(rate_valid), instance.id, owner)
                if owner == "identity_status":
                    emit("tdx_db_up", int(state["up"]), instance.id)
                if not state["up"]:
                    continue
                for definition in METRIC_TEMPLATES.values():
                    if definition.owner_collector != owner:
                        continue
                    value = values.get(definition.source_field)
                    metric_valid = validity.get(definition.source_field, {}).get("validity")
                    if metric_valid is None:
                        metric_valid = (
                            "valid"
                            if definition.metric_type == "gauge" or rate_valid
                            else "unknown"
                        )
                    if metric_valid != "valid" or (
                        definition.metric_type != "gauge" and not rate_valid
                    ):
                        continue
                    emit(definition.prometheus_name, value, instance.id, owner)
    return "\n".join(lines) + "\n"


def start_bridge(session_factory, config):
    host = getattr(config, "metrics_bridge_host", "127.0.0.1")
    port = getattr(config, "metrics_bridge_port", 9120)
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError("Metrics bridge must bind a loopback address")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            try:
                body = render_metrics(session_factory).encode()
                status = 200
            except Exception:
                body = b"# control_database_unavailable\n"
                status = 503
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer((host, int(port)), Handler)
    thread = threading.Thread(target=server.serve_forever, name="metrics-bridge", daemon=True)
    thread.start()
    server.thread = thread
    return server
