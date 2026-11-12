"""Recheck rule observations inside an authorized immutable report window."""

from hashlib import sha256

from fastapi import HTTPException
from sqlalchemy import select

from . import models as m
from .access import authorize
from .capacity_signals import CLASSIFICATION, _configurations, _usable, assess_capacity
from .common import PlatformError, iso, parse_time
from .evidence import digest
from .zabbix_items import safe_text


def _rows_in_window(rows, start, end):
    if not isinstance(rows, list) or len(rows) > 1000:
        return None
    retained = []
    for point in rows:
        if not isinstance(point, dict):
            return None
        try:
            stamp = parse_time(point.get('time'))
        except (PlatformError, TypeError, ValueError, AttributeError):
            return None
        if start <= stamp < end:
            retained.append(point)
    return retained


def _operand(db, actor, inv, signal, identifier):
    member = db.scalar(select(m.ManifestItem).where(
        m.ManifestItem.manifest_id == inv.manifest_id,
        m.ManifestItem.item_type == 'evidence',
        m.ManifestItem.evidence_id == identifier))
    row = db.get(m.Evidence, identifier)
    if (not member or not row or not isinstance(member.payload, dict)
            or not isinstance(row.payload, dict) or not isinstance(row.coverage, dict)):
        return None
    if (not member or not row or member.instance_id != signal.get('instance_id')
            or row.instance_id != member.instance_id or row.source_mode != 'live'
            or not row.source.startswith('zabbix.item.numeric:')
            or not inv.window_start <= row.observed_at < inv.window_end
            or member.payload_sha256 != row.payload_sha256
            or digest(row.payload) != row.payload_sha256 or not _usable(row)):
        return None
    recorded = member.payload or {}
    if recorded.get('source') != row.source or recorded.get('observed_at') != iso(row.observed_at):
        return None
    if row.sensitivity == 'restricted':
        try:
            authorize(actor, 'evidence.read_sensitive', [row.instance_id])
        except HTTPException:
            return None
    payload, coverage = row.payload or {}, row.coverage or {}
    item = payload.get('item')
    if not isinstance(item, dict):
        return None
    calculation = signal['calculation']
    for key in ('source_id', 'mapping_version', 'config_revision'):
        if coverage.get(key) is None or coverage[key] != calculation.get(key):
            return None
    if not isinstance(coverage['source_id'], str) or not coverage['source_id']:
        return None
    hostid, itemid = item.get('hostid'), item.get('itemid')
    if (not isinstance(hostid, str) or not isinstance(itemid, str)
            or coverage.get('hostid') != hostid or coverage.get('itemid') != itemid
            or hostid != calculation.get('hostid')):
        return None
    source = 'zabbix.item.numeric:' + sha256(
        f"{coverage['source_id']}:{hostid}:{itemid}".encode()).hexdigest()[:24]
    if source != row.source:
        return None
    rows = _rows_in_window(payload.get('rows'), inv.window_start, inv.window_end)
    return (item, rows) if rows is not None else None


def _capacity_fact(actor, inv, signal):
    calc = signal.get('calculation')
    refs = signal.get('evidence_ids')
    if (signal.get('category') != 'capacity_pressure' or signal.get('kind') != 'risk'
            or not isinstance(calc, dict) or calc.get('classification') != CLASSIFICATION
            or calc.get('observation_state') != 'active' or not isinstance(refs, list)
            or len(refs) != 2 or any(not isinstance(ref, str) for ref in refs)
            or len(set(refs)) != 2):
        return None
    provenance = calc.get('provenance') or {}
    if not isinstance(provenance, dict):
        return None
    used_ref, limit_ref = provenance.get('used_evidence_id'), provenance.get('limit_evidence_id')
    if not isinstance(used_ref, str) or not isinstance(limit_ref, str):
        return None
    if {used_ref, limit_ref} != set(refs):
        return None
    used = _operand(actor.db, actor, inv, signal, used_ref)
    limit = _operand(actor.db, actor, inv, signal, limit_ref)
    if used is None or limit is None:
        return None
    used_item, used_rows = used
    limit_item, limit_rows = limit
    metadata = {(item['hostid'], item['itemid']): item for item in (used_item, limit_item)}
    configuration = (calc.get('hostid'), calc.get('resource_group'),
        (calc.get('hostid'), calc.get('used_itemid')),
        (calc.get('hostid'), calc.get('limit_itemid')), calc.get('warning_percent'))
    if configuration not in _configurations(metadata):
        return None
    result = assess_capacity(used_item, used_rows, limit_item, limit_rows, inv.window_end,
                             warning_percent=configuration[-1])
    fields = ('used', 'limit', 'utilization_percent', 'remaining', 'warning_percent',
              'unit', 'sample_count', 'time_pairs', 'observed_at')
    if (result.get('state') != 'active' or not isinstance(result.get('unit'), str)
            or any(result.get(key) != calc.get(key) for key in fields)):
        return None
    title = safe_text(used_item.get('name') or '资源', 60) + '容量余量不足'
    statement = (f"用量 {result['used']:g} / {result['limit']:g}，占用 {result['utilization_percent']:g}%，"
                 f"已达到配置的 {result['warning_percent']:g}% 关注水位，剩余 {result['remaining']:g}。")
    return dict(kind='capacity_pressure', instance_id=signal['instance_id'], title=title,
                statement=statement, observed_at=result['observed_at'],
                evidence_ids=[used_ref, limit_ref],
                calculation={key: result[key] for key in fields if key != 'observed_at'})


def frozen_facts(actor, investigation):
    """Return only re-proven frozen facts, never current policy or new samples.

    Authorization covers the complete report first. Missing/expired evidence,
    legacy signals and hidden sensitive operands yield no derived fact.
    """
    from .investigations import get_investigation

    inv = get_investigation(actor, investigation.id)
    db = actor.db
    manifest = db.get(m.EvidenceManifest, inv.manifest_id) if inv.manifest_id else None
    resources = set(db.scalars(select(m.InvestigationResource.instance_id).where(
        m.InvestigationResource.investigation_id == inv.id)))
    if (not manifest or not isinstance(manifest.instance_ids, list)
            or manifest.owner_type != 'investigation' or manifest.owner_id != inv.id
            or set(manifest.instance_ids) != resources
            or manifest.window_start != inv.window_start or manifest.window_end != inv.window_end):
        return []
    task = db.get(m.Task, inv.task_id) if inv.task_id else None
    payload = task.payload if task and isinstance(task.payload, dict) else {}
    options = payload.get('options')
    inspection = options.get('inspection') if isinstance(options, dict) else None
    signals = inspection.get('signals', []) if isinstance(inspection, dict) else []
    if not isinstance(signals, list) or len(signals) > 100:
        return []
    result, seen = [], set()
    for signal in signals:
        if not isinstance(signal, dict) or signal.get('instance_id') not in resources:
            continue
        fact = _capacity_fact(actor, inv, signal)
        if fact and tuple(fact['evidence_ids']) not in seen:
            seen.add(tuple(fact['evidence_ids']))
            result.append(fact)
    return result
