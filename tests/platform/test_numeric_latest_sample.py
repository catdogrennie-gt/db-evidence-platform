"""Explicit latest retained values must preserve independent clocks and references."""
from copy import deepcopy
import json

import pytest

from platform_app import model_context as context


def measured_bundle():
    series = context.new_series()
    series['resources'] = [{'instance_id': 'scope-a', 'source_id': 'source-a'}]
    for ref, first, last, stamp in [
        ('manifest:request', 1.5, 2300, '2026-11-15T06:06:31Z'),
        ('manifest:db-call', .7, .85, '2026-11-15T06:06:31Z'),
        ('manifest:later', .7, 1.05, '2026-11-15T06:06:41Z'),
    ]:
        values = dict(evidence_id=ref, resource=0, hostid='101', itemid=ref,
            name=ref, key=ref, unit='ms', sample_count=3, first=first, last=last,
            min=min(first, last), max=max(first, last), missing_count=1, complete=False,
            reason_codes=['partial_history'], last_time=stamp, original_point_count=4,
            sampling_method='none', value_semantics='gauge', semantics_source='zabbix_tag',
            first_time='2026-11-15T06:00:00Z')
        series['rows'].append([values.get(key) for key in context.SERIES_COLUMNS])
    return {'coverage': {'complete': False}, 'evidence': [], 'numeric_series': series}


def pack(bundle, budget=100000):
    return context.compact_final_context(context.compact_initial(bundle), max_bytes=budget,
        measure=lambda value: len(json.dumps(value, separators=(',', ':')).encode()))


def samples(packed):
    series = packed['numeric_series']
    if not series.get('rows'):return {}
    assert series['summary_scope'] == 'frozen_retained_samples_only'
    columns=series['columns'];result={}
    for row in series['rows']:
        point=row[columns.index('latest_sample')]
        value,stamp_ref=(point['value'],point['observed_at_ref']) if isinstance(point,dict) else point
        stamp=series['latest_sample_time_dictionary'][stamp_ref]
        if value is not None and stamp is not None and row[columns.index('sample_count')]>0:
            result[row[0]]={'value':value,'observed_at':stamp}
    return result



def test_new_value_and_simultaneous_counterevidence_are_explicit_without_merging_clocks():
    bundle = measured_bundle(); before = deepcopy(bundle)
    result = pack(bundle); latest = samples(result)
    assert latest['manifest:request']['value'] == 2300
    assert latest['manifest:db-call']['value'] == .85
    assert latest['manifest:request']['observed_at'] == latest['manifest:db-call']['observed_at']
    assert latest['manifest:later']['observed_at'] != latest['manifest:request']['observed_at']
    assert result['numeric_series']['rows'][0][result['numeric_series']['columns'].index('complete')] is False
    assert bundle == before
    assert context.compact_final_context(result, max_bytes=100000, measure=lambda _: 1) == result


@pytest.mark.parametrize('field,value', [('last', None), ('last', True), ('last', float('nan')),
    ('last_time', None), ('last_time', 'invalid'), ('sample_count', 0)])
def test_unavailable_latest_does_not_fall_back_to_older_first(field, value):
    bundle = measured_bundle()
    bundle['numeric_series']['rows'][0][context.SERIES_COLUMNS.index(field)] = value
    assert 'manifest:request' not in samples(pack(bundle))


def test_budget_pruning_never_leaves_latest_samples_for_omitted_series():
    initial = context.compact_initial(measured_bundle())
    full = pack(measured_bundle())
    budget = len(json.dumps(full, separators=(',', ':')).encode()) - 200
    result = context.compact_final_context(initial, max_bytes=budget,
        measure=lambda value: len(json.dumps(value, separators=(',', ':')).encode()))
    assert len(result['numeric_series']['rows']) < 3
    assert set(samples(result)) == {row[0] for row in result['numeric_series']['rows']}
    series=result['numeric_series'];position=series['columns'].index('latest_sample')
    assert {row[position]['observed_at_ref'] if isinstance(row[position],dict) else row[position][1] for row in series['rows']} == set(range(len(series['latest_sample_time_dictionary'])))


@pytest.mark.parametrize('stamp', ['2026-11-15T05:59:59Z', '2026-11-15T06:07:00Z'])
def test_latest_sample_outside_frozen_window_is_not_presented_as_current(stamp):
    bundle=measured_bundle();bundle.update({'from':'2026-11-15T06:00:00Z','to':'2026-11-15T06:07:00Z'})
    bundle['numeric_series']['rows'][0][context.SERIES_COLUMNS.index('last_time')]=stamp
    result=pack(bundle)
    assert 'manifest:request' not in samples(result)
    series=result['numeric_series']
    assert series['rows'][0][series['columns'].index('latest_sample')]['value'] is None


def test_raw_trigger_latest_pair_does_not_select_old_first():
    item={'evidence_id':'observed-request','source':'zabbix.item.numeric:observed',
        'data':{'item':{'hostid':'101','itemid':'501'},'rows':[
            {'value':1.5,'time':'2026-11-15T06:00:00Z'},
            {'value':2300,'time':'2026-11-15T06:06:31Z'}]}}
    result=context.compact_numeric_evidence(item)
    assert result['data']['latest_sample']=={'value':2300,'observed_at':'2026-11-15T06:06:31Z'}
    assert 'latest_sample' not in item['data']


@pytest.mark.parametrize('point', [
    {'value':None,'time':'2026-11-15T06:06:31Z'},
    {'value':True,'time':'2026-11-15T06:06:31Z'},
    {'value':float('inf'),'time':'2026-11-15T06:06:31Z'},
    {'value':2300,'time':'bad-clock'},
    {'value':2300,'time':'2026-11-15T06:06:31Z','validity':'invalid'},
])
def test_invalid_latest_raw_point_is_not_replaced_with_older_good_value(point):
    item={'source':'zabbix.item.numeric:observed','data':{'rows':[
        {'value':1.5,'time':'2026-11-15T06:00:00Z'},point]}}
    assert context.compact_numeric_evidence(item)['data']['latest_sample']=={'value':None,'observed_at':None}


def test_normal_counterexample_uses_new_low_value_not_old_peak():
    bundle=measured_bundle();row=bundle['numeric_series']['rows'][0]
    row[context.SERIES_COLUMNS.index('first')]=2300
    row[context.SERIES_COLUMNS.index('last')]=1.5
    assert samples(pack(bundle))['manifest:request']['value']==1.5


def test_first_request_also_pairs_latest_samples_without_waiting_for_forced_final():
    initial=context.compact_initial(measured_bundle())
    latest=samples(initial)
    assert latest['manifest:request']['value']==2300
    assert latest['manifest:request']['observed_at']==latest['manifest:db-call']['observed_at']


def test_first_request_pruning_does_not_leave_orphan_time_dictionary_entries():
    initial=context.compact_initial(measured_bundle())
    assert context.trim_numeric_summary(initial)
    series=initial['numeric_series'];position=series['columns'].index('latest_sample')
    assert {row[position]['observed_at_ref'] if isinstance(row[position],dict) else row[position][1] for row in series['rows']}==set(range(len(series['latest_sample_time_dictionary'])))


def test_valid_and_missing_latest_raw_samples_round_trip_together():
    items=[]
    for index,value in enumerate((2300,None)):
        items.append({'evidence_id':f'observed-{index}','source':f'zabbix.item.numeric:{index}',
            'data':{'rows':[{'value':value,'time':'2026-11-15T06:06:31Z'}]}})
    encoded=context.compact_initial({'coverage':{},'evidence':items})
    decoded=context._unpack_final_tables(encoded)
    assert decoded['evidence'][0]['data']['latest_sample']['value']==2300
    assert decoded['evidence'][1]['data']['latest_sample']['value'] is None
    compressed=context.compact_final_context(encoded,max_bytes=0,
        measure=lambda value:len(json.dumps(value)))
    assert compressed['coverage']['retained_evidence_count']>=0


def test_invalid_time_with_frozen_window_does_not_promote_old_first_to_latest():
    item={'source':'zabbix.item.numeric:observed','data':{'rows':[
        {'value':1.5,'time':'2026-11-15T06:00:00Z'},
        {'value':2300,'time':'bad-clock'}]}}
    result=context.compact_numeric_evidence(item,window_start='2026-11-15T06:00:00Z',
        window_end='2026-11-15T06:07:00Z')
    assert result['data']['latest_sample']=={'value':None,'observed_at':None}
    assert result['data']['rows']==[item['data']['rows'][0]]
    assert context.compact_numeric_evidence(result)==result


def test_valid_point_after_frozen_end_is_excluded_without_invalidating_retained_latest():
    item={'source':'zabbix.item.numeric:observed','data':{'rows':[
        {'value':1.5,'time':'2026-11-15T06:00:00Z'},
        {'value':2300,'time':'2026-11-15T06:08:00Z'}]}}
    result=context.compact_numeric_evidence(item,window_start='2026-11-15T06:00:00Z',
        window_end='2026-11-15T06:07:00Z')
    assert result['data']['latest_sample']=={'value':1.5,'observed_at':'2026-11-15T06:00:00Z'}
