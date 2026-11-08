"""Evidence envelopes, atomic cursor persistence and durable object manifests."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, exists, func, or_, select

from .access import authorize
from .common import PlatformError, fields, iso, json_safe, parse_time
from .db import utcnow
from .models import (
    CollectorCapability,
    DigestSnapshot,
    Evidence,
    EvidenceManifest,
    EvidencePin,
    LogCursor,
    LogEvent,
    ManifestItem,
    MetricFrame,
)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            json_safe(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def frame_response(frame):
    return {
        **fields(
            frame,
            [
                "run_id",
                "instance_id",
                "collector",
                "server_uuid",
                "collector_boot_id",
                "counter_epoch",
                "source_epoch",
                "observed_at",
                "window_start",
                "window_end",
                "uptime_seconds",
                "rate_validity",
            ],
        ),
        "derived_values": frame.derived_values or {},
    }


def previous_frame_dict(frame, *, failed_since_previous=False):
    if frame is None:
        return None
    return {
        **frame_response(frame),
        "owner_collector": frame.collector,
        "observed_monotonic_ns": frame.observed_monotonic_ns,
        "raw_values": frame.raw_values,
        "availability": "available",
        "failed_since_previous": failed_since_previous,
    }


def append_batch(
    db, instance, run, batch: dict, *, interval_seconds=30, collector_version="platform-1.0"
):
    """Caller has validated fencing in this SAME transaction; never commit here."""
    evidence_ids = []
    from .models import PlatformSetting

    retention = db.get(PlatformSetting, "retention")
    evidence_days = int((retention.value or {}).get("evidence_days", 14)) if retention else 14
    for item in batch.get("evidence_items", []):
        coverage = dict(item.get("coverage") or {})
        coverage.update(
            availability=item.get("availability", "unknown"),
            error_code=item.get("error_code"),
            collector=run.collector,
            interval_seconds=interval_seconds,
        )
        payload = json_safe(item.get("payload") or {})
        row = Evidence(
            id=str(uuid4()),
            instance_id=instance.id,
            run_id=run.id,
            source=item["source"],
            source_mode=item.get("source_mode", "live"),
            observed_at=parse_time(item.get("observed_at") or batch["observed_at"]),
            server_uuid=batch.get("server_uuid"),
            collector_version=collector_version,
            sensitivity="restricted"
            if item["source"]
            in (
                "mysql.sessions",
                "mysql.transactions",
                "mysql.error_log",
                "mysql.slow_log.file",
                "mysql.slow_log.table",
            )
            else "redacted",
            coverage=coverage,
            payload=payload,
            payload_sha256=digest(payload),
            truncated=item.get("truncated", False),
            expires_at=utcnow() + timedelta(days=evidence_days),
        )
        db.add(row)
        db.flush()
        evidence_ids.append(row.id)
        cursor = item.get("cursor")
        if cursor is not None:
            current = db.scalar(
                select(LogCursor).where(
                    LogCursor.instance_id == instance.id, LogCursor.source == item["source"]
                )
            )
            if current is None:
                current = LogCursor(instance_id=instance.id, source=item["source"])
                db.add(current)
            current.state, current.source_epoch = cursor, str(cursor.get("source_epoch", "unknown"))
            current.version = (current.version or 0) + 1
            current.gap_status = "gap" if coverage.get("gaps") else None
        for event in payload.get("events", []):
            epoch = str(event.get("source_epoch", (cursor or {}).get("source_epoch", "unknown")))
            event_key = str(
                event.get("event_id")
                or event.get("id")
                or event.get("source_event_key")
                or digest(event)
            )[:191]
            duplicate = db.scalar(
                select(LogEvent.id).where(
                    LogEvent.instance_id == instance.id,
                    LogEvent.source == item["source"],
                    LogEvent.source_epoch == epoch,
                    LogEvent.source_event_key == event_key,
                )
            )
            if duplicate:
                continue
            message = (
                event.get("message_categories")
                or event.get("digest_text")
                or event.get("sql_shape")
                or ""
            )
            db.add(
                LogEvent(
                    instance_id=instance.id,
                    evidence_id=row.id,
                    source=item["source"],
                    source_epoch=epoch,
                    source_event_key=event_key,
                    event_time=parse_time(
                        event.get("server_event_at")
                        or event.get("event_time")
                        or item["observed_at"]
                    ),
                    severity=str(
                        event.get("severity")
                        or event.get("priority")
                        or event.get("PRIO")
                        or "unknown"
                    )[:30],
                    code=str(event.get("error_code") or event.get("ERROR_CODE"))[:60]
                    if event.get("error_code") or event.get("ERROR_CODE")
                    else None,
                    message_redacted=json.dumps(message, ensure_ascii=False)
                    if not isinstance(message, str)
                    else message,
                    fingerprint=event.get("message_sha256")
                    or event.get("fingerprint")
                    or digest(event),
                )
            )
    frame = batch.get("metric_frame")
    if frame:
        observed = parse_time(frame["observed_at"])
        seconds = frame.get("window_seconds")
        values = {
            "values": frame.get("derived_values") or {},
            "metric_validity": frame.get("metric_validity") or {},
            "window_validity": frame.get("window_validity", "unknown"),
            "window_seconds": seconds,
            "rate_reasons": frame.get("rate_reasons") or [],
        }
        metric = MetricFrame(
            run_id=run.id,
            instance_id=instance.id,
            collector=run.collector,
            server_uuid=frame["server_uuid"],
            collector_boot_id=frame["collector_boot_id"],
            source_epoch=str(frame.get("source_epoch", frame.get("counter_epoch", 0))),
            observed_monotonic_ns=frame["observed_monotonic_ns"],
            counter_epoch=int(frame.get("counter_epoch", 0)),
            observed_at=observed,
            server_observed_at=parse_time(batch["server_observed_at"])
            if batch.get("server_observed_at")
            else None,
            uptime_seconds=int(frame.get("uptime_seconds") or 0),
            raw_values=json_safe(frame.get("raw_values") or {}),
            derived_values=values,
            rate_validity=frame.get("rate_validity", "unknown"),
            window_start=observed - timedelta(seconds=seconds) if seconds else None,
            window_end=observed,
        )
        db.add(metric)
        if run.collector == "statement_digest":
            by_digest = {
                (item.get("SCHEMA_NAME") or "", item.get("DIGEST")): item
                for item in (frame.get("digest_window") or {}).get("rows", [])
            }
            for value in (frame.get("raw_values") or {}).get("rows", []):
                delta = by_digest.get((value.get("SCHEMA_NAME") or "", value.get("DIGEST")), {})
                db.add(
                    DigestSnapshot(
                        instance_id=instance.id,
                        run_id=run.id,
                        counter_epoch=metric.counter_epoch,
                        schema_name=value.get("SCHEMA_NAME") or "",
                        digest=value["DIGEST"],
                        digest_text=value.get("DIGEST_TEXT") or "",
                        observed_at=observed,
                        count_star=int(value.get("COUNT_STAR") or 0),
                        sum_timer_wait=int(value.get("SUM_TIMER_WAIT") or 0),
                        delta_count=int(delta["count_delta"])
                        if delta.get("count_delta") is not None
                        else None,
                        delta_timer_wait=int(delta["total_time_seconds_delta"] * 1e12)
                        if delta.get("total_time_seconds_delta") is not None
                        else None,
                    )
                )
    for capability in batch.get("capability_results", []):
        name = capability["collector"]
        current = db.scalar(
            select(CollectorCapability).where(
                CollectorCapability.instance_id == instance.id,
                CollectorCapability.collector == name,
            )
        )
        if current is None:
            current = CollectorCapability(instance_id=instance.id, collector=name)
            db.add(current)
        current.availability = capability.get("availability", "unknown")
        current.reason_code = capability.get("reason_code") or capability.get("error_code")
        current.checked_at = utcnow()
        current.details = {
            "owner_collector": run.collector,
            "row_count": capability.get("row_count"),
            "truncated": capability.get("truncated", False),
        }
    return evidence_ids


def create_manifest(db, owner_type, owner_id, instance_ids, from_time, to_time, retain_days=90):
    """Freeze a bounded historical window, including owners that never call AI.

    The caller authorizes the owner and all instance IDs before entering here.
    Frames are materialized directly from committed real observations. This is
    source evidence, not a claim that Prometheus was available or complete.
    """
    start = parse_time(from_time) if isinstance(from_time, str) else from_time
    end = parse_time(to_time) if isinstance(to_time, str) else to_time
    if start >= end or not 1 <= retain_days <= 365:
        raise PlatformError(422, "invalid_manifest_window", "证据保全窗口或保留期无效。")
    ids = sorted(set(instance_ids))
    if not ids or len(ids) > 5:
        raise PlatformError(422, "invalid_manifest_scope", "证据清单需要 1–5 个实例。")
    config = db.info.get("platform_config")
    frozen_metrics = {
        "items": [],
        "coverage": {
            "materialized": False,
            "complete": False,
            "reason_codes": ["prometheus_window_not_materialized"],
        },
    }
    if config is not None:
        from .metrics import materialize_source_windows

        try:
            frozen_metrics = materialize_source_windows(db, config, ids, start, end)
        except PlatformError as exc:
            frozen_metrics["coverage"]["reason_codes"] = [exc.detail["code"]]
    versions = list(
        db.scalars(
            select(EvidenceManifest)
            .where(EvidenceManifest.owner_type == owner_type, EvidenceManifest.owner_id == owner_id)
            .with_for_update()
        )
    )
    # Select by diagnostic layer before the global bound: a chatty metric source
    # must not evict all older SQL/log snapshots or the baseline from a report.
    predicates = [Evidence.instance_id.in_(ids), Evidence.source_mode == "live", Evidence.observed_at >= start, Evidence.observed_at < end]
    groups = list(db.execute(select(Evidence.instance_id, Evidence.source, func.count()).where(*predicates).group_by(Evidence.instance_id, Evidence.source).order_by(Evidence.instance_id, Evidence.source).limit(101)))
    source_group_truncated = len(groups) > 100
    groups = groups[:100]
    quota = max(2, 1000 // max(len(groups), 1))
    selected = {}
    evidence_total = sum(row[2] for row in groups)
    for instance_id, source, count in groups:
        query = select(Evidence).where(*predicates, Evidence.instance_id == instance_id, Evidence.source == source)
        baseline = db.scalar(query.order_by(Evidence.observed_at, Evidence.id).limit(1).with_for_update())
        reserved={baseline.id:baseline} if baseline else {}
        if source=='mysql.lock_waits':
            # Reserve a real nonempty snapshot before recent healthy samples
            # consume this layer's quota. Keep the global 1000-item limit.
            sqlite=db.get_bind().dialect.name=='sqlite'
            length=func.json_array_length(Evidence.payload,'$.rows') if sqlite else func.json_length(Evidence.payload,'$.rows')
            row_type=func.json_type(Evidence.payload,'$.rows') if sqlite else func.json_type(func.json_extract(Evidence.payload,'$.rows'))
            availability=Evidence.coverage['availability'].as_string()
            representative=db.scalar(query.where(length>0,row_type==('array' if sqlite else 'ARRAY'),or_(availability.is_(None),availability.in_(['available','unknown','degraded']))).order_by(Evidence.observed_at.desc(),Evidence.id.desc()).limit(1).with_for_update())
            if representative: reserved[representative.id]=representative
        newest = list(db.scalars(query.order_by(Evidence.observed_at.desc(), Evidence.id.desc()).limit(quota-len(reserved)).with_for_update()))
        for row in newest + list(reserved.values()): selected[row.id] = row
    evidence = sorted(selected.values(), key=lambda row: (row.observed_at, row.id), reverse=True)[:1000]
    frames = list(
        db.scalars(
            select(MetricFrame)
            .where(
                MetricFrame.instance_id.in_(ids),
                MetricFrame.observed_at >= start,
                MetricFrame.observed_at < end,
            )
            .order_by(MetricFrame.observed_at.desc())
            .limit(1001)
            .with_for_update()
        )
    )
    bounded_truncation = source_group_truncated or evidence_total > len(evidence) or len(frames) > 1000
    evidence, frames = evidence[:1000], frames[:1000]
    metric_coverage = frozen_metrics["coverage"]
    source_gaps = sorted(
        {
            f"{item.source}:{(item.coverage or {}).get('availability', 'unknown')}"
            for item in evidence
            if (item.coverage or {}).get("availability", "unknown")
            not in ("available", "not_applicable")
        }
    )
    for item in evidence:
        source_coverage = item.coverage or {}
        if (
            source_coverage.get("complete") is False
            and source_coverage.get("availability") != "not_applicable"
        ):
            source_gaps.append(f"{item.source}:source_coverage_partial")
        if source_coverage.get("gaps"):
            source_gaps.append(f"{item.source}:source_gaps_recorded")
        if item.truncated:
            source_gaps.append(f"{item.source}:source_truncated")
    truncated = bounded_truncation or any(item.truncated for item in evidence)
    gaps = sorted(
        set(
            metric_coverage.get("reason_codes", [])
            + source_gaps
            + (["manifest_limit_reached"] if bounded_truncation else [])
        )
    )
    payload = {
        "instance_ids": ids,
        "window": {"from": iso(start), "to": iso(end)},
        "evidence": [{"id": e.id, "sha256": e.payload_sha256} for e in evidence],
        "metric_frames": [
            {"run_id": frame.run_id, "payload_sha256": digest(frame_response(frame))}
            for frame in frames
        ],
        "materialization": "committed_metric_frames",
        "prometheus_materialization": metric_coverage,
        "prometheus_windows": [
            {
                "instance_id": item["instance_id"],
                "template": item["template"],
                "sha256": digest(item),
            }
            for item in frozen_metrics["items"]
        ],
        "coverage_complete": bool(metric_coverage.get("complete")) and not gaps,
        "truncated": truncated,
        "gaps": gaps,
    }
    manifest = EvidenceManifest(
        id=str(uuid4()),
        owner_type=owner_type,
        owner_id=owner_id,
        version=max((row.version for row in versions), default=0) + 1,
        instance_ids=ids,
        window_start=start,
        window_end=end,
        sources=sorted({item.source for item in evidence})
        + ["metric_frames"]
        + (sorted({item.get("source", "prometheus_resampled") for item in frozen_metrics["items"]}) if metric_coverage.get("materialized") else [])
        + ([] if payload["coverage_complete"] else ["materialization_partial"]),
        manifest_sha256=digest(payload),
        retain_until=utcnow() + timedelta(days=retain_days),
    )
    db.add(manifest)
    db.flush()
    for row in evidence:
        db.add(
            ManifestItem(
                manifest_id=manifest.id,
                evidence_id=row.id,
                instance_id=row.instance_id,
                item_type="evidence",
                payload={"source": row.source, "observed_at": iso(row.observed_at)},
                payload_sha256=row.payload_sha256,
            )
        )
        db.add(
            EvidencePin(
                manifest_id=manifest.id, evidence_id=row.id, retain_until=manifest.retain_until
            )
        )
    for frame in frames:
        materialized = frame_response(frame)
        db.add(
            ManifestItem(
                manifest_id=manifest.id,
                metric_run_id=frame.run_id,
                instance_id=frame.instance_id,
                item_type="metric_window",
                payload=materialized,
                payload_sha256=digest(materialized),
            )
        )
    for item in frozen_metrics["items"]:
        db.add(
            ManifestItem(
                manifest_id=manifest.id,
                instance_id=item["instance_id"],
                item_type="time_series_window" if item.get("source") == "zabbix_history" else "prometheus_window",
                payload=item,
                payload_sha256=digest(item),
            )
        )
    db.add(
        ManifestItem(
            manifest_id=manifest.id,
            instance_id=ids[0],
            item_type="coverage",
            payload=payload,
            payload_sha256=digest(payload),
        )
    )
    db.flush()
    return manifest


def get_manifest(actor, manifest_id):
    manifest = actor.db.get(EvidenceManifest, manifest_id)
    if not manifest:
        raise PlatformError(404, "not_found", "证据清单不存在。")
    authorize(actor, "data.read", manifest.instance_ids)
    return manifest


def evidence_response(actor, row, *, include_payload=True):
    authorize(actor, "data.read", [row.instance_id])
    response = fields(
        row,
        [
            "id",
            "instance_id",
            "run_id",
            "source",
            "source_mode",
            "schema_version",
            "observed_at",
            "ingested_at",
            "server_uuid",
            "collector_version",
            "sensitivity",
            "coverage",
            "payload_sha256",
            "truncated",
        ],
    )
    if include_payload:
        if row.sensitivity == "restricted":
            try:
                authorize(actor, "evidence.read_sensitive", [row.instance_id])
            except Exception as exc:
                from fastapi import HTTPException

                if not isinstance(exc, HTTPException):
                    raise
                response["payload"] = {
                    "redacted": True,
                    "reason_code": "sensitive_permission_required",
                    "row_count": row.payload.get("row_count", len(row.payload.get("events", []))),
                }
                return response
        response["payload"] = row.payload
    return response


def purge_expired(db, now=None, limit=1000):
    """Bounded cleanup that honors every active owner, even without an AI run.

    No implicit commit. Callers release the transaction between batches. Object
    history is retained independently; unresolved foreign references cause safe
    over-retention instead of destroying its provenance.
    """
    from .models import InvestigationEvidence, PlatformSetting
    from .scheduling import db_now

    stamp = now or db_now(db)
    setting = db.get(PlatformSetting, "retention")
    evidence_days = int((setting.value or {}).get("evidence_days", 14)) if setting else 14
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise PlatformError(422, "invalid_cleanup_budget", "每批清理预算为 1–1000 条。")
    stats = {
        "evidence_deleted": 0,
        "metric_frames_deleted": 0,
        "manifest_items_deleted": 0,
        "expired_pins_deleted": 0,
        "log_events_deleted": 0,
        "manifests_deleted": 0,
        "digest_snapshots_deleted": 0,
    }

    def remaining():
        return max(0, limit - sum(stats.values()))

    expired_items = list(
        db.scalars(
            select(ManifestItem.id)
            .join(EvidenceManifest, ManifestItem.manifest_id == EvidenceManifest.id)
            .where(EvidenceManifest.retain_until <= stamp)
            .order_by(EvidenceManifest.retain_until)
            .limit(remaining())
        )
    )
    if expired_items:
        db.execute(delete(ManifestItem).where(ManifestItem.id.in_(expired_items)))
        stats["manifest_items_deleted"] = len(expired_items)
    pins = list(
        db.scalars(
            select(EvidencePin.id).where(EvidencePin.retain_until <= stamp).limit(remaining())
        )
    )
    if pins:
        db.execute(delete(EvidencePin).where(EvidencePin.id.in_(pins)))
        stats["expired_pins_deleted"] = len(pins)
    # Any remaining reference protects an evidence row. Expired references not
    # reached in this bounded pass are cleaned on the next pass, never bypassed.
    # A policy extension also protects rows created under an older expiry.
    # Keep the creation expiry as a lower bound: tightening is a separate
    # reviewed operation and must not silently shorten an existing guarantee.
    candidates = list(
        db.scalars(
            select(Evidence)
            .where(
                Evidence.expires_at <= stamp,
                Evidence.observed_at <= stamp - timedelta(days=evidence_days),
                ~exists(select(EvidencePin.id).where(EvidencePin.evidence_id == Evidence.id)),
                ~exists(select(ManifestItem.id).where(ManifestItem.evidence_id == Evidence.id)),
                ~exists(
                    select(InvestigationEvidence.evidence_id).where(
                        InvestigationEvidence.evidence_id == Evidence.id
                    )
                ),
            )
            .order_by(Evidence.expires_at, Evidence.id)
            .limit(remaining())
            .with_for_update(skip_locked=True)
        )
    )
    identifiers = [item.id for item in candidates]
    if identifiers:
        children = list(
            db.scalars(
                select(LogEvent.id).where(LogEvent.evidence_id.in_(identifiers)).limit(remaining())
            )
        )
        if children:
            db.execute(delete(LogEvent).where(LogEvent.id.in_(children)))
            stats["log_events_deleted"] = len(children)
        db.flush()
        for item in candidates:
            if remaining() and not db.scalar(
                select(LogEvent.id).where(LogEvent.evidence_id == item.id).limit(1)
            ):
                db.delete(item)
                stats["evidence_deleted"] += 1
    days = (setting.value or {}).get("metric_frames_days", 7) if setting else 7

    def protected_run(run_id):
        return or_(
            exists(select(ManifestItem.id).where(ManifestItem.metric_run_id == run_id)),
            exists(
                select(EvidencePin.id)
                .join(Evidence, EvidencePin.evidence_id == Evidence.id)
                .where(Evidence.run_id == run_id)
            ),
            exists(
                select(InvestigationEvidence.evidence_id)
                .join(Evidence, InvestigationEvidence.evidence_id == Evidence.id)
                .where(Evidence.run_id == run_id)
            ),
        )

    frames = list(
        db.scalars(
            select(MetricFrame)
            .where(
                MetricFrame.observed_at < stamp - timedelta(days=days),
                ~protected_run(MetricFrame.run_id),
            )
            .order_by(MetricFrame.observed_at)
            .limit(remaining())
            .with_for_update(skip_locked=True)
        )
    )
    for frame in frames:
        db.delete(frame)
    stats["metric_frames_deleted"] = len(frames)
    digest_days = int((setting.value or {}).get("evidence_days", 14)) if setting else 14
    snapshots = list(
        db.scalars(
            select(DigestSnapshot)
            .where(
                DigestSnapshot.observed_at < stamp - timedelta(days=digest_days),
                ~protected_run(DigestSnapshot.run_id),
            )
            .order_by(DigestSnapshot.observed_at, DigestSnapshot.id)
            .limit(remaining())
            .with_for_update(skip_locked=True)
        )
    )
    for snapshot in snapshots:
        db.delete(snapshot)
    stats["digest_snapshots_deleted"] = len(snapshots)
    manifests = list(
        db.scalars(
            select(EvidenceManifest)
            .where(
                EvidenceManifest.retain_until <= stamp,
                ~exists(
                    select(ManifestItem.id).where(ManifestItem.manifest_id == EvidenceManifest.id)
                ),
                ~exists(
                    select(EvidencePin.id).where(EvidencePin.manifest_id == EvidenceManifest.id)
                ),
            )
            .order_by(EvidenceManifest.retain_until)
            .limit(remaining())
        )
    )
    for manifest in manifests:
        db.delete(manifest)
    stats["manifests_deleted"] = len(manifests)
    db.flush()
    stats["cleanup_budget_exhausted"] = remaining() == 0
    return stats
