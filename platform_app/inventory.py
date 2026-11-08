"""Asset lifecycle and expiring connection preflight bindings."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from .access import authorize_system, current_permissions, scoped_instance
from .audit import append_audit
from .common import PlatformError, fields, iso
from .db import utcnow
from .models import (
    CollectionSchedule,
    CollectorCapability,
    ConnectionDraft,
    ConnectionTest,
    Instance,
    InstanceLease,
    PlatformSetting,
    System,
    Task,
)
from .network_policy import validate_target
from .scheduling import enqueue
from .secrets import SecretStore

COLLECTORS = {
    "identity_status": 30,
    "runtime": 30,
    "statement_digest": 60,
    "schema_config": 300,
    "replication": 60,
    "error_log": 60,
    "slow_log": 60,
    "host": 30,
}
DEFAULT_COLLECTORS = [
    "identity_status",
    "runtime",
    "statement_digest",
    "schema_config",
    "replication",
    "error_log",
]
CONNECTION_FIELDS = ("mode", "db_host", "db_port", "tls_profile_ref", "ssh_profile_ref", "tls_mode")
SOURCE_FIELDS = ("host_profile_ref", "log_profile_ref")


def source_refs(instance):
    current = instance.connection or {}
    staged = current.get("_staged") or {}
    return {
        key: staged.get(key, current.get(key))
        for key in SOURCE_FIELDS
        if staged.get(key, current.get(key)) is not None
    }


def asset_metadata(body, existing=None):
    metadata = dict(existing or {})
    if "description" in body:
        description = body["description"]
        if not isinstance(description, str) or len(description) > 1000:
            raise PlatformError(422, "invalid_description", "用途说明最多 1000 个字符。")
        metadata["description"] = description
    if "tags" in body:
        tags = body["tags"]
        if (
            not isinstance(tags, list)
            or len(tags) > 20
            or any(not isinstance(tag, str) or not 1 <= len(tag) <= 50 for tag in tags)
        ):
            raise PlatformError(422, "invalid_tags", "标签最多 20 项，每项 1–50 个字符。")
        metadata["tags"] = list(dict.fromkeys(tags))
    return metadata


def collection_profiles(db):
    row = db.get(PlatformSetting, "collection_profiles")
    return (row.value or {}).get("items", []) if row else []


def validated_config(body: dict, config) -> dict:
    if body.get("engine", "mysql") != "mysql":
        raise PlatformError(
            422, "unsupported_engine", "本批仅支持 MySQL 8.x；PostgreSQL 适配器尚未启用。"
        )
    connection = body.get("connection") or {}
    # Validation resolves and pins at connection time. Persist the reviewed name,
    # not arbitrary client file paths or a stale DNS answer.
    validate_target(config, connection)
    # Optional references have one canonical representation; an API roundtrip
    # must not change a fingerprint merely by adding explicit null fields.
    clean = {
        "mode": connection.get("mode", "direct"),
        "db_host": connection["db_host"],
        "db_port": int(connection.get("db_port", 3306)),
        "tls_profile_ref": connection.get("tls_profile_ref"),
        "ssh_profile_ref": connection.get("ssh_profile_ref"),
        "tls_mode": connection.get("tls_mode") or "verify_identity",
    }
    requested = body.get("requested_collectors") or DEFAULT_COLLECTORS
    if (
        not isinstance(requested, list)
        or not requested
        or len(requested) > len(COLLECTORS)
        or any(value not in COLLECTORS for value in requested)
    ):
        raise PlatformError(422, "invalid_collectors", "请从平台固定采集器中选择。")
    if "identity_status" not in requested:
        requested = ["identity_status", *requested]
    schemas = body.get("selected_schemas") or []
    if (
        not isinstance(schemas, list)
        or len(schemas) > 20
        or any(
            not isinstance(name, str) or not 1 <= len(name) <= 64 or "\x00" in name
            for name in schemas
        )
    ):
        raise PlatformError(422, "invalid_schemas", "Schema 选择无效，最多 20 项。")
    safe = {
        "engine": "mysql",
        "connection": clean,
        "requested_collectors": list(dict.fromkeys(requested)),
        "selected_schemas": list(dict.fromkeys(schemas)),
        "collection_profile": body.get("collection_profile", "standard"),
    }
    # Optional source profiles are references; raw paths and commands cannot be
    # submitted. The trusted OS configuration defines allowed files and hosts.
    for key in ("host_profile_ref", "log_profile_ref"):
        if body.get(key):
            value = body[key]
            if not isinstance(value, str) or len(value) > 120:
                raise PlatformError(422, "invalid_source_profile", "来源配置引用无效。")
            safe[key] = value
    return safe


def connection_fingerprint(
    system_id: str,
    safe: dict,
    secret_ref: str,
    secret_version=1,
    config_revision=1,
    instance_id=None,
):
    bound = {
        "system_id": system_id,
        "instance_id": instance_id,
        "engine": safe["engine"],
        "connection": safe["connection"],
        "secret_ref": secret_ref,
        "host_profile_ref": safe.get("host_profile_ref"),
        "log_profile_ref": safe.get("log_profile_ref"),
        "secret_version": secret_version,
        "config_revision": config_revision,
    }
    return hashlib.sha256(
        json.dumps(bound, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def secret_for_request(db, actor, store: SecretStore, body, system_id):
    credentials = body.get("credentials")
    if credentials is not None:
        if (
            not isinstance(credentials, dict)
            or not isinstance(credentials.get("username"), str)
            or not 1 <= len(credentials["username"]) <= 128
            or not isinstance(credentials.get("password"), str)
            or len(credentials["password"]) > 4096
        ):
            raise PlatformError(422, "invalid_credentials", "需要监控账号及有效凭据。")
        ref = store.put_secret(
            {"username": credentials["username"], "password": credentials["password"]}
        )
        return ref.ref, ref.version
    ref = body.get("credential_ref")
    if ref:
        registered = db.scalar(select(Instance).where(Instance.credential_ref == ref).limit(1))
        if registered:
            scoped_instance(actor, registered.id, "asset.manage")
            if body.get("instance_id") != registered.id or registered.system_id != system_id:
                raise PlatformError(404, "not_found", "凭据引用不能用于其他资产。")
            return ref, registered.credential_version
        # A secret reference is not a bearer credential. Establish its ownership
        # through an authorized draft/test/asset before reusing it.
        draft = db.scalar(
            select(ConnectionDraft).where(
                ConnectionDraft.created_by == actor.user_id,
                ConnectionDraft.system_id == system_id,
                ConnectionDraft.secret_ref == ref,
                ConnectionDraft.expires_at > utcnow(),
            )
        )
        test = db.scalar(
            select(ConnectionTest).where(
                ConnectionTest.created_by == actor.user_id,
                ConnectionTest.system_id == system_id,
                ConnectionTest.secret_ref == ref,
                ConnectionTest.expires_at > utcnow(),
            )
        )
        if draft or test:
            owner = draft or test
            origin_instance = draft.instance_id if draft else None
            if test and test.task_id:
                origin_task = db.get(Task, test.task_id)
                origin_instance = (
                    (origin_task.payload or {}).get("instance_id") if origin_task else None
                )
            if origin_instance:
                scoped_instance(actor, origin_instance, "asset.manage")
                if body.get("instance_id") != origin_instance:
                    raise PlatformError(404, "not_found", "凭据引用不能用于其他资产。")
            return ref, owner.secret_version
        if body.get("instance_id"):
            instance = scoped_instance(actor, body["instance_id"], "asset.manage")
            staged = (instance.connection or {}).get("_staged", {})
            if instance.system_id == system_id and ref in (
                instance.credential_ref,
                staged.get("credential_ref"),
            ):
                return ref, staged.get(
                    "credential_version", instance.credential_version
                ) if ref != instance.credential_ref else instance.credential_version
    raise PlatformError(
        422, "credentials_required", "请输入监控凭据，或使用本次授权草稿的凭据引用。"
    )


def create_connection_test(
    db, actor, config, body, *, idempotency_key=None, request_hash_override=None
):
    system_id = body.get("system_id")
    safe = validated_config(body, config)
    # A keyed request digest supports safe transport retries BEFORE producing a
    # new credential ref or test UUID; no plain or unkeyed password hash is saved.
    request_hash = (
        request_hash_override
        or hmac.new(
            config.fernet_key_path.read_bytes(),
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
    )
    if idempotency_key:
        key = hashlib.sha256(
            f"{actor.user_id}:connection_test:{idempotency_key}".encode()
        ).hexdigest()
        old_task = db.scalar(select(Task).where(Task.idempotency_key == key))
        if old_task:
            if old_task.request_hash != request_hash:
                raise PlatformError(409, "idempotency_conflict", "同一幂等键不能提交不同内容。")
            return old_task
    instance_id = body.get("instance_id")
    revision = int(body.get("config_revision", 1))
    if instance_id:
        instance = scoped_instance(actor, instance_id, "asset.manage")
        if instance.system_id != system_id or revision != instance.config_revision:
            raise PlatformError(409, "configuration_changed", "实例配置版本或归属不匹配。")
    else:
        authorize_system(actor, system_id, "asset.onboard")
    secret_ref, secret_version = secret_for_request(db, actor, SecretStore(config), body, system_id)
    fingerprint = connection_fingerprint(
        system_id, safe, secret_ref, secret_version, revision, instance_id
    )
    test = ConnectionTest(
        id=str(uuid4()),
        created_by=actor.user_id,
        system_id=system_id,
        config_revision=revision,
        config_fingerprint=fingerprint,
        config=safe,
        secret_ref=secret_ref,
        secret_version=secret_version,
        expires_at=utcnow() + timedelta(minutes=30),
        status="queued",
    )
    db.add(test)
    db.flush()
    task = enqueue(
        db,
        "connection_test",
        actor_id=actor.user_id,
        instance_id=None,
        payload={
            "test_id": test.id,
            "system_id": system_id,
            "instance_id": instance_id,
            "fingerprint": fingerprint,
        },
        timeout_seconds=120,
        idempotency_key=idempotency_key,
        request_hash_override=request_hash,
    )
    # Bind idempotency to configuration including opaque credential version,
    # rather than storing input secrets or trusting a client-supplied test ID.
    test.task_id = task.id
    append_audit(db, actor.user_id, "connection.test", "connection_test", test.id)
    return task


def test_response(test):
    return {
        **fields(
            test, ["id", "system_id", "config_revision", "status", "server_uuid", "expires_at"]
        ),
        "test_id": test.id,
        "server_version": test.source_version,
        "fingerprint": test.config_fingerprint,
        "credential_ref": test.secret_ref,
        "credential_version": test.secret_version,
        "capabilities": (test.results or {}).get("capabilities", []),
        "steps": (test.results or {}).get("steps", []),
        "results": test.results or {},
        "error_code": (test.results or {}).get("error_code"),
        "has_secret": True,
    }


def checked_test(
    db, actor, test_id, system_id, body, config, *, instance_id=None, config_revision=1
):
    test = db.get(ConnectionTest, test_id)
    if not test or test.created_by != actor.user_id or test.system_id != system_id:
        raise PlatformError(404, "not_found", "连接预检记录不存在。")
    if test.status != "succeeded" or test.expires_at <= utcnow() or test.consumed_at:
        raise PlatformError(409, "test_not_valid", "预检未通过、已使用或已过期，请重新预检。")
    safe = validated_config(body, config)
    fingerprint = connection_fingerprint(
        system_id, safe, test.secret_ref, test.secret_version, config_revision, instance_id
    )
    if fingerprint != test.config_fingerprint:
        raise PlatformError(409, "test_configuration_mismatch", "连接配置已改变，请重新预检。")
    return test, safe


def register_instance(db, actor, config, body):
    system_id = body.get("system_id")
    authorize_system(actor, system_id, "asset.onboard")
    name = body.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise PlatformError(422, "invalid_name", "资产名称需要 1–120 个字符。")
    request_hash = hmac.new(
        config.fernet_key_path.read_bytes(),
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    previous = db.get(ConnectionTest, body.get("test_id")) if body.get("test_id") else None
    if (
        previous
        and previous.created_by == actor.user_id
        and previous.system_id == system_id
        and previous.consumed_at
    ):
        receipt = (previous.results or {}).get("registration_receipt") or {}
        if receipt.get("request_hash") == request_hash:
            existing = db.get(Instance, receipt.get("instance_id"))
            if existing:
                return existing
    test, safe = checked_test(db, actor, body.get("test_id"), system_id, body, config)
    gaps = [
        item
        for item in (test.results or {}).get("capabilities", [])
        if item.get("availability") not in ("available", "not_applicable")
    ]
    if gaps and body.get("accept_capability_gaps") is not True:
        raise PlatformError(
            422, "capability_gaps_confirmation_required", "存在采集能力缺口，请明确确认后接入。"
        )
    duplicate = db.scalar(
        select(Instance.id).where(Instance.expected_server_uuid == test.server_uuid)
    )
    if duplicate:
        # Do not reveal another department's existing asset ID.
        raise PlatformError(
            409, "duplicate_database_identity", "该数据库身份已登记，请核实已有资产。"
        )
    profiles = collection_profiles(db)
    profile = next(
        (
            item
            for item in profiles
            if safe["collection_profile"] in (item.get("id"), item.get("name"))
        ),
        None,
    )
    if not profile:
        raise PlatformError(422, "collection_profile_not_found", "采集计划不存在，请刷新后选择。")
    connection = dict(safe["connection"])
    connection["_metadata"] = asset_metadata(body)
    connection["_collection"] = {
        key: safe[key] for key in ("requested_collectors", "selected_schemas", "collection_profile")
    }
    for key in ("host_profile_ref", "log_profile_ref"):
        if key in safe:
            connection[key] = safe[key]
    instance = Instance(
        id=str(uuid4()),
        system_id=system_id,
        name=name.strip(),
        engine="mysql",
        endpoint=connection["db_host"],
        port=connection["db_port"],
        connection_mode=connection["mode"],
        expected_server_uuid=test.server_uuid,
        credential_ref=test.secret_ref,
        credential_version=test.secret_version,
        ssh_profile_ref=connection.get("ssh_profile_ref"),
        tls_profile_ref=connection.get("tls_profile_ref"),
        connection=connection,
        selected_schemas=safe["selected_schemas"],
        lifecycle_state="Ready",
        enabled=False,
        model_policy="rules_only",
        health="unknown",
    )
    db.add(instance)
    db.flush()
    db.add(InstanceLease(instance_id=instance.id, fence=0))
    for name in safe["requested_collectors"]:
        interval = (profile.get("intervals") or {}).get(name, COLLECTORS[name])
        db.add(
            CollectionSchedule(
                instance_id=instance.id,
                collector=name,
                interval_seconds=interval,
                config_revision=1,
                enabled=False,
            )
        )
    for capability in (test.results or {}).get("capabilities", []):
        db.add(
            CollectorCapability(
                instance_id=instance.id,
                collector=capability["collector"],
                availability=capability.get("availability", "unknown"),
                reason_code=capability.get("reason_code") or capability.get("error_code"),
                details={"preflight_only": True},
            )
        )
    test.consumed_at = utcnow()
    test.results = {
        **(test.results or {}),
        "registration_receipt": {"instance_id": instance.id, "request_hash": request_hash},
    }
    for draft in db.scalars(
        select(ConnectionDraft).where(
            ConnectionDraft.created_by == actor.user_id,
            ConnectionDraft.system_id == system_id,
            ConnectionDraft.secret_ref == test.secret_ref,
            ConnectionDraft.instance_id.is_(None),
            ConnectionDraft.expires_at > utcnow(),
        )
    ):
        draft.expires_at = utcnow()
    append_audit(db, actor.user_id, "asset.register", "instance", instance.id)
    return instance


def instance_response(db, actor, instance):
    system = db.get(System, instance.system_id)
    result = fields(
        instance,
        [
            "id",
            "system_id",
            "name",
            "engine",
            "config_revision",
            "lifecycle_state",
            "enabled",
            "health",
            "last_success_at",
            "last_attempt_at",
            "created_at",
            "updated_at",
            "model_policy",
            "policy_version",
        ],
    )
    lifecycle_alias = {
        "Ready": "ready",
        "Active": "active",
        "Paused": "paused",
        "Archived": "archived",
        "Activating": "activating",
        "RevalidationRequired": "revalidation_required",
        "Quarantined": "quarantined",
    }
    result.update(
        {
            "lifecycle": lifecycle_alias.get(
                instance.lifecycle_state, instance.lifecycle_state.lower()
            ),
            "environment": system.environment,
            "system_name": system.name,
            "department_id": system.department_id,
            "server_uuid": instance.expected_server_uuid,
            "expected_server_uuid": instance.expected_server_uuid,
            "has_secret": bool(instance.credential_ref),
            "credential_version": instance.credential_version,
            "connection_mode": instance.connection_mode,
        }
    )
    result.update(asset_metadata((instance.connection or {}).get("_metadata") or {}))
    test = db.scalar(
        select(ConnectionTest)
        .where(
            ConnectionTest.server_uuid == instance.expected_server_uuid,
            ConnectionTest.status == "succeeded",
        )
        .order_by(ConnectionTest.created_at.desc())
        .limit(1)
    )
    result["server_version"] = test.source_version if test else None
    if "asset.manage" in current_permissions(actor):
        # Presence of permission alone is insufficient: its scope must cover this
        # asset. Don't reveal an endpoint due to a management grant elsewhere.
        from .access import authorize

        try:
            authorize(actor, "asset.manage", [instance.id])
            result["connection"] = {key: instance.connection.get(key) for key in CONNECTION_FIELDS}
            result["endpoint"], result["port"] = instance.endpoint, instance.port
        except Exception as exc:
            from fastapi import HTTPException

            if not isinstance(exc, HTTPException):
                raise
    result["capabilities"] = [
        fields(row, ["collector", "availability", "reason_code", "checked_at"])
        for row in db.scalars(
            select(CollectorCapability).where(CollectorCapability.instance_id == instance.id)
        )
    ]
    result["selected_schemas"] = instance.selected_schemas
    from .access import authorize

    try:
        authorize(actor, "data.read", [instance.id])
        result["data_access"] = True
    except Exception as exc:
        from fastapi import HTTPException

        if not isinstance(exc, HTTPException):
            raise
        result.update(
            data_access=False,
            health=None,
            last_success_at=None,
            last_attempt_at=None,
            capabilities=[],
            selected_schemas=[],
            server_version=None,
        )
    return result


def expected_revision(instance, supplied):
    try:
        revision = int(str(supplied).strip('"'))
    except (ValueError, TypeError):
        raise PlatformError(
            409, "configuration_revision_required", "操作需要当前配置版本。"
        ) from None
    if revision != instance.config_revision:
        raise PlatformError(409, "configuration_changed", "资产已被其他操作更新，请刷新。")


def stop_collection(db, instance, *, archive=False):
    instance.enabled = False
    instance.lifecycle_state = "Archived" if archive else "Paused"
    instance.config_revision += 1
    schedules = db.scalars(
        select(CollectionSchedule).where(CollectionSchedule.instance_id == instance.id)
    )
    for schedule in schedules:
        schedule.enabled = False
        schedule.config_revision = instance.config_revision
    count = 0
    for task in db.scalars(
        select(Task).where(
            Task.instance_id == instance.id, Task.status.in_(["queued", "retry_wait", "running"])
        )
    ):
        task.cancel_requested = True
        if task.status != "running":
            task.status, task.finished_at = "cancelled", utcnow()
        count += 1
    return count
