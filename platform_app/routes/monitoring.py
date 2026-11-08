"""Monitoring source administration and business-scoped observations."""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from .. import models as m, monitoring as domain
from ..access import allowed_scope_ids
from ..identity import current_actor
from ..common import time_window

router = APIRouter(prefix='/monitoring', tags=['monitoring'])

class SourceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(max_length=1000)
    system_id: str
    api_token: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)
    allowed_host_group_ids: list[str] = Field(default_factory=list, max_length=100)

class SelectionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    hostids: list[str] = Field(max_length=100)

@router.get('/sources')
def sources(actor=Depends(current_actor)):
    allowed = allowed_scope_ids(actor, 'asset.onboard', 'system')
    items = [domain.source_response(row) for row in actor.db.scalars(select(m.MonitoringSource).where(m.MonitoringSource.system_id.in_(allowed)))]
    return {'items': items, 'total': len(items)}

@router.post('/sources', status_code=201)
def create(body: SourceInput, request: Request, actor=Depends(current_actor)):
    row = domain.save_source(actor, request.app.state.config, body.model_dump())
    actor.db.commit()
    return domain.source_response(row)

@router.put('/sources/{identifier}')
def update(identifier: str, body: SourceInput, request: Request, actor=Depends(current_actor)):
    row = domain.save_source(actor, request.app.state.config, body.model_dump(exclude_unset=True), identifier)
    actor.db.commit()
    return domain.source_response(row)

@router.get('/sources/{identifier}/hosts')
def hosts(identifier: str, request: Request, actor=Depends(current_actor)):
    result = domain.discover(actor, domain.get_source(actor, identifier), request.app.state.config)
    return {**result, 'items': [{k: v for k, v in h.items() if k not in ('items', 'tags')} for h in result['items']]}

@router.post('/sources/{identifier}/selection')
def selection(identifier: str, body: SelectionInput, request: Request, actor=Depends(current_actor)):
    result = domain.select_hosts(actor, domain.get_source(actor, identifier), body.hostids, request.app.state.config)
    actor.db.commit()
    return result

@router.get('/objects')
def objects(system_id: str | None = None, department_id: str | None = None, environment: str | None = None, instance_id: str | None = None, actor=Depends(current_actor)):
    return domain.objects(actor, system_id=system_id, department_id=department_id, environment=environment, instance_id=instance_id)

@router.get('/objects/{identifier}/metrics')
def metrics(identifier: str, request: Request, actor=Depends(current_actor)):
    start, end = time_window(request.query_params.get('from'), request.query_params.get('to'))
    return domain.object_metrics(actor, identifier, start, end, request.app.state.config)
