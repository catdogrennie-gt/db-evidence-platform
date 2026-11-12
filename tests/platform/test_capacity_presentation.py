"""A breached configured waterline is a factual risk, not a forecast or outage."""
from platform_app import models as m
from platform_app.common import iso
from platform_app.db import utcnow
from platform_app.intelligence import upsert_finding, issue_response
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_showcase import showcase_env


def capacity(e):
    now=utcnow()
    e['mapping'].items=[{'hostid':'101','itemid':'1234','name':'数据库连接数'},
                        {'hostid':'101','itemid':'1235','name':'连接容量'}]
    calc=dict(used=34,limit=40,utilization_percent=85,remaining=6,warning_percent=80,
              resource_group='mysql_connections',hostid='101',used_itemid='1234',limit_itemid='1235',
              source_id=e['source'].id,mapping_version=e['mapping'].version,
              config_revision=e['a'].config_revision,observed_at=iso(now),
              episode_started_at=iso(now),observation_state='active',
              classification='configured_threshold_risk_not_root_cause')
    finding=upsert_finding(e['db'],e['a'].id,'capacity_pressure','capacity:test',[],calc,kind='risk',now=now)
    e['db'].flush()
    return finding,e['db'].get(m.Event,finding.event_id)


def test_known_capacity_breach_has_specific_problem_numbers_not_a_forecast(showcase_env):
    e=showcase_env; finding,event=capacity(e)
    result=issue_response(e['db'],event,finding)
    assert result['title']=='数据库连接数容量余量不足'
    for value in ['34 / 40','85%','6','80%']:
        assert value in result['summary']
    assert result['presentation_kind']=='issue'
    assert result['kind']=='risk' and result['finding_status']=='observed'
    assert '宕机' not in result['summary'] and '预测' not in result['summary']


def test_changed_capacity_selection_cannot_claim_a_current_problem(showcase_env):
    e=showcase_env; finding,event=capacity(e)
    e['mapping'].items=e['mapping'].items[:1]
    result=issue_response(e['db'],event,finding)
    assert result['presentation_kind']=='history'
    assert result['observation_state']=='configuration_changed'
    assert '85%' not in result['summary']


def test_fresh_recovery_keeps_real_problem_history_without_claiming_overall_health(showcase_env):
    e=showcase_env; finding,event=capacity(e)
    finding.calculation={**finding.calculation,'observation_state':'recovered'}
    result=issue_response(e['db'],event,finding)
    assert result['presentation_kind']=='history'
    assert '不代表整体健康' in result['summary']


def test_old_model_window_does_not_hide_new_observed_capacity_numbers(showcase_env):
    from datetime import timedelta
    e=showcase_env; finding,event=capacity(e)
    inv=e['investigation'];inv.status='completed';inv.window_end=utcnow()-timedelta(minutes=2)
    finding.latest_investigation_id=inv.id
    result=issue_response(e['db'],event,finding)
    assert result['report_freshness']['status']=='outdated'
    assert '34 / 40' in result['summary'] and '85%' in result['summary']


def test_model_capacity_review_reuses_one_issue_only_with_both_exact_operands(showcase_env):
    from datetime import timedelta
    from types import SimpleNamespace
    from platform_app.evidence import digest
    from platform_app.intelligence import _matching_observed_finding
    from platform_app.investigations import FindingProposal
    e=showcase_env; finding,_=capacity(e); refs=[]
    for itemid in ('1234','1235'):
        payload={'item':{'hostid':'101','itemid':itemid}}
        evidence=m.Evidence(instance_id=e['a'].id,run_id=e['evidence'].run_id,
            source='zabbix.item.numeric:test'+itemid,source_mode='live',observed_at=utcnow(),
            payload=payload,payload_sha256=digest(payload),coverage={'source_id':e['source'].id})
        e['db'].add(evidence);e['db'].flush()
        refs.append(SimpleNamespace(evidence_id=evidence.id))
    proposal=FindingProposal.model_validate(dict(intent='review_issue',category='correlation',
        instance_id=e['a'].id,dimension={'kind':'instance','value':e['a'].id},
        statement='容量余量需要复核',evidence_ids=[item.evidence_id for item in refs]))
    assert _matching_observed_finding(e['db'],proposal,refs[:1]) is None
    assert _matching_observed_finding(e['db'],proposal,refs) is finding
    baseline=upsert_finding(e['db'],e['a'].id,'baseline_change','metric:101:1234',
        [refs[0].evidence_id],{**finding.calculation,'itemid':'1234'})
    baseline.updated_at=utcnow()+timedelta(seconds=1);e['db'].flush()
    proposal.dimension.kind='metric';proposal.dimension.value='1234'
    assert _matching_observed_finding(e['db'],proposal,refs) is finding
    wrong=e['db'].get(m.Evidence,refs[1].evidence_id)
    wrong.payload={'item':{'hostid':'another-host','itemid':'1235'}}
    assert _matching_observed_finding(e['db'],proposal,refs) is baseline
