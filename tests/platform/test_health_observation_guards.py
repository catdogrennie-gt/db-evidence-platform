"""Current observations and metric citations must retain their real identity."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from platform_app import models as m
from platform_app.common import iso, parse_time
from platform_app.db import utcnow
from platform_app.evidence import digest
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_showcase import showcase_env
from tests.platform.test_health_signals import numeric_evidence, series


@pytest.fixture(autouse=True)
def numeric_projection_config(showcase_env):
    e = showcase_env
    key = e['tmp_path'] / 'test-only-alias-key.private'
    key.write_bytes(b'local-test-only-alias-key-value')
    e['config'].fernet_key_path = key


def frozen_manifest(e):
    inv = e['investigation']
    inv.window_start = utcnow() - timedelta(minutes=20)
    inv.window_end = utcnow() + timedelta(seconds=1)
    manifest = m.EvidenceManifest(
        owner_type='investigation', owner_id=inv.id, instance_ids=[e['a'].id],
        window_start=inv.window_start, window_end=inv.window_end,
        manifest_sha256='a' * 64, retain_until=utcnow() + timedelta(days=1))
    e['db'].add(manifest)
    e['db'].flush()
    inv.manifest_id = manifest.id
    e['db'].add(m.InvestigationResource(investigation_id=inv.id, instance_id=e['a'].id))
    return manifest


def metric_proposal(e, reference):
    return dict(intent='review_issue', category='correlation', instance_id=e['a'].id,
                dimension={'kind': 'metric', 'value': '1234'},
                statement='Metric evidence needs review', evidence_ids=[reference])


def complete_metric_finding(e, item, reference):
    from platform_app.intelligence import sync_model_findings
    db = e['db']
    task = m.Task(type='investigation', payload={'mode': 'live_redacted'})
    db.add_all([item, task])
    db.flush()
    inv = e['investigation']
    inv.task_id = task.id
    inv.report = {'findings': [metric_proposal(e, reference(item))]}
    sync_model_findings(db, SimpleNamespace(instance_ids=[e['a'].id]), inv)
    db.flush()
    return db.scalar(select(func.count()).select_from(m.InspectionFinding))


@pytest.mark.parametrize('availability', ['unavailable', 'stale', 'unknown'])
def test_unavailable_numeric_evidence_cannot_register_a_model_issue(showcase_env, availability):
    e = showcase_env
    _, points = series()
    row = numeric_evidence(e, points)
    row.coverage = {**row.coverage, 'availability': availability}
    manifest = frozen_manifest(e)
    item = m.ManifestItem(manifest_id=manifest.id, instance_id=e['a'].id,
                          item_type='evidence', evidence_id=row.id,
                          payload={'source': row.source}, payload_sha256=row.payload_sha256)
    assert complete_metric_finding(e, item, lambda _: row.id) == 0


@pytest.mark.parametrize('damage', ['invalid_value', 'invalid_value_time_offset', 'bad_time',
                                    'outside_window', 'future_observed'])
def test_raw_metric_references_need_valid_values_inside_the_frozen_window(showcase_env, damage):
    e = showcase_env
    _, points = series()
    row = numeric_evidence(e, points)
    manifest = frozen_manifest(e)
    for point in points:
        if damage.startswith('invalid_value'):
            point['validity'] = 'unknown_gap'
        if damage == 'invalid_value_time_offset':
            point['time'] = point['time'].replace('Z', '+00:00')
        if damage == 'bad_time':
            point['time'] = 'invalid-timestamp'
        if damage == 'outside_window':
            point['time'] = iso(manifest.window_start - timedelta(seconds=1))
    if damage == 'future_observed':
        row.observed_at = manifest.window_end + timedelta(seconds=1)
    row.payload = {**row.payload, 'rows': points}
    row.payload_sha256 = digest(row.payload)
    item = m.ManifestItem(manifest_id=manifest.id, instance_id=e['a'].id,
                          item_type='evidence', evidence_id=row.id,
                          payload={'source': row.source}, payload_sha256=row.payload_sha256)
    assert complete_metric_finding(e, item, lambda _: row.id) == 0


@pytest.mark.parametrize('damage', ['invalid_value', 'outside_window', 'wrong_collector'])
def test_invalid_frozen_metric_cannot_register_a_model_issue(showcase_env, damage):
    e = showcase_env
    manifest = frozen_manifest(e)
    point = {'time': iso(utcnow() - timedelta(seconds=5)), 'value': 99, 'validity': 'valid'}
    if damage == 'invalid_value':
        point['validity'] = 'unknown_gap'
    if damage == 'outside_window':
        point['time'] = iso(manifest.window_start - timedelta(seconds=1))
    payload = {'template': 'zabbix_item_numeric', 'owner_collector': 'monitoring',
               'item': {'hostid': '101', 'itemid': '1234', 'value_kind': 'numeric'},
               'points': [point], 'coverage': {'complete': True}}
    if damage == 'wrong_collector':
        payload['owner_collector'] = 'host'
    item = m.ManifestItem(manifest_id=manifest.id, instance_id=e['a'].id,
                          item_type='time_series_window', payload=payload,
                          payload_sha256=digest(payload))
    assert complete_metric_finding(e, item, lambda entry: 'manifest:' + entry.id) == 0


@pytest.mark.parametrize('kind', ['raw', 'frozen'])
def test_available_window_metric_still_registers_a_reviewable_model_issue(showcase_env, kind):
    e = showcase_env
    _, points = series()
    row = numeric_evidence(e, points)
    manifest = frozen_manifest(e)
    if kind == 'raw':
        item = m.ManifestItem(manifest_id=manifest.id, instance_id=e['a'].id,
                              item_type='evidence', evidence_id=row.id,
                              payload={'source': row.source}, payload_sha256=row.payload_sha256)
        reference = lambda _: row.id
    else:
        payload = {'template': 'zabbix_item_numeric', 'owner_collector': 'monitoring',
                   'item': row.payload['item'], 'points': points,
                   'coverage': {'complete': True}, 'provenance': row.coverage}
        item = m.ManifestItem(manifest_id=manifest.id, instance_id=e['a'].id,
                              item_type='time_series_window', payload=payload,
                              payload_sha256=digest(payload))
        reference = lambda entry: 'manifest:' + entry.id
    assert complete_metric_finding(e, item, reference) == 1


@pytest.mark.parametrize('damage', ['stale', 'unselected', 'mapping_revision', 'config_revision',
                                    'source', 'old_finding_observation', 'counter_semantics',
                                    'sample_gap', 'latest_values_returned'])
def test_demo_current_signals_require_current_selection_and_fresh_samples(showcase_env, damage):
    from platform_app.health_signals import scan_group_changes
    from platform_app.showcase import snapshot
    e = showcase_env
    now, points = series()
    row = numeric_evidence(e, points)
    [finding] = scan_group_changes(e['db'], e['policy'], now)
    e['config'].showcase['projection_mode'] = 'numeric'
    e['db'].commit()
    assert snapshot(e['db'], e['config'])['observation']['status'] == 'signals_present'
    if damage == 'stale':
        row.observed_at = now - timedelta(hours=1)
    elif damage == 'unselected':
        e['mapping'].items = [{'hostid': '101', 'itemid': '9999', 'value_kind': 'numeric'}]
    elif damage == 'mapping_revision':
        e['mapping'].version += 1
    elif damage == 'config_revision':
        e['a'].config_revision += 1
    elif damage == 'source':
        row.coverage = {**row.coverage, 'source_id': 'another-zabbix-source'}
    elif damage == 'old_finding_observation':
        finding.calculation = {**finding.calculation, 'observed_at': iso(now-timedelta(hours=1))}
    elif damage == 'counter_semantics':
        row.payload = {**row.payload, 'item': {**row.payload['item'], 'value_semantics': 'counter'}}
    else:
        new_points = [{**point, 'time': iso(parse_time(point['time']) + timedelta(seconds=1))}
                      for point in points]
        if damage == 'sample_gap':
            new_points.pop(-2)
        else:
            for point in new_points[-3:]:
                point['value'] = 10
        numeric_evidence(e, new_points)
    e['db'].commit()
    observation = snapshot(e['db'], e['config'])['observation']
    assert observation['signals'] == []
    assert observation['status'] == ('data_available' if damage in ('old_finding_observation', 'counter_semantics', 'sample_gap', 'latest_values_returned') else 'unknown')
    assert e['db'].get(m.InspectionFinding, finding.id) is not None  # Keep the historical issue.


@pytest.mark.parametrize('kind', ['raw', 'frozen'])
def test_metric_issue_summary_uses_only_its_own_cited_fact(showcase_env, kind):
    from platform_app.health_signals import scan_group_changes
    from platform_app.intelligence import issue_response
    e = showcase_env
    now, points = series()
    row = numeric_evidence(e, points)
    [finding] = scan_group_changes(e['db'], e['policy'], now)
    manifest = frozen_manifest(e)
    inv = e['investigation']
    refs = []
    for itemid in ('9999', '1234'):
        meta = {**row.payload['item'], 'itemid': itemid}
        if kind == 'raw':
            payload = {'item': meta, 'rows': points}
            evidence = m.Evidence(instance_id=e['a'].id, run_id=row.run_id,
                                  source='zabbix.item.numeric:' + itemid, source_mode='live',
                                  observed_at=row.observed_at, payload=payload,
                                  payload_sha256=digest(payload), coverage=row.coverage)
            e['db'].add(evidence)
            e['db'].flush()
            item = m.ManifestItem(manifest_id=manifest.id, instance_id=e['a'].id,
                                  item_type='evidence', evidence_id=evidence.id,
                                  payload={'source': evidence.source}, payload_sha256=evidence.payload_sha256)
            refs.append(evidence.id)
        else:
            payload = {'template': 'zabbix_item_numeric', 'owner_collector': 'monitoring',
                       'item': meta, 'points': points, 'coverage': {'complete': True},
                       'provenance': {**row.coverage, 'hostid': '101', 'itemid': itemid}}
            item = m.ManifestItem(manifest_id=manifest.id, instance_id=e['a'].id,
                                  item_type='time_series_window', payload=payload,
                                  payload_sha256=digest(payload))
        e['db'].add(item)
        e['db'].flush()
        if kind == 'frozen':
            refs.append('manifest:' + item.id)
    inv.status = 'completed'
    inv.report = {'facts': [{'statement': 'Other metric explanation', 'evidence_ids': [refs[0]]},
                            {'statement': 'Selected metric explanation', 'evidence_ids': [refs[1]]}]}
    finding.latest_investigation_id = inv.id
    e['db'].commit()
    result = issue_response(e['db'], e['db'].get(m.Event, finding.event_id), finding)
    assert '报告窗口内：Selected metric explanation' in result['summary']
    assert 'Other metric explanation' not in result['summary']
    assert '近期中位数 40 s' in result['summary'] and '参考基线中位数 10 s' in result['summary']
    assert not result['summary'].startswith('报告窗口内：')
    assert result['presentation_kind']=='signal'


@pytest.mark.parametrize('mismatch', ['source', 'host', 'item', 'provenance_host', 'provenance_item'])
def test_metric_summary_cannot_borrow_a_fact_with_mismatched_identity(showcase_env, mismatch):
    from platform_app.health_signals import scan_group_changes
    from platform_app.intelligence import issue_response
    e = showcase_env
    now, points = series()
    row = numeric_evidence(e, points)
    [finding] = scan_group_changes(e['db'], e['policy'], now)
    manifest = frozen_manifest(e)
    meta = dict(row.payload['item'])
    provenance = {**row.coverage, 'hostid': '101', 'itemid': '1234'}
    if mismatch == 'source':
        provenance['source_id'] = 'other-source'
    elif mismatch == 'host':
        meta['hostid'] = '102'
    elif mismatch == 'item':
        meta['itemid'] = '9999'
    else:
        provenance['hostid' if mismatch == 'provenance_host' else 'itemid'] = '9999'
    payload = {'template': 'zabbix_item_numeric', 'owner_collector': 'monitoring',
               'item': meta, 'points': points, 'coverage': {'complete': True},
               'provenance': provenance}
    item = m.ManifestItem(manifest_id=manifest.id, instance_id=e['a'].id,
                          item_type='time_series_window', payload=payload,
                          payload_sha256=digest(payload))
    e['db'].add(item)
    e['db'].flush()
    inv = e['investigation']
    inv.status = 'completed'
    inv.report = {'facts': [{'statement': 'Unrelated metric explanation',
                            'evidence_ids': ['manifest:' + item.id]}]}
    finding.latest_investigation_id = inv.id
    e['db'].commit()
    result = issue_response(e['db'], e['db'].get(m.Event, finding.event_id), finding)
    assert 'Unrelated metric explanation' not in result['summary']
