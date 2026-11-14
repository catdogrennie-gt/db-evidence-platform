"""Session interpretation must use safe observations, not duration alone."""
from copy import deepcopy
from datetime import datetime
import json

import pytest

from platform_app.common import iso
from platform_app.model_context import bounded_tool_result, compact_final_context, compact_initial
from platform_app.models import Evidence
from platform_app.tools import project_evidence
from tests.platform.test_ai_scope import ai_env


def observation():
    return Evidence(
        id='session-observation', instance_id='authorized-object', source='mysql.sessions',
        source_mode='live', observed_at=datetime(2026, 11, 14, 1, 28, 28),
        payload={'item': {'hostid': '10690', 'itemid': '51388',
                         'key': 'mysql.custom.query[demo,,,sessions]',
                         'tags': [{'tag': 'dbe.contract', 'value': 'mysql-diagnostics-v1'},
                                  {'tag': 'dbe.query', 'value': 'sessions'}]},
                 'rows': [
                     {'THREAD_ID': 41, 'PROCESSLIST_ID': 5, 'PROCESSLIST_TIME': '150999',
                      'PROCESSLIST_COMMAND': 'Daemon', 'PROCESSLIST_STATE': 'Waiting on empty queue',
                      'PROCESSLIST_INFO': 'select PRIVATE-SQL', 'PROCESSLIST_USER': 'PRIVATE-USER'},
                     {'THREAD_ID': 45, 'PROCESSLIST_ID': 7, 'PROCESSLIST_TIME': '150999',
                      'PROCESSLIST_COMMAND': 'Daemon', 'PROCESSLIST_STATE': 'Suspending'},
                 ]},
        coverage={'availability': 'available', 'complete': False, 'source_id': 'zabbix-source',
                  'hostid': '10690', 'itemid': '51388',
                  'snapshot_contract': 'db-evidence-mysql-diagnostics-v1'},
        payload_sha256='a' * 64, truncated=False,
    )


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())


def test_observed_background_context_survives_without_forwarding_private_text():
    row = observation()
    before = deepcopy((row.payload, row.coverage, row.payload_sha256))
    result = project_evidence(row)
    first = result['data']['rows'][0]
    assert first['PROCESSLIST_COMMAND'] == 'Daemon'
    assert first['PROCESSLIST_STATE'] == 'Waiting on empty queue'
    assert first['PROCESSLIST_TIME'] == 150999
    assert 'PRIVATE-' not in json.dumps(result)
    assert result['data']['session_observation']['producer'] == 'performance_schema.threads'
    assert (row.payload, row.coverage, row.payload_sha256) == before
    assert result['coverage']['complete'] is False


@pytest.mark.parametrize('command,state', [('Sleep', ''), ('Query', 'executing'),
                                         ('Query', 'Waiting for table metadata lock')])
def test_allowlisted_observations_are_retained_without_guessing_a_diagnosis(command, state):
    row = observation()
    row.payload['rows'][0].update(PROCESSLIST_COMMAND=command, PROCESSLIST_STATE=state)
    result = project_evidence(row)['data']
    assert result['rows'][0]['PROCESSLIST_COMMAND'] == command
    assert result['rows'][0]['PROCESSLIST_STATE'] == state
    assert set(result['session_observation']) == {'producer', 'timer', 'limits'}
    assert 'Sleep' in result['session_observation']['limits']


@pytest.mark.parametrize('field,value', [
    ('PROCESSLIST_COMMAND', 'Query PRIVATE-PASSWORD'),
    ('PROCESSLIST_STATE', 'Waiting for private_table / PRIVATE-PASSWORD'),
    ('PROCESSLIST_COMMAND', None), ('PROCESSLIST_STATE', ['executing']),
])
def test_unknown_or_nonstring_values_are_omitted_instead_of_sent_or_guessed(field, value):
    row = observation()
    row.payload['rows'][0][field] = value
    projected = project_evidence(row)
    assert field not in projected['data']['rows'][0]
    assert 'PRIVATE-' not in json.dumps(projected)


@pytest.mark.parametrize('field,value', [('processlist_command', 'Sleep'),
                                      ('processlist_state', 'executing')])
def test_conflicting_case_aliases_have_no_trusted_winner(field, value):
    row = observation()
    row.payload['rows'][0][field] = value
    assert field.upper() not in project_evidence(row)['data']['rows'][0]


@pytest.mark.parametrize('mutation', ['wrong_key', 'no_tags', 'conflicting_tag',
                                     'wrong_contract', 'other_source', 'other_query'])
def test_unverified_producers_do_not_gain_verified_timer_semantics(mutation):
    row = observation()
    if mutation == 'wrong_key': row.payload['item']['key'] = 'custom.sessions'
    elif mutation == 'no_tags': row.payload['item']['tags'] = []
    elif mutation == 'conflicting_tag':
        row.payload['item']['tags'].append({'tag': 'dbe.query', 'value': 'transactions'})
    elif mutation == 'wrong_contract': row.coverage['snapshot_contract'] = 'unknown'
    elif mutation == 'other_source': row.source = 'mysql.transactions'
    elif mutation == 'other_query':
        row.payload['item']['key'] = 'mysql.custom.query[demo,,,transactions]'
        row.payload['item']['tags'][1]['value'] = 'transactions'
    result = project_evidence(row)
    assert 'session_observation' not in result['data']
    if mutation == 'other_source':
        assert 'PROCESSLIST_COMMAND' not in result['data']['rows'][0]
        assert 'PROCESSLIST_STATE' not in result['data']['rows'][0]


@pytest.mark.parametrize('pipeline', ['initial', 'final', 'tool'])
def test_session_context_survives_model_packing_without_budget_increase(pipeline):
    row = observation()
    projected = project_evidence(row)
    before = deepcopy(projected)
    bundle = {'from': '2026-11-14T00:28:28Z', 'to': iso(row.observed_at),
              'coverage': {'complete': False}, 'evidence': [projected],
              'inspection_signals': [{'evidence_ids': [row.id]}]}
    if pipeline == 'tool':
        packed = bounded_tool_result({'items': [projected]}, max_bytes=1600)
        assert size(packed) <= 1600
        actual = packed['items'][0]
    else:
        if pipeline == 'final':
            for index in range(10):
                bundle['evidence'].append({
                    'evidence_id': f'optional-{index}', 'instance_id': row.instance_id,
                    'source': f'zabbix.item.numeric:optional-{index}',
                    'observed_at': iso(row.observed_at), 'coverage': {'complete': False},
                    'data': {'row_count': 1, 'item': {'hostid': '10690', 'itemid': str(index),
                        'units': 'ms', 'value_semantics': 'gauge'},
                        'rows': [{'time': iso(row.observed_at), 'value': index}]}})
        packed = compact_initial(bundle)
        if pipeline == 'final':
            assert size(packed) + 512 > 3200
            packed = compact_final_context(packed, max_bytes=3200,
                measure=lambda value: size(value) + 512, protected_evidence_ids=[row.id])
            assert size(packed) + 512 <= 3200
        actual = next(item for item in packed['evidence'] if item['evidence_id'] == row.id)
    assert actual['data']['rows'][0]['PROCESSLIST_COMMAND'] == 'Daemon'
    assert actual['data']['rows'][0]['PROCESSLIST_STATE'] == 'Waiting on empty queue'
    assert actual['data']['session_observation'] == projected['data']['session_observation']
    assert actual['evidence_id'] == row.id
    assert projected == before


def test_preview_discloses_allowlisted_session_fields(ai_env):
    from platform_app.investigations import preview_investigation
    result = preview_investigation(ai_env['actor'], [ai_env['a'].id],
        ai_env['start'], ai_env['end'], 'session review', 'live_redacted')
    assert 'allowlisted_session_command_and_state' in result['external_fields']
