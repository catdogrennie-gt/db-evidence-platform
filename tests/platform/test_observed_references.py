"""Keep stable measured references even before an issue exists; never health labels."""
from datetime import timedelta
import hashlib

import pytest
from sqlalchemy import select

from platform_app import models as m
from platform_app.common import iso
from platform_app.db import utcnow
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_showcase import showcase_env
from tests.platform.test_health_signals import item


def measurement(e,now,values,*,hostid='101',itemid='1234',**metadata):
    meta={**item(),'hostid':hostid,'itemid':itemid,'name':'Observed measurement',**metadata}
    e['mapping'].items=[meta]
    rows=[{'time':iso(now-timedelta(seconds=(len(values)-index)*5)),
           'value':value,'validity':'valid'} for index,value in enumerate(values)]
    identity=hashlib.sha256(':'.join((e['source'].id,hostid,itemid)).encode()).hexdigest()[:24]
    evidence=m.Evidence(instance_id=e['a'].id,run_id=e['evidence'].run_id,
        source='zabbix.item.numeric:'+identity,source_mode='live',
        observed_at=now-timedelta(seconds=5),payload={'item':meta,'rows':rows},
        payload_sha256='a'*64,coverage={'availability':'available','source_id':e['source'].id,
            'mapping_version':e['mapping'].version,'config_revision':e['a'].config_revision})
    e['db'].add(evidence);e['db'].commit()
    return evidence


def stored_references(e):
    return list(e['db'].scalars(select(m.PlatformSetting).where(
        m.PlatformSetting.key.like('observed-reference:%'))))


def test_stable_before_any_finding_survives_full_fault_window_and_return(showcase_env):
    from platform_app.health_signals import scan_group_changes
    e=showcase_env;now=utcnow()
    normal=measurement(e,now,[10.]*21)
    assert scan_group_changes(e['db'],e['policy'],now)==[]
    e['db'].commit()
    [reference]=stored_references(e)
    assert reference.value['evidence_id']==normal.id
    assert reference.value['baseline']['median']==10.
    original_value=dict(reference.value)
    # The entire current retained window is now at 40; no transition remains.
    measurement(e,now+timedelta(minutes=15),[40.]*100)
    [finding]=scan_group_changes(e['db'],e['policy'],now+timedelta(minutes=15))
    assert finding.calculation['baseline']['median']==10.
    episode=finding.calculation['episode_started_at']
    measurement(e,now+timedelta(minutes=30),[40.]*100)
    assert scan_group_changes(e['db'],e['policy'],now+timedelta(minutes=30))==[finding]
    assert stored_references(e)[0].value==original_value
    measurement(e,now+timedelta(minutes=45),[10.]*100)
    assert scan_group_changes(e['db'],e['policy'],now+timedelta(minutes=45))==[]
    assert finding.calculation['observation_state']=='returned_to_baseline'
    assert finding.calculation['episode_started_at']==episode
    assert scan_group_changes(e['db'],e['policy'],now+timedelta(minutes=45,seconds=1))==[]
    assert stored_references(e)[0].value==original_value


@pytest.mark.parametrize('change',['mapping','config','source','host','item'])
def test_reference_is_not_reused_across_identity_or_configuration(showcase_env,change):
    from platform_app.health_signals import scan_group_changes
    e=showcase_env;now=utcnow()
    measurement(e,now,[10.]*21)
    assert scan_group_changes(e['db'],e['policy'],now)==[]
    e['db'].commit()
    assert len(stored_references(e))==1
    hostid='101';itemid='1234'
    if change=='mapping':e['mapping'].version+=1
    elif change=='config':e['a'].config_revision+=1
    elif change=='host':hostid='102'
    elif change=='item':itemid='5678'
    else:
        source=m.MonitoringSource(name='Another source',url='https://monitor.invalid',
            secret_ref='unused',system_id=e['a'].system_id,status='ready')
        e['db'].add(source);e['db'].flush()
        e['mapping'].source_id=source.id;e['source']=source
    measurement(e,now+timedelta(minutes=10),[40.]*21,hostid=hostid,itemid=itemid)
    assert scan_group_changes(e['db'],e['policy'],now+timedelta(minutes=10))==[]
    e['db'].commit()
    references=stored_references(e)
    assert len(references)==2
    assert sorted(reference.value['baseline']['median'] for reference in references)==[10.,40.]
    assert all(reference.value['meaning']=='observed_reference_not_health' for reference in references)


def test_first_stable_observation_during_fault_is_not_certified_healthy(showcase_env):
    from platform_app.health_signals import scan_group_changes
    e=showcase_env;now=utcnow()
    measurement(e,now,[40.]*21)
    assert scan_group_changes(e['db'],e['policy'],now)==[]
    e['db'].commit()
    [reference]=stored_references(e)
    assert reference.value['meaning']=='observed_reference_not_health'
    measurement(e,now+timedelta(minutes=10),[5.]*21)
    [finding]=scan_group_changes(e['db'],e['policy'],now+timedelta(minutes=10))
    assert finding.calculation['direction']=='decrease'
    assert finding.calculation['baseline_basis']=='observed_reference_not_health'
    assert finding.calculation['baseline']['median']==40.


@pytest.mark.parametrize('damage',['counter','unknown','gap','future','stale','unavailable','unstable'])
def test_ineligible_measurements_do_not_create_a_reference(showcase_env,damage):
    from platform_app.health_signals import scan_group_changes
    e=showcase_env;now=utcnow()
    values=[10.]*21 if damage!='unstable' else [float(i%2*100) for i in range(21)]
    evidence=measurement(e,now,values)
    payload=dict(evidence.payload);meta=dict(payload['item']);rows=[dict(row) for row in payload['rows']]
    if damage in ('counter','unknown'):meta['value_semantics']=damage
    if damage=='gap':rows.pop(-2)
    if damage=='future':rows[-1]['time']=iso(now+timedelta(seconds=1))
    if damage=='stale':
        from platform_app.common import parse_time
        for row in rows:row['time']=iso(parse_time(row['time'])-timedelta(minutes=2))
    if damage=='unavailable':evidence.coverage={**evidence.coverage,'availability':'unavailable'}
    evidence.payload={**payload,'item':meta,'rows':rows};e['db'].commit()
    assert scan_group_changes(e['db'],e['policy'],now)==[]
    assert stored_references(e)==[]
