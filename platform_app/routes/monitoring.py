"""Monitoring source administration and business-scoped observations."""

from ..config import require_legacy, group_collection_allowed
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from .. import models as m, monitoring as domain
from ..access import allowed_scope_ids
from ..identity import current_actor
from ..common import time_window
from typing import Literal
from .. import analysis_groups as groups

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

class TagFilter(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tag: str = Field(min_length=1,max_length=255)
    value: str = Field(default='',max_length=1024)
    operator: Literal['equals','contains','exists'] = 'equals'

class ItemQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    hostids: list[str] | None = Field(default=None,max_length=100)
    host_tags: list[TagFilter] = Field(default_factory=list,max_length=20)
    item_tags: list[TagFilter] = Field(default_factory=list,max_length=20)
    search: str = Field(default='',max_length=255)
    offset: int = Field(default=0,ge=0)
    limit: int = Field(default=100,ge=1,le=200)

class ItemRef(BaseModel):
    model_config = ConfigDict(extra='forbid')
    hostid: str = Field(min_length=1,max_length=80)
    itemid: str = Field(min_length=1,max_length=80)

class RelationInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    from_hostid: str = Field(min_length=1,max_length=80)
    to_hostid: str = Field(min_length=1,max_length=80)
    kind: Literal['runs_on','depends_on','connects_to','related_to']
    label: str = Field(default='',max_length=255)

class GroupInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1,max_length=120)
    source_id: str
    description: str = Field(default='',max_length=2000)
    items: list[ItemRef] = Field(min_length=1,max_length=32)
    relations: list[RelationInput] = Field(default_factory=list,max_length=32)
    enabled: bool = True
    interval_seconds: int = Field(default=60,ge=10,le=3600)
    config_revision: int | None = Field(default=None,ge=1)

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
    require_legacy(request.app.state.config)
    result = domain.select_hosts(actor, domain.get_source(actor, identifier), body.hostids, request.app.state.config)
    actor.db.commit()
    return result

@router.get('/objects')
def objects(system_id: str | None = None, department_id: str | None = None, environment: str | None = None, instance_id: str | None = None, actor=Depends(current_actor)):
    return domain.objects(actor, system_id=system_id, department_id=department_id, environment=environment, instance_id=instance_id)

@router.post('/sources/{identifier}/items/query')
def items_query(identifier: str, body: ItemQuery, request: Request, actor=Depends(current_actor)):
    return groups.query_items(actor,domain.get_source(actor,identifier),body.model_dump(),request.app.state.config)

@router.get('/groups')
def group_list(request: Request,system_id: str | None=None,department_id: str | None=None,environment: str | None=None,instance_id: str | None=None,actor=Depends(current_actor)):
    return groups.list_groups(actor,request.app.state.config,system_id=system_id,department_id=department_id,environment=environment,instance_id=instance_id)

@router.post('/groups',status_code=201)
def group_create(body: GroupInput,request: Request,actor=Depends(current_actor)):
    result=groups.save_group(actor,request.app.state.config,body.model_dump()); actor.db.commit(); return result

@router.get('/groups/{identifier}')
def group_detail(identifier: str,request: Request,actor=Depends(current_actor)):
    return groups.read_group(actor,identifier,request.app.state.config)

@router.put('/groups/{identifier}')
def group_update(identifier: str,body: GroupInput,request: Request,actor=Depends(current_actor)):
    result=groups.save_group(actor,request.app.state.config,body.model_dump(),identifier); actor.db.commit(); return result

@router.get('/groups/{identifier}/metrics')
def group_metrics(identifier: str,request: Request,actor=Depends(current_actor)):
    mapping,instance,source=groups.get_group(actor,identifier)
    start,end=time_window(request.query_params.get('from'),request.query_params.get('to'))
    return domain.object_metrics(actor,instance.id,start,end,request.app.state.config)

@router.get('/groups/{identifier}/items/{itemid}/history')
def group_history(identifier: str,itemid: str,request: Request,limit: int=100,actor=Depends(current_actor)):
    start,end=time_window(request.query_params.get('from'),request.query_params.get('to'))
    return groups.item_history(actor,identifier,itemid,start,end,limit,request.app.state.config)

@router.get('/objects/{identifier}/metrics')
def metrics(identifier: str, request: Request, actor=Depends(current_actor)):
    start, end = time_window(request.query_params.get('from'), request.query_params.get('to'))
    return domain.object_metrics(actor, identifier, start, end, request.app.state.config)
