"""Source-qualified point facts; never verification of model-authored prose."""
import hashlib
import json
from pathlib import Path
import re

from sqlalchemy import select

from . import models as m
from .common import iso
from .db import utcnow
from .evidence import digest
from .request_delivery import (MAX_AUDIT_BYTES, prepare_request_delivery,
    confirm_request_delivery, request_sha256)
from .zabbix_diagnostics import CONTRACT, LEGACY_LOCK_CONTRACT, definition, legacy_lock_definition

UNKNOWN = ['continuous_duration', 'incident_count', 'release_mechanism', 'current_state']


def _result(reason=None):
    return {'version': 'report-grounding-v1', 'status': 'unverified',
        'reason_codes': [reason] if reason else [], 'admitted_facts': [],
        'verified_headline': None, 'model_narrative_verification': 'unverified',
        'model_selections': {'status':'not_evaluated','decisions':[],'admitted_point_ids':[]},
        'not_established': list(UNKNOWN)}


def _positive_int(value):
    return type(value) is int and value >= 1


def _qualified_source_point(row, member, manifest):
    """Origin is payload-hashed; mutable coverage cannot grant capabilities."""
    payload = row.payload if isinstance(row.payload, dict) else {}
    if (row.instance_id != member.instance_id or row.source != 'mysql.lock_waits'
            or row.schema_version != '1' or row.collector_version != 'zabbix-generic-1'
            or row.source_mode != 'live' or not manifest.window_start <= row.observed_at < manifest.window_end
            or digest(payload) != member.payload_sha256 or row.payload_sha256 != member.payload_sha256
            or member.payload != {'source': row.source, 'observed_at': iso(row.observed_at)}):
        return None, 'frozen_point_mismatch'
    origin = payload.get('diagnostic_origin')
    if not isinstance(origin, dict) or origin.get('version') != 'diagnostic-origin-v1':
        return None, 'source_origin_missing_or_unsupported'
    producer, identity, point = (origin.get(name) for name in ('producer', 'collection', 'observation'))
    if not all(isinstance(value, dict) for value in (producer, identity, point)):
        return None, 'source_origin_incomplete'
    expected_point = {'instance_id': row.instance_id, 'source': row.source,
        'source_mode': row.source_mode, 'observed_at': iso(row.observed_at)}
    metadata = payload.get('item') if isinstance(payload.get('item'), dict) else {}
    spec = definition(metadata) or legacy_lock_definition(metadata)
    if (point != expected_point or spec is None or spec['query'] != 'locks'
            or spec['source'] != row.source or identity.get('transport') != 'zabbix'
            or not isinstance(identity.get('source_id'), str)
            or re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', identity['source_id']) is None
            or not all(isinstance(identity.get(key), str) and re.fullmatch(r'[0-9]{1,20}', identity[key])
                and identity[key] == metadata.get(key) for key in ('hostid', 'itemid'))
            or not all(_positive_int(identity.get(key)) for key in ('mapping_version', 'config_revision'))):
        return None, 'source_identity_or_capability_mismatch'
    rows = payload.get('rows')
    if (not isinstance(rows, list) or len(rows) > 50 or any(not isinstance(value, dict) for value in rows)
            or producer.get('contract') != spec.get('contract', CONTRACT)
            or producer.get('query_name') != 'locks'
            or producer.get('scope') != 'current_snapshot'
            or type(producer.get('snapshot_complete')) is not bool
            or type(producer.get('snapshot_truncated')) is not bool
            or producer['snapshot_complete'] == producer['snapshot_truncated']
            or type(producer.get('snapshot_row_count')) is not int
            or producer['snapshot_row_count'] != len(rows)
            or type(producer.get('returned_row_count')) is not int
            or producer['returned_row_count'] != len(rows) + int(producer['snapshot_truncated'])
            or producer['snapshot_truncated'] and len(rows) != 50
            or not rows and producer['snapshot_complete'] is not True
            or spec.get('contract') == LEGACY_LOCK_CONTRACT and
                (producer['snapshot_complete'] is not True or producer['snapshot_truncated'] is not False)):
        return None, 'snapshot_capability_not_proven'
    return {'fact_id': row.id, 'evidence_id': row.id, 'kind': 'lock_snapshot_point',
        'instance_id': row.instance_id, 'source': row.source, 'observed_at': iso(row.observed_at),
        'row_count': len(rows),
        'count_scope': 'exact_snapshot' if producer['snapshot_complete'] else 'at_least_projected_rows',
        'source_identity': {key: identity[key] for key in ('source_id', 'hostid', 'itemid', 'mapping_version', 'config_revision')},
        'payload_sha256': member.payload_sha256}, None


def _delivered(fact, observation):
    supplied = observation.get('point') or {}
    metadata = supplied.get('item') or {}
    expected = {key: fact[key] for key in ('instance_id', 'source', 'observed_at')}
    expected['source_mode'] = 'live'
    return (all(supplied.get(key) == value for key, value in expected.items())
        and all(metadata.get(key) == fact['source_identity'][key] for key in ('source_id', 'hostid', 'itemid'))
        and type(supplied.get('row_count')) is int and supplied['row_count'] == fact['row_count']
        and supplied.get('availability') == 'available'
        and not supplied.get('observation_unavailable') and not supplied.get('text_omitted'))


def _identity(fact):
    return (fact['instance_id'], fact['source'], *(fact['source_identity'][key] for key in
        ('source_id', 'hostid', 'itemid', 'mapping_version', 'config_revision')))


def report_grounding(actor, investigation):
    """Recheck current scope and the exact final request on every read.

    Old reports/audits are untouched. Missing receipts or prospective origin
    are not repaired by reading current metadata or earlier-call unions.
    """
    from .investigations import get_investigation
    inv = get_investigation(actor, investigation.id)
    if inv.status not in ('completed', 'partial') or inv.conclusion_type != 'live_model':
        return _result('no_completed_live_model_report')
    db = actor.db
    call = db.scalar(select(m.ModelCall).where(m.ModelCall.investigation_id == inv.id).execution_options(populate_existing=True)
        .order_by(m.ModelCall.sequence.desc()).limit(1))
    response = call.response if call and isinstance(call.response, dict) else {}
    receipt, binding = response.get('request_delivery'), response.get('report_output')
    if not isinstance(receipt, dict):
        return _result('actual_delivery_receipt_missing')
    candidate = {key: value for key, value in (inv.report or {}).items()
        if key not in ('coverage', 'policy_versions', 'prompt_version')}
    report_state = (inv.manifest_id, inv.window_start, inv.window_end, inv.status, inv.conclusion_type)
    if (call.status != 'succeeded' or not isinstance(binding, dict)
            or binding.get('version') != 'report-output-binding-v1'
            or binding.get('status') != 'structure_accepted_unverified'
            or binding.get('hash_scope') != 'validated_candidate_before_server_envelope'
            or binding.get('request_sha256') != call.input_sha256
            or binding.get('scope_ref') != inv.manifest_id or call.scope_ref != inv.manifest_id
            or binding.get('sequence') != call.sequence
            or binding.get('normalized_report_sha256') != request_sha256(candidate)
            or receipt.get('status') != 'response_received'):
        return _result('report_output_binding_unconfirmed')
    config = db.info.get('platform_config')
    filename = response.get('private_audit_ref')
    if config is None or not isinstance(filename, str) or re.fullmatch(r'[1-4]-[0-9a-f]{32}\.json', filename) is None:
        return _result('actual_request_audit_unavailable')
    try:
        path = Path(config.private_dir) / 'model-audit' / inv.id / filename
        if path.stat().st_size > MAX_AUDIT_BYTES:
            return _result('actual_request_audit_invalid')
        raw = path.read_bytes()
        audit = json.loads(raw)
        payload = audit['request']
        if not isinstance(payload, dict) or request_sha256(payload) != call.input_sha256:
            return _result('actual_request_audit_invalid')
        expected = prepare_request_delivery(payload, investigation_id=inv.id,
            manifest_id=inv.manifest_id, sequence=call.sequence)
        confirmed = confirm_request_delivery(expected, payload, path)
        if (confirmed.get('status') != 'response_received' or confirmed != receipt
                or hashlib.sha256(raw).hexdigest() != receipt.get('audit_sha256')
                or receipt.get('projection_inventory_complete') is not True):
            return _result('actual_request_audit_invalid')
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return _result('actual_request_audit_invalid')
    manifest = db.get(m.EvidenceManifest, inv.manifest_id, populate_existing=True)
    resources = set(db.scalars(select(m.InvestigationResource.instance_id).where(m.InvestigationResource.investigation_id == inv.id)))
    if (not manifest or manifest.owner_type != 'investigation' or manifest.owner_id != inv.id
            or not isinstance(manifest.instance_ids, list) or set(manifest.instance_ids) != resources
            or manifest.window_start != inv.window_start or manifest.window_end != inv.window_end
            or manifest.retain_until <= utcnow()):
        return _result('frozen_manifest_invalid_or_expired')
    try:
        context = json.loads(payload['messages'][1]['content'])
        request_scope_matches = (context.get('from') == iso(inv.window_start)
            and context.get('to') == iso(inv.window_end)
            and isinstance(context.get('instance_ids'), list) and set(context['instance_ids']) == resources)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        request_scope_matches = False
    if not request_scope_matches:
        return _result('actual_request_scope_mismatch')
    roots = list(db.scalars(select(m.ManifestItem).execution_options(populate_existing=True).where(m.ManifestItem.manifest_id == manifest.id,
        m.ManifestItem.item_type == 'coverage')))
    if (len(roots) != 1 or not isinstance(roots[0].payload, dict)
            or digest(roots[0].payload) != roots[0].payload_sha256
            or roots[0].payload_sha256 != manifest.manifest_sha256):
        return _result('frozen_manifest_root_mismatch')
    if (roots[0].payload.get('instance_ids') != sorted(resources)
            or roots[0].payload.get('window') != {'from': iso(inv.window_start), 'to': iso(inv.window_end)}):
        return _result('frozen_manifest_root_mismatch')
    root_refs = roots[0].payload.get('evidence')
    if not isinstance(root_refs, list) or any(not isinstance(value, dict) for value in root_refs):
        return _result('frozen_manifest_root_mismatch')
    hashes = {value.get('id'): value.get('sha256') for value in root_refs}
    if len(hashes) != len(root_refs):
        return _result('frozen_manifest_root_mismatch')
    refs = {point['evidence_id'] for point in receipt['observations']}
    # Inspect the frozen candidates for contradictions even when packing did
    # not deliver them. They can veto facts but can never supply new ones.
    frozen = db.execute(select(m.ManifestItem, m.Evidence).execution_options(populate_existing=True).outerjoin(m.Evidence,
        m.ManifestItem.evidence_id == m.Evidence.id).where(m.ManifestItem.manifest_id == manifest.id,
        m.ManifestItem.item_type == 'evidence').order_by(m.ManifestItem.id).limit(2001)).all()
    if len(frozen) > 2000:
        return _result('frozen_conflict_check_incomplete')
    members_by_id = {}
    rows = {}
    for member, row in frozen:
        members_by_id.setdefault(member.evidence_id, []).append(member)
        if row is not None:
            rows[row.id] = row
    runs = {run.id: run for run in db.scalars(select(m.CollectionRun).execution_options(populate_existing=True).where(
        m.CollectionRun.id.in_({row.run_id for row in rows.values()})))} if rows else {}
    result = _result()
    source_facts, source_reasons = {}, {}
    for identifier, candidates in members_by_id.items():
        row = rows.get(identifier)
        if (len(candidates) != 1 or row is None or candidates[0].instance_id not in resources
                or hashes.get(identifier) != candidates[0].payload_sha256):
            source_reasons[identifier] = 'frozen_member_missing_or_mismatched'
            continue
        member = candidates[0]
        fact, reason = _qualified_source_point(row, member, manifest)
        run = runs.get(row.run_id) if fact else None
        if fact and (run is None or run.instance_id != row.instance_id or run.collector != 'monitoring'
                or run.config_revision != fact['source_identity']['config_revision']):
            fact, reason = None, 'collection_config_mismatch'
        if fact:
            source_facts[identifier] = fact
        elif reason:
            source_reasons[identifier] = reason
    # Conflicting points at one source/config/time cannot be resolved by picking
    # the model's preferred value. Different identities never form one headline.
    groups = {}
    for fact in source_facts.values():
        key = (*_identity(fact), fact['observed_at'])
        groups.setdefault(key, []).append(fact)
    conflicts = {fact['fact_id'] for group in groups.values() if len({(f['row_count'],f['count_scope']) for f in group}) > 1 for fact in group}
    if conflicts & refs:
        result['reason_codes'].append('conflicting_same_time_points')
    facts = {}
    for observation in receipt['observations']:
        identifier = observation['evidence_id']
        fact = source_facts.get(identifier)
        if fact and _delivered(fact, observation):
            facts[identifier] = fact
        else:
            result['reason_codes'].append(source_reasons.get(identifier,
                'required_point_fields_not_delivered' if fact else 'frozen_member_missing_or_mismatched'))
    result['admitted_facts'] = sorted((fact for key, fact in facts.items() if key not in conflicts),
        key=lambda fact: (fact['observed_at'], fact['fact_id']))
    result['reason_codes'] = sorted(set(result['reason_codes']))
    if result['admitted_facts']:
        result['status'] = 'point_facts_only'
        identities = {_identity(fact) for fact in result['admitted_facts']}
        if len(identities) == 1:
            result['verified_headline'] = ('历史采样观察到 MySQL 锁等待（仅时点事实）'
                if any(fact['row_count'] for fact in result['admitted_facts'])
                else '历史采样时点未见 MySQL 锁等待（不代表全窗口健康）')
    elif not result['reason_codes']:
        result['reason_codes'].append('no_qualified_lock_points_delivered')
    # Audit IO and frozen-member checks can outlive an authorization change.
    # Recheck before returning even metadata/counts, not just at entry.
    current = get_investigation(actor, inv.id)
    current_candidate = {key:value for key,value in (current.report or {}).items()
        if key not in ('coverage', 'policy_versions', 'prompt_version')}
    current_resources = set(db.scalars(select(m.InvestigationResource.instance_id).where(
        m.InvestigationResource.investigation_id == inv.id)))
    if (report_state != (current.manifest_id, current.window_start, current.window_end, current.status, current.conclusion_type)
            or current_resources != resources
            or request_sha256(current_candidate) != binding['normalized_report_sha256']):
        return _result('report_changed_during_validation')
    from .fact_selection import evaluate_selections
    result['model_selections'] = evaluate_selections(inv.report or {}, result['admitted_facts'])
    return result
