from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from ..access import ActorContext
from ..common import PlatformError
from ..demo import request_start, request_stop, snapshot
from ..identity import current_actor

router = APIRouter(prefix="/demo", tags=["demo"])


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StopInput(EmptyInput):
    run_id: str | None = None


def no_query(request):
    if request.query_params:
        raise PlatformError(422, "demo_parameters_forbidden", "演示不接受查询参数。")


@router.get("")
def get_demo(request: Request, actor: ActorContext = Depends(current_actor)):
    return snapshot(actor, request.app.state.config)


@router.post("/start")
def start(
    request: Request, body: EmptyInput | None = None, actor: ActorContext = Depends(current_actor)
):
    no_query(request)
    request_start(actor, request.app.state.config)
    actor.db.commit()
    return snapshot(actor, request.app.state.config)


@router.post("/stop")
def stop(
    request: Request, body: StopInput | None = None, actor: ActorContext = Depends(current_actor)
):
    no_query(request)
    request_stop(actor, request.app.state.config, body.run_id if body else None)
    actor.db.commit()
    return snapshot(actor, request.app.state.config)
