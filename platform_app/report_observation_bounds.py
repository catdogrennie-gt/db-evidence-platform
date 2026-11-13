"""Re-proven frozen point observations, separate from model-authored conclusions."""
import re
from sqlalchemy import select

from . import models as m
from .common import iso
from .db import utcnow
from .evidence import digest
from .zabbix_items import safe_text

SOURCES = frozenset({'mysql.lock_waits', 'mysql.transactions', 'mysql.sessions'})


def frozen_observation_bounds(actor, investigation):
    from .investigations import get_investigation

    inv = get_investigation(actor, investigation.id)
    result = {'scope': 'frozen_manifest_point_observations',
              'window': {'from': iso(inv.window_start), 'to': iso(inv.window_end)},
              'groups': [], 'complete': False,
              'not_established': ['continuous_duration', 'recovery_mechanism', 'current_state']}
    db = actor.db
    manifest = db.get(m.EvidenceManifest, inv.manifest_id) if inv.manifest_id else None
    resources = set(db.scalars(select(m.InvestigationResource.instance_id).where(
        m.InvestigationResource.investigation_id == inv.id)))
    if (not manifest or manifest.owner_type != 'investigation' or manifest.owner_id != inv.id
            or not isinstance(manifest.instance_ids, list) or set(manifest.instance_ids) != resources
            or manifest.window_start != inv.window_start or manifest.window_end != inv.window_end
            or manifest.retain_until <= utcnow()):
        return result
    rows = db.execute(select(m.ManifestItem, m.Evidence).outerjoin(
        m.Evidence, m.ManifestItem.evidence_id == m.Evidence.id).where(
        m.ManifestItem.manifest_id == manifest.id, m.ManifestItem.item_type == 'evidence')
        .order_by(m.ManifestItem.id).limit(2001)).all()
    complete = len(rows) <= 2000
    buckets = {}
    seen = set()
    for member, row in rows[:2000]:
        recorded = member.payload if isinstance(member.payload, dict) else {}
        if recorded.get('source') not in SOURCES and row and row.source not in SOURCES:
            continue
        if (not row or member.instance_id not in resources or row.instance_id != member.instance_id
                or row.source not in SOURCES or row.source_mode != 'live'
                or not inv.window_start <= row.observed_at < inv.window_end
                or recorded.get('source') != row.source
                or recorded.get('observed_at') != iso(row.observed_at)
                or not isinstance(row.payload, dict) or digest(row.payload) != member.payload_sha256
                or row.payload_sha256 != member.payload_sha256):
            complete = False
            continue
        # Like evidence_response's redacted branch, expose observation metadata
        # under scoped data.read. Never return raw rows or sensitive field values,
        # even when the caller separately holds evidence.read_sensitive.
        coverage = row.coverage if isinstance(row.coverage, dict) else {}
        sample_rows = row.payload.get('rows')
        if (coverage.get('availability') not in {'available', 'degraded', 'unknown'}
                or not isinstance(sample_rows, list) or not all(isinstance(r, dict) for r in sample_rows)):
            complete = False
            continue
        count = len(sample_rows)
        metadata = row.payload.get('item') if isinstance(row.payload.get('item'), dict) else {}
        identity = (coverage.get('source_id'), metadata.get('hostid'), metadata.get('itemid'))
        known = (isinstance(identity[0], str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', identity[0])
                 and all(isinstance(value, str) and re.fullmatch(r'[0-9]{1,20}', value) for value in identity[1:]))
        key = (row.instance_id, row.source, *identity) if known else (row.instance_id, row.source, row.id)
        if not known or coverage.get('complete') is not True or row.truncated:
            complete = False
        if row.id not in seen:
            buckets.setdefault(key, []).append((row.observed_at, row.id, count))
            seen.add(row.id)
    for key, samples in sorted(buckets.items()):
        samples.sort()
        nonempty = [s for s in samples if s[2] > 0]
        later_empty = [s for s in samples if nonempty and s[2] == 0 and s[0] > nonempty[-1][0]]

        def reference(sample):
            return {'evidence_id': sample[1], 'observed_at': iso(sample[0])} if sample else None

        instance = db.get(m.Instance, key[0])
        result['groups'].append({'instance_id': key[0],
            'instance_name': safe_text(instance.name, 80) if instance else key[0], 'source': key[1],
            'identity': dict(zip(('source_id', 'hostid', 'itemid'), key[2:])) if len(key) == 5 else None,
            'sample_count': len(samples), 'nonempty_sample_count': len(nonempty),
            'first_observation': reference(samples[0]), 'last_observation': reference(samples[-1]),
            'first_nonempty': reference(nonempty[0]) if nonempty else None,
            'last_nonempty': reference(nonempty[-1]) if nonempty else None,
            'first_empty_after_last_nonempty': reference(later_empty[0]) if later_empty else None,
            'gap_to_next_empty_seconds': (later_empty[0][0] - nonempty[-1][0]).total_seconds()
                if later_empty else None})
    if len(result['groups']) > 8:
        complete = False
        result['groups'] = result['groups'][:8]
    result['complete'] = complete
    return result
