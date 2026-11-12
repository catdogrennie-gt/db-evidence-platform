"""Bound actual database payload reads, not wall-clock timing in CI."""
from collections import Counter
from datetime import timedelta
import hashlib

import pytest
from sqlalchemy import event

from platform_app import models as m
from platform_app.common import iso
from platform_app.db import utcnow
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_showcase import showcase_env
from tests.platform.test_health_observation_guards import frozen_manifest


def numeric_source(e,item):
    identity=':'.join((e['source'].id,str(item['hostid']),str(item['itemid'])))
    return 'zabbix.item.numeric:'+hashlib.sha256(identity.encode()).hexdigest()[:24]


def retained_history(e):
    now = utcnow()
    items = [dict(hostid='101', itemid=str(itemid), value_kind='numeric',
                  value_semantics='gauge', interval=5, interval_verified=True,
                  history_semantics='periodic_samples', name='Observed metric')
             for itemid in (41, 42, 43)]
    e['mapping'].items = items
    for item in items:
        for age in range(40):
            stamp = now - timedelta(seconds=age * 5 + 1)
            payload = {'item': item, 'rows': [{'time': iso(stamp), 'value': int(item['itemid']) + age,
                                               'validity': 'valid'}]}
            e['db'].add(m.Evidence(instance_id=e['a'].id, run_id=e['evidence'].run_id,
                source=numeric_source(e,item), source_mode='live',
                observed_at=stamp, payload=payload, payload_sha256='a'*64,
                coverage={'availability': 'available', 'source_id': e['source'].id,
                          'mapping_version': e['mapping'].version,
                          'config_revision': e['a'].config_revision}))
    e['config'].showcase['projection_mode'] = 'numeric'
    e['db'].commit()
    return now


def load_counts(db):
    counts = Counter()
    def loaded(session, instance):
        counts[type(instance).__name__] += 1
    event.listen(db, 'loaded_as_persistent', loaded)
    return counts


def dispatch_history(e, count=16):
    now=utcnow()
    rows=[]
    for index in range(count):
        task=m.Task(type='investigation',queue='ai',actor_id=e['actor'].user_id,
                    instance_id=e['a'].id,status='completed',created_at=now-timedelta(seconds=index))
        e['db'].add(task);e['db'].flush()
        inv=m.Investigation(created_by=e['actor'].user_id,task_id=task.id,question='Actual history',
                            window_start=now-timedelta(minutes=5),window_end=now,
                            permission_version=1,status='completed')
        e['db'].add(inv);e['db'].flush()
        e['db'].add(m.InvestigationResource(investigation_id=inv.id,instance_id=e['a'].id))
        dispatch=m.InspectionDispatch(policy_id=e['policy'].id,policy_version=e['policy'].version,
                    dispatch_key='history-'+str(index),trigger='scheduled',instance_ids=[e['a'].id],
                    investigation_id=inv.id,task_id=task.id,status='completed',
                    created_at=now-timedelta(seconds=index))
        e['db'].add(dispatch);rows.append((task,inv,dispatch))
    e['db'].commit()
    return rows


def test_inspection_status_reads_bounded_metadata_without_loading_old_report_payloads(showcase_env):
    from platform_app.showcase import inspection_projection
    e=showcase_env
    rows=dispatch_history(e)
    expected=rows[0][0].id
    policy_id=e['policy'].id;instance_id=e['a'].id
    e['db'].expunge_all()
    counts=load_counts(e['db']);statements=[]
    def query(conn,cursor,statement,parameters,context,executemany): statements.append(statement)
    event.listen(e['db'].bind,'before_cursor_execute',query)
    try:
        value=inspection_projection(e['db'],{instance_id},policy=e['db'].get(m.InspectionPolicy,policy_id))
    finally:
        event.remove(e['db'].bind,'before_cursor_execute',query)
    assert value['task']['id']==expected
    assert counts['Investigation']==0, 'Status polling must not deserialize retained AI reports'
    assert counts['Task']==0, 'Status polling needs a few task fields, not historical payloads'
    assert len(statements)<=12, 'Dispatch history must use bounded bulk reads, not per-row queries'


def test_inspection_status_uses_active_task_but_rejects_hidden_report_resources(showcase_env):
    from platform_app.showcase import inspection_projection
    e=showcase_env
    rows=dispatch_history(e,5)
    rows[1][0].status='running';rows[1][1].status='analyzing'
    e['db'].add(m.InvestigationResource(investigation_id=rows[1][1].id,instance_id=e['b'].id))
    rows[3][0].status='running';rows[3][1].status='analyzing'
    e['db'].commit()
    value=inspection_projection(e['db'],{e['a'].id},policy=e['policy'])
    assert value['task']['id']==rows[3][0].id
    assert value['status']=='analyzing'


def test_snapshot_reads_one_latest_payload_per_selected_item(showcase_env):
    from platform_app.showcase import snapshot
    e = showcase_env
    retained_history(e)
    e['db'].expunge_all()
    counts = load_counts(e['db'])
    value = snapshot(e['db'], e['config'])
    assert {metric['itemid']: metric['value'] for metric in value['observation']['metrics']} == {
        '41': 41, '42': 42, '43': 43}
    assert counts['Evidence'] <= 3, 'Polling must not deserialize every retained sample payload'


def test_snapshot_only_loads_displayed_frozen_windows_and_avoids_per_chart_reauthorization(showcase_env):
    from platform_app.showcase import snapshot
    e = showcase_env
    retained_history(e)
    manifest = frozen_manifest(e)
    inv = e['investigation']
    inv.status = 'completed'
    inv.report = {'executive_summary': {'headline': 'Actual historical report'}}
    for index in range(32):
        payload = {'template': 'zabbix_item_numeric', 'owner_collector': 'monitoring',
                   'item': {'hostid': '101', 'itemid': str(1000 + index), 'value_kind': 'numeric',
                            'name': 'Historical metric'}, 'unit': 'ms',
                   'points': [{'time': iso(inv.window_start), 'value': index, 'validity': 'valid'}],
                   'coverage': {'complete': True}}
        e['db'].add(m.ManifestItem(id='window-' + str(index).zfill(2), manifest_id=manifest.id,
            instance_id=e['a'].id, item_type='time_series_window', payload=payload,
            payload_sha256='b'*64))
    e['db'].commit()
    e['db'].expunge_all()
    counts = load_counts(e['db'])
    value = snapshot(e['db'], e['config'])
    assert [chart['id'] for chart in value['charts']] == ['window-' + str(i).zfill(2) for i in range(6)]
    assert counts['ManifestItem'] <= 6, 'Do not deserialize windows that will not be displayed'
    assert counts['User'] <= 5, 'One authorized report must not repeat user/grant reads for each chart'


@pytest.mark.parametrize('damage', ['unavailable', 'mapping', 'instance'])
def test_latest_invalid_measurement_never_falls_back_to_an_older_fresh_value(showcase_env, damage):
    from platform_app.showcase import snapshot
    e = showcase_env
    now = retained_history(e)
    meta = e['mapping'].items[0]
    coverage = {'availability': 'available', 'source_id': e['source'].id,
                'mapping_version': e['mapping'].version, 'config_revision': e['a'].config_revision}
    if damage == 'unavailable':
        coverage['availability'] = 'unavailable'
    elif damage == 'mapping':
        coverage['mapping_version'] += 1
    instance_id = e['b'].id if damage == 'instance' else e['a'].id
    e['db'].add(m.Evidence(instance_id=instance_id, run_id=e['evidence'].run_id,
        source=numeric_source(e,meta), source_mode='live', observed_at=now,
        payload={'item': meta, 'rows': [{'time': iso(now), 'value': 999, 'validity': 'valid'}]},
        payload_sha256='c'*64, coverage=coverage))
    e['db'].commit()
    metrics = {metric['itemid']: metric for metric in snapshot(e['db'], e['config'])['observation']['metrics']}
    if damage == 'mapping':
        assert '41' not in metrics
    elif damage == 'unavailable':
        assert metrics['41']['status'] != 'fresh'
    else:
        assert metrics['41']['value'] == 41


@pytest.mark.parametrize('damage',['host','item','source','mapping','unavailable','unselected',
                                   'payload_shape','meta_shape','coverage_shape'])
def test_indexed_latest_keeps_actual_identity_and_does_not_fall_back(showcase_env,damage):
    from platform_app.numeric_evidence import latest_numeric_evidence
    e=showcase_env;now=retained_history(e)
    original=e['mapping'].items[0];meta=dict(original)
    coverage={'availability':'available','source_id':e['source'].id,
              'mapping_version':e['mapping'].version,'config_revision':e['a'].config_revision}
    if damage=='host':meta['hostid']='hidden-host'
    if damage=='item':meta['itemid']='hidden-item'
    if damage=='source':coverage['source_id']='different-source'
    if damage=='mapping':coverage['mapping_version']+=1
    if damage=='unavailable':coverage['availability']='unavailable'
    source=numeric_source(e,original)
    if damage=='unselected':source='zabbix.item.numeric:unselected'
    payload={'item':meta,'rows':[{'time':iso(now),'value':999}]}
    if damage=='payload_shape':payload=['invalid']
    if damage=='meta_shape':payload['item']=['invalid']
    if damage=='coverage_shape':coverage=['invalid']
    latest=m.Evidence(instance_id=e['a'].id,run_id=e['evidence'].run_id,source=source,
        source_mode='live',observed_at=now,payload=payload,
        payload_sha256='f'*64,coverage=coverage)
    e['db'].add(latest);e['db'].commit()
    rows=latest_numeric_evidence(e['db'],e['a'].id,e['source'].id,{('101','41')},
                                 observed_since=now-timedelta(seconds=10))
    if damage in ('host','item','source','payload_shape','meta_shape','coverage_shape'):assert rows==[]
    elif damage=='unselected':assert len(rows)==1 and rows[0].payload['rows'][-1]['value']==41
    else:assert len(rows)==1 and rows[0].id==latest.id


def test_snapshot_exposes_actual_first_and_recurrent_episode_in_same_event(showcase_env,monkeypatch):
    from platform_app import showcase
    from platform_app.health_signals import scan_group_changes
    from tests.platform.test_health_signals import numeric_evidence,series
    e=showcase_env
    e['config'].showcase['projection_mode']='numeric'
    now,points=series()
    numeric_evidence(e,points)
    [finding]=scan_group_changes(e['db'],e['policy'],now)
    first_episode=finding.calculation['episode_started_at']
    monkeypatch.setattr(showcase,'db_now',lambda db:now)
    [first]=showcase.snapshot(e['db'],e['config'])['observation']['signals']
    assert first['episode_started_at']==first_episode
    assert first['hostid']=='101' and first['itemid']=='1234'
    recovery=[{'time':iso(now+timedelta(seconds=i*5)),'value':10.,'validity':'valid'} for i in range(3)]
    numeric_evidence(e,recovery)
    assert scan_group_changes(e['db'],e['policy'],now+timedelta(seconds=15))==[]
    recurrence=[{'time':iso(now+timedelta(seconds=15+i*5)),'value':40.,'validity':'valid'} for i in range(3)]
    numeric_evidence(e,recurrence)
    [same_finding]=scan_group_changes(e['db'],e['policy'],now+timedelta(seconds=30))
    monkeypatch.setattr(showcase,'db_now',lambda db:now+timedelta(seconds=30))
    [again]=showcase.snapshot(e['db'],e['config'])['observation']['signals']
    assert again['event_id']==first['event_id']
    assert again['episode_started_at']==same_finding.calculation['episode_started_at']
    assert again['episode_started_at']!=first_episode
    assert again['observed_at']==same_finding.calculation['observed_at']
    assert again['hostid']==first['hostid'] and again['itemid']==first['itemid']
