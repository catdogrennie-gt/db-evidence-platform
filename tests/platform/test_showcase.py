from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from tests.platform.test_ai_scope import ai_env
from platform_app import models as m
from platform_app.access import actor_for_user
from platform_app.db import utcnow


@pytest.fixture
def showcase_env(ai_env):
    e = ai_env
    db = e['db']
    a = e['a']
    a.enabled = a.is_lab = True
    a.lifecycle_state = 'Active'
    a.connection_mode = 'zabbix'
    a.engine = 'analysis'
    original_role=db.get(m.Role, e['grant'].role_id)
    original_role.permissions = [*original_role.permissions,'event.read']
    source = m.MonitoringSource(name='source', url='https://monitor.invalid', secret_ref='unused', system_id=a.system_id, status='ready')
    reader = m.User(username='public-reader', password_hash='unused')
    role = m.Role(name='public-reader', permissions=['data.read', 'event.read', 'knowledge.read'])
    db.add_all([source, reader, role]); db.flush()
    db.add(m.UserGrant(user_id=reader.id, role_id=role.id, scope_type='instance', scope_id=a.id))
    mapping = m.MonitoringMapping(source_id=source.id, hostid='group:fixed', instance_id=a.id, layer='group', selected=True, items=[{'hostid':'101','itemid':'42','enhancement':'mysql.lock_waits'}], last_observed_at=utcnow())
    schedule = m.CollectionSchedule(instance_id=a.id, collector='monitoring', interval_seconds=60, config_revision=a.config_revision)
    policy = m.InspectionPolicy(owner_id=e['actor'].user_id, enabled=True, instance_ids=[a.id], signal_interval_seconds=300, ai_interval_seconds=3600, cooldown_seconds=30, max_daily_runs=24, next_ai_at=utcnow()+timedelta(hours=1))
    db.add_all([mapping,schedule,policy]); db.commit()
    token = e['tmp_path']/'showcase-token.private.txt'; token.write_text('test-only-token-value-'+'x'*40)
    config = SimpleNamespace(showcase=dict(enabled=True, instance_id=a.id, policy_id=policy.id, reader_user_id=reader.id, token_path=str(token)), collection_mode='zabbix_only')
    db.info['platform_config'] = config
    e.update(config=config, reader=actor_for_user(db,reader.id), policy=policy, schedule=schedule, mapping=mapping, token=token, source=source)
    return e


def snapshot_row(e, rows, *, ago=1, complete=True, itemid='42'):
    row=m.Evidence(instance_id=e['a'].id,run_id=e['evidence'].run_id,source='mysql.lock_waits',source_mode='live',observed_at=utcnow()-timedelta(seconds=ago),payload={'rows':rows},payload_sha256='1'*64,truncated=True,coverage={'availability':'available','complete':False,'transport':'zabbix','source_id':e['source'].id,'hostid':'101','itemid':itemid,'mapping_version':e['mapping'].version,'config_revision':e['a'].config_revision,'gaps':['history_truncated'],'reason_codes':['history_truncated'],'snapshot_contract':'db-evidence-lock-snapshot-v2','snapshot_complete':complete,'snapshot_truncated':not complete,'snapshot_total_row_count':len(rows)})
    e['db'].add(row); e['db'].commit(); return row


def test_multiple_fixed_targets_have_isolated_config_and_no_arbitrary_ids(showcase_env):
    from platform_app import showcase
    e=showcase_env
    original=dict(e['config'].showcase)
    e['config'].showcase['targets']=[{'key':'svc-a','instance_id':e['a'].id,
        'policy_id':e['policy'].id,'reader_user_id':e['reader'].user_id,
        'projection_mode':'numeric','fast_ai_seconds':60}]
    selected=showcase.select_target(e['config'],'svc-a')
    assert selected is not e['config']
    assert selected.showcase['instance_id']==e['a'].id
    assert selected.showcase['projection_mode']=='numeric'
    assert e['config'].showcase['instance_id']==original['instance_id']
    with pytest.raises(HTTPException) as exc: showcase.select_target(e['config'],e['a'].id)
    assert exc.value.status_code==404
    with pytest.raises(HTTPException): showcase.select_target(e['config'],'../svc-a')


def test_optional_ai_cadence_lease_uses_normal_scheduler_and_restores(showcase_env):
    from platform_app import showcase
    e=showcase_env
    e['config'].showcase['fast_ai_seconds']=60
    old=e['policy'].ai_interval_seconds
    tasks_before=e['db'].scalar(select(func.count()).select_from(m.Task))
    result=showcase.set_pacing(e['db'],e['config'],True)
    assert result['ai_seconds']==60 and e['policy'].ai_interval_seconds==60
    assert e['policy'].next_ai_at>utcnow()-timedelta(seconds=1)
    assert e['db'].scalar(select(func.count()).select_from(m.Task))==tasks_before
    showcase.set_pacing(e['db'],e['config'],False)
    assert e['policy'].ai_interval_seconds==old


def test_numeric_projection_does_not_claim_no_locks_means_healthy(showcase_env):
    from platform_app import showcase
    e=showcase_env
    e['config'].showcase['projection_mode']='numeric'
    value=showcase.snapshot(e['db'],e['config'])
    assert value['observation']['status']=='unknown'
    assert value['observation']['metrics']==[]
    assert value['report'] is None


def test_token_fixed_target_and_input_contract(showcase_env):
    from platform_app.showcase import authenticate, snapshot
    e=showcase_env
    for supplied in (None,'Bearer wrong','Basic unrelated'):
        with pytest.raises(HTTPException) as out: authenticate(e['config'],supplied)
        assert out.value.status_code==401
    authenticate(e['config'],'Bearer '+e['token'].read_text())
    result=snapshot(e['db'],e['config'])
    assert result['target']['instance_id']==e['a'].id
    assert result['observation']['status']=='unknown' and not result['report_matches_current_observation']
    assert 'DO-NOT-EXFILTRATE' not in str(result)
    e['config'].showcase['instance_id']=e['b'].id
    with pytest.raises(HTTPException): snapshot(e['db'],e['config'])


def test_lease_changes_actual_cadence_and_restores_without_ai(showcase_env):
    from platform_app.showcase import set_pacing, reconcile_pacing
    e=showcase_env; db=e['db']; now=utcnow()
    before=db.scalar(select(func.count()).select_from(m.Investigation))
    result=set_pacing(db,e['config'],True,now=now); db.commit()
    assert result['active'] and e['schedule'].interval_seconds==10 and e['policy'].signal_interval_seconds==5
    assert e['policy'].ai_interval_seconds==3600 and e['policy'].max_daily_runs==24
    set_pacing(db,e['config'],True,now=now+timedelta(seconds=20)); db.commit()
    reconcile_pacing(db,e['config'],now=now+timedelta(seconds=50)); db.commit()
    assert e['schedule'].interval_seconds==10
    reconcile_pacing(db,e['config'],now=now+timedelta(seconds=66)); db.commit()
    assert e['schedule'].interval_seconds==60 and e['policy'].signal_interval_seconds==300
    assert db.scalar(select(func.count()).select_from(m.Investigation))==before
    set_pacing(db,e['config'],True,now=now+timedelta(seconds=70)); db.commit()
    set_pacing(db,e['config'],False,now=now+timedelta(seconds=71)); db.commit()
    assert e['schedule'].interval_seconds==60 and e['policy'].signal_interval_seconds==300


def test_lease_does_not_clobber_admin_edits_or_reapply_while_on(showcase_env):
    from platform_app.showcase import set_pacing, reconcile_pacing
    e=showcase_env; db=e['db']; now=utcnow()
    set_pacing(db,e['config'],True,now=now); db.commit()
    e['schedule'].interval_seconds=90
    e['policy'].version+=1; e['policy'].signal_interval_seconds=60; db.commit()
    reconcile_pacing(db,e['config'],now=now+timedelta(seconds=3)); db.commit()
    result=set_pacing(db,e['config'],True,now=now+timedelta(seconds=4)); db.commit()
    assert not result['active'] and e['schedule'].interval_seconds==90 and e['policy'].signal_interval_seconds==60
    set_pacing(db,e['config'],False,now=now+timedelta(seconds=5)); db.commit()
    assert e['schedule'].interval_seconds==90 and e['policy'].signal_interval_seconds==60


def test_complete_current_snapshot_survives_history_gap_and_rearms(showcase_env):
    from platform_app.intelligence import scan_signals
    from platform_app.showcase import snapshot
    e=showcase_env; db=e['db']; now=utcnow()
    first=snapshot_row(e,[{'blocking_thread_id':11,'waiting_thread_id':12}])
    findings=scan_signals(db,e['policy'],now); db.commit()
    assert len(findings)==1 and first.id in findings[0].evidence_ids
    original_hash=findings[0].signal_hash; findings[0].dispatched_hash=original_hash; db.commit()
    snapshot_row(e,[],ago=0)
    assert scan_signals(db,e['policy'],utcnow())==[]; db.commit()
    assert snapshot(db,e['config'])['observation']['status']=='normal'
    snapshot_row(e,[{'blocking_thread_id':21,'waiting_thread_id':22}],ago=-.01)
    findings=scan_signals(db,e['policy'],utcnow()+timedelta(seconds=1)); db.commit()
    assert len(findings)==1 and findings[0].signal_hash!=original_hash
    assert e['db'].scalar(select(func.count()).select_from(m.InspectionFinding))==1


def test_truncated_or_stale_snapshot_is_not_zero_or_recovery(showcase_env):
    from platform_app.intelligence import scan_signals
    from platform_app.showcase import snapshot
    e=showcase_env; db=e['db']
    snapshot_row(e,[{'blocking_thread_id':11}],complete=False)
    assert scan_signals(db,e['policy'],utcnow())==[]
    assert snapshot(db,e['config'])['observation']['status']=='unknown'


def test_readonly_overview_sees_service_policy_and_actual_check(showcase_env):
    from platform_app.intelligence import scan_signals, overview
    e=showcase_env; now=utcnow()
    snapshot_row(e,[])
    scan_signals(e['db'],e['policy'],now); e['db'].commit()
    result=overview(e['reader'])
    assert result['inspection']['status']!='not_configured'
    assert result['inspection']['last_checked_at'] is not None
    assert result['inspection']['signal_seconds']==300
    assert 'owner_id' not in result['inspection'] and 'token' not in str(result)


def test_urgent_dispatch_is_autonomous_deduped_while_inflight(showcase_env):
    from platform_app.intelligence import dispatch_due
    e=showcase_env
    # Session factories are also the genuine non-legacy group configuration.
    e['factory'].kw['info']={'platform_config':e['config']}
    snapshot_row(e,[{'blocking_thread_id':11,'waiting_thread_id':12}])
    created=dispatch_due(e['factory'])
    e['db'].expire_all()
    assert len(created)==1, (e['policy'].status,e['policy'].reason)
    e['db'].expire_all(); e['policy'].next_signal_at=utcnow()-timedelta(seconds=1); e['db'].commit()
    snapshot_row(e,[{'blocking_thread_id':11,'waiting_thread_id':12},{'blocking_thread_id':11,'waiting_thread_id':13}],ago=0)
    assert dispatch_due(e['factory'])==[]
    assert e['db'].scalar(select(func.count()).select_from(m.InspectionDispatch))==1


def test_expired_lease_restores_even_when_bridge_disabled_or_reader_revoked(showcase_env):
    from platform_app.showcase import set_pacing, reconcile_pacing
    e=showcase_env; db=e['db']; now=utcnow()
    set_pacing(db,e['config'],True,now=now); db.commit()
    e['config'].showcase['enabled']=False
    db.get(m.User,e['reader'].user_id).active=False; db.commit()
    reconcile_pacing(db,e['config'],now=now+timedelta(seconds=46)); db.commit()
    assert e['schedule'].interval_seconds==60 and e['policy'].signal_interval_seconds==300


def test_latest_incomplete_does_not_fall_back_to_positive_and_same_episode_is_stable(showcase_env):
    from platform_app.intelligence import scan_signals
    e=showcase_env
    snapshot_row(e,[{'waiting_thread_id':1}],ago=2)
    [finding]=scan_signals(e['db'],e['policy'],utcnow()); e['db'].commit()
    signal=finding.signal_hash
    snapshot_row(e,[{'waiting_thread_id':1},{'waiting_thread_id':2}],ago=1)
    assert scan_signals(e['db'],e['policy'],utcnow())[0].signal_hash==signal
    snapshot_row(e,[],ago=0,complete=False)
    assert scan_signals(e['db'],e['policy'],utcnow())==[]
    assert finding.calculation['observation_state']=='active'


def test_another_database_empty_snapshot_cannot_recover_active_problem(showcase_env):
    from platform_app.intelligence import scan_signals
    from platform_app.showcase import snapshot
    e=showcase_env
    e['mapping'].items=[*e['mapping'].items,{'hostid':'101','itemid':'43','enhancement':'mysql.lock_waits'}];e['db'].commit()
    snapshot_row(e,[{'waiting_thread_id':1}],ago=1)
    snapshot_row(e,[],ago=0,itemid='43')
    assert len(scan_signals(e['db'],e['policy'],utcnow()))==1
    assert snapshot(e['db'],e['config'])['observation']['status']=='problem'


def test_fixed_token_never_projects_multi_object_report_even_if_reader_scope_grows(showcase_env):
    from platform_app.showcase import snapshot
    e=showcase_env; db=e['db'];inv=e['investigation']
    grant=db.scalar(select(m.UserGrant).where(m.UserGrant.user_id==e['reader'].user_id))
    db.add(m.UserGrant(user_id=e['reader'].user_id,role_id=grant.role_id,scope_type='instance',scope_id=e['b'].id))
    inv.status='completed';inv.report={'executive_summary':{'headline':'OTHER TARGET PRIVATE'}}
    db.add_all([m.InvestigationResource(investigation_id=inv.id,instance_id=e['a'].id),m.InvestigationResource(investigation_id=inv.id,instance_id=e['b'].id)]);db.commit()
    assert 'OTHER TARGET PRIVATE' not in str(snapshot(db,e['config']))


def test_report_is_historical_until_current_episode_is_inside_its_frozen_window(showcase_env):
    from platform_app.intelligence import scan_signals
    from platform_app.showcase import snapshot
    e=showcase_env; db=e['db']; inv=e['investigation']
    inv.status='completed';inv.report={'executive_summary':{'headline':'Historical genuine diagnosis'},'coverage':{'complete':False}}
    db.add(m.InvestigationResource(investigation_id=inv.id,instance_id=e['a'].id)); db.commit()
    snapshot_row(e,[{'waiting_thread_id':1}])
    [finding]=scan_signals(db,e['policy'],utcnow()); finding.latest_investigation_id=inv.id;db.commit()
    result=snapshot(db,e['config'])
    assert result['report']['executive_summary']['headline']=='Historical genuine diagnosis'
    assert not result['report_matches_current_observation']
    inv.window_end=utcnow()+timedelta(seconds=1);db.commit()
    assert snapshot(db,e['config'])['report_matches_current_observation']
    snapshot_row(e,[],ago=0)
    assert not snapshot(db,e['config'])['report_matches_current_observation']


def test_http_capability_rejects_extra_parameters_and_non_boolean(showcase_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from platform_app.routes.showcase import router
    from platform_app.db import get_db
    e=showcase_env; app=FastAPI(); app.state.config=e['config']; app.include_router(router,prefix='/api/v1')
    app.dependency_overrides[get_db]=lambda:e['db']
    client=TestClient(app)
    assert client.get('/api/v1/showcase/snapshot').status_code==401
    headers={'Authorization':'Bearer '+e['token'].read_text()}
    assert client.get('/api/v1/showcase/snapshot?instance_id=other',headers=headers).status_code==422
    for body in ({'enabled':1},{'enabled':True,'instance_id':'other'}):
        assert client.put('/api/v1/showcase/pacing',headers=headers,json=body).status_code==422
    assert client.put('/api/v1/showcase/pacing',headers=headers,json={'enabled':True}).status_code==200


def test_confirmed_recovery_reappearance_bypasses_900_second_old_episode_cooldown(showcase_env):
    from platform_app.intelligence import dispatch_due
    e=showcase_env;db=e['db'];e['factory'].kw['info']={'platform_config':e['config']}
    e['policy'].cooldown_seconds=900;db.commit()
    snapshot_row(e,[{'waiting_thread_id':1}])
    [task_id]=dispatch_due(e['factory']);db.expire_all()
    task=db.get(m.Task,task_id);inv=db.get(m.Investigation,task.input_ref)
    task.status='completed';task.finished_at=utcnow();inv.status='completed';inv.report={}
    e['policy'].next_signal_at=utcnow()-timedelta(seconds=1);db.commit()
    snapshot_row(e,[],ago=0)
    assert dispatch_due(e['factory'])==[]
    db.expire_all(); e['policy'].next_signal_at=utcnow()-timedelta(seconds=1);db.commit()
    snapshot_row(e,[{'waiting_thread_id':2}],ago=0)
    assert len(dispatch_due(e['factory']))==1
    assert db.scalar(select(func.count()).select_from(m.InspectionDispatch))==2


def test_budget_state_reconciles_actual_history_after_limit_increase_without_reset(showcase_env):
    from platform_app.intelligence import dispatch_due, policy_status, overview
    e=showcase_env;db=e['db'];e['factory'].kw['info']={'platform_config':e['config']}
    e['policy'].max_daily_runs=1;db.commit()
    snapshot_row(e,[{'waiting_thread_id':1}])
    [task_id]=dispatch_due(e['factory']);db.expire_all()
    task=db.get(m.Task,task_id);task.status='completed';task.finished_at=utcnow()
    inv=db.get(m.Investigation,task.input_ref);inv.status='completed';inv.report={}
    e['policy'].max_daily_runs=24;e['policy'].status='budget_exhausted';e['policy'].reason='old exhausted'
    e['policy'].next_signal_at=utcnow()-timedelta(seconds=1);db.commit()
    # Preserve the genuine prior dispatch when increasing the old 1/day limit.
    before=db.scalar(select(func.count()).select_from(m.InspectionDispatch))
    assert policy_status(e['actor'],e['policy'])['status']=='scheduled'
    assert overview(e['reader'])['inspection']['status']=='scheduled'
    snapshot_row(e,[])
    assert dispatch_due(e['factory'])==[]
    db.expire_all()
    assert e['policy'].status=='scheduled' and e['policy'].reason is None
    assert db.scalar(select(func.count()).select_from(m.InspectionDispatch))==before


def test_completed_inspection_task_remains_visible_without_overriding_policy_status(showcase_env):
    from platform_app.intelligence import dispatch_due
    from platform_app.showcase import inspection_projection
    e=showcase_env;db=e['db'];e['factory'].kw['info']={'platform_config':e['config']}
    snapshot_row(e,[{'waiting_thread_id':1}])
    [task_id]=dispatch_due(e['factory']);db.expire_all()
    task=db.get(m.Task,task_id);inv=db.get(m.Investigation,task.input_ref)
    task.status='completed';task.started_at=utcnow()-timedelta(seconds=1);task.finished_at=utcnow();inv.status='completed'
    e['policy'].status='scheduled';e['policy'].reason=None;db.commit()
    projection=inspection_projection(db,{e['a'].id},policy=e['policy'])
    assert projection['task']['id']==task_id and projection['task']['status']=='completed'
    assert projection['task']['started_at'] is not None
    assert projection['status']=='scheduled'
    assert inspection_projection(db,{e['b'].id},policy=e['policy'])['task'] is None


def test_source_offline_clears_after_fresh_normal_scan_without_dispatch(showcase_env):
    from platform_app.intelligence import dispatch_due

    e = showcase_env
    db = e['db']
    e['factory'].kw['info'] = {'platform_config': e['config']}
    snapshot_row(e, [])
    policy = e['policy']
    original_version = policy.version
    original_budget = policy.max_daily_runs
    investigation_count = db.scalar(select(func.count()).select_from(m.Investigation))

    e['source'].status = 'failed'
    db.commit()
    assert dispatch_due(e['factory']) == []
    db.expire_all()
    assert policy.status == 'source_offline'

    e['source'].status = 'ready'
    e['mapping'].last_observed_at = utcnow() - timedelta(minutes=6)
    policy.next_signal_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert dispatch_due(e['factory']) == []
    db.expire_all()
    assert policy.status == 'source_offline'

    e['mapping'].last_observed_at = utcnow()
    policy.next_signal_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert dispatch_due(e['factory']) == []
    db.expire_all()
    assert policy.status == 'scheduled'
    assert policy.reason is None
    assert policy.version == original_version
    assert policy.max_daily_runs == original_budget
    assert db.scalar(select(func.count()).select_from(m.InspectionDispatch)) == 0
    assert db.scalar(select(func.count()).select_from(m.Investigation)) == investigation_count
    check = db.get(m.PlatformSetting, 'inspection-status:' + policy.id)
    assert check.value['last_checked_at'] is not None


@pytest.mark.parametrize('disabled_target', ['policy', 'instance'])
def test_source_offline_does_not_clear_before_disabled_checks(showcase_env, disabled_target):
    from platform_app.intelligence import dispatch_due

    e = showcase_env
    db = e['db']
    e['factory'].kw['info'] = {'platform_config': e['config']}
    e['policy'].status = 'source_offline'
    e['policy'].reason = 'source_offline'
    target = e['policy'] if disabled_target == 'policy' else e['a']
    target.enabled = False
    db.commit()

    assert dispatch_due(e['factory']) == []
    db.expire_all()
    assert e['policy'].status != 'scheduled'
    assert db.scalar(select(func.count()).select_from(m.InspectionDispatch)) == 0
