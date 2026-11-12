"""Rule facts belong to an authorized frozen window, never today's mapping."""
from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from platform_app import models as m
from platform_app.access import actor_for_user
from platform_app.common import iso
from platform_app.db import utcnow
from platform_app.evidence import digest
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_showcase import showcase_env
from tests.platform.test_capacity_signals import configure, metadata, points, scan, write


@pytest.fixture
def frozen_capacity(showcase_env):
    e = showcase_env
    db, inv = e['db'], e['investigation']
    stamp = utcnow() - timedelta(days=3)
    used_meta, limit_meta, used, limit = configure(e, stamp)
    finding = scan(e, stamp)[0]
    signal = dict(category=finding.category, kind=finding.kind, instance_id=e['a'].id,
                  signal_hash=finding.signal_hash, evidence_ids=deepcopy(finding.evidence_ids),
                  calculation=deepcopy(finding.calculation))
    inv.window_start, inv.window_end = stamp-timedelta(minutes=1), stamp+timedelta(seconds=1)
    inv.status, inv.report = 'completed', {'executive_summary': {'headline': 'original model wording'}}
    task = m.Task(type='investigation', instance_id=e['a'].id,
                  payload={'options': {'inspection': {'signals': [signal]}}})
    db.add(task); db.flush()
    inv.task_id = task.id
    manifest = m.EvidenceManifest(owner_type='investigation', owner_id=inv.id,
        instance_ids=[e['a'].id], window_start=inv.window_start, window_end=inv.window_end,
        sources=[used.source, limit.source], manifest_sha256='f'*64,
        retain_until=utcnow()+timedelta(days=90))
    db.add(manifest); db.flush()
    inv.manifest_id = manifest.id
    db.add(m.InvestigationResource(investigation_id=inv.id, instance_id=e['a'].id))
    items = []
    for evidence in (used, limit):
        item = m.ManifestItem(manifest_id=manifest.id, evidence_id=evidence.id,
            instance_id=evidence.instance_id, item_type='evidence',
            payload={'source': evidence.source, 'observed_at': iso(evidence.observed_at)},
            payload_sha256=evidence.payload_sha256)
        db.add(item); items.append(item)
    db.commit()
    e.update(stamp=stamp, used=used, limit=limit, used_meta=used_meta, limit_meta=limit_meta,
             frozen_manifest=manifest, frozen_items=items, frozen_task=task, signal=signal)
    return e


def facts(e, actor=None):
    from platform_app.report_facts import frozen_facts
    e['db'].flush()
    return frozen_facts(actor or e['reader'], e['investigation'])


def replace_signal(e, *, calculation=None, evidence_ids=None):
    signal = deepcopy(e['signal'])
    if calculation is not None:
        signal['calculation'] = calculation
    if evidence_ids is not None:
        signal['evidence_ids'] = evidence_ids
    e['frozen_task'].payload = {'options': {'inspection': {'signals': [signal]}}}


def rehash(e, evidence):
    evidence.payload_sha256 = digest(evidence.payload)
    for item in e['frozen_items']:
        if item.evidence_id == evidence.id:
            item.payload_sha256 = evidence.payload_sha256


def test_old_window_fact_survives_recovery_and_current_mapping_edits(frozen_capacity):
    e = frozen_capacity
    original = deepcopy((e['investigation'].report, e['used'].payload, e['limit'].payload))
    later = utcnow()
    write(e, e['used_meta'], points(later, (8, 8, 8)))
    write(e, e['limit_meta'], points(later, (40, 40, 40)))
    e['mapping'].version += 1
    e['mapping'].items = [metadata('999', 'used')]
    e['a'].config_revision += 1
    e['policy'].enabled = False
    e['policy'].instance_ids = []
    rows = facts(e)
    assert len(rows) == 1
    row = rows[0]
    assert row['kind'] == 'capacity_pressure' and row['instance_id'] == e['a'].id
    assert row['title'] and row['statement'] and row['observed_at'] == iso(e['stamp'])
    assert set(row['evidence_ids']) == {e['used'].id, e['limit'].id}
    calc = row['calculation']
    assert {k: calc[k] for k in ('used', 'limit', 'utilization_percent', 'remaining', 'warning_percent', 'unit', 'sample_count')} == dict(
        used=34, limit=40, utilization_percent=85, remaining=6, warning_percent=80, unit='', sample_count=3)
    assert len(calc['time_pairs']) == 3
    assert original == (e['investigation'].report, e['used'].payload, e['limit'].payload)


@pytest.mark.parametrize('damage', ['missing_ref', 'foreign_manifest', 'manifest_hash', 'payload_hash',
    'source', 'host', 'mapping', 'revision', 'tag_group', 'tag_role', 'tag_warning', 'signal_value',
    'signal_threshold', 'replay', 'unavailable'])
def test_invalid_operand_or_frozen_signal_never_yields_a_fact(frozen_capacity, damage):
    e = frozen_capacity
    row = e['limit']
    if damage == 'missing_ref':
        replace_signal(e, evidence_ids=[e['used'].id, 'missing'])
    elif damage == 'foreign_manifest':
        e['db'].delete(e['frozen_items'][1])
    elif damage == 'manifest_hash':
        e['frozen_items'][1].payload_sha256 = '0'*64
    elif damage == 'payload_hash':
        row.payload = {**row.payload, 'rows': points(e['stamp'], (41, 41, 41))}
    elif damage in ('source', 'host', 'mapping', 'revision'):
        key, value = {'source': ('source_id', 'other-source'), 'host': ('hostid', '202'),
                      'mapping': ('mapping_version', 99), 'revision': ('config_revision', 99)}[damage]
        row.coverage = {**row.coverage, key: value}
    elif damage.startswith('tag_'):
        item = deepcopy(row.payload['item'])
        key, value = {'tag_group': ('sl.capacity.group', 'another-group'),
                      'tag_role': ('sl.capacity.role', 'used'),
                      'tag_warning': ('sl.capacity.warning_pct', '90')}[damage]
        item['tags'] = [{**tag, 'value': value} if tag['tag'] == key else tag for tag in item['tags']]
        row.payload = {**row.payload, 'item': item}; rehash(e, row)
    elif damage.startswith('signal_'):
        calc = deepcopy(e['signal']['calculation'])
        calc['used' if damage == 'signal_value' else 'warning_percent'] = 31 if damage == 'signal_value' else 90
        replace_signal(e, calculation=calc)
    elif damage == 'replay':
        row.source_mode = 'replay'
    elif damage == 'unavailable':
        row.coverage = {**row.coverage, 'availability': 'unavailable'}
    assert facts(e) == []


@pytest.mark.parametrize('damage', ['insufficient', 'stale', 'only_over_window'])
def test_missing_or_out_of_window_pairs_do_not_prove_risk(frozen_capacity, damage):
    e = frozen_capacity
    rows = points(e['stamp'])
    if damage == 'insufficient': rows = rows[-2:]
    elif damage == 'stale': rows = points(e['stamp']-timedelta(minutes=5))
    else: rows = points(e['investigation'].window_end+timedelta(seconds=20))
    e['used'].payload = {**e['used'].payload, 'rows': rows}
    rehash(e, e['used'])
    assert facts(e) == []


def test_post_window_recovery_rows_are_excluded_before_recomputation(frozen_capacity):
    e = frozen_capacity
    for evidence, values in ((e['used'], (8, 8, 8)), (e['limit'], (40, 40, 40))):
        evidence.payload = {**evidence.payload, 'rows': evidence.payload['rows'] +
            points(e['investigation'].window_end+timedelta(seconds=20), values)}
        rehash(e, evidence)
    rows = facts(e)
    assert len(rows) == 1 and rows[0]['calculation']['used'] == 34
    assert rows[0]['calculation']['sample_count'] == 3


def test_legacy_report_without_frozen_signal_returns_unknown_empty(frozen_capacity):
    e = frozen_capacity
    e['frozen_task'].payload = {'options': {}}
    assert facts(e) == []


def test_sensitive_operands_require_sensitive_permission(frozen_capacity):
    e = frozen_capacity
    e['used'].sensitivity = 'restricted'
    # Ordinary authorized report readers keep their existing report access;
    # the new projection must reveal neither a restricted value nor a reason/count.
    assert facts(e) == []
    role = e['db'].get(m.Role, e['grant'].role_id)
    role.permissions = [*role.permissions, 'evidence.read_sensitive']
    e['db'].flush()
    assert len(facts(e, actor_for_user(e['db'], e['actor'].user_id))) == 1


def test_mixed_resource_investigation_rejects_all_before_projecting(frozen_capacity):
    e = frozen_capacity
    e['db'].add(m.InvestigationResource(investigation_id=e['investigation'].id, instance_id=e['b'].id))
    with pytest.raises(HTTPException) as exc:
        facts(e)
    # Preserve get_investigation's existing anti-enumeration rejection.
    assert exc.value.status_code == 404


def test_investigation_detail_includes_frozen_facts_without_rewriting_ai_report(frozen_capacity):
    from platform_app.investigations import investigation_detail
    e = frozen_capacity
    before = deepcopy(e['investigation'].report)
    detail = investigation_detail(e['reader'], e['investigation'])
    assert detail['frozen_facts'] == facts(e)
    assert len(detail['frozen_facts']) == 1
    assert detail['report'] == before == e['investigation'].report


def test_issue_detail_uses_selected_report_frozen_facts(frozen_capacity):
    from platform_app.intelligence import issue_detail
    e = frozen_capacity
    inv = e['investigation']
    finding = e['db'].scalar(select(m.InspectionFinding).where(
        m.InspectionFinding.instance_id == e['a'].id))
    finding.latest_investigation_id = inv.id
    e['db'].add(m.EventTransition(event_id=finding.event_id, action='inspection_report',
        details={'investigation_id': inv.id, 'signal_hash': finding.signal_hash}))
    e['db'].commit()
    before = deepcopy(inv.report)
    detail = issue_detail(e['reader'], finding.event_id)
    assert detail['investigation_id'] == inv.id
    assert detail['frozen_facts'] == facts(e)
    assert len(detail['frozen_facts']) == 1
    assert detail['report'] == before == inv.report


@pytest.mark.parametrize('damage', ['task_payload', 'options', 'inspection', 'signals',
    'signal', 'provenance', 'coverage', 'manifest_record'])
def test_malformed_legacy_shapes_return_no_fact_instead_of_server_error(frozen_capacity, damage):
    e = frozen_capacity
    if damage == 'task_payload': e['frozen_task'].payload = ['legacy']
    elif damage == 'options': e['frozen_task'].payload = {'options': ['legacy']}
    elif damage == 'inspection': e['frozen_task'].payload = {'options': {'inspection': ['legacy']}}
    elif damage == 'signals': e['frozen_task'].payload = {'options': {'inspection': {'signals': {'legacy': True}}}}
    elif damage == 'signal': e['frozen_task'].payload = {'options': {'inspection': {'signals': ['legacy']}}}
    elif damage == 'provenance':
        calc = deepcopy(e['signal']['calculation']); calc['provenance'] = ['legacy']
        replace_signal(e, calculation=calc)
    elif damage == 'coverage': e['used'].coverage = ['legacy']
    else: e['frozen_items'][0].payload = ['legacy']
    assert facts(e) == []


@pytest.mark.parametrize('damage', ['owner', 'window', 'instances'])
def test_manifest_must_belong_to_exact_investigation_window_and_resources(frozen_capacity, damage):
    e = frozen_capacity
    manifest = e['frozen_manifest']
    if damage == 'owner': manifest.owner_id = e['frozen_task'].id
    elif damage == 'window': manifest.window_end += timedelta(seconds=1)
    else: manifest.instance_ids = [e['b'].id]
    assert facts(e) == []
