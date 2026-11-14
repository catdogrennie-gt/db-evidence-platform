"""List/detail/export retain raw AI, never presenting its labels as verified facts."""
from copy import deepcopy
from datetime import timedelta
import pytest

from platform_app import models as m
from platform_app.db import utcnow
from platform_app.common import iso
from platform_app.exports import handle_export
from platform_app.scheduling import claim
from tests.platform.test_task_scope import graph,export_body
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_overview_performance import setup_report,add_issue,capture


def legacy_report(g, *, conclusion_type='live_model', status='completed'):
    now=utcnow()-timedelta(minutes=1)
    raw={'executive_summary':{'headline':'已确认自行恢复','conclusion':{'status':'observed','statement':'模型自称已确认'}},
         'report_presentation':{'primary_basis':'verified','primary_headline':'伪造的服务器结论'}}
    row=m.Investigation(created_by=g.user.id,question='legacy report',permission_version=1,
        window_start=now-timedelta(minutes=5),window_end=now,status=status,
        conclusion_type=conclusion_type,report=deepcopy(raw),created_at=now)
    g.db.add(row);g.db.flush()
    g.db.add(m.InvestigationResource(investigation_id=row.id,instance_id=g.a.id));g.db.commit()
    return row,raw


def test_actual_list_is_light_and_labels_model_narrative_without_inventing_verified_facts(graph,monkeypatch):
    from platform_app.routes.investigations import router
    import platform_app.report_grounding as grounding
    g=graph;row,raw=legacy_report(g)
    g.client.app.include_router(router,prefix='/api/v1')
    def forbidden(*_args,**_kwargs):raise AssertionError('List must not rebuild full grounding')
    monkeypatch.setattr(grounding,'report_grounding',forbidden)
    response=g.client.get('/api/v1/investigations')
    assert response.status_code==200,response.text
    [item]=response.json()['items'];presentation=item['report_presentation']
    assert presentation['version']=='report-presentation-v1'
    assert presentation['primary_basis']=='analysis_record'
    assert presentation['primary_headline']=='历史分析记录'
    assert presentation['fact_assessment']=='not_evaluated'
    assert presentation['model_narrative_verification']=='unverified'
    assert item['report']==raw==row.report
    assert 'report_grounding' not in item


def test_actual_export_includes_same_authority_envelope_and_unchanged_legacy_raw_report(graph):
    g=graph;row,raw=legacy_report(g)
    identifier=g.client.post('/api/v1/exports',json=export_body(g,'investigations')).json()['id']
    handle_export(g.factory,claim(g.factory,'presentation-export-worker',queue='export'),g.config)
    response=g.client.get(f'/api/v1/exports/{identifier}/download')
    assert response.status_code==200,response.text
    [item]=response.json()['items']
    assert item['report']==raw==row.report
    assert item['report_grounding']['status']=='unverified'
    assert item['report_presentation']['primary_basis']=='analysis_record'
    assert item['report_presentation']['fact_assessment']=='unverified'
    assert item['report_presentation']['model_narrative_verification']=='unverified'
    assert 'private_audit_ref' not in response.text


@pytest.mark.usefixtures('legacy_collection_compatibility')
def test_actual_overview_keeps_bounded_queries_and_marks_unverified_model_fallback(ai_env):
    from platform_app.intelligence import overview
    e=ai_env;inv=setup_report(e);inv.conclusion_type='live_model'
    event,_=add_issue(e,inv,1);e['db'].commit();e['db'].expire_all()
    result,statements=capture(e,lambda:overview(e['actor']))
    [issue]=result['issues']
    assert issue['event_id']==event.id
    assert issue['summary'].startswith('AI 未核实解释：')
    assert issue['report_presentation']['fact_assessment']=='not_evaluated'
    assert issue['report_presentation']['primary_basis']=='analysis_record'
    assert len(statements)<=40
    assert not any('manifest_items' in query or 'evidence.payload' in query for query in statements)


def test_rule_export_does_not_claim_that_deterministic_result_is_unverified_model_output(graph):
    from platform_app.investigations import investigation_detail
    g=graph;row,raw=legacy_report(g,conclusion_type='rule')
    detail=investigation_detail(g.actor,row,include_audit=False)
    p=detail['report_presentation']
    assert p['primary_basis']=='rule_result'
    assert p['model_narrative_verification']=='not_applicable'
    assert p['primary_headline']=='规则巡检结果'
    assert detail['report']==raw


@pytest.mark.usefixtures('legacy_collection_compatibility')
def test_detail_model_fact_cannot_appear_as_a_verified_unlabelled_summary(ai_env):
    from platform_app.intelligence import issue_detail
    e=ai_env;inv=setup_report(e);inv.conclusion_type='live_model'
    event,_=add_issue(e,inv,1);e['db'].commit()
    result=issue_detail(e['actor'],event.id)
    assert result['summary'].startswith('AI 未核实解释：')
    assert result['report_presentation']['model_narrative_verification']=='unverified'


def test_real_qualified_export_reuses_worker_config_and_preserves_point_authority(ai_env):
    import json
    from platform_app.db import create_session_factory
    from platform_app.exports import enqueue_export,export_path
    from tests.platform.test_report_grounding import prepare_case
    e=prepare_case(ai_env)
    role=e['db'].get(m.Role,e['grant'].role_id)
    role.permissions=[*role.permissions,'export.create'];e['db'].commit()
    task=enqueue_export(e['actor'],{'instance_ids':[e['a'].id],'resource':'investigations',
        'format':'json','from':iso(e['result'].created_at-timedelta(seconds=1)),
        'to':iso(utcnow()+timedelta(seconds=1))})
    e['db'].commit()
    # Match the real worker, whose session factory carries private config.
    factory=create_session_factory(e['engine'],e['config'])
    handle_export(factory,claim(factory,'qualified-export-worker',queue='export'),e['config'])
    [item]=json.loads(export_path(e['config'],task.id).read_bytes())['items']
    assert item['report_grounding']['status']=='point_facts_only'
    assert item['report_presentation']['primary_basis']=='historical_source_points'
    assert item['report_presentation']['model_narrative_verification']=='unverified'
    assert item['report']==e['result'].report
