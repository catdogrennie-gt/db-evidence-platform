"""Only structured templates are accepted; validation never echoes constants."""

from fastapi import APIRouter, Body, Depends, Request
from ..identity import current_actor
from ..explain import enqueue_explain, explain_options
from ..scheduling import task_ref

router = APIRouter(tags=["explain"])


@router.get("/instances/{identifier}/explain-options")
def options(identifier: str, actor=Depends(current_actor)):
    return explain_options(actor, identifier)


@router.post("/instances/{identifier}/explain", status_code=202)
def create(identifier: str, request: Request, body: dict = Body(...), actor=Depends(current_actor)):
    task = enqueue_explain(
        actor,
        request.app.state.config,
        identifier,
        body,
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    actor.db.commit()
    return task_ref(task)
