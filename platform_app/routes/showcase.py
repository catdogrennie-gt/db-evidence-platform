from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from ..db import get_db
from ..common import PlatformError, iso
from .. import showcase
from ..scheduling import db_now

router=APIRouter(prefix='/showcase',tags=['showcase'])


class PacingInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enabled:StrictBool


def capability(request:Request):
    showcase.authenticate(request.app.state.config,request.headers.get('authorization'))
    if request.query_params:raise PlatformError(422,'showcase_parameters_forbidden','展示桥不接受查询参数。')


@router.get('/snapshot',dependencies=[Depends(capability)])
def read_snapshot(request:Request,db=Depends(get_db)):
    return showcase.snapshot(db,request.app.state.config)


@router.get('/targets',dependencies=[Depends(capability)])
def targets(request:Request,db=Depends(get_db)):
    return showcase.target_catalog(db,request.app.state.config)


@router.get('/targets/{key}/snapshot',dependencies=[Depends(capability)])
def target_snapshot(key:str,request:Request,db=Depends(get_db)):
    return showcase.snapshot(db,showcase.select_target(request.app.state.config,key))


@router.put('/targets/{key}/pacing',dependencies=[Depends(capability)])
def target_pacing(key:str,request:Request,body:PacingInput,db=Depends(get_db)):
    value=showcase.set_pacing(db,showcase.select_target(request.app.state.config,key),body.enabled)
    db.commit()
    return {'server_time':iso(db_now(db)),'pacing':value}


@router.put('/pacing',dependencies=[Depends(capability)])
def pacing(request:Request,body:PacingInput,db=Depends(get_db)):
    value=showcase.set_pacing(db,request.app.state.config,body.enabled)
    db.commit()
    return {'server_time':iso(db_now(db)),'pacing':value}
