"""Read-only product inspection endpoints, separate from management APIs."""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import readonly
from ..identity import current_actor

router = APIRouter(prefix='/readonly', tags=['readonly'])


class CatalogQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    search: str = Field(default='', max_length=255)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)


@router.get('/overview')
def overview(request: Request, system_id: str | None = None, department_id: str | None = None,
             environment: str | None = None, instance_id: str | None = None, actor=Depends(current_actor)):
    return readonly.overview(actor, request.app.state.config, system_id=system_id,
                             department_id=department_id, environment=environment, instance_id=instance_id)


@router.get('/sources')
def sources(system_id: str | None = None, actor=Depends(current_actor)):
    items = readonly.source_summaries(actor, readonly.visible_ids(actor, system_id=system_id))
    return {'items': items, 'total': len(items)}


@router.post('/sources/{identifier}/items/query')
def catalog(identifier: str, body: CatalogQuery, actor=Depends(current_actor)):
    # POST is a paginated query: no enqueue, commit, collection, or model call.
    return readonly.selected_catalog(actor, identifier, body.model_dump())
