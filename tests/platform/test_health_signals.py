from datetime import timedelta

import pytest

from platform_app.common import iso
from platform_app.db import utcnow


def series(base=10., changed=40., *, baseline_count=18, recent_count=3):
    now = utcnow()
    values = [base] * baseline_count + [changed] * recent_count
    return now, [{'time': iso(now-timedelta(seconds=(len(values)-i)*5)),
                  'value': value, 'validity': 'valid'} for i, value in enumerate(values)]


def item(**changes):
    return dict(value_kind='numeric', value_semantics='gauge', interval=5,
                interval_verified=True, history_semantics='periodic_samples', **changes)


def test_sustained_change_keeps_actual_baseline_and_observation_times():
    from platform_app.health_signals import assess_change
    now, points = series()
    result = assess_change(item(), points, now)
    assert result['state'] == 'changed'
    assert result['baseline']['median'] == 10
    assert result['recent']['median'] == 40
    assert result['recent']['sample_count'] == 3
    assert result['calculation'] == 'median_mad_sustained_change_v1'
    assert result['diagnosis'] == 'change_only_not_root_cause'
    assert result['baseline']['to'] < result['recent']['from']


@pytest.mark.parametrize('semantics', ['unknown', 'counter', None])
def test_unknown_or_counter_semantics_never_generate_change(semantics):
    from platform_app.health_signals import assess_change
    now, points = series()
    metadata = item(); metadata['value_semantics'] = semantics
    assert assess_change(metadata, points, now)['state'] == 'not_evaluated'


@pytest.mark.parametrize('damage', ['stale', 'gap', 'null', 'future', 'conflicting_duplicate', 'sparse'])
def test_bad_history_cannot_become_anomaly_or_recovery(damage):
    from platform_app.health_signals import assess_change
    now, points = series()
    metadata = item()
    if damage == 'stale': now += timedelta(minutes=5)
    if damage == 'gap': points.pop(13)
    if damage == 'null': points[-2]['value'] = None
    if damage == 'future': points[-1]['time'] = iso(now+timedelta(seconds=5))
    if damage == 'conflicting_duplicate': points.append({**points[-1], 'value': 1})
    if damage == 'sparse': metadata['history_semantics'] = 'sparse_on_change'
    assert assess_change(metadata, points, now)['state'] == 'not_evaluated'


def test_small_history_and_one_spike_are_not_sustained_changes():
    from platform_app.health_signals import assess_change
    now, points = series(baseline_count=5)
    assert assess_change(item(), points, now)['state'] == 'not_evaluated'
    now, points = series(changed=10)
    points[-1]['value'] = 1000
    assert assess_change(item(), points, now)['state'] == 'stable'


def test_return_to_original_baseline_is_not_generic_health_claim():
    from platform_app.health_signals import assess_change
    now, points = series()
    baseline = assess_change(item(), points, now)['baseline']
    later = now+timedelta(seconds=20)
    recovery = [{'time': iso(later-timedelta(seconds=(3-i)*5)), 'value': 10} for i in range(3)]
    result = assess_change(item(), recovery, later, baseline=baseline)
    assert result['state'] == 'returned_to_baseline'
    assert result['diagnosis'] == 'change_only_not_root_cause'
    assert result['recent']['from'] > baseline['to']


def test_numeric_semantics_come_from_explicit_configuration_not_name():
    from platform_app.zabbix_items import numeric_semantics
    assert numeric_semantics({'key_':'looks_like_gauge','name':'latency','tags':[]}) == ('unknown','unverified')
    assert numeric_semantics({'tags':[{'tag':'semantics','value':'gauge'}]}) == ('gauge','zabbix_tag')
    assert numeric_semantics({'preprocessing':[{'type':'10'}]}) == ('rate','zabbix_change_per_second')
    assert numeric_semantics({'tags':[{'tag':'semantics','value':'counter'}], 'preprocessing':[{'type':'10'}]}) == ('unknown','conflicting_semantics')


def test_finding_can_name_a_metric_but_not_an_arbitrary_scenario():
    from platform_app.investigations import FindingProposal
    from pydantic import ValidationError
    raw=dict(intent='review_issue',category='correlation',instance_id='instance-a',
             dimension={'kind':'metric','value':'1234'},statement='Evidence needs review',evidence_ids=['reference'])
    assert FindingProposal.model_validate(raw).dimension.value=='1234'
    raw['dimension']['value']='connection-leak-demo'
    with pytest.raises(ValidationError): FindingProposal.model_validate(raw)


def test_invalid_time_is_unknown_not_a_failed_inspection():
    from platform_app.health_signals import assess_change
    now, points=series()
    points[0]['time']='not-a-time'
    assert assess_change(item(),points,now)['state']=='not_evaluated'


def test_numeric_compaction_retains_actual_inspection_trigger_reference():
    from platform_app.model_context import compact_initial
    from platform_app.investigations import _prune_inspection_signals
    trigger={'evidence_id':'actual-trigger','instance_id':'group-a',
             'source':'zabbix.item.numeric:identity',
             'data':{'item':{'hostid':'101','itemid':'1234'},'rows':[{'value':40}]}}
    bundle={'coverage':{},'numeric_series':{'rows':[['manifest:frozen',0,'101','1234']],
            'resources':[{'instance_id':'group-a'}]},'evidence':[trigger],
            'inspection_signals':[{'category':'baseline_change','evidence_ids':['actual-trigger'],
                'calculation':{'diagnosis':'change_only_not_root_cause'}}]}
    compact=compact_initial(bundle)
    _prune_inspection_signals(compact)
    assert compact['inspection_signals'], 'Deduplication dropped the autonomous discovery evidence'
    assert compact['evidence'][0]['evidence_id']=='actual-trigger'


from tests.platform.test_showcase import showcase_env
from tests.platform.test_ai_scope import ai_env


def numeric_evidence(e, points):
    from platform_app import models as m
    from platform_app.evidence import digest
    from platform_app.common import parse_time
    meta={**item(),'hostid':'101','itemid':'1234','name':'Request duration','units':'s'}
    e['mapping'].items=[meta]
    payload={'item':meta,'rows':points}
    from hashlib import sha256
    identity=sha256((e['source'].id+':101:1234').encode()).hexdigest()[:24]
    row=m.Evidence(instance_id=e['a'].id,run_id=e['evidence'].run_id,
        source='zabbix.item.numeric:'+identity,source_mode='live',
        observed_at=parse_time(points[-1]['time']),payload=payload,payload_sha256=digest(payload),
        coverage={'availability':'available','complete':True,'source_id':e['source'].id,
            'mapping_version':e['mapping'].version,'config_revision':e['a'].config_revision})
    e['db'].add(row);e['db'].commit()
    return row


def test_generic_selected_data_autonomously_schedules_without_a_fault_api(showcase_env):
    from platform_app.intelligence import dispatch_due
    from platform_app import models as m
    from sqlalchemy import select,func
    e=showcase_env
    now,points=series()
    numeric_evidence(e,points)
    assert len(dispatch_due(e['factory']))==1
    e['db'].expire_all()
    assert e['db'].scalar(select(func.count()).select_from(m.InspectionFinding).where(m.InspectionFinding.category=='baseline_change'))==1
    e['policy'].next_signal_at=utcnow()-timedelta(seconds=1);e['db'].commit()
    assert dispatch_due(e['factory'])==[]


def test_outdated_mapping_data_cannot_create_a_new_change(showcase_env):
    from platform_app.health_signals import scan_group_changes
    e=showcase_env
    now,points=series()
    row=numeric_evidence(e,points)
    e['mapping'].version+=1;e['db'].commit()
    assert scan_group_changes(e['db'],e['policy'],now)==[]


def test_recovery_does_not_become_a_reverse_anomaly_when_fault_dominated_window(showcase_env):
    from platform_app.health_signals import scan_group_changes
    e=showcase_env
    now,points=series(changed=400.)
    numeric_evidence(e,points)
    finding=scan_group_changes(e['db'],e['policy'],now)[0]
    extended=points+[{'time':iso(now+timedelta(seconds=i*5)),'value':400.,'validity':'valid'} for i in range(60)]
    extended+=[{'time':iso(now+timedelta(seconds=300+i*5)),'value':10.,'validity':'valid'} for i in range(6)]
    numeric_evidence(e,extended)
    assert scan_group_changes(e['db'],e['policy'],now+timedelta(seconds=330))==[]
    assert finding.calculation['observation_state']=='returned_to_baseline'
    assert scan_group_changes(e['db'],e['policy'],now+timedelta(seconds=335))==[]
    assert finding.calculation['observation_state']=='returned_to_baseline'
    assert finding.calculation['baseline']['median']==10.


def test_fresh_baseline_return_is_visible_even_with_an_existing_report(showcase_env):
    from platform_app.health_signals import scan_group_changes
    from platform_app.intelligence import issue_response
    from platform_app import models as m
    from platform_app.common import parse_time
    e=showcase_env
    now,points=series()
    for p in points:p['time']=iso(parse_time(p['time'])-timedelta(seconds=60))
    numeric_evidence(e,points)
    finding=scan_group_changes(e['db'],e['policy'],now-timedelta(seconds=60))[0]
    inv=e['investigation'];inv.status='completed';inv.report={'facts':[{'statement':'Existing historical report','evidence_ids':[e['evidence'].id]}]}
    finding.latest_investigation_id=inv.id;e['db'].commit()
    recovery=[{'time':iso(now-timedelta(seconds=(3-i)*5)),'value':10} for i in range(3)]
    numeric_evidence(e,recovery)
    assert scan_group_changes(e['db'],e['policy'],now)==[]
    event=e['db'].get(m.Event,finding.event_id)
    result=issue_response(e['db'],event,finding)
    assert result['observation_state']=='returned_to_baseline'
    assert '已回到' in result['summary'] and '不代表业务整体健康' in result['summary']


@pytest.mark.parametrize('state',['returned_to_baseline','configuration_changed'])
def test_historical_baseline_observation_is_retained_but_not_counted_current(showcase_env,state):
    from platform_app.health_signals import scan_group_changes
    from platform_app.intelligence import overview
    e=showcase_env
    now,points=series()
    numeric_evidence(e,points)
    finding=scan_group_changes(e['db'],e['policy'],now)[0]
    if state=='returned_to_baseline':
        finding.calculation={**finding.calculation,'observation_state':state}
    else:
        e['mapping'].version+=1
        e['a'].config_revision+=1
    e['db'].commit()
    value=overview(e['reader'])
    assert value['stats']['current']==0
    assert len(value['issues'])==1
    assert value['issues'][0]['observation_state']==state


def test_recurring_change_starts_new_episode_and_keeps_it_stable(showcase_env):
    from platform_app.health_signals import scan_group_changes
    e=showcase_env
    now,points=series()
    numeric_evidence(e,points)
    finding=scan_group_changes(e['db'],e['policy'],now)[0]
    old_episode=finding.calculation['episode_started_at']
    recovery=[{'time':iso(now+timedelta(seconds=i*5)),'value':10.,'validity':'valid'} for i in range(3)]
    numeric_evidence(e,recovery)
    assert scan_group_changes(e['db'],e['policy'],now+timedelta(seconds=15))==[]
    again=[{'time':iso(now+timedelta(seconds=20+i*5)),'value':40.,'validity':'valid'} for i in range(3)]
    numeric_evidence(e,again)
    assert scan_group_changes(e['db'],e['policy'],now+timedelta(seconds=35))
    new_episode=finding.calculation['episode_started_at']
    assert new_episode>old_episode
    assert 'returned_at' not in finding.calculation
    assert scan_group_changes(e['db'],e['policy'],now+timedelta(seconds=36))
    assert finding.calculation['episode_started_at']==new_episode
