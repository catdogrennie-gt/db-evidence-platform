"""Configured capacity facts use real selected identities, never metric names."""
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256

import pytest
from sqlalchemy import func, select

from platform_app import models as m
from platform_app.common import iso, parse_time
from platform_app.db import utcnow
from platform_app.evidence import digest
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_showcase import showcase_env


def metadata(itemid, role, *, hostid='101', group='connection-budget', warning='80'):
    tags = [{'tag': 'sl.capacity.group', 'value': group},
            {'tag': 'sl.capacity.role', 'value': role}]
    if role == 'limit' and warning is not None:
        tags.append({'tag': 'sl.capacity.warning_pct', 'value': warning})
    return dict(hostid=hostid, itemid=itemid, name='Selected measurement '+itemid,
                value_kind='numeric', value_semantics='gauge', interval=5,
                interval_verified=True, history_semantics='periodic_samples',
                units='', tags=tags)


def points(now, values=(34, 34, 34), *, shift=0):
    return [dict(time=iso(now-timedelta(seconds=(2-i)*5)+timedelta(seconds=shift)),
                 value=value, validity='valid') for i, value in enumerate(values)]


def pair(now, used=(34, 34, 34), limit=(40, 40, 40)):
    return metadata('501', 'used'), points(now, used), metadata('502', 'limit'), points(now, limit)


def assess(now, *args, warning=80):
    from platform_app.capacity_signals import assess_capacity
    return assess_capacity(*args, now, warning_percent=warning)


def write(e, meta, rows, *, availability='available', source_id=None, coverage=None,
          observed_at=None):
    source_id = source_id or e['source'].id
    source = 'zabbix.item.numeric:'+sha256(':'.join((source_id, meta['hostid'], meta['itemid'])).encode()).hexdigest()[:24]
    payload = {'item': deepcopy(meta), 'rows': deepcopy(rows)}
    provenance = dict(availability=availability, complete=True, source_id=source_id,
                      hostid=meta['hostid'], itemid=meta['itemid'],
                      mapping_version=e['mapping'].version, config_revision=e['a'].config_revision)
    provenance.update(coverage or {})
    row = m.Evidence(instance_id=e['a'].id, run_id=e['evidence'].run_id,
                     source=source, source_mode='live',
                     observed_at=observed_at or parse_time(rows[-1]['time']),
                     payload=payload, payload_sha256=digest(payload), coverage=provenance)
    e['db'].add(row); e['db'].flush()
    return row


def configure(e, now, used=(34, 34, 34), limit=(40, 40, 40), *, group='connection-budget', warning='80'):
    used_meta = metadata('501', 'used', group=group)
    limit_meta = metadata('502', 'limit', group=group, warning=warning)
    # Current collection metadata is authoritative; selection need not be refreshed for tags.
    e['mapping'].items = [{k: v for k, v in item.items() if k != 'tags'}
                          for item in (used_meta, limit_meta)]
    a = write(e, used_meta, points(now, used))
    b = write(e, limit_meta, points(now, limit))
    return used_meta, limit_meta, a, b


def scan(e, now):
    from platform_app.capacity_signals import scan_group_capacity
    return scan_group_capacity(e['db'], e['policy'], now)


def test_three_configured_pairs_prove_capacity_risk_below_hard_limit():
    now = utcnow()
    result = assess(now, *pair(now, (32, 33, 34)))
    assert result['state'] == 'active'
    assert (result['used'], result['limit'], result['utilization_percent'], result['remaining']) == (34, 40, 85, 6)
    assert result['warning_percent'] == 80 and result['sample_count'] == 3
    assert result['min_utilization_percent'] == 80 and result['max_utilization_percent'] == 85
    assert len(result['time_pairs']) == 3
    assert result['calculation'] == 'configured_capacity_ratio_v1'
    assert result['classification'] == 'configured_threshold_risk_not_root_cause'
    assert result['time_pairs'][-1]['used_at'] == iso(now)


@pytest.mark.parametrize('values,state', [((31,31,31),'recovered'), ((32,32,32),'active'),
                                       ((31,32,34),'not_evaluated'), ((34,34,31),'not_evaluated')])
def test_three_consecutive_pairs_required_for_activation_and_recovery(values, state):
    now=utcnow()
    assert assess(now, *pair(now, values))['state'] == state


@pytest.mark.parametrize('damage', ['counter','rate','unverified','sparse','unit','invalid',
    'negative','zero_limit','nonfinite','boolean','future','stale','two_points',
    'conflicting_duplicate','skew','interval_cap','threshold_missing','threshold_nan'])
def test_unknown_inputs_never_claim_risk_or_recovery(damage):
    now=utcnow(); used, rows, limit, capacities = pair(now)
    warning=80
    if damage in ('counter','rate'): used['value_semantics']=damage
    if damage=='unverified': used['interval_verified']=False
    if damage=='sparse': limit['history_semantics']='sparse_on_change'
    if damage=='unit': limit['units']='bytes'
    if damage=='invalid': rows[-1]['validity']='invalid_value'
    if damage=='negative': rows[-1]['value']=-1
    if damage=='zero_limit': capacities[-1]['value']=0
    if damage=='nonfinite': rows[-1]['value']=float('inf')
    if damage=='boolean': rows[-1]['value']=True
    if damage=='future': rows[-1]['time']=iso(now+timedelta(seconds=1))
    if damage=='stale': now+=timedelta(seconds=21)
    if damage=='two_points': rows.pop(0)
    if damage=='conflicting_duplicate': rows.append({**rows[-1], 'value':35})
    if damage=='skew': capacities=points(now, (40,40,40), shift=-6)
    if damage=='interval_cap':
        used['interval']=limit['interval']=60
        rows=points(now, (34,34,34), shift=-31)
    if damage=='threshold_missing': warning=None
    if damage=='threshold_nan': warning=float('nan')
    assert assess(now, used, rows, limit, capacities, warning=warning)['state']=='not_evaluated'


def test_latest_three_pairs_not_old_history_and_input_is_immutable():
    now=utcnow(); used, rows, limit, capacities=pair(now)
    rows=[dict(time=iso(now-timedelta(minutes=3)), value=None, validity='invalid_value')]+rows
    before=deepcopy((used, rows, limit, capacities))
    assert assess(now, used, rows, limit, capacities)['state']=='active'
    assert before==(used, rows, limit, capacities)


def test_scan_uses_latest_tags_and_exact_refs_with_stable_episode(showcase_env):
    e=showcase_env; now=utcnow()
    used, limit, a, b=configure(e, now)
    finding=scan(e,now)[0]
    original=deepcopy(a.payload)
    assert finding.kind=='risk' and finding.category=='capacity_pressure'
    assert set(finding.evidence_ids)=={a.id,b.id}
    calc=finding.calculation
    assert calc['source_id']==e['source'].id and calc['hostid']=='101'
    assert calc['used_itemid']=='501' and calc['limit_itemid']=='502'
    assert calc['mapping_version']==e['mapping'].version
    assert calc['config_revision']==e['a'].config_revision
    assert calc['resource_group']=='connection-budget' and calc['observation_state']=='active'
    assert (calc['used'],calc['limit'],calc['remaining'])==(34,40,6)
    old_hash=finding.signal_hash; first=calc['episode_started_at']
    later=now+timedelta(seconds=15)
    a2=write(e,used,points(later,(35,35,35))); b2=write(e,limit,points(later,(40,40,40)))
    assert scan(e,later)[0].id==finding.id
    assert finding.signal_hash==old_hash and finding.calculation['episode_started_at']==first
    assert set(finding.evidence_ids)=={a2.id,b2.id} and a.payload==original
    assert e['db'].scalar(select(func.count()).select_from(m.Event))==1


def test_unknown_latest_does_not_fall_back_or_recover_and_recurrence_is_new_episode(showcase_env):
    e=showcase_env; now=utcnow(); used,limit,_,_=configure(e,now)
    finding=scan(e,now)[0]; first=finding.signal_hash
    later=now+timedelta(seconds=15)
    write(e,used,points(later,(9,9,9)),availability='unavailable')
    write(e,limit,points(later,(40,40,40)))
    assert scan(e,later)==[] and finding.calculation['observation_state']=='active'
    assert finding.signal_hash==first
    later+=timedelta(seconds=15)
    a=write(e,used,points(later,(9,9,9))); b=write(e,limit,points(later,(40,40,40)))
    assert scan(e,later)==[] and finding.calculation['observation_state']=='recovered'
    assert set(finding.evidence_ids)=={a.id,b.id}
    later+=timedelta(seconds=15)
    write(e,used,points(later));write(e,limit,points(later,(40,40,40)))
    assert scan(e,later)[0].id==finding.id and finding.signal_hash!=first
    assert finding.calculation['episode_started_at']>iso(now)


@pytest.mark.parametrize('damage', ['cross_host','cross_source','not_selected','mapping_revision',
    'config_revision','missing_threshold','duplicate_role','duplicate_tag','invalid_group',
    'coverage_gap','payload_identity'])
def test_scanner_refuses_ambiguous_or_wrong_current_identities(showcase_env, damage):
    e=showcase_env; now=utcnow(); used,limit,a,b=configure(e,now)
    if damage=='cross_host':
        limit['hostid']='102';e['mapping'].items=[used,limit];write(e,limit,points(now,(40,40,40)))
    if damage=='cross_source':
        b.coverage={**b.coverage,'source_id':'other-source'}
    if damage=='not_selected': e['mapping'].items=[used]
    if damage=='mapping_revision': e['mapping'].version+=1
    if damage=='config_revision': e['a'].config_revision+=1
    if damage=='missing_threshold': b.payload={**b.payload,'item':metadata('502','limit',warning=None)}
    if damage=='duplicate_role':
        extra=metadata('503','used');e['mapping'].items=[used,limit,extra];write(e,extra,points(now))
    if damage=='duplicate_tag':
        changed=deepcopy(limit);changed['tags'].append({'tag':'sl.capacity.role','value':'used'})
        b.payload={**b.payload,'item':changed}
    if damage=='invalid_group':
        b.payload={**b.payload,'item':metadata('502','limit',group='x'*41)}
    if damage=='coverage_gap': b.coverage={**b.coverage,'reason_codes':['invalid_value']}
    if damage=='payload_identity': b.payload={**b.payload,'item':metadata('999','limit')}
    assert scan(e,now)==[]


def test_threshold_change_creates_independent_dimension_and_preserves_old_event(showcase_env):
    e=showcase_env;now=utcnow();used,limit,a,b=configure(e,now)
    old=scan(e,now)[0]; old_event=e['db'].get(m.Event,old.event_id)
    event_state=(old_event.title,old_event.status,old_event.last_seen_at)
    later=now+timedelta(seconds=15); limit=metadata('502','limit',warning='70')
    write(e,used,points(later));write(e,limit,points(later,(40,40,40)))
    new=scan(e,later)[0]
    assert old.id!=new.id and old.dimension!=new.dimension
    assert old.calculation['observation_state']=='configuration_changed'
    assert (old_event.title,old_event.status,old_event.last_seen_at)==event_state
    assert new.calculation['warning_percent']==70


def test_missing_latest_configuration_is_unknown_not_configuration_changed(showcase_env):
    e=showcase_env;now=utcnow();used,limit,_,_=configure(e,now)
    old=scan(e,now)[0]
    later=now+timedelta(seconds=15)
    write(e,used,points(later),coverage={'mapping_version':0})
    assert scan(e,later)==[]
    assert old.calculation['observation_state']=='active'


def test_pair_freshness_is_capped_even_for_large_verified_interval():
    now=utcnow();used,rows,limit,capacities=pair(now)
    used['interval']=limit['interval']=120
    assert assess(now+timedelta(seconds=121),used,rows,limit,capacities)['state']=='not_evaluated'


@pytest.mark.parametrize('offsets', [(0.002,0.001,0), (120,60,0)])
def test_distinct_points_must_span_real_consecutive_sampling_intervals(offsets):
    now=utcnow();used,rows,limit,capacities=pair(now)
    for stream in (rows,capacities):
        for row,offset in zip(stream,offsets):
            row['time']=iso(now-timedelta(seconds=offset))
    assert assess(now,used,rows,limit,capacities)['state']=='not_evaluated'
