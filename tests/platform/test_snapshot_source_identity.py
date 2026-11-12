"""Qualified snapshot identities keep independent observations apart."""
from copy import deepcopy
from datetime import datetime
import json

from platform_app.models import Evidence
from platform_app.evidence import digest
from platform_app.model_context import refresh_snapshot_chronology
from platform_app.tools import project_evidence


def observation(ref, source_id, *, hostid='101', itemid='11', count=1):
    row = Evidence(id=ref, instance_id='aggregate', source='mysql.lock_waits',
        source_mode='live', observed_at=datetime(2026, 11, 12, 18, 0, 0),
        payload={'item': {'hostid': hostid, 'itemid': itemid,
                         'name': 'Lock snapshot', 'password': 'PRIVATE-PASSWORD'},
                 'rows': [{'waiting_trx_id': 2, 'blocking_trx_id': 1}] if count else []},
        coverage={'source_id': source_id, 'hostid': hostid, 'itemid': itemid,
                  'availability': 'available', 'complete': True,
                  'password': 'PRIVATE-KEY'}, payload_sha256='a' * 64)
    row.payload_sha256 = digest(row.payload)
    return row


def test_projection_exposes_only_safe_source_qualified_identity_and_does_not_modify_raw():
    row = observation('positive', 'source-a')
    before = deepcopy((row.payload, row.coverage))
    result = project_evidence(row)
    identity = result['data']['item']
    assert {name: identity[name] for name in ('source_id', 'hostid', 'itemid')} == {
        'source_id': 'source-a', 'hostid': '101', 'itemid': '11'}
    assert 'PRIVATE-PASSWORD' not in json.dumps(result)
    assert 'PRIVATE-KEY' not in json.dumps(result)
    assert (row.payload, row.coverage) == before


def test_different_zabbix_sources_with_same_host_item_ids_cannot_prove_each_others_recovery():
    positive = project_evidence(observation('positive', 'source-a'))
    other = project_evidence(observation('other', 'source-b', count=0))
    other['observed_at'] = '2026-11-12T18:01:00Z'
    result = {'evidence': [positive, other]}
    refresh_snapshot_chronology(result)
    groups = result['snapshot_chronology']['groups']
    assert len(groups) == 2
    active = next(group for group in groups if group['first_nonempty'])
    assert active['source_id'] == 'source-a'
    assert active['empty_after_last']['count'] == 0


def test_missing_source_identity_stays_separate_from_a_qualified_source():
    positive = project_evidence(observation('positive', 'source-a'))
    unknown = observation('unknown', None, count=0)
    unknown.coverage.pop('source_id')
    later = project_evidence(unknown)
    later['observed_at'] = '2026-11-12T18:01:00Z'
    result = {'evidence': [positive, later]}
    refresh_snapshot_chronology(result)
    groups = result['snapshot_chronology']['groups']
    assert len(groups) == 2
    active = next(group for group in groups if group['first_nonempty'])
    assert active['empty_after_last']['count'] == 0
