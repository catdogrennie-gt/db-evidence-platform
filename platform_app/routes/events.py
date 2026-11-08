"""Scoped event, alert and human operations HTTP contracts."""

from datetime import timedelta
from typing import Annotated
from fastapi import APIRouter, Depends, Body, HTTPException, Query
from sqlalchemy import select, or_
from .. import models as m
from ..access import authorize, authorize_system, allowed_scope_ids, actor_for_user
from ..audit import append_audit
from ..common import PlatformError, fields, page_query, parse_time, time_window
from ..db import utcnow
from ..identity import current_actor
from ..events import (
    list_events,
    event_detail,
    get_event,
    event_instances,
    event_response,
    event_command,
    merge_events,
    _snapshot,
    _transition,
    selected_instances,
)
from ..remediation import (
    create_plan,
    review_plan,
    record_execution,
    plan_response,
    create_verification,
    verification_response,
)
from ..scheduling import task_ref

router = APIRouter(tags=["events"])


@router.get("/events")
def events(
    limit: int = 50,
    cursor: str | None = None,
    status: str | None = None,
    instance_id: str | None = None,
    department_id: str | None = None,
    system_id: str | None = None,
    environment: str | None = None,
    from_time: Annotated[str | None, Query(alias="from")] = None,
    to_time: Annotated[str | None, Query(alias="to")] = None,
    q: str | None = None,
    actor=Depends(current_actor),
):
    return list_events(
        actor,
        limit=limit,
        cursor=cursor,
        status=status,
        instance_id=instance_id,
        department_id=department_id,
        system_id=system_id,
        environment=environment,
        from_time=from_time,
        to_time=to_time,
        q=q,
    )


@router.post("/events", status_code=201)
def new_event(body: dict = Body(...), actor=Depends(current_actor)):
    identifier = body.get("instance_id")
    authorize(actor, "event.create", [identifier])
    authorize(actor, "data.read", [identifier])
    title = str(body.get("title", "")).strip()
    if not title or len(title) > 255:
        raise PlatformError(422, "invalid_title", "需要1–255字事件标题。")
    event = m.Event(
        instance_id=identifier,
        title=title,
        created_by=actor.user_id,
        source="human_assertion",
        severity=body.get("severity", "warning"),
    )
    actor.db.add(event)
    actor.db.flush()
    _transition(
        actor.db,
        event,
        "manual_create",
        actor.user_id,
        {"reason": str(body.get("reason", ""))[:4000], "source": "human_assertion"},
    )
    _snapshot(actor.db, event)
    append_audit(actor.db, actor.user_id, "event.create", "event", event.id)
    actor.db.commit()
    return event_response(event)


@router.post("/events/merge")
def merge(body: dict = Body(...), actor=Depends(current_actor)):
    result = merge_events(actor, body)
    actor.db.commit()
    return result


@router.get("/events/{event_id}")
def detail(event_id: str, actor=Depends(current_actor)):
    result = event_detail(actor, event_id)
    eligible = []
    try:
        authorize(actor, "event.assign", event_instances(actor.db, get_event(actor, event_id)))
    except HTTPException:
        result["eligible_assignees"] = []
        return result
    ids = event_instances(actor.db, get_event(actor, event_id))
    for user in actor.db.scalars(
        select(m.User).where(m.User.active == True).order_by(m.User.id).limit(500)
    ):
        try:
            authorize(actor_for_user(actor.db, user.id), "event.read", ids)
        except HTTPException:
            continue
        eligible.append({"id": user.id, "display_name": user.display_name or user.username})
    result["eligible_assignees"] = eligible
    return result


@router.post("/events/{event_id}/acknowledge")
def acknowledge(event_id: str, body: dict = Body(...), actor=Depends(current_actor)):
    result = event_command(actor, event_id, "acknowledge", body)
    actor.db.commit()
    return result


@router.post("/events/{event_id}/assign")
def assign(event_id: str, body: dict = Body(...), actor=Depends(current_actor)):
    result = event_command(actor, event_id, "assign", body)
    actor.db.commit()
    return result


@router.post("/events/{event_id}/comments")
def comment(event_id: str, body: dict = Body(...), actor=Depends(current_actor)):
    result = event_command(actor, event_id, "comment", body)
    actor.db.commit()
    return result


@router.post("/events/{event_id}/close")
def close(event_id: str, body: dict = Body(...), actor=Depends(current_actor)):
    result = event_command(actor, event_id, "close", body)
    actor.db.commit()
    return result


@router.post("/events/{event_id}/reopen")
def reopen(event_id: str, body: dict = Body(...), actor=Depends(current_actor)):
    result = event_command(actor, event_id, "reopen", body)
    actor.db.commit()
    return result


@router.get("/alerts")
def alerts(
    limit: int = 50,
    cursor: str | None = None,
    status: str | None = None,
    instance_id: str | None = None,
    department_id: str | None = None,
    system_id: str | None = None,
    environment: str | None = None,
    from_time: Annotated[str | None, Query(alias="from")] = None,
    to_time: Annotated[str | None, Query(alias="to")] = None,
    q: str | None = None,
    actor=Depends(current_actor),
):
    ids = authorize(actor, "event.read").instance_ids & authorize(actor, "data.read").instance_ids
    ids = selected_instances(
        actor, ids, department_id=department_id, system_id=system_id, environment=environment
    )
    query = select(m.AlertOccurrence).where(m.AlertOccurrence.instance_id.in_(ids))
    if status:
        query = query.where(m.AlertOccurrence.status == status)
    if instance_id:
        authorize(actor, "event.read", [instance_id])
        query = query.where(m.AlertOccurrence.instance_id == instance_id)
    if from_time or to_time:
        start, end = time_window(from_time, to_time)
        query = query.where(
            m.AlertOccurrence.first_seen_at >= start, m.AlertOccurrence.first_seen_at < end
        )
    if q:
        escaped = q[:120].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        active = select(m.ActiveAlert.id).where(
            m.ActiveAlert.rule_version.ilike(f"%{escaped}%", escape="\\")
        )
        query = query.where(
            or_(m.AlertOccurrence.id == q, m.AlertOccurrence.active_alert_id.in_(active))
        )
    rows, total, next_cursor = page_query(
        actor.db, query, m.AlertOccurrence, limit=limit, cursor=cursor, time_column="first_seen_at"
    )
    return {
        "items": [
            fields(
                row,
                [
                    "id",
                    "instance_id",
                    "status",
                    "severity",
                    "first_seen_at",
                    "last_seen_at",
                    "recovered_at",
                    "evidence",
                ],
            )
            for row in rows
        ],
        "total": total,
        "next_cursor": next_cursor,
    }


@router.get("/alerts/{alert_id}")
def alert_detail(alert_id: str, actor=Depends(current_actor)):
    row = actor.db.get(m.AlertOccurrence, alert_id)
    if not row:
        raise PlatformError(404, "not_found", "告警不存在。")
    authorize(actor, "event.read", [row.instance_id])
    authorize(actor, "data.read", [row.instance_id])
    events = []
    for identifier in actor.db.scalars(
        select(m.EventAlert.event_id).where(m.EventAlert.alert_occurrence_id == row.id)
    ):
        try:
            get_event(actor, identifier)
            events.append(identifier)
        except HTTPException:
            pass
    return dict(
        fields(
            row,
            [
                "id",
                "instance_id",
                "status",
                "severity",
                "first_seen_at",
                "last_seen_at",
                "recovered_at",
                "evidence",
            ],
        ),
        event_ids=events,
        transitions=[
            fields(item, ["id", "from_state", "to_state", "occurred_at", "details"])
            for item in actor.db.scalars(
                select(m.AlertTransition).where(m.AlertTransition.occurrence_id == row.id)
            )
        ],
    )


@router.get("/events/{event_id}/remediation-plans")
def plans(event_id: str, actor=Depends(current_actor)):
    event = get_event(actor, event_id)
    return {
        "items": [
            plan_response(actor.db, row)
            for row in actor.db.scalars(
                select(m.RemediationPlan).where(m.RemediationPlan.event_id == event.id)
            )
        ]
    }


@router.post("/events/{event_id}/remediation-plans", status_code=201)
def new_plan(event_id: str, body: dict = Body(...), actor=Depends(current_actor)):
    result = create_plan(actor, event_id, body)
    actor.db.commit()
    return result


@router.post("/remediation-plans/{plan_id}/review")
def review(plan_id: str, body: dict = Body(...), actor=Depends(current_actor)):
    result = review_plan(actor, plan_id, body)
    actor.db.commit()
    return result


@router.post("/remediation-plans/{plan_id}/execution-records", status_code=201)
def execution(plan_id: str, body: dict = Body(...), actor=Depends(current_actor)):
    result = record_execution(actor, plan_id, body)
    actor.db.commit()
    return result


@router.post("/events/{event_id}/verifications", status_code=202)
def verify(event_id: str, body: dict = Body(...), actor=Depends(current_actor)):
    task = create_verification(actor, event_id, body)
    actor.db.commit()
    return dict(task_ref(task), verification_id=task.payload["verification_id"])


@router.get("/verifications/{verification_id}")
def verification(verification_id: str, actor=Depends(current_actor)):
    row = actor.db.get(m.Verification, verification_id)
    if not row:
        raise PlatformError(404, "not_found", "复验不存在。")
    get_event(actor, row.event_id)
    if row.manifest_id:
        manifest = actor.db.get(m.EvidenceManifest, row.manifest_id)
        authorize(actor, "data.read", manifest.instance_ids)
    return verification_response(row)


@router.get("/maintenance-windows")
def maintenance(limit: int = 50, cursor: str | None = None, actor=Depends(current_actor)):
    ids = authorize(actor, "data.read").instance_ids
    systems = allowed_scope_ids(actor, "data.read", "system")
    query = select(m.MaintenanceWindow).where(
        or_(
            m.MaintenanceWindow.instance_id.in_(ids),
            (m.MaintenanceWindow.instance_id == None) & m.MaintenanceWindow.system_id.in_(systems),
        )
    )
    rows, total, next_cursor = page_query(
        actor.db, query, m.MaintenanceWindow, limit=limit, cursor=cursor
    )
    return {
        "items": [
            fields(
                row,
                [
                    "id",
                    "system_id",
                    "instance_id",
                    "starts_at",
                    "ends_at",
                    "created_by",
                    "reason",
                    "policy",
                ],
            )
            for row in rows
        ],
        "total": total,
        "next_cursor": next_cursor,
    }


@router.post("/maintenance-windows", status_code=201)
def new_maintenance(body: dict = Body(...), actor=Depends(current_actor)):
    instance_id = body.get("instance_id")
    system_id = body.get("system_id")
    if instance_id:
        authorize(actor, "maintenance.manage", [instance_id])
        authorize(actor, "data.read", [instance_id])
        instance = actor.db.get(m.Instance, instance_id)
        system_id = instance.system_id
    else:
        authorize_system(actor, system_id, "maintenance.manage")
        authorize_system(actor, system_id, "data.read")
    start = parse_time(body.get("starts_at"))
    end = parse_time(body.get("ends_at"))
    reason = str(body.get("reason", "")).strip()
    if start >= end or end - start > timedelta(days=7) or not reason:
        raise PlatformError(422, "invalid_maintenance", "维护窗口须递增、不超过7天并填写原因。")
    policy = body.get("policy") or {"suppress_notifications": True}
    if set(policy) - {"suppress_notifications"}:
        raise PlatformError(422, "invalid_maintenance_policy", "维护窗口只允许抑制提醒。")
    row = m.MaintenanceWindow(
        system_id=system_id,
        instance_id=instance_id,
        starts_at=start,
        ends_at=end,
        created_by=actor.user_id,
        reason=reason[:4000],
        policy=policy,
    )
    actor.db.add(row)
    actor.db.flush()
    append_audit(actor.db, actor.user_id, "maintenance.create", "maintenance_window", row.id)
    actor.db.commit()
    return fields(
        row, ["id", "system_id", "instance_id", "starts_at", "ends_at", "reason", "policy"]
    )


@router.post("/maintenance-windows/{window_id}/cancel")
def cancel_maintenance(window_id: str, body: dict = Body(...), actor=Depends(current_actor)):
    row = actor.db.scalar(
        select(m.MaintenanceWindow).where(m.MaintenanceWindow.id == window_id).with_for_update()
    )
    if not row:
        raise PlatformError(404, "not_found", "维护窗口不存在。")
    if row.instance_id:
        authorize(actor, "maintenance.manage", [row.instance_id])
        authorize(actor, "data.read", [row.instance_id])
    else:
        authorize_system(actor, row.system_id, "maintenance.manage")
        authorize_system(actor, row.system_id, "data.read")
    row.ends_at = min(row.ends_at, utcnow())
    append_audit(actor.db, actor.user_id, "maintenance.cancel", "maintenance_window", row.id)
    actor.db.commit()
    return {"id": row.id, "status": "cancelled"}


@router.get("/manual-changes")
def changes(limit: int = 50, cursor: str | None = None, actor=Depends(current_actor)):
    ids = authorize(actor, "data.read").instance_ids
    rows, total, next_cursor = page_query(
        actor.db,
        select(m.ManualChange).where(m.ManualChange.instance_id.in_(ids)),
        m.ManualChange,
        limit=limit,
        cursor=cursor,
    )
    return {
        "items": [
            fields(
                row,
                [
                    "id",
                    "instance_id",
                    "event_id",
                    "event_at",
                    "source",
                    "description",
                    "details",
                    "created_by",
                ],
            )
            for row in rows
        ],
        "total": total,
        "next_cursor": next_cursor,
    }


@router.post("/manual-changes", status_code=201)
def new_change(body: dict = Body(...), actor=Depends(current_actor)):
    identifier = body.get("instance_id")
    authorize(actor, "change.create", [identifier])
    authorize(actor, "data.read", [identifier])
    event_id = body.get("event_id")
    if event_id:
        event = get_event(actor, event_id)
        if event.instance_id != identifier:
            raise PlatformError(409, "cross_instance_change", "人工变更与事件须属于同一实例。")
    stamp = parse_time(body.get("event_at"))
    description = str(body.get("description", "")).strip()
    if not description or len(description) > 4000:
        raise PlatformError(422, "invalid_change", "需要1–4000字脱敏说明。")
    row = m.ManualChange(
        instance_id=identifier,
        event_id=event_id,
        created_by=actor.user_id,
        event_at=stamp,
        source="human_assertion",
        description=description,
        details=body.get("details") or {},
    )
    actor.db.add(row)
    actor.db.flush()
    append_audit(actor.db, actor.user_id, "change.create", "manual_change", row.id)
    actor.db.commit()
    return fields(
        row, ["id", "instance_id", "event_id", "event_at", "source", "description", "details"]
    )
