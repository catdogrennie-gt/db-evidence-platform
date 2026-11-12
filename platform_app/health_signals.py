"""Conservative change screening from explicitly typed, actual Zabbix samples.

This is a repeatable observation, not an inferred incident or root cause. Unknown
semantics, cumulative counters and incomplete recent history are not screened.
"""
from datetime import timedelta
import math
from statistics import median

from .common import iso, parse_time, PlatformError

MIN_BASELINE = 12
RECENT_COUNT = 3
CALCULATION = 'median_mad_sustained_change_v1'
REFERENCE_CONTRACT = 'observed-numeric-reference-v1'
REFERENCE_MEANING = 'observed_reference_not_health'


def observed_reference_identity(instance,mapping,identity):
    """Configuration refresh deliberately creates a new independent reference."""
    from .evidence import digest
    value={'instance_id':instance.id,'source_id':mapping.source_id,
           'hostid':identity[0],'itemid':identity[1],
           'mapping_version':mapping.version,'config_revision':instance.config_revision}
    return 'observed-reference:'+digest(value),value


def reference_value(row,identity):
    value=row.value if row and isinstance(row.value,dict) else {}
    if (value.get('contract')!=REFERENCE_CONTRACT or value.get('identity')!=identity
            or value.get('meaning')!=REFERENCE_MEANING or not isinstance(value.get('baseline'),dict)):
        return None
    return value


def remember_observed_reference(db,key,identity,result,evidence,now):
    """First writer wins; a later stable fault window cannot replace history.

    This is a measured comparison reference, not a healthy-state declaration.
    Long-term workload drift needs deliberate configuration refresh/relearning;
    never silently promote a sustained changed regime to a new normal.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from . import models as m
    value={'contract':REFERENCE_CONTRACT,'identity':identity,'baseline':result['baseline'],
           'meaning':REFERENCE_MEANING,'evidence_id':evidence.id,
           'learned_at':iso(now),'observed_at':iso(evidence.observed_at)}
    row=m.PlatformSetting(key=key,value=value)
    try:
        # Multiple policies may inspect the same object. Resolve a concurrent
        # first insert without overwriting the winner or rolling back other work.
        with db.begin_nested():
            db.add(row);db.flush([row])
        return row
    except IntegrityError:
        return db.scalar(select(m.PlatformSetting).where(m.PlatformSetting.key==key).with_for_update())


def assess_change(item, points, now, *, baseline=None):
    def unknown(reason):
        return {'state':'not_evaluated', 'reason':reason,
                'diagnosis':'change_only_not_root_cause'}
    if item.get('value_semantics') not in ('gauge','rate'):
        return unknown('value_semantics_unverified_or_cumulative')
    interval = item.get('interval')
    if (not item.get('interval_verified') or isinstance(interval,bool)
            or not isinstance(interval,(int,float)) or not math.isfinite(interval) or interval <= 0):
        return unknown('sample_interval_unverified')
    if item.get('history_semantics') != 'periodic_samples':
        return unknown('sparse_history')
    if not isinstance(points,list) or len(points)>1000:
        return unknown('invalid_history')
    samples = {}
    try:
        for row in points:
            value = row.get('value')
            if (isinstance(value,bool) or not isinstance(value,(int,float))
                    or not math.isfinite(value) or row.get('validity','valid')!='valid'):
                return unknown('invalid_sample')
            stamp = parse_time(row['time'])
            if stamp > now: return unknown('future_sample')
            if stamp in samples and samples[stamp] != value:
                return unknown('conflicting_duplicate')
            samples[stamp] = float(value)
    except (KeyError,TypeError,ValueError,AttributeError,PlatformError):
        return unknown('invalid_sample')
    ordered = sorted(samples.items())
    if len(ordered)<RECENT_COUNT: return unknown('insufficient_recent_samples')
    if (now-ordered[-1][0]).total_seconds()>max(20,interval*3):
        return unknown('stale_samples')
    if any((b[0]-a[0]).total_seconds()>interval*1.8 for a,b in zip(ordered,ordered[1:])):
        return unknown('sample_gap')
    recent = ordered[-RECENT_COUNT:]
    if (recent[-1][0]-recent[0][0]).total_seconds()<interval*1.5:
        return unknown('insufficient_recent_duration')
    if baseline is None:
        before = ordered[:-RECENT_COUNT]
        if len(before)<MIN_BASELINE or (before[-1][0]-before[0][0]).total_seconds()<60:
            return unknown('insufficient_baseline')
        center = median(v for _,v in before)
        mad = median(abs(v-center) for _,v in before)
        if mad > max(abs(center)*.2,1e-9):
            return unknown('unstable_baseline')
        baseline = {'from':iso(before[0][0]),'to':iso(before[-1][0]),
                    'sample_count':len(before),'median':center,'mad':mad}
    else:
        try:
            center, mad = baseline['median'], baseline['mad']
            if (any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v)
                    for v in (center,mad)) or mad<0 or baseline['sample_count']<MIN_BASELINE
                    or (parse_time(baseline['to'])-parse_time(baseline['from'])).total_seconds()<60
                    or parse_time(baseline['to'])>=recent[0][0]):
                return unknown('invalid_baseline')
        except (KeyError,TypeError,ValueError,PlatformError):
            return unknown('invalid_baseline')
    current = median(v for _,v in recent)
    direction = 1 if current>=center else -1
    bound = max(6*mad,abs(center)*.75,1e-9)
    changed = all((v-center)*direction>bound for _,v in recent)
    returned = all(abs(v-center)<=max(3*mad,abs(center)*.35,1e-9) for _,v in recent)
    return {'state':'changed' if changed else 'returned_to_baseline' if returned else 'stable',
            'baseline':baseline,'recent':{'from':iso(recent[0][0]),'to':iso(recent[-1][0]),
                'sample_count':len(recent),'median':current,'min':min(v for _,v in recent),
                'max':max(v for _,v in recent)},
            'direction':'increase' if direction==1 else 'decrease',
            'threshold_distance':bound,'calculation':CALCULATION,
            'diagnosis':'change_only_not_root_cause','baseline_basis':REFERENCE_MEANING}


def scan_group_changes(db, policy, now):
    """Check only current selected identities; keep one stable change episode."""
    from sqlalchemy import select
    from . import models as m
    from .evidence import digest
    from .monitoring import mapping_for
    from .intelligence import upsert_finding
    found = []
    for identifier in policy.instance_ids:
        mapping=mapping_for(db,identifier)
        instance=db.get(m.Instance,identifier)
        if not mapping or mapping.layer!='group' or not mapping.selected or not instance: continue
        selected={(str(i.get('hostid')),str(i.get('itemid'))) for i in mapping.items}
        from .numeric_evidence import latest_numeric_evidence
        candidates=latest_numeric_evidence(db,identifier,mapping.source_id,selected,
            observed_since=now-timedelta(minutes=5))
        seen=set()
        for evidence in candidates:
            payload=evidence.payload or {}; meta=payload.get('item') or {}; coverage=evidence.coverage or {}
            identity=(str(meta.get('hostid')),str(meta.get('itemid')))
            if identity in seen or identity not in selected: continue
            seen.add(identity)
            if (coverage.get('availability')!='available'
                    or coverage.get('mapping_version')!=mapping.version
                    or coverage.get('config_revision')!=instance.config_revision
                    or coverage.get('source_id')!=mapping.source_id
                    or set(coverage.get('reason_codes') or [])-{'history_truncated'}): continue
            dimension='metric:'+':'.join(identity)
            old=db.scalar(select(m.InspectionFinding).where(m.InspectionFinding.fingerprint==digest([identifier,'baseline_change',dimension])).with_for_update())
            previous=dict(old.calculation or {}) if old else {}
            compatible=(previous.get('mapping_version')==mapping.version
                        and previous.get('config_revision')==instance.config_revision
                        and previous.get('source_id')==mapping.source_id)
            # Keep the measured reference after return as well. Otherwise a
            # fault-dominated sliding window can call the recovery itself a new
            # reverse anomaly on the very next check. A configuration revision
            # intentionally invalidates this reference and requires fresh data.
            key,reference_identity=observed_reference_identity(instance,mapping,identity)
            # References are immutable: do not gap-lock an absent key before
            # the first-insert/savepoint arbitration below.
            reference=db.scalar(select(m.PlatformSetting).where(m.PlatformSetting.key==key))
            saved=reference_value(reference,reference_identity)
            if reference is not None and saved is None:continue  # Corrupt identity stays unknown.
            baseline=(saved['baseline'] if saved is not None else
                previous.get('baseline') if compatible and previous.get('observation_state') in ('active','returned_to_baseline') else None)
            result=assess_change(meta,payload.get('rows'),now,baseline=baseline)
            if result['state']=='not_evaluated': continue
            stamp=parse_time(result['recent']['to'])
            if previous.get('observed_at') and parse_time(previous['observed_at'])>stamp: continue
            if baseline is None and result['state'] in ('stable','returned_to_baseline'):
                reference=remember_observed_reference(db,key,reference_identity,result,evidence,now)
                saved=reference_value(reference,reference_identity)
                if saved is None:continue
                # A concurrent policy can have saved an earlier valid reference.
                # Re-evaluate against that winner rather than our discarded one.
                if saved['baseline']!=result['baseline']:
                    result=assess_change(meta,payload.get('rows'),now,baseline=saved['baseline'])
                    if result['state']=='not_evaluated':continue
                baseline=saved['baseline']
            if saved is not None:result['reference_key']=key
            if result['state']=='changed':
                first=previous.get('episode_started_at') if compatible and baseline and previous.get('observation_state')=='active' else iso(stamp)
                calc={**result,'observation_state':'active','episode_started_at':first,
                      'observed_at':iso(stamp),'hostid':identity[0],'itemid':identity[1],
                      'mapping_version':mapping.version,'config_revision':instance.config_revision,
                      'source_id':mapping.source_id}
                finding=upsert_finding(db,identifier,'baseline_change',dimension,[evidence.id],calc,now=stamp)
                finding.signal_hash=digest([identifier,'baseline_change',dimension,first])
                found.append(finding)
            elif old and compatible and baseline and result['state']=='returned_to_baseline':
                old.calculation={**previous,**result,'observation_state':'returned_to_baseline',
                                 'observed_at':iso(stamp),'returned_at':previous.get('returned_at',iso(stamp))}
                old.evidence_ids=[evidence.id]
        # Bound urgent model work; later periodic analysis still sees selected data.
    return found[:12]
