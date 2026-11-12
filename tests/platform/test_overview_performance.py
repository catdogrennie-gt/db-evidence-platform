"""Overview reads bounded list metadata, never the frozen detail evidence graph."""
from datetime import timedelta

import pytest
from sqlalchemy import event as sql_event

from tests.platform.test_ai_scope import ai_env
from tests.platform.test_intelligence import policy_env
from platform_app import models as m

pytestmark = pytest.mark.usefixtures('legacy_collection_compatibility')


def setup_report(e):
    from platform_app.evidence import create_manifest
    policy_env(e)
    inv=e['investigation']
    inv.status='completed'
    inv.report={'facts':[{'statement':'Actual referenced waiting observation','evidence_ids':[e['evidence'].id]}],
                'executive_summary':{'headline':'Measured database change requires review'},
                'coverage':{'complete':False}}
    e['db'].add(m.InvestigationResource(investigation_id=inv.id,instance_id=e['a'].id))
    e['db'].flush()
    manifest=create_manifest(e['db'],'investigation',inv.id,[e['a'].id],e['start'],e['end'])
    inv.manifest_id=manifest.id
    e['db'].commit()
    return inv


def add_issue(e,inv,index,*,category='lock_wait',calculation=None):
    row=m.Event(instance_id=e['a'].id,title=f'Observed change {index}',last_seen_at=e['start'])
    e['db'].add(row);e['db'].flush()
    finding=m.InspectionFinding(instance_id=e['a'].id,event_id=row.id,
        category=category,dimension=f'item:{index}',fingerprint=f'{index:064x}',kind='current',
        evidence_ids=[e['evidence'].id],calculation=calculation or {},latest_investigation_id=inv.id)
    e['db'].add(finding)
    return row,finding


def capture(e,call):
    statements=[]
    def record(conn,cursor,statement,parameters,context,executemany):
        statements.append(statement.lower())
    sql_event.listen(e['engine'],'before_cursor_execute',record)
    try: return call(),statements
    finally: sql_event.remove(e['engine'],'before_cursor_execute',record)


def test_overview_does_not_load_frozen_payloads_or_build_detail_facts(ai_env,monkeypatch):
    import platform_app.intelligence as intelligence
    e=ai_env;inv=setup_report(e)
    row,_=add_issue(e,inv,1);e['db'].commit();e['db'].expire_all()
    def detail_forbidden(*args,**kwargs):
        pytest.fail('A list request must not inspect report manifest/evidence details')
    monkeypatch.setattr(intelligence,'_related_report_fact',detail_forbidden)
    result,statements=capture(e,lambda:intelligence.overview(e['actor']))
    [issue]=result['issues']
    assert issue['event_id']==row.id and issue['investigation_id']==inv.id
    assert issue['report_status']=='completed'
    assert 'Measured database change requires review' in issue['summary']
    assert not any('manifest_items' in statement for statement in statements)
    assert not any('evidence.payload' in statement for statement in statements)


def test_overview_batch_query_count_does_not_grow_with_each_event(ai_env):
    from platform_app.intelligence import overview
    e=ai_env;inv=setup_report(e)
    for index in range(1,33): add_issue(e,inv,index)
    e['db'].commit();e['db'].expire_all()
    result,statements=capture(e,lambda:overview(e['actor']))
    assert len(result['issues'])==32
    assert len(statements)<=40, f'{len(statements)} queries for 32 overview cards'
    assert all(issue['investigation_id']==inv.id for issue in result['issues'])
    assert all('Measured database change requires review' in issue['summary'] for issue in result['issues'])
    assert not any('manifest_items' in statement for statement in statements)


def test_overview_hides_the_entire_report_when_any_frozen_resource_is_unauthorized(ai_env):
    from platform_app.intelligence import overview
    e=ai_env;inv=setup_report(e)
    row,_=add_issue(e,inv,1)
    e['db'].add(m.InvestigationResource(investigation_id=inv.id,instance_id=e['b'].id))
    inv.report={'executive_summary':{'headline':'PRIVATE OTHER SCOPE'}}
    e['db'].commit();e['db'].expire_all()
    [issue]=overview(e['actor'])['issues']
    assert issue['event_id']==row.id
    assert issue['investigation_id'] is None and issue['report_status']=='not_started'
    assert 'PRIVATE OTHER SCOPE' not in str(issue)


def test_overview_mixed_scope_model_statement_is_hidden_with_its_report(ai_env):
    from platform_app.intelligence import overview
    e=ai_env;inv=setup_report(e)
    add_issue(e,inv,1,category='evidence_correlation',calculation={'statement':'PRIVATE CROSS SCOPE MODEL STATEMENT','family':'sql'})
    e['db'].add(m.InvestigationResource(investigation_id=inv.id,instance_id=e['b'].id))
    e['db'].commit();e['db'].expire_all()
    [issue]=overview(e['actor'])['issues']
    assert issue['investigation_id'] is None
    assert 'PRIVATE CROSS SCOPE' not in str(issue)
    assert '等待证据分析' in issue['summary']


@pytest.mark.parametrize('scope',[{'instance_id':'not-authorized'}, {'system_id':'another-system'},
                                 {'department_id':'another-department'}, {'environment':'another-environment'}])
def test_overview_explicit_scope_never_retains_another_scope_summary(ai_env,scope):
    from platform_app.intelligence import overview
    e=ai_env;inv=setup_report(e)
    add_issue(e,inv,1);e['db'].commit()
    result=overview(e['actor'],**scope)
    assert result['issues']==[] and result['stats']=={'current':0,'risk':0,'objects':0}
    assert 'Measured database change' not in str(result)


def test_overview_scope_filter_does_not_revoke_an_otherwise_authorized_joint_report(ai_env):
    from platform_app.intelligence import overview
    e=ai_env;inv=setup_report(e)
    add_issue(e,inv,1)
    e['db'].add_all([m.UserGrant(user_id=e['actor'].user_id,role_id=e['grant'].role_id,
        scope_type='instance',scope_id=e['b'].id),m.InvestigationResource(investigation_id=inv.id,instance_id=e['b'].id)])
    other=m.Event(instance_id=e['b'].id,title='Other authorized scope')
    e['db'].add(other);e['db'].commit();e['db'].expire_all()
    [issue]=overview(e['actor'],instance_id=e['a'].id)['issues']
    assert issue['instance_ids']==[e['a'].id]
    assert issue['investigation_id']==inv.id and 'Measured database change' in issue['summary']


def test_overview_does_not_attach_an_unrelated_report_even_when_both_scopes_are_readable(ai_env):
    from platform_app.intelligence import overview
    e=ai_env;inv=setup_report(e)
    add_issue(e,inv,1)
    resource=e['db'].get(m.InvestigationResource,(inv.id,e['a'].id));e['db'].delete(resource)
    e['db'].add_all([m.UserGrant(user_id=e['actor'].user_id,role_id=e['grant'].role_id,
        scope_type='instance',scope_id=e['b'].id),m.InvestigationResource(investigation_id=inv.id,instance_id=e['b'].id)])
    e['db'].commit();e['db'].expire_all()
    [issue]=overview(e['actor'],instance_id=e['a'].id)['issues']
    assert issue['investigation_id'] is None
    assert 'Measured database change' not in issue['summary']


def test_overview_rechecks_revoked_grants_instead_of_trusting_an_old_actor(ai_env):
    from fastapi import HTTPException
    from platform_app.intelligence import overview
    e=ai_env;inv=setup_report(e);add_issue(e,inv,1);e['db'].commit()
    e['db'].delete(e['grant']);e['db'].commit()
    with pytest.raises(HTTPException) as error: overview(e['actor'])
    assert error.value.status_code==403


def test_overview_intersects_event_grants_without_hiding_readable_objects(ai_env):
    from platform_app.intelligence import overview
    e=ai_env;inv=setup_report(e);add_issue(e,inv,1)
    read_role=m.Role(name='data-without-event',permissions=['data.read'])
    e['db'].add(read_role);e['db'].flush()
    e['db'].add(m.UserGrant(user_id=e['actor'].user_id,role_id=read_role.id,scope_type='instance',scope_id=e['b'].id))
    e['b'].enabled=True;e['b'].lifecycle_state='Active'
    e['db'].add(m.Event(instance_id=e['b'].id,title='PRIVATE EVENT WITHOUT EVENT GRANT'))
    e['db'].commit();e['db'].expire_all()
    result=overview(e['actor'])
    assert result['stats']['objects']==2
    assert len(result['issues'])==1
    assert 'PRIVATE EVENT' not in str(result)


def test_overview_resolves_the_latest_event_report_without_a_finding_pointer(ai_env):
    from platform_app.intelligence import overview
    e=ai_env;inv=setup_report(e)
    row=m.Event(instance_id=e['a'].id,title='Existing event without inspection finding',last_seen_at=e['start'])
    e['db'].add(row);e['db'].flush();inv.event_id=row.id;e['db'].commit();e['db'].expire_all()
    [issue]=overview(e['actor'])['issues']
    assert issue['investigation_id']==inv.id
    assert 'Measured database change' in issue['summary']


@pytest.mark.parametrize('status,valid_time',[('firing',True),('recovered',True),('firing',False)])
def test_overview_alert_freshness_uses_target_sample_times_not_alert_wall_clock(ai_env,status,valid_time):
    from platform_app.common import iso
    from platform_app.intelligence import overview
    e=ai_env;inv=setup_report(e);row,finding=add_issue(e,inv,1);finding.evidence_ids=[]
    rule=m.Rule(name='Actual samples',rule_version='sample-v1',template='locks_present')
    active=m.ActiveAlert(instance_id=e['a'].id,rule_version='sample-v1',dimension_fingerprint='a'*64)
    e['db'].add_all([rule,active]);e['db'].flush()
    evaluation=m.RuleEvaluation(instance_id=e['a'].id,rule_id=rule.id,rule_version=rule.rule_version,
        evaluation_key='fixture-observation',state='firing')
    e['db'].add(evaluation);e['db'].flush()
    stamp=iso(e['end']+timedelta(seconds=2)) if valid_time else 'not-a-timestamp'
    occurrence=m.AlertOccurrence(instance_id=e['a'].id,active_alert_id=active.id,evaluation_id=evaluation.id,
        status=status,last_seen_at=e['end']+timedelta(minutes=5),evidence={'latest':{
            'source_observed_at':stamp if status=='firing' else iso(e['start']),
            'recovery_observed_at':stamp if status=='recovered' else None}})
    e['db'].add(occurrence);e['db'].flush()
    e['db'].add(m.EventAlert(event_id=row.id,alert_occurrence_id=occurrence.id))
    e['db'].commit();e['db'].expire_all()
    [issue]=overview(e['actor'])['issues']
    assert issue['report_freshness']['status']==('outdated' if valid_time else 'current')


def test_overview_new_target_observation_marks_a_retained_report_outdated(ai_env):
    from platform_app.intelligence import overview
    e=ai_env;inv=setup_report(e)
    row,finding=add_issue(e,inv,1)
    e['evidence'].observed_at=e['end']+timedelta(seconds=1)
    e['db'].commit();e['db'].expire_all()
    [issue]=overview(e['actor'])['issues']
    assert issue['investigation_id']==inv.id
    assert issue['report_freshness']['status']=='outdated'
    assert '未覆盖更新观测' in issue['summary']
    assert 'Measured database change requires review' not in issue['summary']


def test_overview_model_registration_time_is_not_a_new_target_observation(ai_env):
    from platform_app.intelligence import overview
    e=ai_env;inv=setup_report(e)
    row,finding=add_issue(e,inv,1,category='evidence_correlation',calculation={'statement':'Candidate mechanism only','family':'sql'})
    row.last_seen_at=e['end']+timedelta(minutes=1)
    finding.evidence_ids=[]
    e['db'].commit();e['db'].expire_all()
    [issue]=overview(e['actor'])['issues']
    assert issue['report_freshness']['status']=='current'
    assert issue['finding_status']=='hypothesis'
    assert 'Candidate mechanism only' in issue['summary']
