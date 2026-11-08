from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from .. import intelligence as domain
from ..identity import current_actor
from ..common import PlatformError
from ..scheduling import task_ref

router=APIRouter(prefix='/intelligence',tags=['intelligence'])

class PolicyInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enabled: bool=False
    instance_ids:list[str]=Field(default_factory=list,max_length=100)
    signal_interval_seconds:int=Field(default=300,ge=30,le=86400)
    ai_interval_seconds:int=Field(default=1800,ge=300,le=86400)
    urgent_enabled:bool=True
    cooldown_seconds:int=Field(default=300,ge=30,le=86400)
    max_daily_runs:int=Field(default=24,ge=1,le=100)
    mode:str='rules_only'
    external_confirmed:bool=False

@router.get('/overview')
def overview(system_id:str|None=None,department_id:str|None=None,environment:str|None=None,instance_id:str|None=None,actor=Depends(current_actor)):
    return domain.overview(actor,system_id=system_id,department_id=department_id,environment=environment,instance_id=instance_id)

@router.get('/issues/{identifier}')
def issue(identifier:str,actor=Depends(current_actor)):
    return domain.issue_detail(actor,identifier)

@router.get('/policy')
def policy(actor=Depends(current_actor)):
    row=domain.current_policy(actor)
    return {'policy':domain.policy_response(row),'status':domain.policy_status(actor,row)}

@router.put('/policy')
def save(body:PolicyInput,actor=Depends(current_actor)):
    row=domain.save_policy(actor,body.model_dump())
    actor.db.commit()
    return {'policy':domain.policy_response(row),'status':domain.policy_status(actor,row)}

@router.post('/policy/run',status_code=202)
def run(request:Request,actor=Depends(current_actor)):
    row=domain.current_policy(actor)
    if not row or not row.enabled: raise PlatformError(409,'inspection_policy_paused','请先保存并启用巡检策略。')
    identifier=row.id
    actor.db.commit()
    identifiers=domain.dispatch_due(request.app.state.session_factory,policy_id=identifier,force=True)
    from ..models import Task
    actor.db.expire_all()
    return {'tasks':[task_ref(actor.db.get(Task,i)) for i in identifiers],'status':domain.policy_status(actor,actor.db.get(type(row),identifier))}
