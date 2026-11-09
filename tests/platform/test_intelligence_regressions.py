"""Regression gates from the final independent inspection review."""
from datetime import timedelta
import pytest

pytestmark = pytest.mark.usefixtures("legacy_collection_compatibility")
from sqlalchemy import select
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_intelligence import policy_env
from platform_app import models as m
from platform_app.common import iso
from platform_app.db import utcnow, create_session_factory


@pytest.mark.parametrize('window_count',[1,40])
def test_only_supplied_initial_metric_summaries_are_valid_citations(ai_env,window_count):
    from platform_app.evidence import create_manifest
    from platform_app.investigations import _bundle,validate_report
    e=ai_env;inv=e['investigation']
    e['db'].add(m.InvestigationResource(investigation_id=inv.id,instance_id=e['a'].id))
    manifest=create_manifest(e['db'],'investigation',inv.id,[e['a'].id],inv.window_start,inv.window_end)
    for _ in range(window_count):
        e['db'].add(m.ManifestItem(manifest_id=manifest.id,instance_id=e['a'].id,item_type='time_series_window',payload={'template':'host_cpu_percent','owner_collector':'host','source':'zabbix_history','unit':'%','points':[{'time':iso(inv.window_start),'value':45,'validity':'valid'}],'coverage':{'complete':True}},payload_sha256='a'*64))
    e['db'].flush()
    bundle,_,_,cited_ids=_bundle(e['db'],inv,manifest,{e['a'].id:{'allowed_sources':['host']}})
    supplied={item['evidence_id'] for item in bundle['evidence']}
    assert supplied and all(ref.startswith('manifest:') for ref in supplied)
    assert cited_ids==supplied
    validate_report({'facts':[{'statement':'冻结窗口CPU实测45%','evidence_ids':[next(iter(supplied))]}],'hypotheses':[],'refutations':[],'unknowns':[],'next_readonly_checks':[]},cited_ids)
    blocked,_,_,blocked_ids=_bundle(e['db'],inv,manifest,{e['a'].id:{'allowed_sources':['runtime']}})
    assert all(not item['evidence_id'].startswith('manifest:') for item in blocked['evidence'])
    assert not supplied.intersection(blocked_ids)
    if window_count==40: assert len(supplied)<window_count


def large_scope(e,count=16):
    policy=policy_env(e)
    e['grant'].scope_type='system';e['grant'].scope_id=e['system'].id
    identifiers=[e['a'].id]
    for index in range(count-1):
        instance=m.Instance(system_id=e['system'].id,name='scope-'+str(index),endpoint='localhost',expected_server_uuid='scope-'+str(index),enabled=True,lifecycle_state='Active')
        e['db'].add(instance);e['db'].flush();identifiers.append(instance.id)
    policy.instance_ids=sorted(identifiers);policy.max_daily_runs=100
    e['db'].commit()
    return policy,identifiers


def finish_dispatches(e):
    for dispatch in e['db'].scalars(select(m.InspectionDispatch)):
        inv=e['db'].get(m.Investigation,dispatch.investigation_id)
        inv.status='completed';inv.report={'facts':[],'hypotheses':[]}
        task=e['db'].get(m.Task,dispatch.task_id);task.status='succeeded';task.finished_at=utcnow()
    e['db'].commit()


def test_large_scope_rotates_fairly_after_queue_limit_and_process_restart(ai_env):
    from platform_app.intelligence import dispatch_due
    e=ai_env;policy,identifiers=large_scope(e)
    # ai_env deliberately contains one other queued investigation: only two
    # slots remain, so each cycle can reserve at most ten of sixteen objects.
    first=dispatch_due(e['factory'])
    assert len(first)==2
    e['db'].expire_all();finish_dispatches(e)
    policy.next_ai_at=utcnow()-timedelta(seconds=1);e['db'].commit()
    restarted=create_session_factory(e['engine'])
    assert len(dispatch_due(restarted))==2
    e['db'].expire_all()
    rows=list(e['db'].scalars(select(m.InspectionDispatch).order_by(m.InspectionDispatch.created_at)))
    first_ids=set().union(*(set(row.instance_ids) for row in rows[:2]))
    next_ids=set().union(*(set(row.instance_ids) for row in rows[2:]))
    assert set(identifiers)-first_ids <= next_ids
    assert set().union(*(set(row.instance_ids) for row in rows))==set(identifiers)
    assert all(len(row.instance_ids)<=5 for row in rows)


def test_daily_budget_still_rotates_to_never_reserved_objects(ai_env):
    from platform_app.intelligence import dispatch_due
    e=ai_env;policy,identifiers=large_scope(e)
    policy.max_daily_runs=1;e['db'].commit()
    assert len(dispatch_due(e['factory']))==1
    e['db'].expire_all();finish_dispatches(e)
    first=e['db'].scalar(select(m.InspectionDispatch))
    initial=set(first.instance_ids)
    # A previous UTC day's durable reservation still counts for fairness,
    # though it no longer consumes today's daily run budget.
    first.created_at=utcnow()-timedelta(days=1)
    policy.next_ai_at=utcnow()-timedelta(seconds=1);e['db'].commit()
    assert len(dispatch_due(create_session_factory(e['engine'])))==1
    e['db'].expire_all()
    rows=list(e['db'].scalars(select(m.InspectionDispatch).order_by(m.InspectionDispatch.created_at)))
    assert not initial.intersection(rows[-1].instance_ids)


def test_cross_object_report_keeps_metric_ownership_and_declared_scope(ai_env):
    from platform_app.intelligence import issue_detail
    from platform_app.evidence import create_manifest
    e=ai_env;policy_env(e)
    e['grant'].scope_type='system';e['grant'].scope_id=e['system'].id
    event=m.Event(instance_id=e['a'].id,title='subject a')
    e['db'].add(event);e['db'].flush()
    inv=e['investigation'];inv.event_id=event.id;inv.status='completed'
    for instance in (e['a'],e['b']): e['db'].add(m.InvestigationResource(investigation_id=inv.id,instance_id=instance.id))
    manifest=create_manifest(e['db'],'investigation',inv.id,[e['a'].id,e['b'].id],inv.window_start,inv.window_end)
    inv.manifest_id=manifest.id
    for instance,value in ((e['a'],20),(e['b'],95)):
        e['db'].add(m.ManifestItem(manifest_id=manifest.id,instance_id=instance.id,item_type='time_series_window',payload={'template':'host_cpu_percent','owner_collector':'host','source':'zabbix_history','unit':'%','points':[{'time':iso(inv.window_start),'value':value,'validity':'valid'}],'coverage':{'complete':True}},payload_sha256='a'*64))
    e['db'].commit()
    result=issue_detail(e['actor'],event.id)
    assert result['instance_ids']==[e['a'].id]
    assert set(result['analysis_instance_ids'])=={e['a'].id,e['b'].id}
    charts={c['instance_id']:c for c in result['charts']}
    assert charts[e['a'].id]['points'][0]['value']==20
    assert charts[e['b'].id]['points'][0]['value']==95
    assert charts[e['a'].id]['object_name']=='a' and 'a' in charts[e['a'].id]['title']
    assert charts[e['b'].id]['object_name']=='b' and 'b' in charts[e['b'].id]['title']
    assert all(chart['metric_key']=='host_cpu_percent' for chart in charts.values())
    assert {n['instance_id'] for n in result['nodes'] if n['id'].startswith('manifest:')}=={e['a'].id,e['b'].id}
    ownership=[edge for edge in result['edges'] if edge.get('provenance')=='manifest_item.instance_id']
    assert len(ownership)==2 and all(edge['kind']=='registered' for edge in ownership)


def finish_model_report(e, report):
    from platform_app.intelligence import dispatch_due, overview
    from platform_app.evidence import create_manifest
    policy=policy_env(e)
    policy.mode='live_redacted';policy.external_confirmed=True;e['db'].commit()
    [task_id]=dispatch_due(e['factory'])
    e['db'].expire_all()
    task=e['db'].get(m.Task,task_id);inv=e['db'].get(m.Investigation,task.input_ref)
    manifest=create_manifest(e['db'],'investigation',inv.id,[e['a'].id],inv.window_start,inv.window_end)
    inv.manifest_id=manifest.id;inv.status='completed';inv.report=report
    task.status='succeeded';task.finished_at=utcnow();e['db'].commit()
    dispatch_due(e['factory']);e['db'].expire_all()
    return overview(e['actor']),inv


@pytest.mark.parametrize('statement',[
    'CPU 使用率保持正常，当前未观察到资源压力',
    '没有发生锁阻塞，数据库与网络延迟保持正常',
    '持续锁等待可能造成应用延迟，需要核验事务',
])
def test_legacy_narrative_is_never_implicit_issue_creation_intent(ai_env,statement):
    result,_=finish_model_report(ai_env,{'facts':[],'hypotheses':[{'statement':statement,'evidence_ids':[ai_env['evidence'].id],'refutations':[]}]})
    assert result['issues']==[]


def proposal(e, *, category='locks', dimension=None, refs=None, statement='锁等待与事务延迟的联系需要核验'):
    return {'intent':'review_issue','category':category,'instance_id':e['a'].id,
            'dimension':dimension or {'kind':'instance','value':e['a'].id},
            'statement':statement,'evidence_ids':refs or [e['evidence'].id]}


def test_explicit_structured_finding_creates_unverified_result_without_threshold(ai_env):
    from platform_app.intelligence import issue_detail
    result,_=finish_model_report(ai_env,{'facts':[],'hypotheses':[],
                                      'findings':[proposal(ai_env)]})
    [issue]=result['issues']
    assert issue['kind']=='risk' and issue['finding_status']=='hypothesis'
    assert issue['report_freshness']['status']=='current'
    assert '锁等待与事务延迟' in issue['summary']
    detail=issue_detail(ai_env['actor'],issue['id'])
    assert any(node['kind']=='hypothesis' and '锁等待与事务延迟' in node['label'] for node in detail['nodes'])


def test_sql_findings_require_actual_distinct_frozen_digest_dimensions(ai_env):
    e=ai_env
    evidence=m.Evidence(instance_id=e['a'].id,run_id=e['evidence'].run_id,source='mysql.statement_digest',observed_at=e['start'],coverage={'availability':'available','complete':True},payload={'rows':[{'DIGEST':'a'*64},{'DIGEST':'b'*64}]},payload_sha256='c'*64)
    e['db'].add(evidence);e['db'].flush()
    valid=[proposal(e,category='sql',dimension={'kind':'sql_digest','value':value*64},refs=[evidence.id]) for value in ('a','b')]
    absent=proposal(e,category='sql',dimension={'kind':'sql_digest','value':'c'*64},refs=[evidence.id])
    missing_intent=proposal(e);missing_intent.pop('intent')
    result,_=finish_model_report(e,{'facts':[],'hypotheses':[],'findings':valid+[absent,missing_intent]})
    assert len(result['issues'])==2
    findings=list(e['db'].scalars(select(m.InspectionFinding)))
    assert {f.dimension for f in findings}=={'sql:'+'a'*64,'sql:'+'b'*64}
    assert all(f.kind=='risk' and f.calculation['classification']=='model_hypothesis' for f in findings)


def test_report_finding_schema_and_citations_are_validated():
    from fastapi import HTTPException
    from platform_app.investigations import validate_report
    report={'facts':[],'hypotheses':[],'refutations':[],'unknowns':[],'next_readonly_checks':[]}
    old=validate_report(report,{'real'})
    assert old['findings']==[]
    finding={'intent':'review_issue','category':'sql','instance_id':'object-a','dimension':{'kind':'sql_digest','value':'a'*64},'statement':'需要核验 SQL 延迟','evidence_ids':['real']}
    assert validate_report(dict(report,findings=[finding]),{'real'})['findings'][0]['dimension']['value']=='a'*64
    for patch in ({'intent':'normal'},{'category':'invented'},{'dimension':{'kind':'sql_digest','value':'invented'}},{'evidence_ids':['unsupplied']}):
        with pytest.raises(HTTPException): validate_report(dict(report,findings=[finding|patch]),{'real'})


@pytest.mark.parametrize('invalid',['target_outside_frozen_scope','unsupplied_ref','digest_in_wrong_source','family_in_wrong_source'])
def test_structured_findings_cannot_invent_scope_or_evidence_dimensions(ai_env,invalid):
    e=ai_env;finding=proposal(e)
    if invalid=='target_outside_frozen_scope':
        finding['instance_id']=e['b'].id;finding['dimension']['value']=e['b'].id
    elif invalid=='unsupplied_ref': finding['evidence_ids']=['not-in-manifest']
    elif invalid=='digest_in_wrong_source':
        finding['category']='sql';finding['dimension']={'kind':'sql_digest','value':'a'*64}
    else: finding['category']='network'
    result,_=finish_model_report(e,{'facts':[],'hypotheses':[],'findings':[finding]})
    assert result['issues']==[]


def test_model_finding_summary_respects_entire_report_authorization(ai_env):
    from platform_app.intelligence import overview
    e=ai_env
    result,inv=finish_model_report(e,{'facts':[],'hypotheses':[],'findings':[proposal(e,statement='PRIVATE OTHER OBJECT CORRELATION')]})
    assert len(result['issues'])==1
    e['db'].add(m.InvestigationResource(investigation_id=inv.id,instance_id=e['b'].id));e['db'].commit()
    result=overview(e['actor'])
    assert 'PRIVATE OTHER OBJECT CORRELATION' not in str(result)
    assert result['issues'][0]['investigation_id'] is None


def test_missing_observations_do_not_support_structured_findings(ai_env):
    e=ai_env
    e['evidence'].coverage={'availability':'unavailable','complete':False}
    e['db'].commit()
    result,_=finish_model_report(e,{'facts':[],'hypotheses':[],'findings':[proposal(e)]})
    assert result['issues']==[]


def event_report_fixture(e):
    from platform_app.evidence import create_manifest
    policy_env(e)
    event=m.Event(instance_id=e['a'].id,title='lock observation',last_seen_at=e['end']-timedelta(seconds=1))
    e['db'].add(event);e['db'].flush()
    inv=e['investigation'];inv.event_id=event.id;inv.status='completed'
    e['db'].add(m.InvestigationResource(investigation_id=inv.id,instance_id=e['a'].id))
    manifest=create_manifest(e['db'],'investigation',inv.id,[e['a'].id],inv.window_start,inv.window_end)
    inv.manifest_id=manifest.id
    finding=m.InspectionFinding(instance_id=e['a'].id,category='lock_wait',dimension='instance',fingerprint='d'*64,event_id=event.id,kind='current',latest_investigation_id=inv.id,evidence_ids=[e['evidence'].id])
    e['db'].add(finding);e['db'].flush()
    return event,inv,finding


def test_lock_summary_uses_related_fact_and_declares_later_observation_uncovered(ai_env):
    from platform_app.intelligence import issue_response
    e=ai_env;event,inv,finding=event_report_fixture(e)
    inv.report={'facts':[{'statement':'Throughput normal','evidence_ids':['unrelated']},{'statement':'One historical lock edge observed','evidence_ids':[e['evidence'].id]}]}
    e['db'].commit()
    result=issue_response(e['db'],event,finding,e['actor'])
    assert result['summary']=='One historical lock edge observed'
    assert result['issue_category']=='locks' and result['report_freshness']['status']=='current'
    event.last_seen_at=inv.window_end+timedelta(seconds=30);e['db'].commit()
    # Processing/tick time alone is not a later target observation.
    assert issue_response(e['db'],event,finding,e['actor'])['report_freshness']['status']=='current'
    later=m.Evidence(instance_id=e['a'].id,run_id=e['evidence'].run_id,source='mysql.lock_waits',observed_at=event.last_seen_at,payload={'rows':[{'blocking_trx_id':13}]},coverage={'availability':'available','complete':True},payload_sha256='a'*64)
    e['db'].add(later);e['db'].flush();finding.evidence_ids=[later.id];e['db'].commit()
    result=issue_response(e['db'],event,finding,e['actor'])
    assert result['report_freshness']['status']=='outdated'
    assert '未覆盖' in result['summary'] and 'Throughput normal' not in result['summary']
    assert result['report_freshness']['report_window_to']==iso(inv.window_end)


def test_completed_older_general_report_never_replaces_newer_specific_investigation(ai_env):
    from platform_app.intelligence import link_report
    e=ai_env;event,inv,finding=event_report_fixture(e)
    general=m.Investigation(created_by=e['actor'].user_id,question='general inspection',window_start=inv.window_start-timedelta(minutes=5),window_end=inv.window_end-timedelta(minutes=5),permission_version=1,status='completed')
    e['db'].add(general);e['db'].flush()
    link_report(e['db'],finding,general);e['db'].commit()
    assert finding.latest_investigation_id==inv.id
    transitions=list(e['db'].scalars(select(m.EventTransition).where(m.EventTransition.event_id==event.id)))
    assert any(t.details.get('investigation_id')==general.id for t in transitions)


def test_periodic_bundle_preserves_transient_lock_and_latest_counterevidence(ai_env):
    from platform_app.evidence import create_manifest
    from platform_app.investigations import _bundle
    e=ai_env;inv=e['investigation']
    e['db'].add(m.InvestigationResource(investigation_id=inv.id,instance_id=e['a'].id))
    for index in range(80):
        e['db'].add(m.Evidence(instance_id=e['a'].id,run_id=e['evidence'].run_id,source='mysql.lock_waits',observed_at=e['start']+timedelta(seconds=1+index*6),payload={'rows':[]},coverage={'availability':'available','complete':True},payload_sha256='a'*64))
    e['db'].flush()
    manifest=create_manifest(e['db'],'investigation',inv.id,[e['a'].id],inv.window_start,inv.window_end)
    bundle,*_=_bundle(e['db'],inv,manifest)
    assert e['evidence'].id in {row['evidence_id'] for row in bundle['evidence']}
    positive=next(row for row in bundle['evidence'] if row['evidence_id']==e['evidence'].id)
    assert positive['selection_reason']=='nonempty_lock_snapshot'
    latest=max((row for row in bundle['evidence'] if row['source']=='mysql.lock_waits'),key=lambda row:row['observed_at'])
    assert latest['data']['row_count']==0


def test_recovered_rule_tick_uses_actual_source_observation_not_processing_time(ai_env):
    from platform_app.intelligence import issue_response
    e=ai_env;event,inv,finding=event_report_fixture(e)
    event.last_seen_at=inv.window_end+timedelta(minutes=5)
    rule=m.Rule(name='fixture core stale',rule_version='fixture-stale-v1',template='collector_stale')
    e['db'].add(rule);e['db'].flush()
    evaluation=m.RuleEvaluation(instance_id=e['a'].id,rule_id=rule.id,rule_version=rule.rule_version,evaluation_kind='clock_tick',evaluation_key='fixture-clock',state='ok',result={})
    active=m.ActiveAlert(instance_id=e['a'].id,rule_version=rule.rule_version,dimension_fingerprint='a'*64,state='ok')
    e['db'].add_all([evaluation,active]);e['db'].flush()
    episode=m.AlertOccurrence(instance_id=e['a'].id,active_alert_id=active.id,evaluation_id=evaluation.id,status='recovered',evidence={'latest':{'source_observed_at':iso(event.last_seen_at),'recovery_observed_at':iso(inv.window_end-timedelta(seconds=1))}})
    e['db'].add(episode);e['db'].flush()
    e['db'].add(m.EventAlert(event_id=event.id,alert_occurrence_id=episode.id));e['db'].commit()
    result=issue_response(e['db'],event,finding,e['actor'])
    assert result['report_freshness']['status']=='current'
    assert result['report_freshness']['event_updated_at']==iso(inv.window_end-timedelta(seconds=1))


@pytest.mark.parametrize('event_status',['open','closed'])
def test_ai_lock_explanation_attaches_to_matching_active_deterministic_issue_only(ai_env,event_status):
    from platform_app.intelligence import upsert_finding,issue_detail
    e=ai_env
    original=upsert_finding(e['db'],e['a'].id,'lock_wait','instance',[e['evidence'].id],{'row_count':1},now=e['evidence'].observed_at)
    event=e['db'].get(m.Event,original.event_id);event.status=event_status;e['db'].commit()
    result,inv=finish_model_report(e,{'facts':[],'hypotheses':[],'findings':[proposal(e)]})
    [issue]=result['issues']
    assert original.category=='lock_wait' and original.kind=='current' and original.calculation=={'row_count':1}
    if event_status=='open':
        assert issue['id']==original.event_id and issue['kind']=='current'
        assert len(list(e['db'].scalars(select(m.InspectionFinding))))==1
        detail=issue_detail(e['actor'],issue['id'])
        assert inv.id in {entry['id'] for entry in detail['history']}
        assert detail['report']['findings'][0]['intent']=='review_issue'
    else:
        assert issue['id']!=original.event_id and issue['kind']=='risk'
        assert e['db'].get(m.Event,original.event_id).status=='closed'


def test_sql_explanation_dedupes_only_same_normalized_digest(ai_env):
    from platform_app.intelligence import upsert_finding
    e=ai_env
    evidence=m.Evidence(instance_id=e['a'].id,run_id=e['evidence'].run_id,source='mysql.statement_digest',observed_at=e['start'],coverage={'availability':'available','complete':True},payload={'rows':[{'DIGEST':'a'*64},{'DIGEST':'b'*64}]},payload_sha256='c'*64)
    e['db'].add(evidence);e['db'].flush()
    original=upsert_finding(e['db'],e['a'].id,'sql_latency','A'*64,[evidence.id],{'avg_latency_seconds':.75,'count_delta':15},now=evidence.observed_at)
    e['db'].commit()
    proposals=[proposal(e,category='sql',dimension={'kind':'sql_digest','value':value*64},refs=[evidence.id]) for value in ('a','b')]
    result,_=finish_model_report(e,{'facts':[],'hypotheses':[],'findings':proposals})
    assert len(result['issues'])==2
    assert {finding.category for finding in e['db'].scalars(select(m.InspectionFinding))}=={'sql_latency','evidence_correlation'}
    assert e['db'].get(m.Event,original.event_id).source=='inspection_rule'


def test_reverse_completion_keeps_new_model_finding_content_and_refs(ai_env):
    from platform_app.evidence import create_manifest
    from platform_app.intelligence import sync_reports,overview
    from platform_app.investigations import create_investigation
    e=ai_env;p=policy_env(e);p.mode='live_redacted';p.external_confirmed=True
    e['evidence'].coverage={'availability':'available','complete':True}
    now=utcnow();new=m.Evidence(instance_id=e['a'].id,run_id=e['evidence'].run_id,source='mysql.lock_waits',observed_at=now-timedelta(minutes=2),coverage={'availability':'available','complete':True},payload={'rows':[{'waiting_trx_id':21,'blocking_trx_id':22}]},payload_sha256='a'*64)
    e['db'].add(new);e['db'].flush()
    investigations=[]
    for index,(minutes,label,reference) in enumerate([(5,'OLD',e['evidence'].id),(1,'NEW',new.id)]):
        inv,task=create_investigation(e['actor'],[e['a'].id],now-timedelta(hours=1),now-timedelta(minutes=minutes),label,mode='live_redacted',options={'external_confirmed':True})
        manifest=create_manifest(e['db'],'investigation',inv.id,[e['a'].id],inv.window_start,inv.window_end)
        inv.manifest_id=manifest.id;inv.report={'facts':[],'hypotheses':[],'findings':[proposal(e,refs=[reference],statement=label)]}
        inv.status='analyzing' if index==0 else 'completed';task.status='running' if index==0 else 'succeeded'
        e['db'].add(m.InspectionDispatch(policy_id=p.id,policy_version=p.version,dispatch_key=str(index)*64,trigger='periodic',instance_ids=[e['a'].id],investigation_id=inv.id,task_id=task.id))
        investigations.append((inv,task))
    e['db'].commit();sync_reports(e['db']);e['db'].commit()
    [issue]=overview(e['actor'])['issues'];assert 'NEW' in issue['summary']
    old,old_task=investigations[0];old.status='completed';old_task.status='succeeded';old_task.finished_at=utcnow()
    e['db'].commit();sync_reports(e['db']);e['db'].commit()
    [issue]=overview(e['actor'])['issues']
    assert issue['investigation_id']==investigations[1][0].id and 'NEW' in issue['summary'] and 'OLD' not in issue['summary']
    finding=e['db'].scalar(select(m.InspectionFinding))
    assert finding.evidence_ids==[new.id]
    transitions=list(e['db'].scalars(select(m.EventTransition).where(m.EventTransition.event_id==issue['id'])))
    assert {row.details['investigation_id'] for row in transitions}=={inv.id for inv,_ in investigations}


def test_manifest_budget_preserves_middle_lock_before_projection(ai_env):
    from platform_app.evidence import create_manifest
    from platform_app.investigations import _bundle
    e=ai_env;inv=e['investigation'];start=utcnow()-timedelta(hours=1)
    inv.window_start=start;inv.window_end=start+timedelta(hours=1)
    e['evidence'].payload={'rows':[]};e['late'].payload={'rows':[]}
    objects=[e['a'],e['b']]
    for index in range(3):
        obj=m.Instance(system_id=e['system'].id,name='budget-'+str(index),endpoint='fixture.invalid',expected_server_uuid='budget-'+str(index))
        e['db'].add(obj);e['db'].flush();objects.append(obj)
    positive=None
    for obj in objects:
        e['db'].add(m.InvestigationResource(investigation_id=inv.id,instance_id=obj.id))
        for source in ('mysql.lock_waits','mysql.global_status','linux.host','mysql.statement_digest'):
            task=m.Task(type='collection',instance_id=obj.id);e['db'].add(task);e['db'].flush()
            run=m.CollectionRun(task_id=task.id,instance_id=obj.id,collector='runtime');e['db'].add(run);e['db'].flush()
            for minute in range(60):
                abnormal=obj.id==e['a'].id and source=='mysql.lock_waits' and minute==8
                row=m.Evidence(instance_id=obj.id,run_id=run.id,source=source,observed_at=start+timedelta(minutes=minute),coverage={'availability':'available','complete':True},payload={'rows':[{'waiting_trx_id':21,'blocking_trx_id':22}] if abnormal else []},payload_sha256='a'*64)
                e['db'].add(row)
                if abnormal: positive=row
    e['db'].flush()
    manifest=create_manifest(e['db'],'investigation',inv.id,[obj.id for obj in objects],inv.window_start,inv.window_end)
    items=list(e['db'].scalars(select(m.ManifestItem).where(m.ManifestItem.manifest_id==manifest.id,m.ManifestItem.evidence_id.is_not(None))))
    assert positive.id in {item.evidence_id for item in items}
    assert len(items)<=1000
    assert {(item.instance_id,item.payload['source']) for item in items}=={(obj.id,source) for obj in objects for source in ('mysql.lock_waits','mysql.global_status','linux.host','mysql.statement_digest')}
    bundle,*_=_bundle(e['db'],inv,manifest)
    assert positive.id in {row['evidence_id'] for row in bundle['evidence']}
    lock_rows=[row for row in bundle['evidence'] if row['instance_id']==e['a'].id and row['source']=='mysql.lock_waits']
    assert max(lock_rows,key=lambda row:row['observed_at'])['data']['row_count']==0
