"""Periodic work must not spend an anomaly's last analysis opportunity."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from platform_app import models as m
from platform_app.db import utcnow
from platform_app.intelligence import dispatch_due, effective_policy_state
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_intelligence import policy_env

pytestmark = pytest.mark.usefixtures('legacy_collection_compatibility')


def history(e, policy, triggers, *, old_day=False):
    """Prior failed work still consumed real durable reservations, even on v0."""
    for index, trigger in enumerate(triggers):
        inv = m.Investigation(created_by=e['actor'].user_id, question='prior attempt',
                              window_start=e['start'], window_end=e['end'],
                              permission_version=1, status='failed')
        e['db'].add(inv); e['db'].flush()
        task = m.Task(type='investigation', queue='ai', status='failed',
                      actor_id=e['actor'].user_id, input_ref=inv.id)
        e['db'].add(task); e['db'].flush()
        e['db'].add(m.InspectionDispatch(policy_id=policy.id, policy_version=0,
            dispatch_key=f'{index:064x}', trigger=trigger, instance_ids=[e['a'].id],
            investigation_id=inv.id, task_id=task.id, status='failed',
            created_at=utcnow()-timedelta(days=1) if old_day else utcnow()))
    e['db'].commit()


def count(e):
    return e['db'].scalar(select(func.count()).select_from(m.InspectionDispatch))


def normal_due(e, policy):
    policy.next_ai_at = policy.next_signal_at = utcnow()-timedelta(seconds=1)
    e['db'].commit()


@pytest.mark.parametrize('force', [False, True])
def test_normal_or_forced_work_cannot_spend_reserved_anomaly_slots(ai_env, force):
    e=ai_env; p=policy_env(e); p.max_daily_runs=12
    history(e,p,['periodic']*8+['manual'])
    normal_due(e,p)
    assert dispatch_due(e['factory'],policy_id=p.id,force=force)==[]
    e['db'].expire_all()
    assert count(e)==9
    assert p.status=='periodic_budget_reserved'
    assert '异常' in p.reason


def test_reserved_slot_dispatches_actual_new_signal_then_hard_cap_still_holds(ai_env):
    e=ai_env; p=policy_env(e); p.max_daily_runs=12
    history(e,p,['periodic']*9+['urgent']*2)
    # Both schedules due: rejected normal work must not consume this signal.
    evidence=e['evidence']; evidence.observed_at=utcnow()-timedelta(seconds=1)
    evidence.coverage={'availability':'available','complete':True}
    evidence.payload={'rows':[{'waiting_trx_id':1,'blocking_trx_id':2}]}
    normal_due(e,p)
    assert dispatch_due(e['factory'],policy_id=p.id)==[]
    e['db'].expire_all()
    finding=e['db'].scalar(select(m.InspectionFinding))
    assert finding and finding.signal_hash != finding.dispatched_hash
    p.next_signal_at=utcnow()-timedelta(seconds=1); e['db'].commit()
    [task_id]=dispatch_due(e['factory'],policy_id=p.id)
    e['db'].expire_all()
    row=e['db'].scalar(select(m.InspectionDispatch).where(m.InspectionDispatch.task_id==task_id))
    assert row.trigger=='urgent' and row.event_id==finding.event_id
    assert count(e)==12
    assert dispatch_due(e['factory'],policy_id=p.id,force=True)==[]
    e['db'].expire_all(); assert count(e)==12
    assert effective_policy_state(e['db'],p)[0]=='budget_exhausted'


def test_prior_urgent_work_does_not_consume_ordinary_allocation_twice(ai_env):
    e=ai_env; p=policy_env(e); p.max_daily_runs=12
    history(e,p,['urgent']*3+['periodic']*8)
    normal_due(e,p)
    assert len(dispatch_due(e['factory'],policy_id=p.id))==1
    e['db'].expire_all(); assert count(e)==12


@pytest.mark.parametrize('limit,urgent,prior',[(1,True,0),(2,True,1),(3,True,2),(12,False,11)])
def test_small_or_urgent_disabled_policies_have_explicit_bounds(ai_env,limit,urgent,prior):
    e=ai_env; p=policy_env(e); p.max_daily_runs=limit; p.urgent_enabled=urgent
    history(e,p,['periodic']*prior); normal_due(e,p)
    result=dispatch_due(e['factory'],policy_id=p.id)
    assert len(result)==(0 if urgent and limit>1 else 1)


@pytest.mark.parametrize('change',['utc_day','urgent_disabled','larger_budget'])
def test_reserved_state_releases_from_current_ledger_and_configuration(ai_env,change):
    e=ai_env; p=policy_env(e); p.max_daily_runs=12
    history(e,p,['periodic']*9,old_day=change=='utc_day')
    p.status='periodic_budget_reserved'; p.reason='previous reservation'
    if change=='urgent_disabled': p.urgent_enabled=False
    if change=='larger_budget': p.max_daily_runs=20
    e['db'].commit()
    assert effective_policy_state(e['db'],p)==('scheduled',None)


def test_completed_report_cannot_hide_a_still_reserved_or_exhausted_budget(ai_env):
    e=ai_env; p=policy_env(e); p.max_daily_runs=12
    history(e,p,['periodic']*9)
    p.status='completed'; e['db'].commit()
    assert effective_policy_state(e['db'],p)[0]=='periodic_budget_reserved'
    p.max_daily_runs=9; e['db'].commit()
    assert effective_policy_state(e['db'],p)[0]=='budget_exhausted'


def test_budget_projection_preserves_permission_failure(ai_env):
    e=ai_env; p=policy_env(e); p.max_daily_runs=12
    history(e,p,['periodic']*9)
    p.status='permission_denied'; p.reason='revoked'; e['db'].commit()
    assert effective_policy_state(e['db'],p)==('permission_denied','revoked')
