"""Issue cards require actual rules or a scoped, version-bound finding review."""
from copy import deepcopy
from datetime import timedelta
import pytest
from sqlalchemy import select,func

from platform_app import models as m
from platform_app.common import iso
from platform_app.health_signals import scan_group_changes
from platform_app.intelligence import overview,issue_detail,sync_model_findings
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_showcase import showcase_env
from tests.platform.test_health_signals import numeric_evidence,series


def test_a_newer_real_lock_observation_remains_visible_beside_an_older_report(showcase_env):
    from platform_app.db import utcnow
    from platform_app.intelligence import upsert_finding,issue_response
    e=showcase_env;now=utcnow()
    finding=upsert_finding(e['db'],e['a'].id,'lock_wait','instance',[],
        {'row_count':1,'observed_at':iso(now),'observation_state':'active'},now=now)
    inv=e['investigation'];inv.status='completed';inv.window_end=now-timedelta(minutes=1)
    finding.latest_investigation_id=inv.id
    event=e['db'].get(m.Event,finding.event_id)
    value=issue_response(e['db'],event,finding)
    assert '1 条等待链' in value['title']
    assert '1 条等待链' in value['summary'] and iso(now) in value['summary']
    assert value['report_freshness']['status']=='outdated'
    assert '具体 SQL' in value['summary']


def baseline(e, *, changed=40):
    e['config'].fernet_key_path=e['tmp_path']/'absent-test-alias-key'
    now,points=series(changed=changed);numeric_evidence(e,points)
    finding=scan_group_changes(e['db'],e['policy'],now)[0]
    e['db'].commit()
    return finding,e['db'].get(m.Event,finding.event_id),now


def reviewed_report(e,finding,now, *, statement='请求延迟持续上升，需要核对等待位置',valid=True):
    inv=e['investigation'];inv.window_start=now-timedelta(minutes=10);inv.window_end=now
    inv.event_id=finding.event_id;inv.status='completed'
    proposal={'intent':'review_issue','category':'correlation','instance_id':e['a'].id,
        'dimension':{'kind':'metric','value':'1234'},'statement':statement,
        'evidence_ids':list(finding.evidence_ids) if valid else ['not-supplied']}
    inv.report={'facts':[],'hypotheses':[],'findings':[proposal],
        'executive_summary':{'headline':'UNRELATED BROAD REPORT HEADLINE'}}
    e['db'].add(m.InvestigationResource(investigation_id=inv.id,instance_id=e['a'].id))
    manifest=m.EvidenceManifest(owner_type='investigation',owner_id=inv.id,instance_ids=[e['a'].id],
        window_start=inv.window_start,window_end=inv.window_end,manifest_sha256='a'*64,
        retain_until=now+timedelta(days=1))
    e['db'].add(manifest);e['db'].flush();inv.manifest_id=manifest.id
    for ref in finding.evidence_ids:
        evidence=e['db'].get(m.Evidence,ref)
        e['db'].add(m.ManifestItem(manifest_id=manifest.id,instance_id=e['a'].id,item_type='evidence',
            evidence_id=ref,payload={'source':evidence.source},payload_sha256=evidence.payload_sha256))
    task=m.Task(type='investigation',instance_id=e['a'].id,actor_id=e['actor'].user_id,status='succeeded')
    e['db'].add(task);e['db'].flush();inv.task_id=task.id
    task.payload={'mode':'live_redacted','options':{'inspection':{'signals':[{
        'instance_id':finding.instance_id,'category':finding.category,'kind':finding.kind,
        'signal_hash':finding.signal_hash,'evidence_ids':finding.evidence_ids,'calculation':deepcopy(finding.calculation)}]}}}
    finding.latest_investigation_id=inv.id
    dispatch=m.InspectionDispatch(policy_id=e['policy'].id,policy_version=e['policy'].version,
        dispatch_key='review-test',trigger='urgent',instance_ids=[e['a'].id],event_id=finding.event_id,
        investigation_id=inv.id,task_id=inv.task_id)
    e['db'].add(dispatch);e['db'].commit()
    return inv,dispatch,statement


@pytest.mark.parametrize('changed,direction',[(40,'升高'),(1,'降低')])
def test_bare_baseline_change_is_signal_not_problem_even_on_decrease(showcase_env,changed,direction):
    e=showcase_env;finding,event,now=baseline(e,changed=changed)
    value=overview(e['reader']);[card]=value['issues']
    assert card['presentation_kind']=='signal' and value['stats']['current']==value['stats']['risk']==0
    assert card['title']=='Request duration'+direction
    assert 's' in card['summary'] and str(changed) in card['summary'] and '10' in card['summary']
    assert issue_detail(e['reader'],event.id)['presentation_kind']=='signal'


def test_valid_finding_review_promotes_only_that_signal_without_mutating_history(showcase_env):
    e=showcase_env;finding,event,now=baseline(e);original_title=event.title
    inv,dispatch,statement=reviewed_report(e,finding,now)
    sync_model_findings(e['db'],dispatch,inv);sync_model_findings(e['db'],dispatch,inv);e['db'].commit()
    reviews=list(e['db'].scalars(select(m.EventTransition).where(m.EventTransition.action=='inspection_finding_review')))
    assert len(reviews)==1
    assert reviews[0].details=={'investigation_id':inv.id,'signal_hash':finding.signal_hash,'statement':statement,'evidence_ids':finding.evidence_ids}
    assert event.title==original_title
    [card]=overview(e['reader'])['issues'];detail=issue_detail(e['reader'],event.id)
    assert card['presentation_kind']==detail['presentation_kind']=='issue'
    assert card['title']==detail['title']=='待核验：'+statement and card['finding_status']=='hypothesis'
    assert '复核' in card['summary'] and 'UNRELATED' not in card['summary']
    assert overview(e['reader'])['stats']['current']==1


@pytest.mark.parametrize('damage',['new_episode','new_report','unauthorized_report','invalid_refs','empty_findings'])
def test_review_never_promotes_another_episode_report_or_unvalidated_finding(showcase_env,damage):
    e=showcase_env;finding,event,now=baseline(e)
    inv,dispatch,_=reviewed_report(e,finding,now,valid=damage!='invalid_refs')
    if damage=='empty_findings':inv.report={**inv.report,'findings':[]}
    sync_model_findings(e['db'],dispatch,inv)
    if damage=='new_episode':finding.signal_hash='different-episode'
    if damage=='new_report':
        other=m.Investigation(created_by=e['actor'].user_id,question='Later report',window_start=inv.window_start,window_end=inv.window_end+timedelta(seconds=1),status='completed',permission_version=1)
        e['db'].add(other);e['db'].flush();finding.latest_investigation_id=other.id
        e['db'].add(m.InvestigationResource(investigation_id=other.id,instance_id=e['a'].id))
    if damage=='unauthorized_report':e['db'].add(m.InvestigationResource(investigation_id=inv.id,instance_id=e['b'].id))
    e['db'].commit();[card]=overview(e['reader'])['issues']
    assert card['presentation_kind']=='signal' and '请求延迟持续上升' not in str(card)
    assert 'UNRELATED BROAD' not in str(card)
    assert overview(e['reader'])['stats']['current']==0


@pytest.mark.parametrize('state',['returned_to_baseline','configuration_changed','recovered'])
def test_historical_observation_states_are_history_even_with_review(showcase_env,state):
    e=showcase_env;finding,event,now=baseline(e);inv,dispatch,_=reviewed_report(e,finding,now)
    sync_model_findings(e['db'],dispatch,inv);finding.calculation={**finding.calculation,'observation_state':state};e['db'].commit()
    [card]=overview(e['reader'])['issues']
    assert card['presentation_kind']=='history' and overview(e['reader'])['stats']['current']==0
    assert issue_detail(e['reader'],event.id)['presentation_kind']=='history'


def test_old_report_completion_cannot_review_the_new_episode(showcase_env):
    e=showcase_env;finding,event,now=baseline(e);inv,dispatch,_=reviewed_report(e,finding,now)
    finding.signal_hash='new-episode';e['db'].commit()
    sync_model_findings(e['db'],dispatch,inv);e['db'].commit()
    assert e['db'].scalar(select(func.count()).select_from(m.EventTransition).where(m.EventTransition.action=='inspection_finding_review'))==0
    assert overview(e['reader'])['issues'][0]['presentation_kind']=='signal'


def test_overview_reviews_are_batched_without_full_report_or_evidence_load(showcase_env,monkeypatch):
    from tests.platform.test_overview_performance import capture
    e=showcase_env;finding,event,now=baseline(e);inv,dispatch,_=reviewed_report(e,finding,now)
    sync_model_findings(e['db'],dispatch,inv)
    for i in range(1,32):
        extra=m.Event(instance_id=e['a'].id,title='Preserved event '+str(i),last_seen_at=event.last_seen_at)
        e['db'].add(extra);e['db'].flush()
        copied=m.InspectionFinding(instance_id=e['a'].id,event_id=extra.id,category='baseline_change',
            dimension='synthetic:'+str(i),fingerprint=str(i).zfill(64),kind='current',
            evidence_ids=finding.evidence_ids,calculation=deepcopy(finding.calculation),
            signal_hash=finding.signal_hash,latest_investigation_id=inv.id)
        e['db'].add_all([copied,m.EventTransition(event_id=extra.id,action='inspection_finding_review',
            details={'investigation_id':inv.id,'signal_hash':finding.signal_hash,
                'statement':'Synthetic reviewed observation '+str(i),'evidence_ids':finding.evidence_ids})])
    e['db'].commit();e['db'].expire_all()
    monkeypatch.setattr('platform_app.intelligence._related_report_fact',lambda *a:pytest.fail('no detail fact path in overview'))
    result,statements=capture(e,lambda:overview(e['reader']))
    assert len(result['issues'])==32 and all(v['presentation_kind']=='issue' for v in result['issues'])
    assert len(statements)<=40
    assert sum('event_transitions' in s and 'inspection_finding_review' not in s for s in statements)<=1
    assert not any('manifest_items' in s or 'evidence.payload' in s for s in statements)


def test_late_old_report_cannot_write_a_review_for_current_selected_report(showcase_env):
    e=showcase_env;finding,event,now=baseline(e);old,dispatch,_=reviewed_report(e,finding,now)
    current=m.Investigation(created_by=e['actor'].user_id,event_id=event.id,question='Newer',
        window_start=old.window_start,window_end=old.window_end+timedelta(seconds=1),
        status='completed',permission_version=1)
    e['db'].add(current);e['db'].flush();finding.latest_investigation_id=current.id
    e['db'].add(m.InvestigationResource(investigation_id=current.id,instance_id=e['a'].id));e['db'].commit()
    sync_model_findings(e['db'],dispatch,old);e['db'].commit()
    assert finding.latest_investigation_id==current.id
    assert e['db'].scalar(select(func.count()).select_from(m.EventTransition).where(m.EventTransition.action=='inspection_finding_review'))==0


def test_same_item_on_another_host_cannot_review_the_existing_baseline(showcase_env):
    from platform_app.evidence import digest
    e=showcase_env;finding,event,now=baseline(e)
    evidence=e['db'].get(m.Evidence,finding.evidence_ids[0]);payload=deepcopy(evidence.payload)
    payload['item']['hostid']='another-host';evidence.payload=payload;evidence.payload_sha256=digest(payload)
    inv,dispatch,_=reviewed_report(e,finding,now)
    sync_model_findings(e['db'],dispatch,inv);e['db'].commit()
    assert e['db'].scalar(select(func.count()).select_from(m.EventTransition).where(m.EventTransition.action=='inspection_finding_review'))==0
    card=next(v for v in overview(e['reader'])['issues'] if v['id']==event.id)
    assert card['presentation_kind']=='signal'


def test_nonbaseline_deterministic_rule_remains_issue(showcase_env):
    from platform_app.intelligence import upsert_finding
    e=showcase_env;f=upsert_finding(e['db'],e['a'].id,'lock_wait','instance',[],{'observation_state':'active'})
    e['db'].commit();result=overview(e['reader']);[card]=result['issues']
    assert card['presentation_kind']=='issue' and result['stats']['current']==1
    assert issue_detail(e['reader'],f.event_id)['presentation_kind']=='issue'


@pytest.mark.parametrize('reviewed',[True,False])
@pytest.mark.parametrize('view',['overview','detail'])
def test_new_same_episode_sample_keeps_useful_summary_with_truthful_report_freshness(showcase_env,reviewed,view):
    e=showcase_env;finding,event,now=baseline(e);inv,dispatch,statement=reviewed_report(e,finding,now)
    if not reviewed:inv.report={**inv.report,'findings':[]}
    sync_model_findings(e['db'],dispatch,inv);original_window=(inv.window_start,inv.window_end)
    sample=numeric_evidence(e,[{'time':iso(now+timedelta(seconds=1)),'value':50,'validity':'valid'}])
    finding.evidence_ids=[sample.id]
    finding.calculation={**finding.calculation,'observed_at':iso(sample.observed_at),'recent':{'median':50}}
    e['db'].commit()
    card=overview(e['reader'])['issues'][0] if view=='overview' else issue_detail(e['reader'],event.id)
    assert card['report_freshness']['status']=='outdated'
    assert (inv.window_start,inv.window_end)==original_window
    if reviewed:
        assert statement in card['summary'] and card['summary'].startswith('报告窗口内：')
        assert card['presentation_kind']=='issue'
    else:
        assert '50' in card['summary'] and not card['summary'].startswith('报告窗口内：')
        assert card['presentation_kind']=='signal'


def test_legacy_candidate_without_assessment_provenance_is_history(ai_env,legacy_collection_compatibility):
    from tests.platform.test_intelligence_regressions import finish_model_report,proposal
    e=ai_env;result,inv=finish_model_report(e,{'facts':[],'hypotheses':[],'findings':[proposal(e)]})
    finding=e['db'].scalar(select(m.InspectionFinding))
    finding.calculation={key:value for key,value in finding.calculation.items() if key!='assessment_investigation_id'}
    original_title=e['db'].get(m.Event,finding.event_id).title;original_report=deepcopy(inv.report)
    e['db'].commit()
    [card]=overview(e['actor'])['issues'];detail=issue_detail(e['actor'],finding.event_id)
    for projection in (card,detail):
        assert projection['presentation_kind']=='history'
        assert '历史候选' in projection['title'] and '未被本次报告复核' in projection['summary']
        assert '锁等待与事务延迟' not in projection['title']+projection['summary']
        assert projection['observation_state']=='historical_candidate'
    assert overview(e['actor'])['stats']['risk']==0
    assert e['db'].get(m.Event,finding.event_id).title==original_title and inv.report==original_report


def test_valid_candidate_requires_its_own_selected_report_not_a_later_neutral_report(ai_env,legacy_collection_compatibility):
    from platform_app.intelligence import sync_reports
    from tests.platform.test_intelligence_regressions import finish_model_report,proposal
    e=ai_env;result,inv=finish_model_report(e,{'facts':[],'hypotheses':[],'findings':[proposal(e)]})
    finding=e['db'].scalar(select(m.InspectionFinding));statement=finding.calculation['statement']
    assert finding.calculation['assessment_investigation_id']==inv.id
    assert result['issues'][0]['presentation_kind']=='issue' and result['stats']['risk']==1
    assert statement in issue_detail(e['actor'],finding.event_id)['summary']
    original_report=deepcopy(inv.report);original_title=e['db'].get(m.Event,finding.event_id).title
    old_dispatch=e['db'].scalar(select(m.InspectionDispatch).where(m.InspectionDispatch.investigation_id==inv.id))
    task=m.Task(type='investigation',instance_id=e['a'].id,actor_id=e['actor'].user_id,status='succeeded',payload={'mode':'live_redacted'})
    e['db'].add(task);e['db'].flush()
    later=m.Investigation(created_by=e['actor'].user_id,question='Later neutral assessment',task_id=task.id,
        window_start=inv.window_start,window_end=inv.window_end+timedelta(seconds=1),status='completed',
        permission_version=1,report={'facts':[],'hypotheses':[],'findings':[],
            'executive_summary':{'headline':'This report does not review the previous candidate'}})
    e['db'].add(later);e['db'].flush()
    e['db'].add_all([m.InvestigationResource(investigation_id=later.id,instance_id=e['a'].id),
        m.InspectionDispatch(policy_id=old_dispatch.policy_id,policy_version=old_dispatch.policy_version,
            dispatch_key='neutral-later',trigger='periodic',instance_ids=[e['a'].id],
            investigation_id=later.id,task_id=task.id)])
    e['db'].flush();sync_reports(e['db']);e['db'].commit()
    assert finding.latest_investigation_id==later.id and finding.calculation['assessment_investigation_id']==inv.id
    [card]=overview(e['actor'])['issues'];detail=issue_detail(e['actor'],finding.event_id)
    for projection in (card,detail):
        assert projection['presentation_kind']=='history' and projection['investigation_id']==later.id
        assert '未被本次报告复核' in projection['summary'] and statement not in projection['summary']
        assert projection['observation_state']=='historical_candidate'
    assert overview(e['actor'])['stats']['risk']==0
    assert inv.report==original_report and e['db'].get(m.Event,finding.event_id).title==original_title
