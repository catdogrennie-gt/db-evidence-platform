"""Explicitly configured, selected-item capacity facts; never a root-cause rule."""
import math
import re

from sqlalchemy import select

from . import models as m
from .common import PlatformError, iso, parse_time
from .evidence import digest
from .monitoring import mapping_for
from .numeric_evidence import latest_numeric_evidence


CALCULATION = 'configured_capacity_ratio_v1'
CLASSIFICATION = 'configured_threshold_risk_not_root_cause'
CATEGORY = 'capacity_pressure'
GROUP = re.compile(r'[A-Za-z0-9_.:-]{1,40}\Z')


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _interval(item):
    value = item.get('interval')
    if (item.get('value_kind') == 'numeric' and item.get('value_semantics') == 'gauge'
            and item.get('history_semantics') == 'periodic_samples'
            and item.get('interval_verified') is True and _finite(value) and value > 0):
        return value


def _unit(item):
    return item.get('units', item.get('unit', ''))


def _recent(item, points, now):
    interval = _interval(item)
    if interval is None or not isinstance(points, list) or len(points) > 1000:
        return None
    samples = {}
    try:
        for row in points:
            stamp = parse_time(row['time'])
            if stamp in samples and samples[stamp] != row:
                return None
            samples[stamp] = row
        recent = sorted(samples.items())[-3:]
        if len(recent) != 3 or recent[-1][0] > now:
            return None
        if (now-recent[-1][0]).total_seconds() > min(120, max(20, 3*interval)):
            return None
        if any(not _finite(row.get('value')) or row.get('validity', 'valid') != 'valid'
               for _, row in recent):
            return None
        if (recent[-1][0]-recent[0][0]).total_seconds() < interval*1.5:
            return None
        if any((b[0]-a[0]).total_seconds() > interval*1.8 for a, b in zip(recent, recent[1:])):
            return None
        return [(stamp, float(row['value'])) for stamp, row in recent]
    except (KeyError, TypeError, ValueError, AttributeError, PlatformError):
        return None


def assess_capacity(used_item, used_points, limit_item, limit_points, now, *, warning_percent):
    """Three distinct, fresh, synchronized observations support one local fact.

    Unknown/mixed observations never prove recovery. Older history is not a
    baseline or a health declaration, and invalid latest points are not skipped.
    """
    unknown = {'state': 'not_evaluated', 'classification': CLASSIFICATION}
    if (not _finite(warning_percent) or not 0 < warning_percent <= 100
            or _unit(used_item) != _unit(limit_item)):
        return unknown
    used = _recent(used_item, used_points, now)
    limit = _recent(limit_item, limit_points, now)
    if used is None or limit is None:
        return unknown
    skew_bound = min(30, max(_interval(used_item), _interval(limit_item)))
    pairs = []
    for (used_at, value), (limit_at, capacity) in zip(used, limit):
        if value < 0 or capacity <= 0 or abs((used_at-limit_at).total_seconds()) > skew_bound:
            return unknown
        percent = value/capacity*100
        if not math.isfinite(percent):
            return unknown
        pairs.append(dict(used_at=iso(used_at), limit_at=iso(limit_at), used=value,
                          limit=capacity, utilization_percent=percent, remaining=capacity-value))
    ratios = [pair['utilization_percent'] for pair in pairs]
    state = ('active' if all(value >= warning_percent for value in ratios) else
             'recovered' if all(value < warning_percent for value in ratios) else 'not_evaluated')
    return dict(state=state, used=pairs[-1]['used'], limit=pairs[-1]['limit'],
                utilization_percent=ratios[-1], remaining=pairs[-1]['remaining'],
                warning_percent=warning_percent, sample_count=3, time_pairs=pairs,
                min_utilization_percent=min(ratios), max_utilization_percent=max(ratios),
                unit=_unit(used_item), observed_at=iso(max(used[-1][0], limit[-1][0])),
                calculation=CALCULATION, classification=CLASSIFICATION,
                method={'formula': '100 * used / limit', 'consecutive_pairs': 3,
                        'maximum_pair_skew_seconds': skew_bound,
                        'used_interval_seconds': _interval(used_item),
                        'limit_interval_seconds': _interval(limit_item)})


def _tags(item):
    """Ambiguous declarations stay unknown, including duplicate tag keys."""
    tags = {}
    raw = item.get('tags')
    if not isinstance(raw, list):
        return None
    for tag in raw:
        if not isinstance(tag, dict):
            return None
        key = tag.get('tag')
        if key not in ('sl.capacity.group', 'sl.capacity.role', 'sl.capacity.warning_pct'):
            continue
        if key in tags:
            return None
        tags[key] = tag.get('value')
    return tags


def _configurations(metadata):
    groups, blocked_hosts = {}, set()
    for identity, item in metadata.items():
        tags = _tags(item)
        if tags == {}:
            continue
        if tags is None:
            blocked_hosts.add(identity[0]); continue
        group, role = tags.get('sl.capacity.group'), tags.get('sl.capacity.role')
        if not isinstance(group, str) or not GROUP.fullmatch(group) or role not in ('used', 'limit'):
            blocked_hosts.add(identity[0]); continue
        groups.setdefault((identity[0], group), []).append((identity, role, item, tags))
    result = []
    for (hostid, group), items in groups.items():
        if hostid in blocked_hosts or len(items) != 2 or {row[1] for row in items} != {'used', 'limit'}:
            continue
        used, limit = sorted(items, key=lambda row: row[1] == 'limit')
        try:
            threshold = float(limit[3]['sl.capacity.warning_pct'])
        except (KeyError, ValueError, TypeError):
            continue
        if (not _finite(threshold) or not 0 < threshold <= 100
                or 'sl.capacity.warning_pct' in used[3]):
            continue
        result.append((hostid, group, used[0], limit[0], threshold))
    return result


def _current_metadata(evidence, mapping, instance):
    coverage = evidence.coverage or {}
    if (coverage.get('source_id') != mapping.source_id
            or coverage.get('mapping_version') != mapping.version
            or coverage.get('config_revision') != instance.config_revision):
        return None
    item = evidence.payload['item']
    if any(coverage.get(key, item.get(key)) != item.get(key) for key in ('hostid', 'itemid')):
        return None
    return item


def _usable(evidence):
    coverage = evidence.coverage or {}
    reasons = coverage.get('reason_codes') or []
    gaps = coverage.get('gaps') or []
    if not isinstance(reasons, list) or not isinstance(gaps, list):
        return False
    return (coverage.get('availability') == 'available'
            and all(reason == 'history_truncated' for reason in reasons)
            and all((gap.get('reason') if isinstance(gap, dict) else gap) == 'history_truncated' for gap in gaps))


def current_capacity_fact(db, finding, mapping, instance, now, *, candidates=None):
    """Recheck one stored rule against both actual latest operands; never write.

    A fresh numerator alone cannot revive a stale denominator or configuration.
    The returned refs and values belong to this read, not to the frozen report.
    Callers may pass their canonical latest-item query to avoid duplicate reads.
    """
    previous = finding.calculation or {}
    if (finding.category != CATEGORY or finding.instance_id != instance.id
            or previous.get('observation_state') != 'active'
            or not mapping or mapping.layer != 'group' or not mapping.selected):
        return None
    selected = {(str(item.get('hostid')), str(item.get('itemid'))) for item in mapping.items}
    if candidates is None:
        candidates = latest_numeric_evidence(db, instance.id, mapping.source_id, selected)
    metadata, evidence_by_item = {}, {}
    for evidence in candidates:
        if evidence.instance_id != instance.id or evidence.source_mode != 'live':
            continue
        item = _current_metadata(evidence, mapping, instance)
        if item is not None:
            identity = (str(item.get('hostid')), str(item.get('itemid')))
            if identity in selected:
                metadata[identity], evidence_by_item[identity] = item, evidence
    for hostid, group, used_id, limit_id, threshold in _configurations(metadata):
        identity = dict(source_id=mapping.source_id, mapping_version=mapping.version,
                        config_revision=instance.config_revision, hostid=hostid,
                        used_itemid=used_id[1], limit_itemid=limit_id[1],
                        resource_group=group, warning_percent=threshold)
        if any(previous.get(key) != value for key, value in identity.items()):
            continue
        a, b = evidence_by_item[used_id], evidence_by_item[limit_id]
        if not _usable(a) or not _usable(b):
            return None
        if any(not 0 <= (now-row.observed_at).total_seconds() <= min(120,max(20,3*(_interval(meta) or 0)))
               for row,meta in ((a,metadata[used_id]),(b,metadata[limit_id]))):
            return None
        result = assess_capacity(metadata[used_id], a.payload.get('rows'),
                                 metadata[limit_id], b.payload.get('rows'), now,
                                 warning_percent=threshold)
        if result['state'] != 'active':
            return None
        return {'calculation': {**result, **identity, 'observation_state': 'active',
                                'episode_started_at': previous.get('episode_started_at')},
                'evidence_ids': sorted({a.id, b.id})}
    return None


def scan_group_capacity(db, policy, now):
    """Selected source/host pairs produce stable observed risk episodes only.

    Latest collection metadata supplies explicit configuration. Selection remains
    the exact mapping identity set; data loss neither recovers an episode nor
    silently reuses an older successful collection. No tasks are created here.
    """
    from .intelligence import upsert_finding
    found = []
    for identifier in policy.instance_ids:
        mapping, instance = mapping_for(db, identifier), db.get(m.Instance, identifier)
        if instance is None or mapping is None or mapping.layer != 'group' or not mapping.selected:
            continue
        selected = {(str(item.get('hostid')), str(item.get('itemid'))) for item in mapping.items}
        metadata, evidence_by_item = {}, {}
        for evidence in latest_numeric_evidence(db, identifier, mapping.source_id, selected):
            item = _current_metadata(evidence, mapping, instance)
            if item is not None:
                identity = (str(item.get('hostid')), str(item.get('itemid')))
                metadata[identity], evidence_by_item[identity] = item, evidence
        configurations = []
        for hostid, group, used_id, limit_id, threshold in _configurations(metadata):
            identity = dict(source_id=mapping.source_id, mapping_version=mapping.version,
                            config_revision=instance.config_revision, hostid=hostid,
                            used_itemid=used_id[1], limit_itemid=limit_id[1], resource_group=group,
                            warning_percent=threshold)
            dimension = 'capacity:'+digest(identity)
            configurations.append((dimension, identity, used_id, limit_id))
        current_dimensions = {row[0] for row in configurations}
        olds = list(db.scalars(select(m.InspectionFinding).where(
            m.InspectionFinding.instance_id == identifier, m.InspectionFinding.category == CATEGORY).with_for_update()))
        for old in olds:
            previous = dict(old.calculation or {})
            pair_ids = {(previous.get('hostid'), previous.get(key)) for key in ('used_itemid', 'limit_itemid')}
            changed = (any(previous.get(key) != value for key, value in (
                ('source_id', mapping.source_id), ('mapping_version', mapping.version),
                ('config_revision', instance.config_revision))) or not pair_ids <= selected
                or (pair_ids <= metadata.keys() and old.dimension not in current_dimensions))
            if changed and previous.get('observation_state') != 'configuration_changed':
                old.calculation = {**previous, 'observation_state': 'configuration_changed',
                                   'configuration_changed_at': iso(now)}
        for dimension, identity, used_id, limit_id in configurations:
            a, b = evidence_by_item[used_id], evidence_by_item[limit_id]
            if not _usable(a) or not _usable(b):
                continue
            result = assess_capacity(metadata[used_id], a.payload.get('rows'),
                                     metadata[limit_id], b.payload.get('rows'), now,
                                     warning_percent=identity['warning_percent'])
            if result['state'] == 'not_evaluated':
                continue
            old = next((row for row in olds if row.dimension == dimension), None)
            previous = dict(old.calculation or {}) if old else {}
            stamp = parse_time(result['observed_at'])
            if previous.get('observed_at') and parse_time(previous['observed_at']) > stamp:
                continue
            first = (previous.get('episode_started_at') if previous.get('observation_state') == 'active'
                     else result['observed_at'])
            calculation = {**result, **identity, 'observation_state': result['state'],
                           'episode_started_at': first,
                           'provenance': {'used_evidence_id': a.id, 'limit_evidence_id': b.id}}
            if result['state'] == 'active':
                finding = upsert_finding(db, identifier, CATEGORY, dimension, [a.id, b.id],
                                         calculation, kind='risk', now=stamp)
                finding.signal_hash = digest([identifier, CATEGORY, dimension, first])
                found.append(finding)
            elif old is not None and previous.get('observation_state') in ('active', 'recovered'):
                old.calculation = {**calculation, 'episode_started_at': previous.get('episode_started_at'),
                                   'recovered_at': previous.get('recovered_at', result['observed_at'])}
                old.evidence_ids = sorted({a.id, b.id})
    return found
