"""Bounded local JSON snapshots with permission checks at both handoffs."""

from __future__ import annotations

from datetime import timedelta
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import exists, select

from . import models as m
from .access import actor_for_user, authorize
from .audit import append_audit
from .common import PlatformError, iso, json_safe, parse_time, time_window
from .evidence import evidence_response
from .scheduling import check_fence, complete, db_now, enqueue

MAX_ROWS = 5000
MAX_BYTES = 16 * 1024 * 1024


def authorize_export(actor, payload):
    ids = payload.get("instance_ids")
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 10
        or any(not isinstance(i, str) for i in ids)
    ):
        raise PlatformError(422, "invalid_export_scope", "导出需要1–10个明确实例。")
    if (
        payload.get("resource") not in ("evidence", "events", "investigations")
        or payload.get("format", "json") != "json"
    ):
        raise PlatformError(422, "unsupported_export", "请选择受支持的资料类型和 JSON 格式。")
    authorize(actor, "export.create", ids)
    authorize(actor, "data.read", ids)
    if payload["resource"] == "events":
        authorize(actor, "event.read", ids)
    time_window(payload.get("from"), payload.get("to"))
    return frozenset(ids)


def enqueue_export(actor, body, *, idempotency_key=None):
    ids = authorize_export(actor, body)
    start, end = time_window(body.get("from"), body.get("to"))
    payload = {
        "instance_ids": sorted(ids),
        "resource": body["resource"],
        "from": iso(start),
        "to": iso(end),
        "format": "json",
    }
    task = enqueue(
        actor.db,
        "export",
        actor_id=actor.user_id,
        queue="export",
        payload=payload,
        timeout_seconds=180,
        max_attempts=1,
        idempotency_key=idempotency_key,
    )
    append_audit(
        actor.db,
        actor.user_id,
        "export.request",
        "task",
        task.id,
        details={"resource": body["resource"], "instance_count": len(ids)},
    )
    return task


def export_path(config, identifier):
    try:
        if str(UUID(identifier)) != identifier:
            raise ValueError()
    except (TypeError, ValueError):
        raise PlatformError(404, "not_found", "导出不存在。") from None
    directory = (Path(config.private_dir) / "exports").resolve()
    return directory / f"{identifier}.json"


def _bounded_rows(db, query, model):
    # Materialize one small batch before running authorization queries. MySQL
    # server-side streaming cursors cannot safely share a connection with them.
    after = None
    scanned = 0
    while scanned <= MAX_ROWS:
        current = query.where(model.id > after) if after else query
        batch = list(db.scalars(current.order_by(model.id).limit(min(100, MAX_ROWS + 1 - scanned))))
        if not batch:
            return
        for row in batch:
            scanned += 1
            yield row
        after = batch[-1].id
        if len(batch) < 100:
            return


def _records(actor, payload, ids, start, end):
    resource = payload["resource"]
    db = actor.db
    if resource == "evidence":
        query = select(m.Evidence).where(
            m.Evidence.instance_id.in_(ids),
            m.Evidence.observed_at >= start,
            m.Evidence.observed_at < end,
        )
        for row in _bounded_rows(db, query, m.Evidence):
            yield (
                evidence_response(actor, row),
                row.instance_id if row.sensitivity == "restricted" else None,
            )
    elif resource == "events":
        from .events import event_instances, event_response, get_event

        query = select(m.Event).where(
            m.Event.instance_id.in_(ids), m.Event.created_at >= start, m.Event.created_at < end
        )
        for row in _bounded_rows(db, query, m.Event):
            if not event_instances(db, row).issubset(ids):
                yield None, None
                continue
            get_event(actor, row.id)
            yield event_response(row), None
    else:
        from .investigations import get_investigation, investigation_detail

        disallowed = exists(
            select(m.InvestigationResource.investigation_id).where(
                m.InvestigationResource.investigation_id == m.Investigation.id,
                m.InvestigationResource.instance_id.not_in(ids),
            )
        )
        present = exists(
            select(m.InvestigationResource.investigation_id).where(
                m.InvestigationResource.investigation_id == m.Investigation.id
            )
        )
        query = select(m.Investigation).where(
            ~disallowed,
            present,
            m.Investigation.created_at >= start,
            m.Investigation.created_at < end,
        )
        for row in _bounded_rows(db, query, m.Investigation):
            get_investigation(actor, row.id)
            yield investigation_detail(actor, row, include_audit=False), None


def handle_export(session_factory, lease, config):
    """Worker entry point; no public path or user-provided filename is accepted."""
    path = export_path(config, lease.id)
    temporary = None
    published_identity = None
    try:
        with session_factory.begin() as db:
            check_fence(db, lease, allow_inactive=True)
            actor = actor_for_user(db, lease.actor_id)
            ids = authorize_export(actor, lease.payload)
            start, end = time_window(lease.payload.get("from"), lease.payload.get("to"))
            created = db_now(db)
            expires = created + timedelta(hours=24)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.parent / f".{lease.id}.{uuid4()}.tmp"
            metadata = {
                "schema_version": "1",
                "export_id": lease.id,
                "resource": lease.payload["resource"],
                "instance_ids": sorted(ids),
                "from": iso(start),
                "to": iso(end),
                "created_at": iso(created),
                "expires_at": iso(expires),
                "source": "authorized_platform_snapshot",
            }
            prefix = (
                json.dumps(json_safe(metadata), ensure_ascii=False, separators=(",", ":"))[:-1]
                + ',"items":['
            )
            count = 0
            scanned = 0
            size = len(prefix.encode())
            truncated = False
            sensitive = set()
            with temporary.open("xb") as output:
                output.write(prefix.encode())
                for record, restricted_instance in _records(actor, lease.payload, ids, start, end):
                    scanned += 1
                    if scanned > MAX_ROWS:
                        truncated = True
                        break
                    if record is None:
                        continue
                    encoded = json.dumps(
                        json_safe(record), ensure_ascii=False, separators=(",", ":")
                    ).encode()
                    if count >= MAX_ROWS or size + len(encoded) + 2048 > MAX_BYTES:
                        truncated = True
                        break
                    if count % 100 == 0:
                        check_fence(db, lease, allow_inactive=True)
                        authorize_export(actor, lease.payload)
                    if count:
                        output.write(b",")
                        size += 1
                    output.write(encoded)
                    size += len(encoded)
                    count += 1
                    if restricted_instance and not (record.get("payload") or {}).get("redacted"):
                        sensitive.add(restricted_instance)
                coverage = {
                    "complete": False,
                    "gaps": ["bounded_snapshot", "source_retention_not_extended"],
                }
                tail = (
                    '],"count":'
                    + str(count)
                    + ',"truncated":'
                    + json.dumps(truncated)
                    + ',"coverage":'
                    + json.dumps(coverage, separators=(",", ":"))
                    + "}"
                )
                output.write(tail.encode())
                output.flush()
                os.fsync(output.fileno())
            authorize_export(actor, lease.payload)
            if sensitive:
                authorize(actor, "evidence.read_sensitive", sensitive)
            check_fence(db, lease, allow_inactive=True)
            os.replace(temporary, path)
            temporary = None
            published = path.stat()
            published_identity = (published.st_ino, published.st_mtime_ns, published.st_size)
            result = {
                "export_id": lease.id,
                "filename": f"dba-export-{lease.id}.json",
                "count": count,
                "truncated": truncated,
                "coverage": coverage,
                "expires_at": iso(expires),
                "_sensitive_instance_ids": sorted(sensitive),
            }
            complete(db, lease, result=result, allow_inactive=True)
            append_audit(
                db,
                actor.user_id,
                "export.complete",
                "task",
                lease.id,
                details={"count": count, "truncated": truncated},
            )
            return {key: value for key, value in result.items() if not key.startswith("_")}
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        # A stale attempt must never delete a successor's completed artifact.
        if published_identity is not None and path.is_file():
            current = path.stat()
            if (current.st_ino, current.st_mtime_ns, current.st_size) == published_identity:
                path.unlink(missing_ok=True)
        raise


def purge_expired_exports(session_factory, config, limit=100):
    """Reclaim only expired, completed artifacts; task history stays intact."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise PlatformError(422, "invalid_cleanup_budget", "导出清理批次必须为1–1000。")
    removed = 0
    with session_factory.begin() as db:
        current = db_now(db)
        tasks = list(
            db.scalars(
                select(m.Task)
                .where(
                    m.Task.type == "export",
                    m.Task.status == "succeeded",
                    m.Task.finished_at <= current - timedelta(hours=24),
                    m.Task.result["_artifact_purged"].as_boolean().is_not(True),
                )
                .order_by(m.Task.finished_at, m.Task.id)
                .limit(limit)
            )
        )
        for task in tasks:
            try:
                expiry = parse_time((task.result or {}).get("expires_at"))
            except PlatformError:
                continue
            if expiry > current:
                continue
            path = export_path(config, task.id)
            if path.is_file() and not path.is_symlink():
                path.unlink()
                removed += 1
            task.result = {**(task.result or {}), "_artifact_purged": True}
    return {"expired_exports_deleted": removed}
