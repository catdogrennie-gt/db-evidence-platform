"""Omitted raw history entries must not masquerade as diagnostic rows."""
from copy import deepcopy
from datetime import datetime
import json

import pytest

from platform_app.models import Evidence
from platform_app.tools import project_evidence
from platform_app.model_context import compact_initial, compact_final_context, compact_tool_item


def observation(kind='text', count=8):
    return Evidence(id='text-history', instance_id='authorized-object',
        source='zabbix.item.text:abc', source_mode='live',
        observed_at=datetime(2026, 11, 14, 2),
        payload={'item': {'hostid': '10687', 'itemid': '50957', 'value_kind': kind},
                 'rows': [{'time': '2026-11-14T01:00:00Z', 'value': 'PRIVATE-RAW-TEXT'} for _ in range(count)]},
        coverage={'availability': 'available', 'complete': True},
        payload_sha256='a' * 64, truncated=False)


@pytest.mark.parametrize('kind', ['text', 'log', 'unsupported', 'unknown'])
@pytest.mark.parametrize('count', [0, 8, 104])
def test_history_count_is_not_diagnostic_row_count(kind, count):
    row = observation(kind, count)
    before = deepcopy((row.payload, row.coverage, row.payload_sha256))
    result = project_evidence(row)
    assert 'row_count' not in result['data']
    assert result['data']['history_sample_count'] == min(count, 100)
    assert result['data']['history_count_scope'] == 'projected_history_entries_not_diagnostic_rows'
    assert result['data']['text_omitted'] is True
    assert 'PRIVATE-RAW-TEXT' not in json.dumps(result)
    assert (row.payload, row.coverage, row.payload_sha256) == before


@pytest.mark.parametrize('path', ['initial', 'final', 'tool'])
def test_cardinality_scope_survives_packing(path):
    projected = project_evidence(observation())
    if path == 'tool':
        result = compact_tool_item(projected)
    else:
        context = {'evidence': [projected], 'metrics': []}
        packed = compact_initial(context)
        if path == 'final':
            packed = compact_final_context(packed, max_bytes=24000,
                measure=lambda value: len(json.dumps(value).encode()))
        result = packed['evidence'][0]
    assert 'row_count' not in result['data']
    assert result['data']['history_sample_count'] == 8
    assert result['data']['history_count_scope'] == 'projected_history_entries_not_diagnostic_rows'
    assert result['data']['text_omitted'] is True


def test_diagnostic_snapshot_count_remains_diagnostic():
    row = observation()
    row.source = 'mysql.lock_waits'
    result = project_evidence(row)
    assert result['data']['row_count'] == 8
    assert 'history_sample_count' not in result['data']
