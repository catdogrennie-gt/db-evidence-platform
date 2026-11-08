"""Current-scope task control and private export download endpoints."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import re
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .. import models as m
from ..access import ActorContext, authorize, authorize_system, current_permissions, scoped_instance
from ..audit import append_audit
from ..common import PlatformError, iso, page_query, parse_time, time_window
from ..db import utcnow
from ..identity import current_actor
from ..inventory import COLLECTORS
from ..scheduling import TERMINAL, enqueue, request_cancel, task_ref

router = APIRouter(tags=["tasks", "exports"])


def source_instances(db, task):
    payload = task.payload or {}
    ids = set()
    if task.instance_id:
        ids.add(task.instance_id)
    values = payload.get("instance_ids", [])
    if isinstance(values, list):
        ids.update(v for v in values if isinstance(v, str))
    if task.type == "investigation":
        investigation_id = task.input_ref or payload.get("investigation_id")
        if investigation_id:
            ids.update(
                db.scalars(
                    select(m.InvestigationResource.instance_id).where(
                        m.InvestigationResource.investigation_id == investigation_id
                    )
                )
            )
    if task.type == "verification":
        verification = (
            db.get(m.Verification, payload.get("verification_id"))
            if payload.get("verification_id")
            else None
        )
        if verification:
            ids.add(verification.instance_id)
            from ..events import event_instances

            event = db.get(m.Event, verification.event_id)
            if event:
                ids.update(event_instances(db, event))
            manifest = (
                db.get(m.EvidenceManifest, verification.manifest_id)
                if verification.manifest_id
                else None
            )
            if manifest:
                ids.update(manifest.instance_ids or [])
    return ids


def authorize_task(actor, task, *, write=False):
    if task is None:
        raise PlatformError(404, "not_found", "任务不存在。")
    payload = task.payload or {}
    if task.type == "connection_test":
        # Connection test references are private to the author; they can expose
        # opaque credential references only through their separately checked API.
        if task.actor_id != actor.user_id:
            raise PlatformError(404, "not_found", "任务不存在。")
        if payload.get("instance_id"):
            scoped_instance(actor, payload["instance_id"], "asset.manage")
        else:
            authorize_system(actor, payload.get("system_id"), "asset.onboard")
        return set()
    ids = source_instances(actor.db, task)
    if not ids:
        raise PlatformError(404, "not_found", "任务不存在。")
    if task.actor_id != actor.user_id:
        authorize(actor, "task.manage", ids)
    if task.type == "activate_instance":
        authorize(actor, "asset.manage", ids)
    else:
        authorize(actor, "data.read", ids)
        if task.type == "verification" or (
            task.type == "export" and payload.get("resource") == "events"
        ):
            authorize(actor, "event.read", ids)
    if task.type == "explain":
        authorize(actor, "collection.trigger", ids)
        authorize(actor, "evidence.read_sensitive", ids)
    if write:
        action = {
            "collect_instance": "collection.trigger",
            "activate_instance": "asset.manage",
            "investigation": "investigation.create",
            "verification": "verification.create",
            "export": "export.create",
            "explain": "collection.trigger",
        }.get(task.type)
        if action is None:
            raise PlatformError(409, "unsupported_task_action", "该任务不支持通用操作。")
        authorize(actor, action, ids)
    return ids


def _uuid(value):
    try:
        return str(UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _unknown_cost(db, task):
    return (
        "unknown" in (task.error_code or "")
        or db.scalar(
            select(m.ModelCall.id)
            .where(
                m.ModelCall.investigation_id == task.input_ref,
                m.ModelCall.status.in_(["pending", "outcome_unknown"]),
            )
            .limit(1)
        )
        is not None
    )


def safe_task_detail(db, task):
    result = {}
    raw = task.result or {}
    for key in (
        "instance_id",
        "test_id",
        "investigation_id",
        "verification_id",
        "event_id",
        "export_id",
        "manifest_id",
        "evidence_id",
    ):
        if _uuid(raw.get(key)):
            result[key] = raw[key]
    for key in ("run_ids", "evidence_ids"):
        if isinstance(raw.get(key), list):
            result[key] = [v for v in raw[key][:5000] if _uuid(v)]
    if isinstance(raw.get("collectors"), dict):
        result["collectors"] = {
            key: value
            for key, value in raw["collectors"].items()
            if key in COLLECTORS
            and value in ("succeeded", "partial", "failed", "cancelled", "not_applicable")
        }
    if isinstance(raw.get("count"), int) and not isinstance(raw["count"], bool):
        result["count"] = max(0, raw["count"])
    if isinstance(raw.get("truncated"), bool):
        result["truncated"] = raw["truncated"]
    if raw.get("expires_at"):
        try:
            result["expires_at"] = iso(parse_time(raw["expires_at"]))
        except PlatformError:
            pass
    if task.type == "export" and result.get("export_id") == task.id:
        result["filename"] = f"dba-export-{task.id}.json"
        result["coverage"] = {
            "complete": False,
            "gaps": ["bounded_snapshot", "source_retention_not_extended"],
        }
    progress = {
        key: value
        for key, value in (task.progress or {}).items()
        if key in ("completed", "total", "percent", "row_count")
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    }
    reference = None
    for key, path in [
        ("evidence_id", "evidence"),
        ("investigation_id", "investigations"),
        ("verification_id", "verifications"),
        ("instance_id", "instances"),
        ("event_id", "events"),
    ]:
        if result.get(key):
            reference = f"/api/v1/{path}/{result[key]}"
            break
    code = (
        task.error_code
        if isinstance(task.error_code, str)
        and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", task.error_code)
        else None
    )
    instance = db.get(m.Instance, task.instance_id) if task.instance_id else None
    return {
        **task_ref(task),
        "instance_id": task.instance_id,
        "instance_name": instance.name if instance else None,
        "attempt": task.attempt,
        "parent_task_id": task.parent_task_id,
        "started_at": iso(task.started_at),
        "finished_at": iso(task.finished_at),
        "heartbeat_at": None,
        "cancel_requested": task.cancel_requested,
        "config_revision": (task.payload or {}).get("config_revision"),
        "result_ref": reference,
        "result": result,
        "progress": progress,
        "error_code": code,
        "needs_external_confirmation": task.type == "investigation"
        and (task.payload or {}).get("mode") == "live_redacted",
        "needs_unknown_cost_acknowledgement": task.type == "investigation"
        and _unknown_cost(db, task),
        "error_message": "任务未完成，请根据错误码核对来源、权限及当前状态。" if code else None,
    }


@router.get("/tasks")
def list_tasks(
    limit: int = 50,
    cursor: str | None = None,
    status: str | None = None,
    instance_id: str | None = None,
    department_id: str | None = None,
    system_id: str | None = None,
    environment: str | None = None,
    from_time: str | None = Query(default=None, alias="from"),
    to_time: str | None = Query(default=None, alias="to"),
    q: str | None = None,
    actor: ActorContext = Depends(current_actor),
):
    statement = select(m.Task)
    if "task.manage" not in current_permissions(actor):
        statement = statement.where(m.Task.actor_id == actor.user_id)
    visible = []
    for task in actor.db.scalars(statement):
        try:
            ids = authorize_task(actor, task)
        except PlatformError:
            continue
        except Exception as exc:
            from fastapi import HTTPException

            if isinstance(exc, HTTPException):
                continue
            raise
        if instance_id and instance_id not in ids:
            continue
        if department_id or system_id or environment:
            selected = (
                select(m.Instance.id)
                .join(m.System, m.System.id == m.Instance.system_id)
                .where(m.Instance.id.in_(ids))
            )
            if department_id:
                selected = selected.where(m.System.department_id == department_id)
            if system_id:
                selected = selected.where(m.System.id == system_id)
            if environment:
                selected = selected.where(m.System.environment == environment)
            if not actor.db.scalar(selected.limit(1)):
                continue
        visible.append(task.id)
    scoped = select(m.Task).where(m.Task.id.in_(visible))
    if status:
        scoped = scoped.where(m.Task.status == status)
    if from_time or to_time:
        start, end = time_window(from_time, to_time)
        scoped = scoped.where(m.Task.created_at >= start, m.Task.created_at < end)
    if q:
        escaped = q[:120].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        scoped = scoped.where((m.Task.id == q) | m.Task.type.ilike(f"%{escaped}%", escape="\\"))
    rows, total, next_cursor = page_query(actor.db, scoped, m.Task, limit=limit, cursor=cursor)
    return {
        "items": [safe_task_detail(actor.db, row) for row in rows],
        "total": total,
        "next_cursor": next_cursor,
    }


@router.get("/tasks/{identifier}")
def detail(identifier: str, actor: ActorContext = Depends(current_actor)):
    task = actor.db.get(m.Task, identifier)
    authorize_task(actor, task)
    return safe_task_detail(actor.db, task)


@router.post("/tasks/{identifier}/cancel")
def cancel(
    identifier: str, body: dict = Body(default={}), actor: ActorContext = Depends(current_actor)
):
    task = actor.db.scalar(select(m.Task).where(m.Task.id == identifier).with_for_update())
    authorize_task(actor, task, write=True)
    was_waiting = task.status in ("queued", "retry_wait")
    request_cancel(actor.db, task)
    if was_waiting and task.status == "cancelled":
        payload = task.payload or {}
        if task.type == "investigation":
            row = actor.db.get(m.Investigation, task.input_ref or payload.get("investigation_id"))
            if row and row.task_id == task.id and row.status == "queued":
                row.status = "cancelled"
        elif task.type == "connection_test":
            row = actor.db.get(m.ConnectionTest, payload.get("test_id"))
            if row and row.task_id == task.id and row.status == "queued":
                row.status = "cancelled"
        elif task.type == "verification":
            row = actor.db.get(m.Verification, payload.get("verification_id"))
            if row and row.status == "queued":
                row.status = "cancelled"
        elif task.type == "activate_instance":
            instance = actor.db.get(m.Instance, task.instance_id)
            if (
                instance
                and instance.lifecycle_state == "Activating"
                and instance.config_revision == payload.get("config_revision")
            ):
                instance.lifecycle_state = "Ready"
                instance.enabled = False
    append_audit(actor.db, actor.user_id, "task.cancel", "task", task.id)
    actor.db.commit()
    return safe_task_detail(actor.db, task)


@router.post("/tasks/{identifier}/retry", status_code=202)
def retry(
    identifier: str,
    request: Request,
    body: dict = Body(default={}),
    actor: ActorContext = Depends(current_actor),
):
    old = actor.db.get(m.Task, identifier)
    ids = authorize_task(actor, old, write=True)
    if old.status not in ("failed", "cancelled", "expired"):
        raise PlatformError(409, "retry_not_allowed", "只有失败、取消或过期任务可以创建新任务。")
    original = old.payload or {}
    header = request.headers.get("Idempotency-Key")
    key = hashlib.sha256(f"retry:{old.id}:{header}".encode()).hexdigest() if header else None
    if old.type == "connection_test":
        raise PlatformError(
            409, "new_preflight_required", "连接预检需要重新提交当前连接配置和有效凭据。"
        )
    if old.type == "collect_instance":
        instance = scoped_instance(actor, old.instance_id, "collection.trigger")
        if not instance.enabled or instance.lifecycle_state != "Active":
            raise PlatformError(409, "instance_not_active", "实例已停采，不能重试采集。")
        collectors = original.get("collectors") or ["identity_status"]
        if not isinstance(collectors, list) or any(v not in COLLECTORS for v in collectors):
            raise PlatformError(409, "new_collection_required", "请重新选择当前采集器。")
        new = enqueue(
            actor.db,
            "collect_instance",
            actor_id=actor.user_id,
            instance_id=instance.id,
            payload={"collectors": collectors, "config_revision": instance.config_revision},
            parent_task_id=old.id,
            idempotency_key=key,
        )
    elif old.type == "activate_instance":
        instance = scoped_instance(actor, old.instance_id, "asset.manage")
        from ..scheduling import find_idempotent

        payload = {"config_revision": instance.config_revision}
        new = find_idempotent(
            actor.db, "activate_instance", actor.user_id, key, payload, instance_id=instance.id
        )
        if not new:
            if instance.lifecycle_state not in ("Ready", "Paused"):
                raise PlatformError(409, "new_activation_required", "请先核对实例状态后再启用。")
            new = enqueue(
                actor.db,
                "activate_instance",
                actor_id=actor.user_id,
                instance_id=instance.id,
                payload=payload,
                parent_task_id=old.id,
                idempotency_key=key,
            )
            instance.lifecycle_state = "Activating"
    elif old.type == "investigation":
        unknown = _unknown_cost(actor.db, old)
        if unknown and body.get("acknowledge_unknown_cost") is not True:
            raise PlatformError(
                409,
                "unknown_model_cost_acknowledgement_required",
                "上次模型请求可能已计费；明确接受未知费用结果后才能创建新调查。",
            )
        from ..investigations import create_investigation

        options = dict(original.get("options") or {})
        if original.get("mode") == "live_redacted" and body.get("external_confirmed") is not True:
            raise PlatformError(
                422, "external_confirmation_required", "重试实时调查需要再次确认外发。"
            )
        options["external_confirmed"] = body.get("external_confirmed") is True
        _, new = create_investigation(
            actor,
            sorted(ids),
            parse_time(original.get("from")),
            parse_time(original.get("to")),
            original.get("question", ""),
            mode=original.get("mode", "rules_only"),
            event_id=original.get("event_id"),
            options=options,
            idempotency_key=key,
        )
        new.parent_task_id = old.id
    elif old.type == "export":
        from ..exports import enqueue_export

        new = enqueue_export(actor, original, idempotency_key=key)
        new.parent_task_id = old.id
    else:
        raise PlatformError(
            409, "new_request_required", "该工作流需要在原业务页核对当前版本后重新提交。"
        )
    append_audit(
        actor.db, actor.user_id, "task.retry", "task", new.id, details={"parent_task_id": old.id}
    )
    actor.db.commit()
    return task_ref(new)


class ExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    instance_ids: list[str] = Field(min_length=1, max_length=10)
    resource: str
    from_time: str = Field(alias="from")
    to_time: str = Field(alias="to")
    format: str = "json"


@router.post("/exports", status_code=202)
def create_export(
    body: ExportInput, request: Request, actor: ActorContext = Depends(current_actor)
):
    from ..exports import enqueue_export

    task = enqueue_export(
        actor,
        body.model_dump(by_alias=True),
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    actor.db.commit()
    return task_ref(task)


@router.get("/exports/{identifier}/download")
def download(identifier: str, request: Request, actor: ActorContext = Depends(current_actor)):
    from ..exports import authorize_export, export_path

    task = actor.db.get(m.Task, identifier)
    if not task or task.type != "export":
        raise PlatformError(404, "not_found", "导出不存在。")
    authorize_export(actor, task.payload)
    result = task.result or {}
    if result.get("_sensitive_instance_ids"):
        authorize(actor, "evidence.read_sensitive", result["_sensitive_instance_ids"])
    if task.status != "succeeded":
        raise PlatformError(409, "export_not_ready", "导出尚未成功完成。")
    expires = parse_time(result.get("expires_at"))
    if expires <= utcnow():
        raise PlatformError(410, "export_expired", "导出已过期，请重新生成。")
    path = export_path(request.app.state.config, task.id)
    if not path.is_file() or path.is_symlink():
        raise PlatformError(410, "export_expired", "导出文件不可用，请重新生成。")
    append_audit(actor.db, actor.user_id, "export.download", "task", task.id)
    actor.db.commit()
    return FileResponse(
        path,
        media_type="application/json",
        filename=f"dba-export-{task.id}.json",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
