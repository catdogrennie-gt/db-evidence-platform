from datetime import timedelta
import pytest

pytestmark = pytest.mark.usefixtures("legacy_collection_compatibility")
from sqlalchemy import select, func
from tests.platform.test_ai_scope import ai_env
from platform_app import models as m
from platform_app.db import utcnow


def policy_env(e):
    from platform_app.intelligence import save_policy
    e['a'].enabled = True; e['a'].lifecycle_state = 'Active'
    role = e['db'].scalar(select(m.Role).where(m.Role.name == 'ai-test'))
    role.permissions = [*role.permissions, 'event.read']
    e['db'].commit()
    p = save_policy(e['actor'], dict(enabled=True, instance_ids=[e['a'].id], signal_interval_seconds=300, ai_interval_seconds=1800, urgent_enabled=True, cooldown_seconds=300, max_daily_runs=3, mode='rules_only', external_confirmed=False))
    e['db'].commit()
    return p


def test_periodic_dispatch_without_anomaly_dedupes_and_no_catchup(ai_env):
    from platform_app.intelligence import dispatch_due
    p = policy_env(ai_env)
    ids = dispatch_due(ai_env['factory'])
    assert len(ids) == 1
    assert dispatch_due(ai_env['factory']) == []
    ai_env['db'].expire_all()
    p = ai_env['db'].get(m.InspectionPolicy, p.id)
    assert p.next_ai_at > utcnow()
    p.next_ai_at = utcnow() - timedelta(days=3)
    ai_env['db'].commit()
    assert len(dispatch_due(ai_env['factory'])) == 1
    assert dispatch_due(ai_env['factory']) == []


def test_daily_budget_and_permission_revocation_are_visible(ai_env):
    from platform_app.intelligence import dispatch_due
    p = policy_env(ai_env); p.max_daily_runs = 1; ai_env['db'].commit()
    assert len(dispatch_due(ai_env['factory'])) == 1
    ai_env['db'].expire_all(); p.next_ai_at = utcnow()-timedelta(seconds=1); ai_env['db'].commit()
    assert dispatch_due(ai_env['factory']) == []
    ai_env['db'].expire_all(); assert p.status == 'budget_exhausted'
    ai_env['db'].delete(ai_env['grant']); p.next_ai_at = utcnow()-timedelta(seconds=1); ai_env['db'].commit()
    assert dispatch_due(ai_env['factory']) == []
    ai_env['db'].expire_all(); assert p.status == 'permission_denied'


def test_queued_inspection_stops_when_policy_paused(ai_env):
    from platform_app.intelligence import dispatch_due, check_inspection_task
    p = policy_env(ai_env)
    [identifier] = dispatch_due(ai_env['factory'])
    ai_env['db'].expire_all()
    task = ai_env['db'].get(m.Task, identifier)
    p.enabled = False; ai_env['db'].commit()
    from fastapi import HTTPException
    with pytest.raises(HTTPException): check_inspection_task(ai_env['db'], task)


def test_live_policy_requires_consent_and_normal_scope_validation(ai_env):
    from platform_app.intelligence import save_policy
    p = policy_env(ai_env)
    from fastapi import HTTPException
    with pytest.raises(HTTPException): save_policy(ai_env['actor'], dict(enabled=True, instance_ids=[ai_env['a'].id], mode='live_redacted', external_confirmed=False))
    with pytest.raises(HTTPException): save_policy(ai_env['actor'], dict(enabled=True, instance_ids=[ai_env['b'].id], mode='rules_only'))


def test_conditional_risk_requires_sufficient_contiguous_actual_history():
    from platform_app.intelligence import conditional_risk
    now = utcnow()
    def points(n, gap=False):
        from platform_app.common import iso
        return [{'time': iso(now-timedelta(minutes=(n-i)*5)), 'value': None if gap and i == 3 else 80+i*.2} for i in range(n)]
    assert conditional_risk(points(3), threshold=95) is None
    assert conditional_risk(points(20, True), threshold=95) is None
    risk = conditional_risk(points(20), threshold=95)
    assert risk and risk['threshold'] == 95 and risk['kind'] == 'conditional_prediction'


def test_conditional_prediction_summary_exposes_window_threshold_and_assumptions():
    from platform_app.intelligence import prediction_summary
    calculation={'observed_from':'2026-11-08T01:00:00Z','observed_to':'2026-11-08T02:00:00Z','threshold':95,'slope_per_hour':3,'estimated_hours_if_trend_continues':2.3,'assumptions':['趋势持续','负载结构不变']}
    summary=prediction_summary(calculation)
    assert '2026-11-08T01:00:00Z' in summary and '95' in summary
    assert '2.3' in summary and '趋势持续' in summary and '负载结构不变' in summary


def test_issue_projection_uses_original_event_and_typed_provenance(ai_env):
    from platform_app.intelligence import overview, issue_detail
    policy_env(ai_env)
    event = m.Event(instance_id=ai_env['a'].id, title='Actual lock observation', source='rule', severity='warning')
    ai_env['db'].add(event); ai_env['db'].flush()
    finding = m.InspectionFinding(instance_id=ai_env['a'].id, category='lock_wait', dimension='instance', fingerprint='a'*64, event_id=event.id, kind='current')
    ai_env['db'].add(finding); ai_env['db'].commit()
    result = overview(ai_env['actor'])
    assert result['issues'][0]['id'] == event.id
    detail = issue_detail(ai_env['actor'], event.id)
    assert detail['event_id'] == event.id and detail['report'] is None
    assert detail['report_status'] == 'not_started'
    assert all({'kind','evidence_ids'}.issubset(edge) for edge in detail['edges'])


def test_urgent_changed_signal_reuses_issue_and_cooldown(ai_env):
    from platform_app.intelligence import dispatch_due
    p=policy_env(ai_env)
    p.next_ai_at=utcnow()+timedelta(hours=1)
    evidence=ai_env['evidence']; evidence.observed_at=utcnow()-timedelta(seconds=1)
    evidence.coverage={'availability':'available','complete':True}
    evidence.payload={'rows':[{'waiting_trx_id':1,'blocking_trx_id':2}]}
    ai_env['db'].commit()
    assert len(dispatch_due(ai_env['factory']))==1
    ai_env['db'].expire_all()
    first=ai_env['db'].scalar(select(m.InspectionFinding))
    original_event=first.event_id
    p.next_signal_at=utcnow()-timedelta(seconds=1);ai_env['db'].commit()
    assert dispatch_due(ai_env['factory'])==[]
    ai_env['db'].expire_all()
    assert ai_env['db'].scalar(select(func.count()).select_from(m.InspectionFinding))==1
    assert ai_env['db'].get(m.InspectionFinding,first.id).event_id==original_event


def test_overview_never_leaks_report_from_partially_visible_scope(ai_env):
    from platform_app.intelligence import overview
    policy_env(ai_env)
    e=ai_env
    event=m.Event(instance_id=e['a'].id,title='visible event')
    e['db'].add(event);e['db'].flush()
    inv=e['investigation'];inv.event_id=event.id;inv.status='completed';inv.report={'facts':[{'statement':'PRIVATE OTHER RESOURCE','evidence_ids':[]}]}
    e['db'].add_all([m.InvestigationResource(investigation_id=inv.id,instance_id=e['a'].id),m.InvestigationResource(investigation_id=inv.id,instance_id=e['b'].id)])
    e['db'].commit()
    assert 'PRIVATE OTHER RESOURCE' not in str(overview(e['actor']))


@pytest.mark.parametrize('statement,expected',[(None,0),('持续锁等待可能造成应用延迟，需要核验事务',1),('未发现异常，当前证据无法证明风险',0)])
def test_periodic_explicit_finding_becomes_stable_review_result_only_with_real_refs(ai_env,statement,expected):
    from platform_app.intelligence import dispatch_due,overview
    from platform_app.evidence import create_manifest
    e=ai_env;p=policy_env(e)
    p.mode='live_redacted';p.external_confirmed=True;e['db'].commit()
    [task_id]=dispatch_due(e['factory'])
    e['db'].expire_all()
    task=e['db'].get(m.Task,task_id); inv=e['db'].get(m.Investigation,task.input_ref)
    manifest=create_manifest(e['db'],'investigation',inv.id,[e['a'].id],inv.window_start,inv.window_end)
    inv.manifest_id=manifest.id;inv.status='completed'
    inv.report={'facts':[],'hypotheses':[{'statement':statement,'evidence_ids':[e['evidence'].id],'refutations':[]}] if statement else [],'unknowns':[],
                'findings':[{'intent':'review_issue','category':'locks','instance_id':e['a'].id,'dimension':{'kind':'instance','value':e['a'].id},'statement':statement,'evidence_ids':[e['evidence'].id]}] if expected else []}
    task.status='succeeded';task.finished_at=utcnow();e['db'].commit()
    dispatch_due(e['factory'])
    e['db'].expire_all()
    result=overview(e['actor'])
    assert len(result['issues'])==expected
    if expected:
        issue=result['issues'][0]
        assert issue['kind']=='risk' and issue['finding_status']=='hypothesis' and '待核验' in issue['title']
        assert issue['investigation_id']==inv.id


def test_repeated_periodic_hypothesis_keeps_same_issue_and_report_versions(ai_env):
    from platform_app.intelligence import dispatch_due,overview,issue_detail
    from platform_app.evidence import create_manifest
    e=ai_env;p=policy_env(e)
    p.mode='live_redacted';p.external_confirmed=True;e['db'].commit()
    reports=[]
    for cycle in range(2):
        if cycle:
            e['db'].expire_all();p.next_ai_at=utcnow()-timedelta(seconds=1);e['db'].commit()
        [task_id]=dispatch_due(e['factory'])
        e['db'].expire_all();task=e['db'].get(m.Task,task_id);inv=e['db'].get(m.Investigation,task.input_ref)
        manifest=create_manifest(e['db'],'investigation',inv.id,[e['a'].id],inv.window_start,inv.window_end)
        inv.manifest_id=manifest.id;inv.status='completed';inv.report={'facts':[],'hypotheses':[{'statement':'锁等待可能造成延迟，待核验','evidence_ids':[e['evidence'].id],'refutations':[]}]}
        inv.report=dict(inv.report,findings=[{'intent':'review_issue','category':'locks','instance_id':e['a'].id,'dimension':{'kind':'instance','value':e['a'].id},'statement':'锁等待可能造成延迟，待核验','evidence_ids':[e['evidence'].id]}])
        task.status='succeeded';task.finished_at=utcnow();reports.append(inv.id);e['db'].commit()
        dispatch_due(e['factory'])
    e['db'].expire_all()
    result=overview(e['actor'])
    assert len(result['issues'])==1
    detail=issue_detail(e['actor'],result['issues'][0]['id'])
    assert {h['id'] for h in detail['history']}==set(reports)
