"""Structured, bounded EXPLAIN; accepts metadata references, never SQL text."""

from __future__ import annotations
from datetime import timedelta
import hashlib
import hmac
import json
import math
import time
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator
from sqlalchemy import select

from . import models as m
from .access import actor_for_user, authorize, scoped_instance
from .audit import append_audit
from .collection_service import _target, _ssh
from .common import PlatformError, iso, json_safe
from .connectors import ConnectorError, open_mysql
from .db import utcnow
from .evidence import digest
from .scheduling import check_fence, complete, db_now, enqueue, find_idempotent
from .secrets import SecretStore

MAX_METADATA_AGE = timedelta(minutes=45)
SAFE_TYPES = frozenset(
    {
        "tinyint",
        "smallint",
        "mediumint",
        "int",
        "integer",
        "bigint",
        "decimal",
        "numeric",
        "float",
        "double",
        "char",
        "varchar",
        "date",
        "datetime",
        "timestamp",
        "time",
        "year",
        "bit",
    }
)
SYSTEM_SCHEMAS = frozenset({"mysql", "sys", "information_schema", "performance_schema"})


class Predicate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    column: str = Field(min_length=1, max_length=64)
    value: object

    @field_validator("value")
    @classmethod
    def bounded_constant(cls, value):
        if type(value) not in (str, int, float, bool):
            raise ValueError("A bounded scalar is required")
        if isinstance(value, str) and (len(value) > 256 or "\0" in value):
            raise ValueError("Invalid scalar")
        if type(value) is int and abs(value) > 2**63 - 1:
            raise ValueError("Invalid scalar")
        if type(value) is float and (not math.isfinite(value) or abs(value) > 1e18):
            raise ValueError("Invalid scalar")
        return value


class Ordering(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    column: str = Field(min_length=1, max_length=64)
    direction: Literal["asc", "desc"] = "asc"


class ExplainInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    template: Literal["single_table_lookup"]
    metadata_evidence_id: str
    config_revision: StrictInt = Field(ge=1)
    schema_name: str = Field(alias="schema", min_length=1, max_length=64)
    table: str = Field(min_length=1, max_length=64)
    projection: list[str] = Field(min_length=1, max_length=8)
    predicates: list[Predicate] = Field(min_length=1, max_length=3)
    order_by: Ordering | None = None
    limit: StrictInt = Field(default=20, ge=1, le=100)


def _parse(body):
    try:
        request = ExplainInput.model_validate(body)
        UUID(request.metadata_evidence_id)
        if len(set(request.projection)) != len(request.projection):
            raise ValueError()
        if any(not 1 <= len(column) <= 64 or "\0" in column for column in request.projection):
            raise ValueError()
        return request
    except (ValidationError, ValueError, TypeError):
        # Never serialize Pydantic input values, SQL fragments or constants.
        raise PlatformError(
            422,
            "unsupported_shape",
            "仅支持已采单表元数据、列投影、1–3个等值绑定条件和受限排序/LIMIT；不接受 SQL。",
        ) from None


def require_explain(actor, identifier):
    instance = scoped_instance(actor, identifier, "data.read")
    authorize(actor, "collection.trigger", [identifier])
    authorize(actor, "evidence.read_sensitive", [identifier])
    if not instance.enabled or instance.lifecycle_state != "Active":
        raise PlatformError(409, "instance_not_active", "实例须处于启用状态才能获取计划。")
    return instance


def _metadata(db, instance, identifier, now=None):
    now = now or utcnow()
    row = db.get(m.Evidence, identifier)
    if row is None or row.instance_id != instance.id:
        raise PlatformError(404, "not_found", "元数据引用不可访问。")
    run = db.get(m.CollectionRun, row.run_id)
    if (
        row.source != "mysql.columns"
        or row.source_mode != "live"
        or row.server_uuid != instance.expected_server_uuid
        or not run
        or run.collector != "schema_config"
        or run.config_revision != instance.config_revision
        or row.observed_at < now - MAX_METADATA_AGE
        or row.observed_at > now + timedelta(minutes=1)
        or (row.expires_at and row.expires_at <= now)
        or row.coverage.get("availability") != "available"
    ):
        raise PlatformError(
            409, "metadata_not_current", "需要当前配置下的新鲜真实列元数据，请补采库表结构。"
        )
    tables = db.scalar(
        select(m.Evidence).where(
            m.Evidence.run_id == row.run_id,
            m.Evidence.instance_id == instance.id,
            m.Evidence.source == "mysql.tables",
        )
    )
    if not tables or tables.coverage.get("availability") != "available":
        raise PlatformError(409, "table_metadata_unavailable", "同一采集运行的表类型资料不可用。")
    base = {
        (r.get("TABLE_SCHEMA"), r.get("TABLE_NAME"))
        for r in (tables.payload or {}).get("rows", [])
        if isinstance(r, dict) and r.get("TABLE_TYPE") == "BASE TABLE"
    }
    result = {}
    for column in (row.payload or {}).get("rows", [])[:2000]:
        if not isinstance(column, dict):
            continue
        schema, table, name = (
            column.get("TABLE_SCHEMA"),
            column.get("TABLE_NAME"),
            column.get("COLUMN_NAME"),
        )
        if (
            (schema, table) not in base
            or schema not in instance.selected_schemas
            or schema in SYSTEM_SCHEMAS
        ):
            continue
        if not all(
            isinstance(v, str) and 1 <= len(v) <= 64 and "\0" not in v
            for v in (schema, table, name)
        ):
            continue
        result.setdefault((schema, table), {})[name] = str(column.get("DATA_TYPE", "")).lower()
    return row, result


def _checked(actor, identifier, request):
    instance = require_explain(actor, identifier)
    if request.config_revision != instance.config_revision:
        raise PlatformError(409, "configuration_changed", "连接配置已变化，请重新选择结构资料。")
    metadata, tables = _metadata(actor.db, instance, request.metadata_evidence_id, db_now(actor.db))
    columns = tables.get((request.schema_name, request.table))
    if not columns or any(column not in columns for column in request.projection):
        raise PlatformError(422, "unsupported_shape", "表和列必须来自同一份已采基本表元数据。")
    if any(
        p.column not in columns or columns[p.column] not in SAFE_TYPES for p in request.predicates
    ):
        raise PlatformError(422, "unsupported_shape", "等值条件需要已采且支持的标量列。")
    if request.order_by and request.order_by.column not in columns:
        raise PlatformError(422, "unsupported_shape", "排序列必须来自选定基本表。")
    return instance, metadata, columns


def explain_options(actor, identifier):
    instance = require_explain(actor, identifier)
    candidates = list(
        actor.db.scalars(
            select(m.Evidence)
            .where(m.Evidence.instance_id == identifier, m.Evidence.source == "mysql.columns")
            .order_by(m.Evidence.observed_at.desc())
            .limit(5)
        )
    )
    for candidate in candidates:
        try:
            metadata, tables = _metadata(actor.db, instance, candidate.id, db_now(actor.db))
        except PlatformError:
            continue
        items = [
            {
                "schema": schema,
                "table": table,
                "columns": [
                    {"name": name, "data_type": kind, "predicate_allowed": kind in SAFE_TYPES}
                    for name, kind in columns.items()
                ],
            }
            for (schema, table), columns in sorted(tables.items())
        ]
        return {
            "items": items,
            "total": len(items),
            "next_cursor": None,
            "metadata_evidence_id": metadata.id,
            "config_revision": instance.config_revision,
            "observed_at": iso(metadata.observed_at),
            "coverage": metadata.coverage,
            "template": "single_table_lookup",
        }
    raise PlatformError(409, "metadata_not_current", "暂无可用的新鲜列元数据，请先补采库表结构。")


def enqueue_explain(actor, config, identifier, body, *, idempotency_key=None):
    request = _parse(body)
    instance, metadata, columns = _checked(actor, identifier, request)
    store = SecretStore(config)
    try:
        key = config.fernet_key_path.read_bytes()
    except OSError:
        raise PlatformError(503, "secret_unavailable", "参数加密主密钥不可用。") from None
    fingerprint = hmac.new(
        key,
        json.dumps(
            {"instance_id": identifier, "body": request.model_dump(by_alias=True)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    prior = find_idempotent(
        actor.db,
        "explain",
        actor.user_id,
        idempotency_key,
        {},
        instance_id=identifier,
        request_hash_override=fingerprint,
    )
    if prior:
        return prior
    shape = request.model_dump(by_alias=True)
    values = [p.value for p in request.predicates]
    shape["predicates"] = [{"column": p.column} for p in request.predicates]
    secret = store.put_secret(
        {
            "values": values,
            "actor_id": actor.user_id,
            "instance_id": identifier,
            "metadata_evidence_id": metadata.id,
            "config_revision": instance.config_revision,
            "request_hash": fingerprint,
        },
        purpose="explain_parameter",
    )
    try:
        task = enqueue(
            actor.db,
            "explain",
            actor_id=actor.user_id,
            instance_id=identifier,
            queue="collection",
            timeout_seconds=45,
            max_attempts=1,
            payload={
                "shape": shape,
                "parameter_ref": secret.ref,
                "config_revision": instance.config_revision,
            },
            idempotency_key=idempotency_key,
            request_hash_override=fingerprint,
        )
        append_audit(
            actor.db,
            actor.user_id,
            "explain.request",
            "task",
            task.id,
            details={"metadata_evidence_id": metadata.id, "template": "single_table_lookup"},
        )
        return task
    except Exception:
        store._path(secret.ref).unlink(missing_ok=True)
        raise


def _identifier(value):
    return "`" + value.replace("`", "``") + "`"


def _sql(request):
    sql = "EXPLAIN FORMAT=JSON SELECT " + ", ".join(_identifier(c) for c in request.projection)
    sql += " FROM " + _identifier(request.schema_name) + "." + _identifier(request.table)
    sql += " WHERE " + " AND ".join(_identifier(p.column) + " = %s" for p in request.predicates)
    if request.order_by:
        sql += (
            " ORDER BY "
            + _identifier(request.order_by.column)
            + " "
            + request.order_by.direction.upper()
        )
    return sql + " LIMIT " + str(request.limit)


def _project_plan(value, columns, table, index_names=frozenset()):
    # Plans may embed bound literals in attached_condition/ref/message. Persist
    # numeric/access information only; never save or forward the raw JSON tree.
    def number(v):
        try:
            r = float(v)
            return r if math.isfinite(r) and abs(r) < 1e100 else None
        except (ValueError, TypeError):
            return None

    def walk(node, depth=0):
        if depth > 8 or not isinstance(node, dict):
            return {}
        output = {}
        for key, v in node.items():
            if key in (
                "select_id",
                "rows_examined_per_scan",
                "rows_produced_per_join",
                "filtered",
                "query_cost",
                "read_cost",
                "eval_cost",
                "prefix_cost",
                "sort_cost",
            ):
                numeric = number(v)
                if numeric is not None:
                    output[key] = numeric
            elif key == "table_name" and v == table:
                output[key] = table
            elif key == "key" and isinstance(v, str) and v in index_names:
                output[key] = v
            elif key == "possible_keys" and isinstance(v, list):
                output[key] = [
                    name for name in v[:64] if isinstance(name, str) and name in index_names
                ]
            elif key == "access_type" and v in (
                "system",
                "const",
                "eq_ref",
                "ref",
                "ref_or_null",
                "index_merge",
                "range",
                "index",
                "ALL",
            ):
                output[key] = v
            elif (
                key in ("using_filesort", "using_temporary_table", "using_index")
                and type(v) is bool
            ):
                output[key] = v
            elif key in ("used_columns", "used_key_parts") and isinstance(v, list):
                output[key] = [x for x in v[:64] if x in columns]
            elif key in (
                "query_block",
                "table",
                "cost_info",
                "ordering_operation",
                "grouping_operation",
                "duplicates_removal",
            ) and isinstance(v, dict):
                output[key] = walk(v, depth + 1)
        return output

    return walk(value)


def handle_explain(session_factory, lease, config):
    revision = lease.payload.get("config_revision")
    with session_factory.begin() as db:
        task = check_fence(db, lease, config_revision=revision)
        actor = actor_for_user(db, lease.actor_id)
        secret = (
            SecretStore(config)
            .resolve(lease.payload.get("parameter_ref"), purpose="explain_parameter")
            .value
        )
        if not isinstance(secret, dict) or any(
            secret.get(k) != v
            for k, v in {
                "actor_id": lease.actor_id,
                "instance_id": lease.instance_id,
                "config_revision": revision,
                "request_hash": task.request_hash,
            }.items()
        ):
            raise PlatformError(409, "parameter_binding_invalid", "加密参数与任务身份不匹配。")
        shape = dict(lease.payload.get("shape") or {})
        predicates = shape.get("predicates") or []
        values = secret.get("values")
        if (
            not isinstance(values, list)
            or len(values) != len(predicates)
            or secret.get("metadata_evidence_id") != shape.get("metadata_evidence_id")
        ):
            raise PlatformError(409, "parameter_binding_invalid", "加密参数与模板不匹配。")
        shape["predicates"] = [{**p, "value": v} for p, v in zip(predicates, values)]
        request = _parse(shape)
        instance, metadata, columns = _checked(actor, lease.instance_id, request)
        index_evidence = db.scalar(
            select(m.Evidence).where(
                m.Evidence.run_id == metadata.run_id,
                m.Evidence.instance_id == instance.id,
                m.Evidence.source == "mysql.indexes",
            )
        )
        index_names = frozenset(
            row.get("INDEX_NAME")
            for row in (index_evidence.payload if index_evidence else {}).get("rows", [])
            if isinstance(row, dict)
            and row.get("TABLE_SCHEMA") == request.schema_name
            and row.get("TABLE_NAME") == request.table
            and isinstance(row.get("INDEX_NAME"), str)
        )
        connection = dict(instance.connection)
        credential_ref = instance.credential_ref
        expected = instance.expected_server_uuid
        run_started_at = db_now(db)
        run_started_mono = time.monotonic()
        connect_budget = min(30, max(0, (lease.deadline_at - db_now(db)).total_seconds()))
    deadline = time.monotonic() + connect_budget
    try:
        with open_mysql(
            _target(config, connection, expected),
            SecretStore(config).resolve(credential_ref),
            deadline,
            ssh_profile_resolver=lambda ref: _ssh(config, ref),
        ) as database:
            sql_started_mono = time.monotonic()
            query_deadline = sql_started_mono + 2
            handler_open = False

            def execute(cursor, sql, args=None):
                remaining = query_deadline - time.monotonic()
                if remaining <= 0:
                    raise ConnectorError("timeout")
                database._read_timeout = min(2, remaining)
                database._write_timeout = min(2, remaining)
                cursor.execute(sql, args)

            try:
                with database.cursor() as cursor:
                    execute(cursor, "SET SESSION MAX_EXECUTION_TIME=2000")
                    execute(cursor, "SET SESSION lock_wait_timeout=2")
                    execute(cursor, "START TRANSACTION READ ONLY")
                    # MySQL 8.4 sql/sql_handler.cc: required BASE_TABLE, SELECT_ACL,
                    # MDL_EXPLICIT until CLOSE. OPEN does not issue HANDLER READ.
                    # https://github.com/mysql/mysql-server/blob/8.4/sql/sql_handler.cc
                    execute(
                        cursor,
                        "HANDLER "
                        + _identifier(request.schema_name)
                        + "."
                        + _identifier(request.table)
                        + " OPEN AS `dba_explain_guard`",
                    )
                    handler_open = True
                    execute(
                        cursor,
                        "SELECT TABLE_TYPE FROM information_schema.tables WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s LIMIT 1",
                        [request.schema_name, request.table],
                    )
                    actual = cursor.fetchone()
                    if not actual or actual.get("TABLE_TYPE") != "BASE TABLE":
                        raise PlatformError(
                            409, "metadata_changed", "目标已不再是所选基本表，请重新采集元数据。"
                        )
                    execute(
                        cursor,
                        "SELECT COLUMN_NAME,DATA_TYPE FROM information_schema.columns WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION LIMIT 2001",
                        [request.schema_name, request.table],
                    )
                    actual_columns = {
                        r["COLUMN_NAME"]: str(r["DATA_TYPE"]).lower()
                        for r in cursor.fetchall()[:2001]
                    }
                    used = (
                        set(request.projection)
                        | {p.column for p in request.predicates}
                        | ({request.order_by.column} if request.order_by else set())
                    )
                    if any(
                        c not in actual_columns or actual_columns[c] != columns[c] for c in used
                    ):
                        raise PlatformError(
                            409, "metadata_changed", "目标列定义已变化，请重新采集元数据。"
                        )
                    with session_factory.begin() as db:
                        check_fence(db, lease, config_revision=revision)
                        _checked(actor_for_user(db, lease.actor_id), lease.instance_id, request)
                    execute(cursor, _sql(request), values)
                    row = cursor.fetchone()
                    if time.monotonic() > query_deadline:
                        raise ConnectorError("timeout")
                    raw = (
                        next(iter(row.values()))
                        if isinstance(row, dict) and len(row) == 1
                        else None
                    )
                    if isinstance(raw, str):
                        if len(raw.encode("utf-8")) > 256 * 1024:
                            raise ConnectorError("plan_too_large")
                        raw = json.loads(raw)
                    if not isinstance(raw, dict):
                        raise ConnectorError("plan_unavailable")
                    plan = _project_plan(raw, columns, request.table, index_names)
                    sql_duration_ms = (time.monotonic() - sql_started_mono) * 1000
            finally:
                try:
                    if handler_open:
                        try:
                            with database.cursor() as cleanup:
                                cleanup.execute("HANDLER `dba_explain_guard` CLOSE")
                        except Exception:
                            pass  # The private connection closes immediately below.
                finally:
                    database.rollback()
    except (ValueError, TypeError, KeyError):
        raise ConnectorError("plan_unavailable") from None
    with session_factory.begin() as db:
        check_fence(db, lease, config_revision=revision)
        actor = actor_for_user(db, lease.actor_id)
        instance, metadata, columns = _checked(actor, lease.instance_id, request)
        now = db_now(db)
        run = m.CollectionRun(
            task_id=lease.id,
            instance_id=instance.id,
            collector="explain",
            attempt=lease.attempt,
            config_revision=revision,
            fence=lease.fence,
            started_at=run_started_at,
            finished_at=now,
            duration_ms=(time.monotonic() - run_started_mono) * 1000,
            status="succeeded",
            row_count=1,
        )
        db.add(run)
        db.flush()
        payload = {
            "template": "single_table_lookup",
            "metadata_evidence_id": metadata.id,
            "schema": request.schema_name,
            "table": request.table,
            "projection": request.projection,
            "predicate_columns": [p.column for p in request.predicates],
            "limit": request.limit,
            "plan": plan,
            "omitted_fields": [
                "bound_constants",
                "sql_text",
                "attached_condition",
                "ref",
                "message",
            ],
            "execution": "EXPLAIN only; business query was not executed",
        }
        evidence = m.Evidence(
            instance_id=instance.id,
            run_id=run.id,
            source="mysql.explain_plan",
            source_mode="live",
            observed_at=now,
            server_uuid=expected,
            collector_version="explain-1",
            sensitivity="restricted",
            coverage={
                "availability": "available",
                "complete": True,
                "gaps": [],
                "sql_budget_seconds": 2,
                "sql_duration_ms": sql_duration_ms,
                "collector": "explain",
                "error_code": None,
                "metadata_guard": "handler_open_explicit_mdl",
            },
            payload=payload,
            payload_sha256=digest(payload),
            expires_at=now + timedelta(days=14),
        )
        db.add(evidence)
        db.flush()
        complete(
            db,
            lease,
            result={"evidence_id": evidence.id},
            result_ref=f"/api/v1/evidence/{evidence.id}",
            config_revision=revision,
        )
        append_audit(
            db,
            actor.user_id,
            "explain.complete",
            "evidence",
            evidence.id,
            details={"task_id": lease.id, "template": "single_table_lookup"},
        )
