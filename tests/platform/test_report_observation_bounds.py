"""Frozen point observations qualify a report; they never prove continuous recovery."""
from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi import HTTPException

from platform_app.common import iso
from platform_app.evidence import digest
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_showcase import showcase_env
from tests.platform.test_frozen_rule_facts import frozen_capacity


@pytest.fixture
def snapshots(frozen_capacity):
    e = frozen_capacity
    for row, count, offset in [(e['used'], 1, 0), (e['limit'], 0, .5)]:
        row.source = 'mysql.lock_waits'
        row.observed_at = e['stamp'] + timedelta(seconds=offset)
        row.payload = {'item': {'hostid': '101', 'itemid': '501'},
                       'rows': [{'waiting_trx_id': 1}] if count else []}
        row.coverage = {'availability': 'available', 'complete': True, 'source_id': 'source-a'}
        row.payload_sha256 = digest(row.payload)
        for item in e['frozen_items']:
            if item.evidence_id == row.id:
                item.payload = {'source': row.source, 'observed_at': iso(row.observed_at)}
                item.payload_sha256 = row.payload_sha256
    e['db'].commit()
    return e


def bounds(e, actor=None):
    from platform_app.report_observation_bounds import frozen_observation_bounds
    e['db'].flush()
    return frozen_observation_bounds(actor or e['reader'], e['investigation'])


def test_active_then_empty_has_explicit_observation_gap_and_no_recovery_claim(snapshots):
    e = snapshots
    original = deepcopy(e['investigation'].report)
    result = bounds(e)
    assert result['scope'] == 'frozen_manifest_point_observations'
    assert result['window'] == {'from': iso(e['investigation'].window_start), 'to': iso(e['investigation'].window_end)}
    group, = result['groups']
    assert group['instance_name'] == e['a'].name
    assert group['identity'] == {'source_id': 'source-a', 'hostid': '101', 'itemid': '501'}
    assert group['sample_count'] == 2 and group['nonempty_sample_count'] == 1
    assert group['first_empty_after_last_nonempty']['observed_at'] == iso(e['limit'].observed_at)
    assert group['gap_to_next_empty_seconds'] == .5
    assert result['not_established'] == ['continuous_duration', 'recovery_mechanism', 'current_state']
    assert e['investigation'].report == original


@pytest.mark.parametrize('change', ['outside_window', 'bad_hash', 'unavailable', 'replay'])
def test_missing_or_unusable_later_observation_never_becomes_recovery(snapshots, change):
    e = snapshots
    row = e['limit']
    if change == 'outside_window': row.observed_at = e['investigation'].window_end + timedelta(seconds=1)
    if change == 'bad_hash': row.payload = {'rows': []}
    if change == 'unavailable': row.coverage = {'availability': 'unavailable'}
    if change == 'replay': row.source_mode = 'replay'
    result = bounds(e)
    assert result['complete'] is False
    assert all(g['first_empty_after_last_nonempty'] is None for g in result['groups'])


@pytest.mark.parametrize('field', ['source_id', 'itemid', 'source'])
def test_different_sources_do_not_form_a_temporal_transition(snapshots, field):
    e = snapshots
    row = e['limit']
    if field == 'source_id': row.coverage = {**row.coverage, 'source_id': 'source-b'}
    if field == 'itemid':
        row.payload = {**row.payload, 'item': {'hostid': '101', 'itemid': '502'}}
        row.payload_sha256 = digest(row.payload)
        e['frozen_items'][1].payload_sha256 = row.payload_sha256
    if field == 'source':
        row.source = 'mysql.sessions'
        e['frozen_items'][1].payload = {'source': row.source, 'observed_at': iso(row.observed_at)}
    result = bounds(e)
    assert len(result['groups']) == 2
    assert all(g['first_empty_after_last_nonempty'] is None for g in result['groups'])
    if field == 'source_id':
        assert {g['identity']['source_id'] for g in result['groups']} == {'source-a', 'source-b'}


def test_numeric_rows_are_not_snapshots_and_missing_identity_does_not_guess_a_link(snapshots):
    e = snapshots
    e['limit'].source = 'zabbix.item.numeric:counter'
    result = bounds(e)
    assert sum(g['sample_count'] for g in result['groups']) == 1
    e['limit'].source = 'mysql.lock_waits'
    e['limit'].coverage = {'availability': 'available', 'complete': True}
    result = bounds(e)
    assert len(result['groups']) == 2
    assert result['complete'] is False


def test_cross_object_manifest_is_not_exposed(snapshots):
    e = snapshots
    e['frozen_manifest'].instance_ids = [e['b'].id]
    assert bounds(e)['groups'] == []


def test_restricted_payload_yields_only_same_allowlisted_metadata_for_both_reader_types(snapshots):
    import json
    from platform_app import models as m
    from platform_app.access import actor_for_user
    e = snapshots
    row = e['used']
    row.sensitivity = 'restricted'
    row.payload = {**row.payload, 'password': 'private-password-sentinel',
        'item': {**row.payload['item'], 'name': 'private-item-sentinel'},
        'rows': [{'sql_text': 'private-sql-sentinel', 'waiting_trx_id': 9988776655,
                  'schema_name': 'private-schema-sentinel', 'nested': {'session_id': 'private-session-sentinel'}}]}
    row.payload_sha256 = digest(row.payload)
    e['frozen_items'][0].payload_sha256 = row.payload_sha256
    ordinary = bounds(e)
    role = e['db'].get(m.Role, e['grant'].role_id)
    role.permissions = [*role.permissions, 'evidence.read_sensitive']
    privileged = bounds(e, actor_for_user(e['db'], e['reader'].user_id))
    assert ordinary == privileged
    assert ordinary['groups'][0]['nonempty_sample_count'] == 1
    text = json.dumps(ordinary)
    for private in ['private-password', 'private-item', 'private-sql', '9988776655', 'private-schema', 'private-session', 'sql_text', 'waiting_trx_id']:
        assert private not in text


def test_malformed_metadata_is_not_published_or_used_as_a_group_identity(snapshots):
    e = snapshots
    e['limit'].coverage = {**e['limit'].coverage, 'source_id': 'unexpected\nprivate text'}
    result = bounds(e)
    assert result['complete'] is False
    assert any(group['identity'] is None for group in result['groups'])
    assert 'private text' not in str(result)


def test_cross_object_report_authorization_is_enforced(snapshots):
    from platform_app import models as m
    e = snapshots
    e['db'].add(m.InvestigationResource(investigation_id=e['investigation'].id, instance_id=e['b'].id))
    with pytest.raises(HTTPException) as exc:
        bounds(e)
    assert exc.value.status_code == 404  # Resource masking preserves the existing authorization contract.
