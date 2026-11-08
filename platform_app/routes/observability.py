"""Scoped current catalog summaries and bounded historical source queries."""

from __future__ import annotations

from datetime import timedelta
import json

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select

from ..access import ActorContext, authorize, current_permissions, scoped_instance
from ..common import (
    PlatformError,
    decode_cursor,
    encode_cursor,
    fields,
    iso,
    page_query,
    time_window,
)
from ..db import utcnow
from ..evidence import evidence_response
from ..identity import current_actor
from ..metrics import query_template
from ..metrics_bridge import source_health
from ..models import (
    CollectionRun,
    CollectionSchedule,
    CollectorCapability,
    Department,
    DigestSnapshot,
    Evidence,
    Event,
    Host,
    Instance,
    LogEvent,
    System,
)

router = APIRouter(tags=["observability"])


def filtered_instances(
    actor,
    *,
    department_id=None,
    system_id=None,
    environment=None,
    instance_id=None,
    allow_control=False,
):
    permissions = current_permissions(actor)
    metadata_only = False
    action = "data.read"
    if action not in permissions and allow_control:
        for candidate in ("asset.manage", "platform.read", "asset.onboard"):
            if candidate in permissions:
                action = candidate
                metadata_only = True
                break
    ids = authorize(actor, action).instance_ids
    query = (
        select(Instance)
        .join(System, Instance.system_id == System.id)
        .where(Instance.id.in_(ids), Instance.lifecycle_state != "Archived")
    )
    if department_id:
        query = query.where(System.department_id == department_id)
    if system_id:
        query = query.where(System.id == system_id)
    if environment:
        query = query.where(System.environment == environment)
    if instance_id:
        authorize(actor, action, [instance_id])
        query = query.where(Instance.id == instance_id)
    return list(actor.db.scalars(query.order_by(Instance.name, Instance.id))), metadata_only


def public_source(state):
    return {
        "collector": state["collector"],
        "up": state["up"],
        "reason_code": state["reason_code"],
        "availability": state["availability"],
        "attempt_in_progress": state["attempt_in_progress"],
        "latest_attempt_status": state["latest_attempt_status"],
        "interval_seconds": state["interval_seconds"],
        "freshness_limit_seconds": state["freshness_limit_seconds"],
        "sample_age_seconds": state["sample_age_seconds"],
        "last_attempt_at": iso(state["last_attempt_at"]),
        "last_success_at": iso(state["last_success_at"]),
    }


def instance_summary(db, instance, *, metadata_only=False):
    health = "unknown"
    last = None
    if not metadata_only:
        state = source_health(db, instance, "identity_status")
        last = state["last_success_at"]
        if state["up"]:
            health = "healthy"
        elif not state["enabled"]:
            health = "unknown"
        elif state["reason_code"] == "latest_attempt_failed":
            health = "unreachable"
        elif state["reason_code"] == "stale":
            health = "stale"
    return {
        "id": instance.id,
        "name": instance.name,
        "system_id": instance.system_id,
        "lifecycle": instance.lifecycle_state.lower(),
        "enabled": instance.enabled,
        "health": health,
        "last_success_at": iso(last),
    }


def overview_response(actor, **filters):
    db = actor.db
    instances, metadata = filtered_instances(actor, allow_control=True, **filters)
    ids = [i.id for i in instances]
    summaries = [instance_summary(db, i, metadata_only=metadata) for i in instances]
    enabled = [i for i in instances if i.enabled and i.lifecycle_state == "Active"]
    counts = {
        "registered": len(instances),
        "enabled": len(enabled),
        "healthy": None,
        "abnormal": None,
        "unknown": None,
        "open_events": None,
        "paused": sum(i.lifecycle_state == "Paused" for i in instances),
        "ready": sum(i.lifecycle_state == "Ready" for i in instances),
        "logical_schema_count": None,
        "host_count": len(
            {
                i.host_id
                for i in instances
                if i.host_id
                and db.get(Host, i.host_id)
                and db.get(Host, i.host_id).verified_identity
            }
        ),
        "unknown_host_relationship_count": sum(i.host_id is None for i in instances),
    }
    events = []
    sources = []
    if not metadata:
        active_ids = {i.id for i in enabled}
        counts["healthy"] = sum(
            r["id"] in active_ids and r["health"] == "healthy" for r in summaries
        )
        counts["abnormal"] = sum(
            r["id"] in active_ids and r["health"] in ("stale", "unreachable", "degraded")
            for r in summaries
        )
        counts["unknown"] = sum(
            r["id"] in active_ids and r["health"] == "unknown" for r in summaries
        )
        logical_schemas = set()
        has_metadata = False
        for instance in instances:
            evidence = db.scalar(
                select(Evidence)
                .where(
                    Evidence.instance_id == instance.id,
                    Evidence.source == "mysql.tables",
                    Evidence.source_mode == "live",
                )
                .order_by(Evidence.observed_at.desc())
                .limit(1)
            )
            if evidence is None or (evidence.coverage or {}).get("availability") not in (
                "available",
                "degraded",
            ):
                continue
            has_metadata = True
            for row in (evidence.payload or {}).get("rows", []):
                schema = row.get("TABLE_SCHEMA")
                if (
                    isinstance(schema, str)
                    and schema not in ("mysql", "sys", "performance_schema", "information_schema")
                    and (not instance.selected_schemas or schema in instance.selected_schemas)
                ):
                    logical_schemas.add((instance.id, schema))
        counts["logical_schema_count"] = len(logical_schemas) if has_metadata else None
        # A data grant alone does not confer event.read.
        if "event.read" in current_permissions(actor):
            from ..events import list_events

            # The primary asset selector only narrows the dashboard. Canonical
            # event authorization still checks every alert/change/manifest source
            # against the actor's complete current grants before count and page.
            scoped_events = list_events(
                actor, limit=20, primary_instance_ids=ids, exclude_statuses=("closed", "merged")
            )
            counts["open_events"] = scoped_events["total"]
            events = [
                {
                    key: event[key]
                    for key in ("id", "instance_id", "title", "severity", "status", "created_at")
                }
                for event in scoped_events["items"]
            ]
        for instance in instances:
            owners = set(
                db.scalars(
                    select(CollectionSchedule.collector).where(
                        CollectionSchedule.instance_id == instance.id
                    )
                )
            )
            sources.extend(
                {"instance_id": instance.id, **public_source(source_health(db, instance, owner))}
                for owner in sorted(owners)
            )
    counts.update(
        registered_count=counts["registered"],
        enabled_count=counts["enabled"],
        healthy_count=counts["healthy"],
    )
    return {
        "counts": counts,
        "metadata_only": metadata,
        "instances": summaries[:50],
        "events": events,
        "source_health": sources,
        "updated_at": iso(utcnow()),
        "coverage": {"complete": False, "gaps": ["unknown_sources_are_not_healthy"]},
    }


@router.get("/overview")
def overview(
    actor: ActorContext = Depends(current_actor),
    department_id: str | None = None,
    system_id: str | None = None,
    environment: str | None = None,
    instance_id: str | None = None,
):
    return overview_response(
        actor,
        department_id=department_id,
        system_id=system_id,
        environment=environment,
        instance_id=instance_id,
    )


@router.get("/topology")
def topology(
    actor: ActorContext = Depends(current_actor),
    department_id: str | None = None,
    system_id: str | None = None,
    environment: str | None = None,
    instance_id: str | None = None,
    from_time: str | None = Query(default=None, alias="from"),
    to_time: str | None = Query(default=None, alias="to"),
):
    start, end = time_window(from_time, to_time)
    db = actor.db
    instances, _ = filtered_instances(
        actor,
        department_id=department_id,
        system_id=system_id,
        environment=environment,
        instance_id=instance_id,
    )
    nodes = []
    edges = []
    departments = {}
    systems = {}
    visible_uuid = {i.expected_server_uuid: i for i in instances if i.expected_server_uuid}
    for instance in instances:
        system = db.get(System, instance.system_id)
        department = db.get(Department, system.department_id)
        if department.id not in departments:
            departments[department.id] = {
                "id": department.id,
                "name": department.name,
                "systems": [],
            }
            nodes.append({"id": department.id, "name": department.name, "type": "department"})
        if system.id not in systems:
            systems[system.id] = {"id": system.id, "name": system.name, "instances": []}
            departments[department.id]["systems"].append(systems[system.id])
            nodes.append({"id": system.id, "name": system.name, "type": "system"})
            edges.append(
                {
                    "source": department.id,
                    "target": system.id,
                    "type": "configured_ownership",
                    "source_mode": "manual",
                }
            )
        summary = instance_summary(db, instance)
        systems[system.id]["instances"].append(summary)
        nodes.append({**summary, "type": "instance"})
        edges.append(
            {
                "source": system.id,
                "target": instance.id,
                "type": "configured_ownership",
                "source_mode": "manual",
            }
        )
        if instance.host_id:
            host = db.get(Host, instance.host_id)
            if host and host.verified_identity:
                if not any(n["id"] == host.id for n in nodes):
                    nodes.append({"id": host.id, "name": host.name, "type": "verified_host"})
                edges.append(
                    {
                        "source": host.id,
                        "target": instance.id,
                        "type": "verified_host_association",
                        "source_mode": "manual",
                    }
                )
        replica = db.scalar(
            select(Evidence)
            .where(
                Evidence.instance_id == instance.id,
                Evidence.source == "mysql.replication",
                Evidence.source_mode == "live",
                Evidence.observed_at >= start,
                Evidence.observed_at < end,
            )
            .order_by(Evidence.observed_at.desc())
            .limit(1)
        )
        for index, row in enumerate((replica.payload.get("rows", []) if replica else [])[:20]):
            upstream = visible_uuid.get(row.get("Source_UUID"))
            target = upstream.id if upstream else f"restricted-upstream-{instance.id}-{index}"
            if upstream is None:
                nodes.append(
                    {"id": target, "name": "未纳管或无权查看的上游", "type": "restricted_upstream"}
                )
            edges.append(
                {
                    "source": instance.id,
                    "target": target,
                    "type": "observed_replication",
                    "source_mode": "live",
                    "observed_at": iso(replica.observed_at),
                    "evidence_id": replica.id,
                    "confidence": "observed_replica_side",
                }
            )
    return {
        "departments": list(departments.values()),
        "nodes": nodes,
        "edges": edges,
        "coverage": {"complete": False, "gaps": ["replica_side_observations_only"]},
    }


@router.get("/instances/{identifier}/overview")
def instance_overview(identifier: str, actor: ActorContext = Depends(current_actor)):
    instance = scoped_instance(actor, identifier)
    db = actor.db
    owners = set(
        db.scalars(
            select(CollectionSchedule.collector).where(CollectionSchedule.instance_id == identifier)
        )
    )
    sources = [public_source(source_health(db, instance, owner)) for owner in sorted(owners)]
    host = db.scalar(
        select(Evidence)
        .where(
            Evidence.instance_id == identifier,
            or_(Evidence.source.like("host.%"), Evidence.source == "linux.host"),
            Evidence.source_mode == "live",
        )
        .order_by(Evidence.observed_at.desc())
        .limit(1)
    )
    gaps = [
        s["collector"] + ":" + s["reason_code"]
        for s in sources
        if not s["up"] and s["reason_code"] != "not_applicable"
    ]
    return {
        **instance_summary(db, instance),
        "source_health": sources,
        "host": evidence_response(actor, host) if host else {"availability": "not_configured"},
        "coverage": {
            "complete": bool(sources) and not gaps,
            "gaps": gaps,
            "available_sources": [s["collector"] for s in sources if s["up"]],
            "not_applicable_sources": [
                s["collector"] for s in sources if s["reason_code"] == "not_applicable"
            ],
        },
    }


@router.get("/instances/{identifier}/metrics")
def metrics(
    identifier: str,
    request: Request,
    template: str = "connections",
    from_time: str | None = Query(default=None, alias="from"),
    to_time: str | None = Query(default=None, alias="to"),
    step: float = 30,
    actor: ActorContext = Depends(current_actor),
):
    start, end = time_window(from_time, to_time)
    return query_template(
        actor, identifier, template, start, end, step=step, config=request.app.state.config
    )


def evidence_query(
    actor,
    start,
    end,
    *,
    instance_id=None,
    source=None,
    source_mode="live",
    department_id=None,
    system_id=None,
    environment=None,
):
    ids = authorize(actor, "data.read").instance_ids
    query = (
        select(Evidence)
        .join(Instance, Evidence.instance_id == Instance.id)
        .join(System, Instance.system_id == System.id)
        .where(
            Evidence.instance_id.in_(ids), Evidence.observed_at >= start, Evidence.observed_at < end
        )
    )
    if instance_id:
        authorize(actor, "data.read", [instance_id])
        query = query.where(Evidence.instance_id == instance_id)
    if source:
        query = query.where(Evidence.source == source)
    if source_mode not in ("live", "replay", "imported"):
        raise PlatformError(422, "invalid_source_mode", "来源模式无效。")
    query = query.where(Evidence.source_mode == source_mode)
    if department_id:
        query = query.where(System.department_id == department_id)
    if system_id:
        query = query.where(System.id == system_id)
    if environment:
        query = query.where(System.environment == environment)
    return query


@router.get("/evidence")
def evidence_history(
    actor: ActorContext = Depends(current_actor),
    from_time: str | None = Query(default=None, alias="from"),
    to_time: str | None = Query(default=None, alias="to"),
    instance_id: str | None = None,
    source: str | None = None,
    source_mode: str = "live",
    department_id: str | None = None,
    system_id: str | None = None,
    environment: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
):
    start, end = time_window(from_time, to_time)
    query = evidence_query(
        actor,
        start,
        end,
        instance_id=instance_id,
        source=source,
        source_mode=source_mode,
        department_id=department_id,
        system_id=system_id,
        environment=environment,
    )
    rows, total, next_cursor = page_query(
        actor.db, query, Evidence, limit=limit, cursor=cursor, time_column="observed_at"
    )
    return {
        "items": [
            {
                **evidence_response(actor, row, include_payload=False),
                "instance_name": actor.db.get(Instance, row.instance_id).name,
            }
            for row in rows
        ],
        "total": total,
        "next_cursor": next_cursor,
        "coverage": {"complete": False, "gaps": ["retention_and_selected_window_apply"]},
    }


@router.get("/evidence/{identifier}")
def evidence_detail(identifier: str, actor: ActorContext = Depends(current_actor)):
    row = actor.db.get(Evidence, identifier)
    if row is None:
        raise PlatformError(404, "not_found", "证据不存在。")
    return evidence_response(actor, row)


def _snapshot(actor, identifier, sources, start, end):
    scoped_instance(actor, identifier)
    rows = []
    snapshots = []
    for source in sources:
        evidence = actor.db.scalar(
            select(Evidence)
            .where(
                Evidence.instance_id == identifier,
                Evidence.source == source,
                Evidence.source_mode == "live",
                Evidence.observed_at >= start,
                Evidence.observed_at < end,
            )
            .order_by(Evidence.observed_at.desc(), Evidence.id.desc())
            .limit(1)
        )
        if evidence is None:
            continue
        response = evidence_response(actor, evidence)
        snapshots.append(response)
        for item in (response.get("payload") or {}).get("rows", []):
            if isinstance(item, dict):
                rows.append(
                    {
                        **{str(k).lower(): v for k, v in item.items()},
                        "evidence_id": evidence.id,
                        "observed_at": iso(evidence.observed_at),
                        "source": source,
                    }
                )
    return rows, snapshots


def _page_snapshot(items, snapshots, limit, cursor, q=""):
    if q:
        items = [
            row for row in items if q.casefold() in json.dumps(row, ensure_ascii=False).casefold()
        ]
    total = len(items)
    offset = 0
    if cursor:
        try:
            offset = int(cursor)
        except (ValueError, TypeError):
            raise PlatformError(422, "invalid_cursor", "分页游标无效。") from None
        if offset < 0 or offset > 100000:
            raise PlatformError(422, "invalid_cursor", "分页游标无效。")
    selected = items[offset : offset + limit]
    availability = "available" if snapshots else "unknown"
    gaps = []
    for snapshot in snapshots:
        coverage = snapshot.get("coverage") or {}
        if coverage.get("availability") not in ("available", "degraded", None):
            availability = coverage.get("availability", "unknown")
        if snapshot.get("truncated"):
            gaps.append("snapshot_truncated")
        if (snapshot.get("payload") or {}).get("redacted"):
            gaps.append("sensitive_permission_required")
    if not snapshots:
        gaps.append("no_observation_in_selected_window")
    return {
        "items": selected,
        "total": total,
        "next_cursor": str(offset + limit) if offset + limit < total else None,
        "observed_at": max((s["observed_at"] for s in snapshots), default=None),
        "capability": {"availability": availability, "reason_code": gaps[0] if gaps else None},
        "coverage": {"complete": bool(snapshots) and not gaps, "gaps": gaps},
        "evidence_refs": [{"id": snapshot["id"]} for snapshot in snapshots],
    }


@router.get("/instances/{identifier}/schemas")
@router.get("/instances/{identifier}/locks")
@router.get("/instances/{identifier}/queries")
@router.get("/instances/{identifier}/replication")
@router.get("/instances/{identifier}/config")
def snapshot_view(
    identifier: str,
    request: Request,
    actor: ActorContext = Depends(current_actor),
    from_time: str | None = Query(default=None, alias="from"),
    to_time: str | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    q: str = Query(default="", max_length=200),
):
    start, end = time_window(from_time, to_time)
    kind = request.url.path.rsplit("/", 1)[-1]
    sources = {
        "schemas": ["mysql.tables", "mysql.indexes"],
        "locks": ["mysql.lock_waits"],
        "queries": ["mysql.statement_digest"],
        "replication": ["mysql.replication"],
        "config": ["mysql.configuration"],
    }[kind]
    items, snapshots = _snapshot(actor, identifier, sources, start, end)
    if kind == "schemas":
        indexes = [item for item in items if item["source"] == "mysql.indexes"]
        tables = [item for item in items if item["source"] == "mysql.tables"]
        for table in tables:
            matching = [
                index
                for index in indexes
                if index.get("table_schema") == table.get("table_schema")
                and index.get("table_name") == table.get("table_name")
            ]
            table["indexes"] = matching[:50]
            table["index_coverage"] = {
                "complete": len(matching) <= 50,
                "count_observed": len(matching),
            }
        items = tables
    if kind == "queries":
        from ..query_windows import apply_query_windows

        query_window = apply_query_windows(actor.db, identifier, start, end, items)
    if kind == "replication":
        permitted = authorize(actor, "data.read").instance_ids
        visible = {
            i.expected_server_uuid: i
            for i in actor.db.scalars(select(Instance).where(Instance.id.in_(permitted)))
            if i.expected_server_uuid
        }
        for item in items:
            upstream = visible.get(item.pop("source_uuid", None))
            item["upstream_instance_id"] = upstream.id if upstream else None
            item["upstream_visibility"] = "authorized" if upstream else "unmanaged_or_restricted"
    result = _page_snapshot(items, snapshots, limit, cursor, q)
    if kind == "schemas":
        result["coverage"].update(row_counts_are_estimates=True)
    if kind == "queries":
        old_coverage = result["coverage"]
        result.update(query_window)
        result["coverage"]["gaps"] = list(
            dict.fromkeys(old_coverage["gaps"] + result["coverage"]["gaps"])
        )
        result["coverage"]["complete"] = old_coverage["complete"] and result["coverage"]["complete"]
    return result


@router.get("/instances/{identifier}/collection-runs")
def collection_runs(
    identifier: str,
    actor: ActorContext = Depends(current_actor),
    from_time: str | None = Query(default=None, alias="from"),
    to_time: str | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
):
    scoped_instance(actor, identifier)
    start, end = time_window(from_time, to_time)
    query = select(CollectionRun).where(
        CollectionRun.instance_id == identifier,
        CollectionRun.started_at >= start,
        CollectionRun.started_at < end,
    )
    rows, total, next_cursor = page_query(
        actor.db, query, CollectionRun, limit=limit, cursor=cursor, time_column="started_at"
    )
    return {
        "items": [
            fields(
                row,
                (
                    "id",
                    "collector",
                    "status",
                    "started_at",
                    "finished_at",
                    "duration_ms",
                    "error_code",
                    "row_count",
                    "truncated",
                ),
            )
            for row in rows
        ],
        "total": total,
        "next_cursor": next_cursor,
    }


@router.get("/instances/{identifier}/logs")
def logs(
    identifier: str,
    actor: ActorContext = Depends(current_actor),
    from_time: str | None = Query(default=None, alias="from"),
    to_time: str | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    q: str = Query(default="", max_length=200),
    severity: str | None = None,
    source: str | None = None,
):
    scoped_instance(actor, identifier)
    start, end = time_window(from_time, to_time)
    sensitive = False
    if "evidence.read_sensitive" in current_permissions(actor):
        sensitive = identifier in authorize(actor, "evidence.read_sensitive").instance_ids
    query = select(LogEvent).where(
        LogEvent.instance_id == identifier, LogEvent.event_time >= start, LogEvent.event_time < end
    )
    if source:
        query = query.where(LogEvent.source == source)
    if severity:
        query = query.where(LogEvent.severity == severity)
    if q:
        condition = or_(
            LogEvent.source.contains(q, autoescape=True), LogEvent.code.contains(q, autoescape=True)
        )
        if sensitive:
            condition = or_(condition, LogEvent.message_redacted.contains(q, autoescape=True))
        query = query.where(condition)
    rows, total, next_cursor = page_query(
        actor.db, query, LogEvent, limit=limit, cursor=cursor, time_column="event_time"
    )
    latest = actor.db.scalar(
        select(Evidence)
        .where(
            Evidence.instance_id == identifier,
            Evidence.source.in_(
                ["mysql.error_log", "mysql.slow_log", "mysql.slow_log_file", "mysql.slow_log_table"]
            ),
            Evidence.source_mode == "live",
            Evidence.observed_at >= start,
            Evidence.observed_at < end,
        )
        .order_by(Evidence.observed_at.desc())
        .limit(1)
    )
    capability = {
        "availability": (latest.coverage or {}).get("availability", "unknown")
        if latest
        else "unknown",
        "reason_code": (latest.coverage or {}).get("error_code")
        if latest
        else "no_observation_in_selected_window",
    }
    gaps = [] if latest else ["no_observation_in_selected_window"]
    gaps += ["text_requires_sensitive_permission"] if not sensitive else []
    return {
        "items": [
            {
                **fields(row, ("id", "instance_id", "evidence_id", "source", "severity", "code")),
                "observed_at": iso(row.event_time),
                "message": row.message_redacted if sensitive else None,
                "redacted": not sensitive,
            }
            for row in rows
        ],
        "total": total,
        "next_cursor": next_cursor,
        "capability": capability,
        "coverage": {"complete": False, "gaps": gaps + ["bounded_source_cursors_apply"]},
    }
