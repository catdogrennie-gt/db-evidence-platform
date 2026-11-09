"""Whole source-window budgets retain measured samples, never synthetic values."""
from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest
from tests.platform.test_ai_scope import ai_env
from platform_app import metrics, models as m
from platform_app.common import iso


START = datetime(2026, 1, 1)
END = START + timedelta(hours=1)


def chart(identifier, template, count, *, gaps=False, metadata_bytes=0):
    interval = min(2, 3599 / max(1, count - 1))
    points = [{'time': iso(START+timedelta(seconds=index*interval)), 'value': index % 31, 'validity': 'valid'} for index in range(count)]
    if count > 8:
        points[count//3]['value'] = 999
        points[count*2//3]['value'] = -99
    missing = []
    if gaps:
        for index in (10, 20, 21): points[index].update(value=None, validity='unknown_gap')
        missing = [{'from': points[index]['time'], 'to': points[index+1]['time'], 'reason': 'missing_samples'} for index in (10, 20)]
    return {'instance_id': identifier, 'template': template, 'id': template, 'source': 'zabbix_history',
            'owner_collector': 'host', 'unit': '%', 'points': points, 'gaps': missing,
            'coverage': {'complete': not gaps, 'truncated': False},
            'provenance': {'transport': 'zabbix', 'itemid': template, 'mapping_version': 7, 'detail': 'x'*metadata_bytes}}


def materialize(env, monkeypatch, charts):
    from platform_app import monitoring
    identifiers = {item['instance_id'] for item in charts}
    for instance in env['db'].query(m.Instance).filter(m.Instance.id.in_(identifiers)):
        instance.connection_mode = 'zabbix'
    env['db'].flush()
    monkeypatch.setattr(monitoring, 'metric_charts', lambda db, instance, *args: deepcopy([item for item in charts if item['instance_id'] == instance.id]))
    return metrics.materialize_source_windows(env['db'], object(), sorted(identifiers), START, END)


def assert_hard_bounds(result):
    assert len(result['items']) <= 192
    assert sum(len(item['points']) for item in result['items']) <= 10000
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 2_000_000


def test_eight_curves_above_point_budget_keep_original_extrema_and_gaps(ai_env, monkeypatch):
    counts = [1266, 1266, 1266, 1263, 1266, 1263, 1263, 1263]
    originals = [chart(ai_env['a' if index < 4 else 'b'].id, f'metric-{index}', count, gaps=index == 0) for index, count in enumerate(counts)]
    result = materialize(ai_env, monkeypatch, originals)
    assert len(result['items']) == 8
    assert_hard_bounds(result)
    assert result['coverage']['materialized'] is True and result['coverage']['complete'] is False
    assert result['coverage']['sampling']['original_point_count'] == 10116
    assert result['coverage']['sampling']['retained_point_count'] < 10116
    lookup = {item['template']: item for item in originals}
    for item in result['items']:
        original = lookup[item['template']]
        assert item['points'][0] == original['points'][0] and item['points'][-1] == original['points'][-1]
        assert all(point in original['points'] for point in item['points'])
        assert original['points'][len(original['points'])//3] in item['points']
        assert original['points'][len(original['points'])*2//3] in item['points']
        assert item['gaps'] == original['gaps']
        assert item['provenance']['mapping_version'] == 7
        assert item['coverage']['complete'] is False
        assert item['provenance']['sampling']['method'] == 'original_point_bucket_minmax'
    sampled_gap = next(item for item in result['items'] if item['template'] == 'metric-0')
    assert all(originals[0]['points'][index] in sampled_gap['points'] for index in (9, 10, 11, 19, 20, 21, 22))


def test_under_budget_window_keeps_all_observations_complete(ai_env, monkeypatch):
    original = chart(ai_env['a'].id, 'small', 12)
    result = materialize(ai_env, monkeypatch, [original])
    assert_hard_bounds(result)
    assert result['items'][0]['points'] == original['points']
    assert result['items'][0]['coverage']['complete'] is True
    assert result['coverage']['complete'] is True


def test_single_oversized_metadata_omits_only_that_curve(ai_env, monkeypatch):
    originals = [chart(ai_env['a'].id, 'oversized', 12, metadata_bytes=2_100_000), chart(ai_env['b'].id, 'small', 12)]
    result = materialize(ai_env, monkeypatch, originals)
    assert [item['template'] for item in result['items']] == ['small']
    assert_hard_bounds(result)
    assert result['coverage']['complete'] is False
    assert result['coverage']['sampling']['omitted_series_count'] == 1
    assert result['coverage']['sampling']['omitted_series'][0]['template'] == 'oversized'
    assert 'source_window_byte_budget' in result['coverage']['reason_codes']


def test_byte_budget_samples_points_before_omitting_fitting_curves(ai_env, monkeypatch):
    originals = [chart(ai_env['a'].id, 'large-metadata', 3000, metadata_bytes=1_700_000), chart(ai_env['b'].id, 'second', 3000)]
    result = materialize(ai_env, monkeypatch, originals)
    assert len(result['items']) == 2
    assert_hard_bounds(result)
    assert result['coverage']['complete'] is False
    assert result['coverage']['sampling']['omitted_series_count'] == 0
    assert 'source_window_byte_budget' in result['coverage']['reason_codes']
    lookup = {item['template']: item for item in originals}
    for item in result['items']:
        assert all(point in lookup[item['template']]['points'] for point in item['points'])
        assert item['points'][0] == lookup[item['template']]['points'][0]
        assert item['points'][-1] == lookup[item['template']]['points'][-1]


def test_item_budget_is_explicit_and_keeps_each_object_represented(ai_env, monkeypatch):
    originals = [chart(ai_env[key].id, f'{key}-{index}', 1) for key in ('a', 'b') for index in range(100)]
    result = materialize(ai_env, monkeypatch, originals)
    assert len(result['items']) == 192
    assert {item['instance_id'] for item in result['items']} == {ai_env['a'].id, ai_env['b'].id}
    assert_hard_bounds(result)
    assert result['coverage']['sampling']['omitted_series_count'] == 8
    assert 'source_window_item_budget' in result['coverage']['reason_codes']


def test_unrepresentable_gap_density_is_omitted_without_joining_gaps(ai_env, monkeypatch):
    dense = chart(ai_env['a'].id, 'dense-gaps', 10004)
    for index in range(1, len(dense['points']), 2): dense['points'][index].update(value=None, validity='unknown_gap')
    result = materialize(ai_env, monkeypatch, [dense, chart(ai_env['b'].id, 'small', 5)])
    assert [item['template'] for item in result['items']] == ['small']
    assert_hard_bounds(result)
    assert result['coverage']['sampling']['omitted_series_count'] == 1
    assert 'source_window_gap_budget' in result['coverage']['reason_codes']


def test_generic_curves_keep_same_template_distinct_ids_and_group_relationships(ai_env, monkeypatch):
    originals = [chart(ai_env['a'].id, 'zabbix_item_numeric', 6000) for _ in range(2)]
    for index, original in enumerate(originals):
        original['id'] = f'zabbix:source:host-{index}:item-{index}'
        original['owner_collector'] = 'monitoring'
        original['item'] = {'itemid': f'item-{index}', 'hostid': f'host-{index}', 'name': 'Same display name', 'units': 'B', 'value_type': 3}
        original['provenance'].update(source_id='source', hostid=f'host-{index}', itemid=f'item-{index}')
        original['registered_relationships'] = [{'from_hostid': 'host-0', 'to_hostid': 'host-1', 'kind': 'runs_on', 'provenance': 'user_registered'}]
    result = materialize(ai_env, monkeypatch, originals)
    assert len(result['items']) == 2
    assert_hard_bounds(result)
    assert {item['id'] for item in result['items']} == {item['id'] for item in originals}
    assert all(item['template'] == 'zabbix_item_numeric' for item in result['items'])
    assert all(item['registered_relationships'] == originals[0]['registered_relationships'] for item in result['items'])
    assert {item['item']['itemid'] for item in result['items']} == {'item-0', 'item-1'}


def test_combined_direct_and_zabbix_share_one_point_budget(ai_env, monkeypatch):
    from platform_app import monitoring
    ai_env['a'].connection_mode = 'zabbix'
    ai_env['b'].connection_mode = 'direct'
    ai_env['db'].flush()
    external = chart(ai_env['a'].id, 'external', 6000, gaps=True)
    direct = chart(ai_env['b'].id, 'connections', 6000)
    direct.update(source='prometheus_resampled', owner_collector='identity_status', unit='connections')
    monkeypatch.setattr(monitoring, 'metric_charts', lambda *args: deepcopy([external]))
    monkeypatch.setattr(metrics, 'materialize_metric_window', lambda *args: {'items': deepcopy([direct]), 'coverage': {'materialized': True, 'complete': True, 'reason_codes': []}})
    result = metrics.materialize_source_windows(ai_env['db'], object(), [ai_env['a'].id, ai_env['b'].id], START, END)
    assert_hard_bounds(result)
    assert {item['source'] for item in result['items']} == {'zabbix_history', 'prometheus_resampled'}
    assert len(result['items']) == 2
    assert result['coverage']['complete'] is False
    assert all(item['coverage']['complete'] is False for item in result['items'])


def test_sampled_payload_hash_matches_final_frozen_content(ai_env, monkeypatch):
    import hashlib
    originals = [chart(ai_env['a'].id, 'first', 6000), chart(ai_env['b'].id, 'second', 6000)]
    for item in originals:
        item['payload_sha256'] = '0' * 64
    result = materialize(ai_env, monkeypatch, originals)
    assert len(result['items']) == 2
    for item in result['items']:
        expected = hashlib.sha256(json.dumps({k: v for k, v in item.items() if k != 'payload_sha256'}, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        assert item['payload_sha256'] == expected
    assert all(item['payload_sha256'] == '0' * 64 for item in originals)


def test_full_group_point_allocation_is_deterministic_and_keeps_all_items(ai_env, monkeypatch):
    originals = [chart(ai_env['a'].id, f'item-{index:02}', 2000) for index in range(32)]
    first = materialize(ai_env, monkeypatch, originals)
    second = materialize(ai_env, monkeypatch, list(reversed(originals)))
    assert first == second
    assert len(first['items']) == 32
    assert_hard_bounds(first)
    assert sum(len(item['points']) for item in first['items']) == 10000
    assert first['coverage']['sampling']['original_point_count'] == 64000
    assert first['coverage']['sampling']['omitted_series_count'] == 0
    for item in first['items']:
        assert {999, -99}.issubset({point['value'] for point in item['points']})
        assert item['coverage']['complete'] is False
        assert item['coverage']['truncated'] is True


def test_sampling_preserves_change_of_reason_inside_missing_interval(ai_env, monkeypatch):
    original = chart(ai_env['a'].id, 'source-transition', 6000)
    for index in range(10, 200):
        original['points'][index].update(value=None, validity='unknown_gap' if index < 100 else 'source_unavailable')
    original['gaps'] = [
        {'from': original['points'][10]['time'], 'to': original['points'][100]['time'], 'reason': 'missing_samples'},
        {'from': original['points'][100]['time'], 'to': original['points'][200]['time'], 'reason': 'source_unavailable'},
    ]
    original['coverage']['complete'] = False
    result = materialize(ai_env, monkeypatch, [original, chart(ai_env['b'].id, 'other', 6000)])
    sampled = next(item for item in result['items'] if item['template'] == 'source-transition')
    assert all(original['points'][index] in sampled['points'] for index in (9, 10, 99, 100, 199, 200))
    assert sampled['gaps'] == original['gaps']
    assert_hard_bounds(result)
