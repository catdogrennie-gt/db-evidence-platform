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


@router.put('/pacing',dependencies=[Depends(capability)])
def pacing(request:Request,body:PacingInput,db=Depends(get_db)):
    value=showcase.set_pacing(db,request.app.state.config,body.enabled)
    db.commit()
    return {'server_time':iso(db_now(db)),'pacing':value}
