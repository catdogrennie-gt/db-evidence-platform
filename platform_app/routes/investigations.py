"""Investigation API: one immutable report, separately appended human reviews."""

from __future__ import annotations
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exists, select

from ..access import ActorContext, authorize
from ..common import PlatformError, page_query, time_window
from ..identity import current_actor
from ..investigations import (
    create_investigation,
    get_investigation,
    investigation_detail,
    preview_investigation,
    review_investigation,
    get_frozen_metric,
)
from ..models import Evidence, Investigation, InvestigationResource
from ..scheduling import task_ref

router = APIRouter(prefix="/investigations", tags=["investigations"])


class InvestigationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    instance_ids: list[str] = Field(min_length=1, max_length=5)
    from_time: str = Field(alias="from")
    to_time: str = Field(alias="to")
    question: str = Field(min_length=1, max_length=2000)
    mode: str = "rules_only"
    event_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    model_profile_id: str | None = None
    max_tool_calls: int = Field(default=6, ge=0, le=6)
    max_duration_seconds: int = Field(default=180, ge=10, le=180)
    max_cost_usd: float | None = Field(default=None, gt=0, le=5)
    external_confirmed: bool = False


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: str
    reason: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)


@router.post("/preview")
def preview(body: InvestigationInput, actor: ActorContext = Depends(current_actor)):
    start, end = time_window(body.from_time, body.to_time)
    return preview_investigation(actor, body.instance_ids, start, end, body.question, body.mode)


@router.post("", status_code=202)
def create(
    body: InvestigationInput,
    request: Request,
    actor: ActorContext = Depends(current_actor),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    start, end = time_window(body.from_time, body.to_time)
    if body.model_profile_id not in (None, "default", request.app.state.config.model.get("id")):
        raise PlatformError(404, "model_profile_not_found", "模型配置不存在。")
    if body.mode == "live_redacted" and body.max_cost_usd is not None:
        pricing = request.app.state.config.model.get("pricing", {})
        if not all(k in pricing for k in ("input_usd_per_million", "output_usd_per_million")):
            raise PlatformError(
                422, "model_pricing_unavailable", "当前配置无已核实价格，不能承诺美元费用上限。"
            )
    if body.evidence_ids:
        # Additional selected references never expand the instance/window bundle.
        found = set(
            actor.db.scalars(
                select(Evidence.id).where(
                    Evidence.id.in_(body.evidence_ids),
                    Evidence.instance_id.in_(body.instance_ids),
                    Evidence.observed_at >= start,
                    Evidence.observed_at < end,
                )
            )
        )
        if found != set(body.evidence_ids):
            raise PlatformError(404, "resource_not_found", "资源不存在。")
    _, task = create_investigation(
        actor,
        body.instance_ids,
        start,
        end,
        body.question,
        mode=body.mode,
        event_id=body.event_id,
        idempotency_key=idempotency_key,
        options={
            "max_tool_calls": body.max_tool_calls,
            "max_duration_seconds": body.max_duration_seconds,
            "max_cost_usd": body.max_cost_usd,
            "external_confirmed": body.external_confirmed,
            "evidence_ids": body.evidence_ids,
        },
    )
    actor.db.commit()
    return task_ref(task)


@router.get("")
def list_investigations(
    actor: ActorContext = Depends(current_actor),
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    instance_id: str | None = None,
    status: str | None = None,
    department_id: str | None = None,
    system_id: str | None = None,
    environment: str | None = None,
    from_time: Annotated[str | None, Query(alias="from")] = None,
    to_time: Annotated[str | None, Query(alias="to")] = None,
    q: str | None = None,
):
    allowed = authorize(actor, "data.read").instance_ids
    # A report with ANY unauthorized resource is excluded before pagination/count.
    disallowed = exists(
        select(InvestigationResource.investigation_id).where(
            InvestigationResource.investigation_id == Investigation.id,
            InvestigationResource.instance_id.not_in(allowed),
        )
    )
    resources = exists(
        select(InvestigationResource.investigation_id).where(
            InvestigationResource.investigation_id == Investigation.id
        )
    )
    query = select(Investigation).where(~disallowed, resources)
    if instance_id:
        authorize(actor, "data.read", [instance_id])
        query = query.where(
            exists(
                select(InvestigationResource.investigation_id).where(
                    InvestigationResource.investigation_id == Investigation.id,
                    InvestigationResource.instance_id == instance_id,
                )
            )
        )
    if department_id or system_id or environment:
        from ..models import Instance, System

        scoped = (
            select(Instance.id)
            .join(System, Instance.system_id == System.id)
            .where(Instance.id.in_(allowed))
        )
        if department_id:
            scoped = scoped.where(System.department_id == department_id)
        if system_id:
            scoped = scoped.where(System.id == system_id)
        if environment:
            scoped = scoped.where(System.environment == environment)
        query = query.where(
            exists(
                select(InvestigationResource.investigation_id).where(
                    InvestigationResource.investigation_id == Investigation.id,
                    InvestigationResource.instance_id.in_(scoped),
                )
            )
        )
    if status:
        query = query.where(Investigation.status == status)
    if from_time or to_time:
        start, end = time_window(from_time, to_time)
        query = query.where(Investigation.created_at >= start, Investigation.created_at < end)
    if q:
        escaped = q[:120].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(
            (Investigation.id == q) | Investigation.question.ilike(f"%{escaped}%", escape="\\")
        )
    rows, total, next_cursor = page_query(
        actor.db, query, Investigation, limit=limit, cursor=cursor
    )
    return {
        "items": [investigation_detail(actor, row, include_audit=False, include_facts=False) for row in rows],
        "total": total,
        "next_cursor": next_cursor,
    }


@router.get("/{identifier}")
def detail(identifier: str, actor: ActorContext = Depends(current_actor)):
    return investigation_detail(actor, get_investigation(actor, identifier))


@router.get("/{identifier}/metric-windows/{item_id}")
def frozen_metric(identifier: str, item_id: str, actor: ActorContext = Depends(current_actor)):
    return get_frozen_metric(actor, identifier, item_id)


@router.post("/{identifier}/reviews", status_code=201)
def review(identifier: str, body: ReviewInput, actor: ActorContext = Depends(current_actor)):
    audit = review_investigation(actor, identifier, body.verdict, body.reason, body.evidence_ids)
    actor.db.commit()
    return {"id": audit.id, "source": "human_assertion", "verdict": body.verdict}
