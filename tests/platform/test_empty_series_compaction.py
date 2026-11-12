"""Empty display tables must not crowd out protected frozen observations."""
import copy
import json

from platform_app import model_context as context


def measured_size(value):
    # A complete serialized request includes the fixed prompt and tool group.
    messages = [
        {'role': 'system', 'content': 'Frozen evidence contract. ' * 12},
        {'role': 'user', 'content': json.dumps(value, ensure_ascii=False, separators=(',', ':'))},
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'read-once', 'type': 'function', 'function': {'name': 'get_evidence', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'read-once', 'content': '{"items":[]}'},
    ]
    return len(json.dumps({'messages': messages, 'tools': [], 'max_tokens': 4000},
                          ensure_ascii=False, separators=(',', ':')).encode()) + 512


def frozen_context():
    stamp = '2026-11-13T01:00:00Z'
    evidence = []
    for index, value in enumerate((7, 19)):
        evidence.append({
            'evidence_id': 'measured-' + str(index), 'instance_id': 'asset-a',
            'source': 'zabbix.item.numeric:source-item-' + str(index),
            'source_mode': 'live', 'observed_at': stamp,
            'coverage': {'complete': False, 'gaps': ['history_truncated']},
            'data': {'row_count': 3, 'sample_count': 3,
                     'first_time': '2026-11-13T00:59:50Z', 'last_time': stamp,
                     'projection_scope': 'retained_samples_subset_not_upstream_gap',
                     'rows': [{'time': stamp, 'value': value}], 'rows_omitted': 2,
                     'item': {'hostid': 'host-a', 'itemid': 'item-' + str(index),
                              'units': 'count', 'value_semantics': 'gauge',
                              'semantics_source': 'verified_fixture'}}})
    columns = list(context.SERIES_COLUMNS)
    for name in ('reason_codes', 'semantics_source', 'first_time', 'last_time'):
        columns[columns.index(name)] = name + '_ref'
    initial = {'evidence': evidence, 'coverage': {'complete': False, 'gaps': ['source_gap']},
               'inspection_signals': [{'kind': 'current', 'category': 'baseline_change',
                    'evidence_ids': [e['evidence_id']],
                    'calculation': {'observed_at': stamp, 'value': e['data']['rows'][0]['value']}}
                    for e in evidence],
               'numeric_series': {'columns': columns, 'rows': [],
                    'resources': [{'instance_id': 'asset-a', 'source_id': 'source-a',
                                   'mapping_version': 2, 'config_revision': 3}],
                    'reason_codes_dictionary': [], 'semantics_source_dictionary': [],
                    'first_time_dictionary': [], 'last_time_dictionary': [],
                    'registered_relationships': [{'from': 'host-a', 'to': 'service-a', 'kind': 'registered'}],
                    'summary_scope': 'frozen_retained_samples_only', 'omitted_count': 5,
                    'omission_reason': 'remaining_total_context_budget'}}
    context._encode_numeric_context(initial)
    initial['coverage']['retained_evidence_count'] = len(evidence)
    return initial


def test_empty_codec_metadata_cannot_exhaust_budget_while_protected_facts_fit():
    initial = frozen_context()
    before = copy.deepcopy(initial)
    budget = measured_size(initial) - 350
    result = context.compact_final_context(initial, max_bytes=budget, measure=measured_size)
    assert measured_size(result) <= budget, 'Unused empty-table codec metadata consumed the remaining budget'
    assert context.context_refs(result) == {'measured-0', 'measured-1'}
    original = context._unpack_final_tables(initial)
    decoded = context._unpack_final_tables(result)
    assert decoded['evidence'] == original['evidence']
    assert decoded['inspection_signals'] == original['inspection_signals']
    assert initial == before
    assert context.compact_final_context(result, max_bytes=budget, measure=measured_size) == result


def test_empty_table_preserves_unknown_omissions_source_and_registered_relationships():
    initial = frozen_context()
    result = context.compact_final_context(initial, max_bytes=0, measure=measured_size)
    series = result['numeric_series']
    assert series['rows'] == []
    assert 'columns' not in series
    assert not any(name.endswith('_dictionary') and value == [] for name, value in series.items())
    for name in ('resources', 'registered_relationships', 'summary_scope', 'omitted_count', 'omission_reason'):
        assert series[name] == initial['numeric_series'][name]
    assert result['coverage']['complete'] is False
    assert result['coverage']['gaps'] == ['source_gap']
    assert measured_size(result) > 0  # Genuine impossible budgets still fail closed upstream.


def test_nonempty_protected_table_keeps_its_decodable_values_and_source_identity():
    initial = frozen_context()
    series = initial['numeric_series']
    series['reason_codes_dictionary'] = [['source_gap']]
    series['semantics_source_dictionary'] = ['verified_fixture']
    series['first_time_dictionary'] = ['2026-11-13T00:59:50Z']
    series['last_time_dictionary'] = ['2026-11-13T01:00:00Z']
    values = {'evidence_id': 'manifest:selected-series', 'resource': 0, 'hostid': 'host-a',
              'itemid': 'other-item', 'name': 'Measured observation', 'key': 'observed.value',
              'unit': 'count', 'sample_count': 3, 'first': 4, 'last': 9, 'min': 4, 'max': 9,
              'missing_count': 0, 'complete': False, 'reason_codes_ref': 0, 'last_time_ref': 0,
              'first_time_ref': 0, 'original_point_count': 3, 'sampling_method': 'none',
              'value_semantics': 'gauge', 'semantics_source_ref': 0}
    series['rows'] = [[values[name] for name in series['columns']]]
    before = copy.deepcopy(initial)
    result = context.compact_final_context(initial, max_bytes=0, measure=measured_size,
                                          protected_evidence_ids=['manifest:selected-series'])
    packed = result['numeric_series']
    assert len(packed['rows']) == 1
    fields = dict(zip(packed['columns'], packed['rows'][0]))
    for name in ('evidence_id', 'hostid', 'itemid', 'sample_count', 'last', 'value_semantics'):
        assert fields[name] == values[name]
    assert packed['resources'][fields['resource']] == series['resources'][0]
    assert packed['last_time_dictionary'][fields['last_time_ref']] == '2026-11-13T01:00:00Z'
    for name, value in fields.items():
        if name.endswith('_ref'):
            assert type(value) is int and 0 <= value < len(packed[name[:-4] + '_dictionary'])
    assert initial == before
