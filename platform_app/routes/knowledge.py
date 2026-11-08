"""Canonical scoped knowledge authoring and manual review routes."""

from fastapi import APIRouter, Depends, Body
from ..identity import current_actor
from ..knowledge import (
    create_entry,
    get_entry,
    list_entries,
    add_version,
    submit_review,
    review_entry,
    publish_entry,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get("")
def entries(
    limit: int = 50,
    cursor: str | None = None,
    q: str | None = None,
    engine: str | None = None,
    tag: str | None = None,
    status: str | None = None,
    actor=Depends(current_actor),
):
    return list_entries(
        actor, limit=limit, cursor=cursor, q=q, engine=engine, tag=tag, status=status
    )


@router.post("", status_code=201)
def create(body: dict = Body(...), actor=Depends(current_actor)):
    result = create_entry(actor, body)
    actor.db.commit()
    return result


@router.get("/{entry_id}")
def detail(entry_id: str, actor=Depends(current_actor)):
    return get_entry(actor, entry_id)


@router.post("/{entry_id}/versions", status_code=201)
def version(entry_id: str, body: dict = Body(...), actor=Depends(current_actor)):
    result = add_version(actor, entry_id, body)
    actor.db.commit()
    return result


@router.post("/{entry_id}/submit-review")
def submit(entry_id: str, body: dict = Body(...), actor=Depends(current_actor)):
    result = submit_review(actor, entry_id, body)
    actor.db.commit()
    return result


@router.post("/{entry_id}/reviews")
def review(entry_id: str, body: dict = Body(...), actor=Depends(current_actor)):
    result = review_entry(actor, entry_id, body)
    actor.db.commit()
    return result


@router.post("/{entry_id}/publish")
def publish(entry_id: str, body: dict = Body(...), actor=Depends(current_actor)):
    result = publish_entry(actor, entry_id, body)
    actor.db.commit()
    return result
