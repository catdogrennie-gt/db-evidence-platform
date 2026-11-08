"""Current-scope unacknowledged events; no emails or external notifications."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from .. import models as m
from ..access import authorize
from ..common import PlatformError, iso, page_query
from ..events import _maintenance, event_instances, event_response
from ..identity import current_actor
from ..scheduling import db_now

router = APIRouter(tags=["event-inbox"])


@router.get("/events/inbox")
def inbox(
    limit: int = 30,
    cursor: str | None = None,
    instance_id: str | None = None,
    department_id: str | None = None,
    system_id: str | None = None,
    environment: str | None = None,
    actor=Depends(current_actor),
):
    ids = authorize(actor, "data.read").instance_ids & authorize(actor, "event.read").instance_ids
    if instance_id:
        authorize(actor, "data.read", [instance_id])
        authorize(actor, "event.read", [instance_id])
        ids = frozenset([instance_id])
    scope = (
        select(m.Instance.id)
        .join(m.System, m.System.id == m.Instance.system_id)
        .where(m.Instance.id.in_(ids))
    )
    if department_id:
        scope = scope.where(m.System.department_id == department_id)
    if system_id:
        scope = scope.where(m.Instance.system_id == system_id)
    if environment:
        scope = scope.where(m.System.environment == environment)
    ids = frozenset(actor.db.scalars(scope))
    now = db_now(actor.db)
    candidates = list(
        actor.db.scalars(
            select(m.Event)
            .where(
                m.Event.instance_id.in_(ids),
                m.Event.status == "open",
                m.Event.merged_into_event_id == None,
            )
            .order_by(m.Event.created_at.desc())
            .limit(5001)
        )
    )
    # Every authoritative linked source is checked before counts or pagination.
    # Refuse an oversized inbox rather than return a fabricated exact total.
    if len(candidates) > 5000:
        raise PlatformError(503, "inbox_too_large", "未确认事件较多，请缩小实例范围后查询。")
    visible = []
    suppressed = []
    for event in candidates:
        if not event_instances(actor.db, event).issubset(ids):
            continue
        instance = actor.db.get(m.Instance, event.instance_id)
        if instance is None:
            continue
        windows = _maintenance(actor.db, instance, now)
        if windows:
            suppressed.append(
                {
                    "event_id": event.id,
                    "title": event.title,
                    "instance_id": event.instance_id,
                    "reason": "active_maintenance",
                    "maintenance_window_ids": windows,
                }
            )
        else:
            visible.append(event.id)
    query = select(m.Event).where(m.Event.id.in_(visible))
    current, total, next_cursor = page_query(actor.db, query, m.Event, limit=limit, cursor=cursor)
    items = []
    for event in current:
        value = event_response(event)
        value["instance_name"] = actor.db.get(m.Instance, event.instance_id).name
        items.append(value)
    return {
        "items": items,
        "total": total,
        "next_cursor": next_cursor,
        "suppressed_count": len(suppressed),
        "suppressed": suppressed[:100],
        "as_of": iso(now),
        "kind": "unacknowledged_events",
        "sends_email": False,
    }
