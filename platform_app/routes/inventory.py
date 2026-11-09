"""Scope-aware inventory and asynchronous onboarding commands."""

from ..config import require_legacy, group_collection_allowed

from datetime import timedelta
import hashlib
import hmac
import json
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy import exists, or_, select

from ..access import (
    ActorContext,
    allowed_scope_ids,
    authorize,
    authorize_system,
    current_permissions,
    scoped_instance,
)
from ..audit import append_audit
from ..common import PlatformError, fields, iso, page_query
from ..db import utcnow
from ..identity import current_actor
from ..inventory import (
    COLLECTORS,
    CONNECTION_FIELDS,
    asset_metadata,
    checked_test,
    create_connection_test,
    expected_revision,
    instance_response,
    register_instance,
    secret_for_request,
    stop_collection,
    test_response,
    validated_config,
    source_refs,
    SOURCE_FIELDS,
)
from ..models import CollectionSchedule, ConnectionDraft, ConnectionTest, Instance, Task
from ..scheduling import enqueue, find_idempotent, task_ref
from ..secrets import SecretStore

router = APIRouter(tags=["inventory"])


def _ids(actor, actions=("data.read", "asset.manage", "asset.onboard")):
    permissions = current_permissions(actor)
    result = set()
    for action in actions:
        if action in permissions:
            result.update(authorize(actor, action).instance_ids)
    return result


def _instance(actor, identifier):
    if identifier not in _ids(actor):
        raise PlatformError(404, "not_found", "资产不存在。")
    return actor.db.get(Instance, identifier)


def _draft_response(row):
    return {
        **fields(
            row,
            [
                "id",
                "system_id",
                "instance_id",
                "revision",
                "created_at",
                "updated_at",
                "expires_at",
            ],
        ),
        **row.config,
        "credential_ref": row.secret_ref,
        "has_secret": bool(row.secret_ref),
    }


@router.get("/connection-drafts")
def list_drafts(
    limit: int = 50, cursor: str | None = None, actor: ActorContext = Depends(current_actor)
):
    authorize(actor, "asset.onboard")
    statement = select(ConnectionDraft).where(
        ConnectionDraft.created_by == actor.user_id,
        ConnectionDraft.expires_at > utcnow(),
        ConnectionDraft.system_id.in_(allowed_scope_ids(actor, "asset.onboard", "system")),
        ~exists(select(Instance.id).where(Instance.credential_ref == ConnectionDraft.secret_ref)),
    )
    rows, total, next_cursor = page_query(
        actor.db, statement, ConnectionDraft, limit=limit, cursor=cursor, time_column="updated_at"
    )
    return {
        "items": [_draft_response(row) for row in rows],
        "total": total,
        "next_cursor": next_cursor,
    }


def _save_draft(actor, config, body, row=None):
    system_id = body.get("system_id") or (row.system_id if row else None)
    authorize_system(actor, system_id, "asset.onboard")
    # These drafts are for NEW assets only. Existing assets use their scoped,
    # versioned revalidation flow, never an origin-free credential container.
    if body.get("instance_id") or (row and row.instance_id):
        raise PlatformError(
            422, "existing_asset_draft_forbidden", "已有资产请使用该实例的连接重验流程。"
        )
    if row is None:
        row = ConnectionDraft(
            id=str(uuid4()),
            created_by=actor.user_id,
            system_id=system_id,
            config={},
            expires_at=utcnow() + timedelta(days=7),
        )
        actor.db.add(row)
    if body.get("revision") is not None and body["revision"] != row.revision:
        raise PlatformError(409, "draft_revision_changed", "草稿已更新，请刷新。")
    allowed = (
        "name",
        "engine",
        "connection",
        "selected_schemas",
        "requested_collectors",
        "collection_profile",
        "host_profile_ref",
        "log_profile_ref",
        "description",
        "tags",
    )
    clean = dict(row.config)
    clean.update({key: body[key] for key in allowed if key in body})
    if "connection" in clean:
        clean["connection"] = {
            key: clean["connection"][key] for key in CONNECTION_FIELDS if key in clean["connection"]
        }
    if len(str(clean)) > 65536:
        raise PlatformError(422, "draft_too_large", "草稿字段超过限制。")
    if row.system_id != system_id:
        row.secret_ref, row.secret_version = None, 1
    row.config, row.system_id = clean, system_id
    row.revision = (row.revision or 0) + 1
    if body.get("credentials") is not None or body.get("credential_ref"):
        row.secret_ref, row.secret_version = secret_for_request(
            actor.db, actor, SecretStore(config), body, system_id
        )
    row.expires_at = utcnow() + timedelta(days=7)
    append_audit(actor.db, actor.user_id, "connection.draft.save", "connection_draft", row.id)
    actor.db.commit()
    return _draft_response(row)


@router.post("/connection-drafts", status_code=201)
def create_draft(
    request: Request, body: dict = Body(...), actor: ActorContext = Depends(current_actor)
):
    require_legacy(request.app.state.config)
    return _save_draft(actor, request.app.state.config, body)


@router.get("/connection-drafts/{identifier}")
def get_draft(identifier: str, actor: ActorContext = Depends(current_actor)):
    row = actor.db.get(ConnectionDraft, identifier)
    if not row or row.created_by != actor.user_id or row.expires_at <= utcnow():
        raise PlatformError(404, "not_found", "草稿不存在或已过期。")
    authorize_system(actor, row.system_id, "asset.onboard")
    if row.secret_ref and actor.db.scalar(
        select(Instance.id).where(Instance.credential_ref == row.secret_ref).limit(1)
    ):
        raise PlatformError(404, "not_found", "草稿已用于登记资产。")
    return _draft_response(row)


@router.patch("/connection-drafts/{identifier}")
def patch_draft(
    identifier: str,
    request: Request,
    body: dict = Body(...),
    actor: ActorContext = Depends(current_actor),
):
    require_legacy(request.app.state.config)
    get_draft(identifier, actor)
    return _save_draft(
        actor, request.app.state.config, body, actor.db.get(ConnectionDraft, identifier)
    )


@router.post("/connections/test", status_code=202)
def test_connection(
    request: Request, body: dict = Body(...), actor: ActorContext = Depends(current_actor)
):
    require_legacy(request.app.state.config)
    task = create_connection_test(
        actor.db,
        actor,
        request.app.state.config,
        body,
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    actor.db.commit()
    return task_ref(task)


@router.get("/connections/tests/{identifier}")
def get_test(identifier: str, actor: ActorContext = Depends(current_actor)):
    row = actor.db.get(ConnectionTest, identifier)
    if not row or row.created_by != actor.user_id:
        raise PlatformError(404, "not_found", "预检记录不存在。")
    task = actor.db.get(Task, row.task_id) if row.task_id else None
    origin_instance = (task.payload or {}).get("instance_id") if task else None
    if origin_instance:
        scoped_instance(actor, origin_instance, "asset.manage")
    else:
        authorize_system(actor, row.system_id, "asset.onboard")
    return test_response(row)


@router.get("/instances")
def list_instances(
    system_id: str | None = None,
    department_id: str | None = None,
    environment: str | None = None,
    health: str | None = None,
    q: str | None = None,
    lifecycle: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    actor: ActorContext = Depends(current_actor),
):
    from ..models import System

    statement = select(Instance).where(Instance.id.in_(_ids(actor)))
    if lifecycle:
        statement = statement.where(Instance.lifecycle_state == lifecycle)
    else:
        statement = statement.where(Instance.lifecycle_state != "Archived")
    if system_id:
        statement = statement.where(Instance.system_id == system_id)
    if department_id or environment:
        statement = statement.join(System, System.id == Instance.system_id)
        if department_id:
            statement = statement.where(System.department_id == department_id)
        if environment:
            statement = statement.where(System.environment == environment)
    if health:
        statement = statement.where(Instance.health == health)
    if q:
        escaped = q[:120].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = statement.where(
            or_(Instance.name.ilike(f"%{escaped}%", escape="\\"), Instance.id == q)
        )
    rows, total, next_cursor = page_query(actor.db, statement, Instance, limit=limit, cursor=cursor)
    return {
        "items": [instance_response(actor.db, actor, row) for row in rows],
        "total": total,
        "next_cursor": next_cursor,
    }


@router.post("/instances", status_code=201)
def create_instance(
    request: Request, body: dict = Body(...), actor: ActorContext = Depends(current_actor)
):
    require_legacy(request.app.state.config)
    row = register_instance(actor.db, actor, request.app.state.config, body)
    actor.db.commit()
    return instance_response(actor.db, actor, row)


@router.get("/instances/{identifier}")
def get_instance(identifier: str, actor: ActorContext = Depends(current_actor)):
    return instance_response(actor.db, actor, _instance(actor, identifier))


@router.patch("/instances/{identifier}")
def update_instance(
    identifier: str,
    request: Request,
    body: dict = Body(...),
    actor: ActorContext = Depends(current_actor),
):
    row = scoped_instance(actor, identifier, "asset.manage")
    if set(body) & {"connection", "endpoint", "port", "connection_mode", "collectors", "collection_profile_id", "credential_ref", *SOURCE_FIELDS}:
        require_legacy(request.app.state.config)
    expected_revision(row, request.headers.get("If-Match"))
    if any(
        key in body
        for key in ("system_id", "expected_server_uuid", "server_uuid", "engine", "is_lab")
    ):
        raise PlatformError(
            409, "immutable_asset_identity", "实例身份与归属需要专门迁移，不能普通编辑。"
        )
    if "name" in body:
        if not isinstance(body["name"], str) or not 1 <= len(body["name"].strip()) <= 120:
            raise PlatformError(422, "invalid_name", "资产名称需要 1–120 个字符。")
        row.name = body["name"].strip()
    if "description" in body or "tags" in body:
        row.connection = {
            **row.connection,
            "_metadata": asset_metadata(body, row.connection.get("_metadata")),
        }
    if "connection" in body or any(key in body for key in SOURCE_FIELDS):
        safe = validated_config(
            {
                **row.connection.get("_collection", {}),
                **source_refs(row),
                **{key: body[key] for key in SOURCE_FIELDS if key in body},
                "connection": body.get("connection")
                or {key: row.connection.get(key) for key in CONNECTION_FIELDS},
            },
            request.app.state.config,
        )
        stop_collection(actor.db, row)
        connection = dict(row.connection)
        connection["_staged"] = {
            "connection": safe["connection"],
            "credential_ref": row.credential_ref,
            "credential_version": row.credential_version,
            **{key: safe.get(key) for key in SOURCE_FIELDS},
        }
        row.connection, row.lifecycle_state = connection, "RevalidationRequired"
    else:
        row.config_revision += 1
        for schedule in actor.db.scalars(
            select(CollectionSchedule).where(CollectionSchedule.instance_id == row.id)
        ):
            schedule.config_revision = row.config_revision
    append_audit(actor.db, actor.user_id, "asset.update", "instance", row.id)
    actor.db.commit()
    return instance_response(actor.db, actor, row)


@router.post("/instances/{identifier}/activate", status_code=202)
def activate(
    identifier: str,
    request: Request,
    body: dict = Body(...),
    actor: ActorContext = Depends(current_actor),
):
    require_legacy(request.app.state.config)
    row = scoped_instance(actor, identifier, "asset.manage")
    payload = {"config_revision": body.get("config_revision")}
    old_task = find_idempotent(
        actor.db,
        "activate_instance",
        actor.user_id,
        request.headers.get("Idempotency-Key"),
        payload,
        instance_id=row.id,
    )
    if old_task:
        return task_ref(old_task)
    expected_revision(row, body.get("config_revision"))
    if row.lifecycle_state not in ("Ready", "Paused"):
        raise PlatformError(409, "activation_not_allowed", "资产需要处于 Ready 或 Paused 状态。")
    task = enqueue(
        actor.db,
        "activate_instance",
        actor_id=actor.user_id,
        instance_id=row.id,
        payload=payload,
        timeout_seconds=180,
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    row.lifecycle_state = "Activating"
    append_audit(actor.db, actor.user_id, "asset.activate.request", "instance", row.id)
    actor.db.commit()
    return task_ref(task)


@router.post("/instances/{identifier}/pause")
def pause(
    identifier: str, body: dict = Body(default={}), actor: ActorContext = Depends(current_actor)
):
    row = scoped_instance(actor, identifier, "asset.manage")
    if row.lifecycle_state == "Archived":
        raise PlatformError(409, "asset_archived", "已退役资产不能停采。")
    count = stop_collection(actor.db, row)
    append_audit(actor.db, actor.user_id, "asset.pause", "instance", row.id)
    actor.db.commit()
    return {**instance_response(actor.db, actor, row), "cancel_requested_count": count}


@router.post("/instances/{identifier}/archive")
def archive(identifier: str, body: dict = Body(...), actor: ActorContext = Depends(current_actor)):
    row = scoped_instance(actor, identifier, "asset.manage")
    if body.get("instance_id") != identifier or not str(body.get("reason", "")).strip():
        raise PlatformError(
            422, "archive_confirmation_required", "退役需要确认实例 ID 并填写原因。"
        )
    count = stop_collection(actor.db, row, archive=True)
    append_audit(
        actor.db,
        actor.user_id,
        "asset.archive",
        "instance",
        row.id,
        details={"reason": str(body["reason"])[:1000]},
    )
    actor.db.commit()
    return {**instance_response(actor.db, actor, row), "cancel_requested_count": count}


@router.post("/instances/{identifier}/collect", status_code=202)
def collect(
    identifier: str,
    request: Request,
    body: dict = Body(default={}),
    actor: ActorContext = Depends(current_actor),
):
    row = scoped_instance(actor, identifier, "collection.trigger")
    if not group_collection_allowed(actor.db, row, request.app.state.config):
        require_legacy(request.app.state.config)
    if not row.enabled or row.lifecycle_state != "Active":
        raise PlatformError(409, "instance_not_active", "实例未激活或已停采。")
    permitted=set(COLLECTORS)
    defaults=['identity_status']
    if not group_collection_allowed(actor.db, row, request.app.state.config):
        require_legacy(request.app.state.config)
    if row.connection_mode=='zabbix':
        from ..monitoring import mapping_for
        from ..models import MonitoringSource
        mapping=mapping_for(actor.db,row.id)
        source=actor.db.get(MonitoringSource,mapping.source_id) if mapping else None
        if not mapping or not mapping.selected or not source or source.system_id!=row.system_id:
            raise PlatformError(409,'monitoring_mapping_inactive','监控对象未绑定或映射已停用。')
        permitted={'monitoring'} if mapping.layer=='group' else {item['collector'] for item in mapping.items}
        defaults=sorted(permitted)
    collectors = body.get("collectors") or body.get("collector") or defaults
    if isinstance(collectors, str):
        collectors = [collectors]
    if (
        not isinstance(collectors, list)
        or not collectors
        or any(value not in permitted for value in collectors)
    ):
        raise PlatformError(422, "invalid_collectors", "请从固定采集器中选择。")
    # One task may perform several bounded profiles sequentially for the same
    # asset; other assets continue on independent worker slots.
    task = enqueue(
        actor.db,
        "collect_instance",
        actor_id=actor.user_id,
        instance_id=row.id,
        payload={
            "collectors": list(dict.fromkeys(collectors)),
            "config_revision": row.config_revision,
        },
        idempotency_key=request.headers.get("Idempotency-Key"),
        timeout_seconds=180,
    )
    actor.db.commit()
    return task_ref(task)


@router.post("/instances/{identifier}/capabilities/recheck", status_code=202)
def recheck(identifier: str, request: Request, actor: ActorContext = Depends(current_actor)):
    require_legacy(request.app.state.config)
    row = scoped_instance(actor, identifier, "asset.manage")
    staged = row.connection.get("_staged") or {}
    body = {
        **row.connection.get("_collection", {}),
        **source_refs(row),
        "system_id": row.system_id,
        "instance_id": row.id,
        "config_revision": row.config_revision,
        "connection": staged.get("connection")
        or {key: row.connection.get(key) for key in CONNECTION_FIELDS},
        "credential_ref": staged.get("credential_ref") or row.credential_ref,
    }
    task = create_connection_test(
        actor.db,
        actor,
        request.app.state.config,
        body,
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    actor.db.commit()
    return task_ref(task)


@router.post("/instances/{identifier}/credentials/rotate", status_code=202)
def rotate(
    identifier: str,
    request: Request,
    body: dict = Body(...),
    actor: ActorContext = Depends(current_actor),
):
    require_legacy(request.app.state.config)
    row = scoped_instance(actor, identifier, "asset.manage")
    stable_request = {
        "route": f"/instances/{identifier}/credentials/rotate",
        "body": body,
        "if_match": request.headers.get("If-Match"),
    }
    request_hash = hmac.new(
        request.app.state.config.fernet_key_path.read_bytes(),
        json.dumps(stable_request, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    old = find_idempotent(
        actor.db,
        "connection_test",
        actor.user_id,
        request.headers.get("Idempotency-Key"),
        {},
        request_hash_override=request_hash,
    )
    if old:
        return task_ref(old)
    expected_revision(row, request.headers.get("If-Match") or body.get("config_revision"))
    secret_ref, _ = secret_for_request(
        actor.db, actor, SecretStore(request.app.state.config), body, row.system_id
    )
    stop_collection(actor.db, row)
    staged_connection = (row.connection.get("_staged") or {}).get("connection") or {
        key: row.connection.get(key) for key in CONNECTION_FIELDS
    }
    connection = dict(row.connection)
    connection["_staged"] = {
        "connection": staged_connection,
        "credential_ref": secret_ref,
        "credential_version": row.credential_version + 1,
        **source_refs(row),
    }
    row.connection, row.lifecycle_state = connection, "RevalidationRequired"
    test_body = {
        **row.connection.get("_collection", {}),
        **source_refs(row),
        "system_id": row.system_id,
        "instance_id": row.id,
        "config_revision": row.config_revision,
        "connection": staged_connection,
        "credential_ref": secret_ref,
    }
    task = create_connection_test(
        actor.db,
        actor,
        request.app.state.config,
        test_body,
        idempotency_key=request.headers.get("Idempotency-Key"),
        request_hash_override=request_hash,
    )
    append_audit(actor.db, actor.user_id, "asset.credentials.rotate.request", "instance", row.id)
    actor.db.commit()
    return task_ref(task)


@router.post("/instances/{identifier}/revalidate")
def revalidate(
    identifier: str,
    request: Request,
    body: dict = Body(...),
    actor: ActorContext = Depends(current_actor),
):
    require_legacy(request.app.state.config)
    row = scoped_instance(actor, identifier, "asset.manage")
    expected_revision(row, request.headers.get("If-Match") or body.get("config_revision"))
    staged = row.connection.get("_staged") or {}
    candidate = {
        **row.connection.get("_collection", {}),
        **source_refs(row),
        "connection": body.get("connection")
        or staged.get("connection")
        or {key: row.connection.get(key) for key in CONNECTION_FIELDS},
    }
    test, safe = checked_test(
        actor.db,
        actor,
        body.get("test_id"),
        row.system_id,
        candidate,
        request.app.state.config,
        instance_id=row.id,
        config_revision=row.config_revision,
    )
    if test.server_uuid != row.expected_server_uuid:
        raise PlatformError(409, "identity_mismatch", "新 UUID 必须登记为新资产，不能拼接旧历史。")
    history = list(row.connection.get("_history", []))[-19:]
    history.append(
        {
            "config_revision": row.config_revision,
            "connection": {key: row.connection.get(key) for key in CONNECTION_FIELDS},
            "credential_ref": row.credential_ref,
            "credential_version": row.credential_version,
            "retired_at": iso(utcnow()),
        }
    )
    row.connection = {
        **safe["connection"],
        **source_refs(row),
        "_collection": row.connection.get("_collection", {}),
        "_metadata": row.connection.get("_metadata", {}),
        "_history": history,
    }
    row.endpoint, row.port, row.connection_mode = (
        safe["connection"]["db_host"],
        safe["connection"]["db_port"],
        safe["connection"]["mode"],
    )
    row.ssh_profile_ref, row.tls_profile_ref = (
        safe["connection"].get("ssh_profile_ref"),
        safe["connection"].get("tls_profile_ref"),
    )
    row.credential_ref, row.credential_version = test.secret_ref, test.secret_version
    row.config_revision += 1
    row.enabled, row.lifecycle_state = False, "Ready"
    test.consumed_at = utcnow()
    append_audit(actor.db, actor.user_id, "asset.revalidate", "instance", row.id)
    actor.db.commit()
    return instance_response(actor.db, actor, row)
