"""Durable inspection orchestration over the original Event/Investigation pipeline."""
from datetime import timedelta
import hashlib
import json
import math
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, func, or_

from . import models as m
from .access import actor_for_user, authorize, scoped_instance
from .common import PlatformError, iso, fields, parse_time
from .db import utcnow
from .evidence import digest
from .investigations import create_investigation, require_investigation_access, get_investigation, FindingProposal
from .config import group_collection_allowed, require_legacy
from .monitoring import mapping_for
from .scheduling import db_now, task_ref

POLICY_FIELDS = ['id','version','enabled','instance_ids','signal_interval_seconds','ai_interval_seconds','urgent_enabled','cooldown_seconds','max_daily_runs','mode','external_confirmed','next_signal_at','next_ai_at','last_dispatched_at','last_completed_at','status','reason']
LABELS = {'lock_wait': '存在数据库锁等待', 'sql_latency': 'SQL 窗口平均耗时偏高', 'cpu_pressure': '主机 CPU 持续压力', 'probe_unavailable': '数据库探测不可用', 'cpu_trend': 'CPU 增长趋势的条件性容量风险', 'baseline_change': '监控项出现持续基线变化，待关联分析', 'evidence_correlation': '待核验：AI 证据关联假设'}
LABELS['capacity_pressure'] = '资源容量余量不足'
RULE_CATEGORY = {'locks_present': 'lock_wait', 'sql_latency': 'sql_latency'}
TERMINAL_EVENTS = ('resolved','closed','merged')
HISTORICAL_OBSERVATIONS = frozenset(('returned_to_baseline','recovered','configuration_changed','historical_candidate'))


def current_policy(actor):
    authorize(actor, 'investigation.create')
    return actor.db.scalar(select(m.InspectionPolicy).where(m.InspectionPolicy.owner_id == actor.user_id))


def policy_response(policy):
    return fields(policy, POLICY_FIELDS) if policy else None


def effective_policy_state(db,policy,*,now=None,used=None):
    """A former budget block is not a permanent policy state.

    Derive availability from the existing UTC-day dispatch ledger, including
    after midnight or an explicit administrator budget increase. No history or
    limits are changed, and unrelated failure/permission states are preserved.
    """
    if policy.enabled and policy.status=='budget_exhausted':
        if used is None:
            now=now or db_now(db)
            day=now.replace(hour=0,minute=0,second=0,microsecond=0)
            used=db.scalar(select(func.count()).select_from(m.InspectionDispatch).where(m.InspectionDispatch.policy_id==policy.id,m.InspectionDispatch.created_at>=day)) or 0
        if used<policy.max_daily_runs:return 'scheduled',None
    return policy.status,policy.reason


def save_policy(actor, body):
    identifiers = sorted(set(body.get('instance_ids', [])))
    if len(identifiers) > 100 or not identifiers and body.get('enabled', False): raise PlatformError(422, 'invalid_inspection_scope', '启用巡检需要1–100个授权对象。')
    for identifier in identifiers:
        if not group_collection_allowed(actor.db, actor.db.get(m.Instance, identifier)):
            require_legacy(db=actor.db)
    mode = body.get('mode', 'rules_only')
    if identifiers: require_investigation_access(actor, identifiers, mode)
    else: authorize(actor, 'investigation.create')
    if mode == 'live_redacted' and not body.get('external_confirmed'): raise PlatformError(422, 'external_confirmation_required', '自动模型巡检需要明确、可撤回的外发许可。')
    for key, low, high, default in [('signal_interval_seconds',5,86400,300), ('ai_interval_seconds',300,86400,1800), ('cooldown_seconds',30,86400,300), ('max_daily_runs',1,100,24)]:
        value = body.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high: raise PlatformError(422, 'invalid_inspection_policy', '巡检间隔或预算超出允许范围。')
    # The owner's unique policy row serializes scheduler/API updates.
    policy = actor.db.scalar(select(m.InspectionPolicy).where(m.InspectionPolicy.owner_id == actor.user_id).with_for_update())
    if policy is None:
        policy = m.InspectionPolicy(owner_id=actor.user_id); actor.db.add(policy); actor.db.flush()
    else: policy.version += 1
    policy.instance_ids, policy.mode = identifiers, mode
    for key in ('enabled','urgent_enabled','external_confirmed'): setattr(policy, key, bool(body.get(key, key == 'urgent_enabled')))
    for key, default in [('signal_interval_seconds',300),('ai_interval_seconds',1800),('cooldown_seconds',300),('max_daily_runs',24)]: setattr(policy,key,body.get(key,default))
    policy.status, policy.reason = ('scheduled', None) if policy.enabled else ('paused', '策略已停用')
    policy.next_signal_at = policy.next_ai_at = db_now(actor.db)
    actor.db.flush()
    return policy


def check_inspection_task(db, task):
    marker = (task.payload or {}).get('options', {}).get('inspection')
    if not marker: return
    policy = db.get(m.InspectionPolicy, marker.get('policy_id'), populate_existing=True)
    if not policy or not policy.enabled or policy.version != marker.get('version') or policy.owner_id != task.actor_id:
        raise PlatformError(409, 'inspection_policy_paused', '巡检策略已暂停或版本改变。')
    if policy.mode == 'live_redacted' and not policy.external_confirmed: raise PlatformError(403, 'inspection_consent_revoked', '巡检外发许可已撤回。')
    for identifier in task.payload.get('instance_ids', []):
        instance = db.get(m.Instance, identifier, populate_existing=True)
        mapping = mapping_for(db, identifier)
        if not group_collection_allowed(db, instance): require_legacy(db=db)
        if not instance or not instance.enabled or (mapping and not mapping.selected): raise PlatformError(409, 'inspection_object_paused', '巡检对象已停用。')


def conditional_risk(points, *, threshold):
    """Transparent linear condition, never a guaranteed fault prediction."""
    if len(points) < 20 or any(p.get('value') is None or not isinstance(p.get('value'), (int,float)) or not math.isfinite(p['value']) for p in points): return None
    try: times = [parse_time(p['time']) for p in points]
    except HTTPException: return None
    hours = [(t-times[0]).total_seconds()/3600 for t in times]
    if hours[-1] < .5 or any(b <= a or b-a > .25 for a,b in zip(hours,hours[1:])): return None
    values = [float(p['value']) for p in points]
    mean_x, mean_y = sum(hours)/len(hours), sum(values)/len(values)
    variance = sum((x-mean_x)**2 for x in hours)
    slope = sum((x-mean_x)*(y-mean_y) for x,y in zip(hours,values))/variance if variance else 0
    if slope <= 0 or not values[-1] < threshold: return None
    total = sum((y-mean_y)**2 for y in values)
    residual = sum((y-(mean_y+slope*(x-mean_x)))**2 for x,y in zip(hours,values))
    fit = 1-residual/total if total else 0
    eta = (threshold-values[-1])/slope
    if fit < .8 or not 0 < eta <= 6: return None
    return {'kind':'conditional_prediction','threshold':threshold,'observed_from':iso(times[0]),'observed_to':iso(times[-1]),'sample_count':len(points),'slope_per_hour':slope,'estimated_hours_if_trend_continues':eta,'calculation':'least_squares_linear_trend','assumptions':['增长趋势持续且采样连续','负载未发生结构变化；估计不代表必然故障']}


def prediction_summary(calculation):
    return (f"观测 {calculation.get('observed_from')} 至 {calculation.get('observed_to')}；"
            f"线性增长 {calculation.get('slope_per_hour',0):.1f}%/小时，阈值 {calculation.get('threshold')}%；"
            f"若趋势持续，约 {calculation.get('estimated_hours_if_trend_continues',0):.1f} 小时达到。"
            f"条件：{'；'.join(calculation.get('assumptions',[]))}。")


def upsert_finding(db, instance_id, category, dimension, evidence_ids, calculation=None, *, kind='current', now=None, existing_event=None):
    if category not in LABELS: raise ValueError('Unregistered finding category')
    now = now or db_now(db)
    fingerprint = digest([instance_id, category, dimension])
    finding = db.scalar(select(m.InspectionFinding).where(m.InspectionFinding.fingerprint == fingerprint).with_for_update())
    if finding is None:
        event = existing_event or m.Event(instance_id=instance_id, title=LABELS[category], source='inspection_hypothesis' if category == 'evidence_correlation' else 'inspection_rule', severity='warning', last_seen_at=now)
        if not existing_event: db.add(event); db.flush()
        finding = m.InspectionFinding(instance_id=instance_id, category=category, dimension=dimension, fingerprint=fingerprint, event_id=event.id, kind=kind)
        db.add(finding)
    else: event = db.get(m.Event, finding.event_id)
    event.last_seen_at = now
    if event.status in ('resolved','closed'): event.status = 'open'; event.version += 1
    finding.evidence_ids, finding.calculation = sorted(set(evidence_ids)), calculation or {}
    finding.signal_hash = digest(calculation or [category, dimension])
    db.flush()
    return finding


def scan_signals(db, policy, now):
    from .showcase import current_locks, _save
    _save(db,'inspection-status:'+policy.id,{'last_checked_at':iso(now)})
    findings = []
    # Adopt existing rule events instead of creating a parallel issue identity.
    rows = db.execute(select(m.Event,m.Rule).join(m.EventAlert,m.EventAlert.event_id==m.Event.id).join(m.AlertOccurrence,m.AlertOccurrence.id==m.EventAlert.alert_occurrence_id).join(m.ActiveAlert,m.ActiveAlert.id==m.AlertOccurrence.active_alert_id).join(m.Rule,m.Rule.rule_version==m.ActiveAlert.rule_version).where(m.Event.instance_id.in_(policy.instance_ids),m.Event.status.notin_(TERMINAL_EVENTS),m.Rule.template.in_(RULE_CATEGORY))).all()
    for event, rule in rows:
        findings.append(upsert_finding(db,event.instance_id,RULE_CATEGORY[rule.template],'instance',[],{'rule_version':rule.rule_version},now=event.last_seen_at,existing_event=event))
    for identifier in policy.instance_ids:
        current=current_locks(db,identifier,now)
        if current['usable']:
            old=db.scalar(select(m.InspectionFinding).where(m.InspectionFinding.fingerprint==digest([identifier,'lock_wait','instance'])).with_for_update())
            previous=dict(old.calculation or {}) if old else {}
            stamp=current['observed_at']
            if not previous.get('observed_at') or parse_time(previous['observed_at'])<=stamp:
                if not current['count']:
                    if old:
                        old.calculation={**previous,'observation_state':'recovered','observed_at':iso(stamp),'recovered_at':iso(stamp)}
                        old.evidence_ids=current['evidence_ids']
                else:
                    first=previous.get('episode_started_at') if previous.get('observation_state')=='active' else iso(stamp)
                    calculation={'row_count':current['count'],'observation_state':'active','episode_started_at':first}
                    found=upsert_finding(db,identifier,'lock_wait','instance',current['evidence_ids'],calculation,now=stamp)
                    found.signal_hash=digest([identifier,'lock_wait',first])
                    found.calculation={**calculation,'observed_at':iso(stamp)}
                    findings.append(found)
        latest = list(db.scalars(select(m.Evidence).where(m.Evidence.instance_id==identifier,m.Evidence.source_mode=='live',m.Evidence.observed_at>=now-timedelta(minutes=5)).order_by(m.Evidence.observed_at.desc()).limit(50)))
        seen = set()
        for evidence in latest:
            coverage = evidence.coverage or {}
            payload = evidence.payload or {}
            if evidence.source=='mysql.lock_waits': continue
            key = (evidence.source, coverage.get('itemid'))
            if key in seen: continue
            seen.add(key)
            if coverage.get('availability') != 'available' or evidence.truncated: continue
            category, dimension, calc = None, 'instance', {}
            values = payload.get('values') or {r.get('VARIABLE_NAME'):r.get('VARIABLE_VALUE') for r in payload.get('rows',[]) if isinstance(r,dict)}
            if evidence.source == 'mysql.lock_waits' and payload.get('rows'):
                category, calc = 'lock_wait', {'row_count':len(payload['rows'])}
            elif values.get('host_cpu_percent',0) >= 85: category, calc = 'cpu_pressure', {'value':values['host_cpu_percent'],'threshold':85,'unit':'%'}
            elif values.get('probe_availability') == 0: category, calc = 'probe_unavailable', {'value':0,'threshold':1}
            elif evidence.source == 'mysql.statement_digest':
                window = payload.get('window') or {}
                for row in window.get('rows',[]) if window.get('valid') else []:
                    latency = row.get('avg_latency_seconds')
                    if row.get('count_delta',0)>=10 and isinstance(latency,(int,float)) and latency >= .5:
                        category, dimension, calc = 'sql_latency', str(row.get('DIGEST',''))[:64], {'avg_latency_seconds':latency,'count_delta':row['count_delta'],'threshold':.5}
                        break
            if category:
                existing = next((f for f in findings if f.instance_id==identifier and f.category==category and f.dimension==dimension),None)
                findings.append(upsert_finding(db,identifier,category,dimension,[evidence.id],calc,now=evidence.observed_at,existing_event=db.get(m.Event,existing.event_id) if existing else None))
    # Conditional capacity warning only from continuous actual percent history.
    config=db.info.get("platform_config")
    if config:
        from .monitoring import metric_charts
        for identifier in policy.instance_ids:
            mapping=mapping_for(db,identifier)
            if not mapping or mapping.layer!="host": continue
            try: charts=metric_charts(db,db.get(m.Instance,identifier),now-timedelta(hours=1),now,config)
            except HTTPException: continue
            for chart in charts:
                if chart["id"]!="host_cpu_percent" or not chart["coverage"].get("complete"): continue
                risk=conditional_risk(chart["points"],threshold=95)
                if not risk: continue
                evidence=db.scalar(select(m.Evidence).where(m.Evidence.instance_id==identifier,m.Evidence.source=="linux.host",m.Evidence.observed_at>=now-timedelta(minutes=5)).order_by(m.Evidence.observed_at.desc()).limit(1))
                if evidence: findings.append(upsert_finding(db,identifier,"cpu_trend","instance",[evidence.id],risk,kind="risk",now=now))
    from .capacity_signals import scan_group_capacity
    findings.extend(scan_group_capacity(db,policy,now))
    from .health_signals import scan_group_changes
    findings.extend(scan_group_changes(db,policy,now))
    return list({f.id:f for f in findings}.values())


def link_report(db, finding, investigation):
    previous=db.get(m.Investigation,finding.latest_investigation_id) if finding.latest_investigation_id else None
    rank=lambda inv:(inv.window_end,inv.event_id==finding.event_id,inv.created_at,inv.id)
    # Completion order is not observation order. Retain the newer diagnosis even
    # when an older broad inspection happens to finish last.
    preferred=not previous or rank(investigation)>=rank(previous)
    if previous and previous.event_id==finding.event_id and investigation.event_id!=finding.event_id:
        preferred=preferred and bool(_related_report_fact(db,investigation,finding))
    if preferred: finding.latest_investigation_id=investigation.id
    duplicate=db.scalar(select(m.EventTransition.id).where(m.EventTransition.event_id==finding.event_id,m.EventTransition.action=='inspection_report',m.EventTransition.details['investigation_id'].as_string()==investigation.id).limit(1))
    if not duplicate: db.add(m.EventTransition(event_id=finding.event_id,action='inspection_report',from_state=None,to_state=None,details={'investigation_id':investigation.id,'analysis_status':investigation.status}))


def sync_reports(db):
    for dispatch in db.scalars(select(m.InspectionDispatch).where(m.InspectionDispatch.status.in_(['queued','running'])).limit(200).with_for_update(skip_locked=True)):
        inv, task = db.get(m.Investigation,dispatch.investigation_id), db.get(m.Task,dispatch.task_id)
        if inv.status not in ('completed','partial','failed','cancelled'):
            dispatch.status = 'running' if task.status == 'running' else 'queued'; continue
        dispatch.status = inv.status
        policy = db.get(m.InspectionPolicy,dispatch.policy_id)
        if inv.status in ('completed','partial'):
            policy.last_completed_at = task.finished_at or db_now(db)
            if policy.status not in ('paused','permission_denied','budget_exhausted'): policy.status = 'completed'; policy.reason = None
            for finding in db.scalars(select(m.InspectionFinding).where(m.InspectionFinding.instance_id.in_(dispatch.instance_ids))):
                if not dispatch.event_id or finding.event_id==dispatch.event_id: link_report(db,finding,inv)
            # Model findings remain reviewable hypotheses, never proven current faults.
            if policy.enabled and policy.version == dispatch.policy_version:
                sync_model_findings(db, dispatch, inv)
        elif policy.enabled:
            policy.status, policy.reason = 'analysis_failed', task.error_code or inv.status


def sync_model_findings(db, dispatch, investigation):
    task=db.get(m.Task,investigation.task_id) if investigation.task_id else None
    if not task or (task.payload or {}).get("mode")!="live_redacted" or not investigation.manifest_id: return
    manifest=db.get(m.EvidenceManifest,investigation.manifest_id)
    resource_ids=set(db.scalars(select(m.InvestigationResource.instance_id).where(m.InvestigationResource.investigation_id==investigation.id)))
    if not manifest: return
    permitted=set(dispatch.instance_ids)&set(manifest.instance_ids)&resource_ids
    references = {}
    for item in db.scalars(select(m.ManifestItem).where(m.ManifestItem.manifest_id == investigation.manifest_id)):
        if item.instance_id not in permitted: continue
        if item.evidence_id: references[item.evidence_id] = item
        elif item.item_type in ('prometheus_window','time_series_window'): references['manifest:'+item.id] = item
    # Historical reports without the additive intent field stay readable, but
    # narrative hypotheses (including negated/healthy sentences) never create events.
    for raw in (investigation.report or {}).get('findings',[])[:20]:
        try: proposal=FindingProposal.model_validate(raw)
        except ValidationError: continue
        identifier=proposal.instance_id
        refs=proposal.evidence_ids
        if identifier not in permitted or any(ref not in references for ref in refs): continue
        local=[references[ref] for ref in refs if references[ref].instance_id==identifier]
        if not local or not finding_dimension_supported(db,proposal,local): continue
        # A model explanation of an already observed problem belongs to its
        # original Event. Keep the deterministic category, state and facts.
        observed=_matching_observed_finding(db,proposal,local)
        if observed:
            link_report(db,observed,investigation)
            if observed.category=='baseline_change':
                _record_finding_review(db,observed,investigation,proposal,task)
            continue
        dimension=('metric:'+proposal.dimension.value if proposal.dimension.kind=='metric' else
                   'sql:'+proposal.dimension.value.lower() if proposal.category=='sql' else proposal.category+':instance')
        existing=db.scalar(select(m.InspectionFinding).where(m.InspectionFinding.fingerprint==digest([identifier,'evidence_correlation',dimension])).with_for_update())
        previous=db.get(m.Investigation,existing.latest_investigation_id) if existing and existing.latest_investigation_id else None
        rank=lambda inv:(inv.window_end,inv.event_id==existing.event_id,inv.created_at,inv.id)
        if previous and rank(investigation)<rank(previous):
            # Protect statement, observation refs and calculation together with
            # the latest pointer; late completion is historical context only.
            link_report(db,existing,investigation)
            continue
        calculation={'classification':'model_hypothesis','family':proposal.category,'dimension':proposal.dimension.model_dump(),
                     'statement':proposal.statement,'assessment_investigation_id':investigation.id}
        finding=upsert_finding(db,identifier,'evidence_correlation',dimension,refs,calculation,kind='risk',now=investigation.updated_at)
        if finding.latest_investigation_id != investigation.id: link_report(db,finding,investigation)


def _matching_observed_finding(db,proposal,items=()):
    candidates=db.scalars(select(m.InspectionFinding).join(m.Event,m.Event.id==m.InspectionFinding.event_id).where(m.InspectionFinding.instance_id==proposal.instance_id,m.InspectionFinding.category!='evidence_correlation',m.Event.status.notin_(TERMINAL_EVENTS)).order_by(m.InspectionFinding.updated_at.desc()).with_for_update())
    expected=proposal.dimension.value.lower() if proposal.dimension.kind=='sql_digest' else 'instance'
    # A capacity rule and its numerator's baseline change may coexist. Attach
    # a two-operand model explanation to the rule before considering changes.
    for finding in sorted(candidates,key=lambda row:row.category!='capacity_pressure'):
        if finding.category=='capacity_pressure' and proposal.category=='correlation':
            calc=finding.calculation or {}
            metric_ids={str(calc.get('used_itemid')),str(calc.get('limit_itemid'))}
            # Keep one deterministic issue identity; require both operands from
            # this exact host/source in the model's cited frozen evidence.
            supported=set()
            for item in items:
                for metric_id in metric_ids:
                    if _reference_matches_signal(db,item,finding,itemid=metric_id):supported.add(metric_id)
            if (supported==metric_ids and (proposal.dimension.kind=='instance'
                    or proposal.dimension.kind=='metric' and proposal.dimension.value in metric_ids)):
                return finding
        if proposal.dimension.kind=='metric':
            if (finding.category=='baseline_change' and str((finding.calculation or {}).get('itemid'))==proposal.dimension.value
                    and any(_reference_matches_signal(db,item,finding) for item in items)):
                return finding
            continue
        if _issue_category(finding)!=proposal.category: continue
        dimension=finding.dimension.lower()
        if proposal.dimension.kind=='sql_digest' and dimension.startswith('sql:'): dimension=dimension[4:]
        if dimension==expected: return finding
    return None


def _reference_matches_signal(db,item,finding,*,itemid=None):
    """A same-number item on another host/source is not this observed signal."""
    calc=finding.calculation or {}
    if itemid is not None:calc={**calc,'itemid':itemid}
    if item.evidence_id:
        evidence=db.get(m.Evidence,item.evidence_id)
        if not evidence:return False
        meta=(evidence.payload or {}).get('item') or {};provenance=evidence.coverage or {}
    else:
        payload=item.payload or {};meta=payload.get('item') or {};provenance=payload.get('provenance') or {}
    return all(calc.get(key) is not None and str(calc[key])==str(value)
               for key,value in (('source_id',provenance.get('source_id')),('hostid',meta.get('hostid')),('itemid',meta.get('itemid'))))


def _record_finding_review(db,finding,investigation,proposal,task):
    if (finding.latest_investigation_id!=investigation.id or investigation.status not in ('completed','partial')
            or not finding.signal_hash):return
    current=finding.calculation or {};frozen_hash=None
    for signal in (task.payload or {}).get('options',{}).get('inspection',{}).get('signals',[]):
        calc=signal.get('calculation') or {}
        if (signal.get('instance_id')!=finding.instance_id or signal.get('category')!='baseline_change'
                or any(calc.get(k)!=current.get(k) for k in ('source_id','hostid','itemid','mapping_version','config_revision'))):continue
        frozen_hash=signal.get('signal_hash')
        if not frozen_hash and calc.get('episode_started_at'):
            frozen_hash=digest([finding.instance_id,'baseline_change',finding.dimension,calc['episode_started_at']])
        if frozen_hash==finding.signal_hash:break
    if frozen_hash!=finding.signal_hash:return
    duplicate=db.scalar(select(m.EventTransition.id).where(m.EventTransition.event_id==finding.event_id,
        m.EventTransition.action=='inspection_finding_review',
        m.EventTransition.details['investigation_id'].as_string()==investigation.id,
        m.EventTransition.details['signal_hash'].as_string()==finding.signal_hash).limit(1))
    if not duplicate:
        db.add(m.EventTransition(event_id=finding.event_id,action='inspection_finding_review',
            details={'investigation_id':investigation.id,'signal_hash':finding.signal_hash,
                     'statement':proposal.statement,'evidence_ids':list(proposal.evidence_ids)}))


def finding_dimension_supported(db, proposal, items):
    """Validate identity against cited frozen facts, without a threshold gate."""
    from .tools import project_evidence, project_metric_window
    for item in items:
        if proposal.dimension.kind=='metric':
            manifest=db.get(m.EvidenceManifest,item.manifest_id)
            if (not manifest or item.instance_id!=proposal.instance_id
                    or item.instance_id not in manifest.instance_ids): continue
            if item.evidence_id:
                evidence=db.get(m.Evidence,item.evidence_id)
                if (not evidence or evidence.instance_id!=item.instance_id
                        or evidence.payload_sha256!=item.payload_sha256
                        or not manifest.window_start<=evidence.observed_at<manifest.window_end
                        or not evidence.source.startswith('zabbix.item.numeric:')): continue
                try: projected=project_evidence(evidence)
                except (HTTPException,TypeError,ValueError,KeyError,AttributeError): continue
                data=projected['data']; meta=data.get('item') or {}
                if (projected['coverage'].get('availability')!='available'
                        or meta.get('value_kind')!='numeric'
                        or str(meta.get('itemid'))!=proposal.dimension.value): continue
                invalid_times=set()
                for row in (evidence.payload or {}).get('rows',[]):
                    if not isinstance(row,dict) or row.get('validity','valid')=='valid': continue
                    try: invalid_times.add(parse_time(row.get('time')))
                    except (HTTPException,TypeError,ValueError,AttributeError): continue
                for row in data.get('rows',[]):
                    try: stamp=parse_time(row['time'])
                    except (KeyError,TypeError,ValueError,HTTPException): continue
                    if (manifest.window_start<=stamp<manifest.window_end
                            and stamp not in invalid_times
                            and isinstance(row.get('value'),(int,float)) and not isinstance(row.get('value'),bool)
                            and math.isfinite(row['value'])): return True
            elif item.item_type=='time_series_window':
                projected=project_metric_window(item,manifest.window_start,manifest.window_end)
                if (projected and projected['template']=='zabbix_item_numeric'
                        and str((projected.get('item') or {}).get('itemid'))==proposal.dimension.value
                        and any(p['value'] is not None for p in projected['points'])):
                    return True
            continue
        if item.evidence_id:
            evidence=db.get(m.Evidence,item.evidence_id)
            if not evidence or evidence.instance_id!=item.instance_id or evidence.payload_sha256!=item.payload_sha256: continue
            source=evidence.source
            projected=project_evidence(evidence)['data']
            if projected.get('observation_unavailable') or projected.get('payload_omitted'): continue
            if proposal.category=='sql' and source=='mysql.statement_digest':
                rows=projected.get('rows',[])+projected.get('window',{}).get('rows',[])
                if any(str(row.get('digest','')).lower()==proposal.dimension.value.lower() for row in rows): return True
            elif proposal.category=='locks' and source in ('mysql.lock_waits','mysql.transactions','mysql.sessions') and any(projected.get('rows',[])): return True
            elif proposal.category=='host' and source=='linux.host' and projected.get('values'): return True
            elif proposal.category=='network' and source=='linux.host' and any(k.startswith('probe_') for k in projected.get('values',{})): return True
            elif proposal.category=='correlation' and any(projected.get(k) for k in ('rows','values','variables','event_count')): return True
        else:
            payload=item.payload or {}
            manifest=db.get(m.EvidenceManifest,item.manifest_id)
            projected=project_metric_window(item,manifest.window_start,manifest.window_end) if manifest else None
            if not projected or not any(point['value'] is not None for point in projected['points']): continue
            if proposal.category=='host' and payload.get('owner_collector')=='host': return True
            if proposal.category=='network' and str(payload.get('template','')).startswith('probe_'): return True
            if proposal.category=='correlation': return True
    return False


def fair_periodic_groups(db, policy):
    """Resume fairness from committed reservations, independent of process memory.

    Only comprehensive/manual dispatches affect the rotation. Failed diagnosis
    still consumed a queue/model opportunity; it does not justify a free retry.
    At most twenty groups exist for the policy's bounded 100-resource scope.
    """
    identifiers = sorted(set(policy.instance_ids))
    groups = [identifiers[index:index+5] for index in range(0,len(identifiers),5)]
    latest = {}
    history = db.execute(select(m.InspectionDispatch.instance_ids,m.InspectionDispatch.created_at).where(m.InspectionDispatch.policy_id==policy.id,m.InspectionDispatch.trigger.in_(['periodic','manual'])).order_by(m.InspectionDispatch.created_at.desc(),m.InspectionDispatch.id.desc()).limit(2000))
    for members, stamp in history:
        latest.setdefault(tuple(sorted(members)),stamp)
    # New/unreserved groups go first, then the oldest committed opportunity.
    from datetime import datetime
    return sorted(groups,key=lambda group:(tuple(group) in latest,latest.get(tuple(group),datetime.min),tuple(group)))


def dispatch_due(factory, *, policy_id=None, force=False):
    created = []
    with factory.begin() as db:
        now = db_now(db)
        sync_reports(db)
        query = select(m.InspectionPolicy).where(m.InspectionPolicy.enabled.is_(True))
        if policy_id: query = query.where(m.InspectionPolicy.id==policy_id)
        else: query = query.where(or_(m.InspectionPolicy.next_signal_at<=now,m.InspectionPolicy.next_ai_at<=now))
        for policy in db.scalars(query.order_by(m.InspectionPolicy.next_signal_at).limit(100).with_for_update(skip_locked=True)):
            periodic = force or policy.next_ai_at <= now
            signal = force or policy.next_signal_at <= now
            policy.next_signal_at = now + timedelta(seconds=policy.signal_interval_seconds)
            if periodic: policy.next_ai_at = now + timedelta(seconds=policy.ai_interval_seconds)
            try:
                actor = actor_for_user(db,policy.owner_id)
                require_investigation_access(actor,policy.instance_ids,policy.mode)
                if policy.mode=='live_redacted' and not policy.external_confirmed: raise PlatformError(403,'inspection_consent_revoked','巡检外发许可已撤回。')
                # Shared object locks serialize separate owners' policies, before
                # mapping/finding locks. Task checks below use a current read.
                list(db.scalars(select(m.Instance).where(m.Instance.id.in_(policy.instance_ids)).order_by(m.Instance.id).with_for_update()))
                for identifier in policy.instance_ids:
                    instance = db.get(m.Instance,identifier)
                    if not group_collection_allowed(db, instance): require_legacy(db=db)
                    mapping = mapping_for(db,identifier)
                    if not instance.enabled or instance.lifecycle_state != 'Active' or mapping and not mapping.selected: raise PlatformError(409,'object_paused','巡检对象已停用。')
                    if mapping:
                        source = db.get(m.MonitoringSource,mapping.source_id)
                        if source.status != 'ready' or not mapping.last_observed_at or mapping.last_observed_at < now-timedelta(minutes=5): raise PlatformError(409,'source_offline','来源离线或近期样本缺失。')
            except HTTPException as exc:
                code = exc.detail.get('code') if isinstance(exc.detail,dict) else 'permission_denied'
                policy.status = 'source_offline' if code=='source_offline' else 'object_paused' if code=='object_paused' else 'consent_required' if code=='inspection_consent_revoked' else 'permission_denied'
                policy.reason = code
                continue
            if policy.status == 'source_offline':
                policy.status, policy.reason = 'scheduled', None
            findings = scan_signals(db,policy,now) if signal else []
            urgent = [f for f in findings if f.signal_hash != f.dispatched_hash and (not f.last_dispatched_at or (now-f.last_dispatched_at).total_seconds()>=policy.cooldown_seconds or ((f.calculation or {}).get('episode_started_at') and parse_time(f.calculation['episode_started_at'])>f.last_dispatched_at))] if policy.urgent_enabled else []
            groups = fair_periodic_groups(db,policy) if periodic else []
            work = [('periodic' if not force else 'manual',group,None) for group in groups]
            if not periodic:
                work = [('urgent',[f.instance_id],f) for f in urgent]
            day = now.replace(hour=0,minute=0,second=0,microsecond=0)
            used = db.scalar(select(func.count()).select_from(m.InspectionDispatch).where(m.InspectionDispatch.policy_id==policy.id,m.InspectionDispatch.created_at>=day)) or 0
            policy.status,policy.reason=effective_policy_state(db,policy,now=now,used=used)
            for trigger, identifiers, finding in work:
                # A fast sampling cadence is not a request to stack AI work.
                # Serialize the current Zabbix group objects across policies.
                group_ids={i for i in identifiers if (mapping_for(db,i) and mapping_for(db,i).layer=='group')}
                active=list(db.scalars(select(m.Task).where(m.Task.queue=='ai',m.Task.status.in_(['queued','running','retry_wait'])).with_for_update().execution_options(populate_existing=True))) if group_ids else []
                if any(group_ids.intersection((t.payload or {}).get('instance_ids',[])) for t in active):
                    policy.status,policy.reason='queued','analysis_in_progress'
                    continue
                if used >= policy.max_daily_runs: policy.status,policy.reason='budget_exhausted','UTC 当日自动分析次数预算已用尽'; break
                event_id = finding.event_id if finding else None
                key = digest([policy.id,policy.version,trigger,identifiers,iso(now) if trigger!='urgent' else [finding.fingerprint,finding.signal_hash]])
                if db.scalar(select(m.InspectionDispatch.id).where(m.InspectionDispatch.dispatch_key==key)): continue
                # Nested transaction preserves a visible blocked state on queue limits.
                try:
                    with db.begin_nested():
                        inv, task = create_investigation(actor,identifiers,now-timedelta(hours=1),now,'自动综合巡检：关联应用、数据库、主机和网络的实际证据；先说明发生什么、影响和建议操作，再比较异常与基线，列出反证和未知。区分应用侧与数据库侧原因，不能把相关性当成已证实根因。',mode=policy.mode,event_id=event_id,idempotency_key=key,options={'external_confirmed':policy.external_confirmed,'max_tool_calls':6,'inspection':{'policy_id':policy.id,'version':policy.version,'signals':[{'category':f.category,'kind':f.kind,'instance_id':f.instance_id,'signal_hash':f.signal_hash,'evidence_ids':f.evidence_ids,'calculation':f.calculation} for f in findings if f.instance_id in identifiers]}})
                        db.add(m.InspectionDispatch(policy_id=policy.id,policy_version=policy.version,dispatch_key=key,trigger=trigger,event_id=event_id,instance_ids=identifiers,investigation_id=inv.id,task_id=task.id))
                        for f in findings:
                            if f.instance_id in identifiers:
                                f.latest_investigation_id=inv.id; f.last_dispatched_at=now; f.dispatched_hash=f.signal_hash
                        db.flush()
                except HTTPException as exc:
                    policy.status,policy.reason='queue_full',exc.detail.get('code','dispatch_failed') if isinstance(exc.detail,dict) else 'dispatch_failed'; break
                created.append(task.id); used+=1
                policy.last_dispatched_at,policy.status,policy.reason=now,'queued',None
    return created


def _visible_ids(actor, scope, action='data.read'):
    ids = authorize(actor,action).instance_ids
    query = select(m.Instance.id).join(m.System,m.System.id==m.Instance.system_id).where(m.Instance.id.in_(ids))
    for key,column in [('instance_id',m.Instance.id),('system_id',m.System.id),('department_id',m.System.department_id),('environment',m.System.environment)]:
        if scope.get(key): query=query.where(column==scope[key])
    return set(actor.db.scalars(query))


def _latest(db,event,finding):
    if finding and finding.latest_investigation_id: return db.get(m.Investigation,finding.latest_investigation_id)
    return db.scalar(select(m.Investigation).where(m.Investigation.event_id==event.id).order_by(m.Investigation.created_at.desc()).limit(1))


def _issue_category(finding):
    if not finding: return 'unknown'
    if finding.category=='evidence_correlation':
        family=(finding.calculation or {}).get('family')
        return family if family in ('locks','sql','host','network','correlation') else 'correlation'
    return {'lock_wait':'locks','sql_latency':'sql','cpu_pressure':'host','cpu_trend':'host','probe_unavailable':'network','baseline_change':'correlation','capacity_pressure':'correlation'}.get(finding.category,'unknown')


def _evidence_category(source):
    if source in ('mysql.lock_waits','mysql.transactions','mysql.sessions'): return 'locks'
    if source=='mysql.statement_digest': return 'sql'
    if source=='linux.host': return 'host'
    return 'unknown'


def _related_report_fact(db,inv,finding):
    if not finding or not inv.manifest_id: return None
    relevant=set()
    calculation=finding.calculation or {}
    metric_identity=(str(calculation.get('source_id') or ''),str(calculation.get('hostid') or ''),str(calculation.get('itemid') or '')) if finding.category=='baseline_change' else None
    for item in db.scalars(select(m.ManifestItem).where(m.ManifestItem.manifest_id==inv.manifest_id,m.ManifestItem.instance_id==finding.instance_id)):
        payload=item.payload or {}
        if metric_identity:
            if not all(metric_identity): continue
            if item.evidence_id:
                evidence=db.get(m.Evidence,item.evidence_id)
                if (not evidence or evidence.instance_id!=finding.instance_id
                        or evidence.payload_sha256!=item.payload_sha256
                        or not evidence.source.startswith('zabbix.item.numeric:')): continue
                meta=(evidence.payload or {}).get('item') or {}; provenance=evidence.coverage or {}
            elif item.item_type=='time_series_window' and payload.get('template')=='zabbix_item_numeric':
                meta=payload.get('item') or {}; provenance=payload.get('provenance') or {}
            else: continue
            if (str(provenance.get('source_id') or ''),str(meta.get('hostid') or ''),str(meta.get('itemid') or ''))!=metric_identity: continue
            if any(provenance.get(key) is not None and str(provenance[key])!=expected
                   for key,expected in (('hostid',metric_identity[1]),('itemid',metric_identity[2]))): continue
            proposal=FindingProposal(intent='review_issue',category='correlation',instance_id=finding.instance_id,
                dimension={'kind':'metric','value':metric_identity[2]},statement='Metric fact reference',evidence_ids=['reference'])
            if finding_dimension_supported(db,proposal,[item]): relevant.add(item.evidence_id or 'manifest:'+item.id)
            continue
        if item.evidence_id and _evidence_category(payload.get('source'))==_issue_category(finding): relevant.add(item.evidence_id)
        elif item.item_type in ('prometheus_window','time_series_window'):
            key=payload.get('template','')
            category='network' if key.startswith('probe_') else 'host' if key.startswith('host_') else 'locks' if key in ('lock_waits','transactions') else 'unknown'
            if category==_issue_category(finding): relevant.add('manifest:'+item.id)
    facts=[entry for entry in (inv.report or {}).get('facts',[]) if isinstance(entry,dict)]
    direct=set(finding.evidence_ids or [])&relevant
    for ids in (direct,relevant):
        fact=next((entry for entry in facts if ids.intersection(entry.get('evidence_ids',[]))),None)
        if fact: return fact.get('statement')
    return None


def _finding_reviews(db,rows,reports):
    """One small ledger query, restricted to already-authorized selected reports."""
    candidates={event.id:(finding,reports.get(event.id)) for event,finding in rows
                if finding and finding.category=='baseline_change' and finding.signal_hash
                and reports.get(event.id) and reports[event.id].status in ('completed','partial')
                and finding.latest_investigation_id==reports[event.id].id}
    if not candidates:return {}
    result={}
    query=select(m.EventTransition.event_id,m.EventTransition.details).where(
        m.EventTransition.action=='inspection_finding_review',m.EventTransition.event_id.in_(candidates),
        m.EventTransition.details['investigation_id'].as_string().in_({inv.id for _,inv in candidates.values()}),
        m.EventTransition.details['signal_hash'].as_string().in_({finding.signal_hash for finding,_ in candidates.values()})
    ).order_by(m.EventTransition.occurred_at.desc(),m.EventTransition.id.desc())
    for event_id,details in db.execute(query):
        finding,inv=candidates[event_id]
        if (event_id not in result and isinstance(details,dict) and details.get('investigation_id')==inv.id
                and details.get('signal_hash')==finding.signal_hash
                and isinstance(details.get('statement'),str) and details['statement'].strip()
                and isinstance(details.get('evidence_ids'),list) and details['evidence_ids']):
            result[event_id]=details
    return result


def _baseline_selection(instance,mapping,calculation):
    if (not mapping or not mapping.selected or calculation.get('config_revision')!=instance.config_revision
            or calculation.get('mapping_version')!=mapping.version or calculation.get('source_id')!=mapping.source_id):return None
    return next((item for item in mapping.items
        if str(item.get('hostid'))==str(calculation.get('hostid'))
        and str(item.get('itemid'))==str(calculation.get('itemid'))),None)


def _readable_number(value):
    if not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(value):return '未知'
    return f'{value:.4g}'


def _finding_presentation(event,finding,instance,mapping,inv,review=None):
    """Presentation does not rewrite the original event or prove a root cause."""
    calc=(finding.calculation or {}) if finding else {};state=calc.get('observation_state')
    title=event.title;summary=None;reviewed=False
    if finding and finding.category=='baseline_change':
        item=_baseline_selection(instance,mapping,calc)
        if item is None:state='configuration_changed'
        from .zabbix_items import safe_text
        name=safe_text((item or {}).get('name') or ('监控项 '+str(calc.get('itemid') or '未知')),60)
        direction={'increase':'升高','decrease':'降低'}.get(calc.get('direction'),'发生变化')
        title=(name+direction)[:80]
        unit=safe_text((item or {}).get('units',(item or {}).get('unit','')),24)
        suffix=(' '+unit) if unit else ''
        summary=(f"近期中位数 {_readable_number((calc.get('recent') or {}).get('median'))}{suffix}，"
                 f"参考基线中位数 {_readable_number((calc.get('baseline') or {}).get('median'))}{suffix}；变化待分析。")
        if review and inv and review.get('investigation_id')==inv.id and review.get('signal_hash')==finding.signal_hash:
            reviewed=True;title=review['statement'].strip()[:80];summary='AI 提请复核：'+review['statement']
    elif finding and finding.category=='lock_wait':
        count=calc.get('row_count')
        if type(count) is int and count>0 and calc.get('observed_at'):
            title=f'数据库锁等待（{count} 条等待链）'
            summary=(f"最近完整快照 {calc['observed_at']} 发现 {count} 条等待链；"
                     '具体 SQL 与持锁原因需核对对应报告，不能由等待数量推断。')
    elif finding and finding.category=='capacity_pressure':
        selected={(str(item.get('hostid')),str(item.get('itemid'))):item for item in mapping.items} if mapping and mapping.selected else {}
        used=selected.get((str(calc.get('hostid')),str(calc.get('used_itemid'))))
        limit=selected.get((str(calc.get('hostid')),str(calc.get('limit_itemid'))))
        if (not used or not limit or calc.get('source_id')!=mapping.source_id
                or calc.get('mapping_version')!=mapping.version or calc.get('config_revision')!=instance.config_revision):
            state='configuration_changed'
        else:
            from .zabbix_items import safe_text
            title=(safe_text(used.get('name') or '资源',60)+'容量余量不足')[:80]
            summary=(f"用量 {_readable_number(calc.get('used'))} / {_readable_number(calc.get('limit'))}，"
                     f"占用 {_readable_number(calc.get('utilization_percent'))}%，"
                     f"剩余 {_readable_number(calc.get('remaining'))}；"
                     f"已达到或超过配置的 {_readable_number(calc.get('warning_percent'))}% 关注水位。具体原因待分析。")
    elif finding and finding.category=='evidence_correlation':
        if inv and inv.status in ('completed','partial') and calc.get('assessment_investigation_id')==inv.id:
            statement=calc.get('statement')
            if isinstance(statement,str) and statement.strip():
                title=('待核验：'+statement.strip())[:80];summary=statement
        else:
            state='historical_candidate';title='历史候选解释，待复核'
            summary='当时判断未被本次报告复核，不能据此判断当前状态。' if inv else '等待证据分析；当时判断未被本次报告复核，不能据此判断当前状态。'
    historical=state in HISTORICAL_OBSERVATIONS or event.status in TERMINAL_EVENTS
    kind='history' if historical else 'signal' if finding and finding.category=='baseline_change' and not reviewed else 'issue'
    if state=='configuration_changed':summary='当前监控选择或配置已变化；此处保留历史观测，需要依据新配置的后续样本重新判断。'
    elif state=='returned_to_baseline':summary='后续实际样本已回到该指标原基线；这不代表业务整体健康或根因已解决。'
    elif state=='recovered':summary='后续实际观测已恢复；此处保留原问题与报告，不代表整体健康或根因已解决。'
    return {'title':title,'summary':summary,'presentation_kind':kind,'observation_state':state,
            'reviewed':reviewed}


def _presentation_summary(summary,presentation,finding,freshness):
    if freshness['status']!='outdated' or presentation['observation_state'] in HISTORICAL_OBSERVATIONS:return summary
    if presentation['reviewed'] or finding and finding.category=='evidence_correlation':
        return '报告窗口内：'+summary if summary else freshness['reason']
    if finding and finding.category in ('baseline_change','capacity_pressure'):return summary
    if finding and finding.category=='lock_wait' and presentation.get('summary'):return summary
    return freshness['reason']


def _problem_observed_at(db,event,finding,inv):
    observed=[]
    if finding and finding.evidence_ids:
        stamp=db.scalar(select(func.max(m.Evidence.observed_at)).where(m.Evidence.instance_id==event.instance_id,m.Evidence.id.in_(finding.evidence_ids)))
        if stamp: observed.append(stamp)
    # A model registers a candidate after processing; that wall-clock time is
    # not a new target observation beyond its own frozen input window.
    if finding and finding.category=='evidence_correlation' and inv: return max(observed) if observed else inv.window_end
    episodes=db.scalars(select(m.AlertOccurrence).join(m.EventAlert,m.EventAlert.alert_occurrence_id==m.AlertOccurrence.id).where(m.EventAlert.event_id==event.id).order_by(m.AlertOccurrence.last_seen_at.desc()).limit(20))
    for episode in episodes:
        latest=(episode.evidence or {}).get('latest',{})
        stamp=latest.get('recovery_observed_at') if episode.status=='recovered' else latest.get('source_observed_at')
        if stamp:
            try: observed.append(parse_time(stamp))
            except HTTPException: pass
    return max(observed) if observed else event.last_seen_at


def _report_freshness(db,event,inv,finding,*,observed_at=None):
    observed=observed_at if observed_at is not None else _problem_observed_at(db,event,finding,inv)
    result={'status':'unavailable','reason':'尚无可用的问题分析报告','event_updated_at':iso(observed),'report_window_to':iso(inv.window_end) if inv else None}
    if inv and inv.status not in ('completed','partial','failed','cancelled'):
        result.update(status='pending',reason='问题分析尚未完成')
    elif inv and inv.status in ('completed','partial'):
        if observed>inv.window_end:
            result.update(status='outdated',reason='已有报告未覆盖更新观测；旧窗口不能说明最新观测状态')
        else: result.update(status='current',reason=None)
    return result


def issue_response(db,event,finding=None,actor=None):
    instance=db.get(m.Instance,event.instance_id); inv=_latest(db,event,finding)
    if inv and actor:
        try: get_investigation(actor,inv.id)
        except HTTPException: inv=None
    task=db.get(m.Task,inv.task_id) if inv and inv.task_id else None
    report=(inv.report or {}) if inv else {}
    mapping=mapping_for(db,instance.id) if finding and finding.category in ('baseline_change','capacity_pressure') else None
    review=_finding_reviews(db,[(event,finding)],{event.id:inv} if inv else {}).get(event.id)
    presentation=_finding_presentation(event,finding,instance,mapping,inv,review)
    report_fact=_related_report_fact(db,inv,finding) if inv else None
    summary=report_fact
    if inv and finding and finding.category=='evidence_correlation': summary=(finding.calculation or {}).get('statement') or summary
    freshness=_report_freshness(db,event,inv,finding)
    observation_state=presentation['observation_state']
    if presentation['summary'] is not None:summary=presentation['summary']
    if (finding and finding.category=='baseline_change' and not presentation['reviewed']
            and observation_state not in HISTORICAL_OBSERVATIONS and report_fact):
        summary+=' 报告窗口内：'+report_fact
    summary=_presentation_summary(summary,presentation,finding,freshness)
    if not summary: summary='报告未提供与该问题直接对应的事实，待定向核验' if inv and report else '分析尚未完成' if inv else '异常已登记，等待证据分析'
    return {'id':event.id,'event_id':event.id,'title':presentation['title'],'presentation_kind':presentation['presentation_kind'],'kind':finding.kind if finding else 'current','status':event.status,'severity':event.severity,'summary':(('待核验假设：' if finding and finding.category=='evidence_correlation' else '') + summary),'issue_category':_issue_category(finding),'report_freshness':freshness,'object_names':[instance.name],'instance_ids':[instance.id],'first_seen_at':iso(event.created_at),'updated_at':iso(event.last_seen_at),'investigation_id':inv.id if inv else None,'analysis_mode':(task.payload or {}).get('mode','rules_only') if task else 'rules_only','coverage_status':'complete' if report.get('coverage',{}).get('complete') else 'unknown' if not inv else 'partial','observation_state':observation_state,'finding_status':'hypothesis' if presentation['reviewed'] or finding and finding.category=='evidence_correlation' else 'conditional_prediction' if finding and finding.kind=='risk' and finding.category!='capacity_pressure' else 'observed','report_status':inv.status if inv else 'not_started','report_error':task.error_code if task else None}


def _overview_reports(db, rows, allowed_data):
    """Resolve whole-report authorization before selecting any report text.

    A list needs the report's title/window/status, not its frozen manifest,
    metric payloads, full task payload or per-finding detail validation.
    """
    from types import SimpleNamespace
    chosen={event.id:finding.latest_investigation_id for event,finding in rows
            if finding and finding.latest_investigation_id}
    unlinked={event.id for event,_ in rows if event.id not in chosen}
    if unlinked:
        latest=select(m.Investigation.id,m.Investigation.event_id,
            func.row_number().over(partition_by=m.Investigation.event_id,
                order_by=(m.Investigation.created_at.desc(),m.Investigation.id.desc())).label('position')
        ).where(m.Investigation.event_id.in_(unlinked)).subquery()
        chosen.update((event_id,identifier) for identifier,event_id in db.execute(
            select(latest.c.id,latest.c.event_id).where(latest.c.position==1)))
    identifiers=set(chosen.values())
    resources={identifier:set() for identifier in identifiers}
    if identifiers:
        for identifier,instance_id in db.execute(select(m.InvestigationResource.investigation_id,
                m.InvestigationResource.instance_id).where(m.InvestigationResource.investigation_id.in_(identifiers))):
            resources[identifier].add(instance_id)
    permitted={identifier for identifier,ids in resources.items() if ids and ids.issubset(allowed_data)}
    reports={}
    if permitted:
        projection=select(m.Investigation.id,m.Investigation.status,m.Investigation.window_start,
            m.Investigation.window_end,m.Investigation.task_id,
            m.Investigation.report['executive_summary']['headline'].as_string().label('headline'),
            m.Investigation.report['coverage']['complete'].as_boolean().label('coverage_complete')
        ).where(m.Investigation.id.in_(permitted))
        reports={row.id:SimpleNamespace(**row._mapping) for row in db.execute(projection)}
    task_ids={report.task_id for report in reports.values() if report.task_id}
    tasks={row.id:row for row in db.execute(select(m.Task.id,m.Task.error_code,
            m.Task.payload['mode'].as_string().label('mode')).where(m.Task.id.in_(task_ids)))} if task_ids else {}
    subjects={event.id:event.instance_id for event,_ in rows}
    return {event_id:reports[identifier] for event_id,identifier in chosen.items()
            if identifier in reports and subjects[event_id] in resources[identifier]},tasks


def _overview_observations(db, rows):
    """Only timestamps and alert metadata; never materialize evidence payloads."""
    identifiers={identifier for _,finding in rows if finding for identifier in (finding.evidence_ids or [])}
    evidence={row.id:row for row in db.execute(select(m.Evidence.id,m.Evidence.instance_id,
        m.Evidence.observed_at).where(m.Evidence.id.in_(identifiers)))} if identifiers else {}
    episodes={}
    event_ids={event.id for event,_ in rows}
    if event_ids:
        ranked=select(m.EventAlert.event_id,m.AlertOccurrence.status,
            m.AlertOccurrence.evidence['latest']['source_observed_at'].as_string().label('source_at'),
            m.AlertOccurrence.evidence['latest']['recovery_observed_at'].as_string().label('recovery_at'),
            func.row_number().over(partition_by=m.EventAlert.event_id,
                order_by=m.AlertOccurrence.last_seen_at.desc()).label('position')
        ).join(m.AlertOccurrence,m.AlertOccurrence.id==m.EventAlert.alert_occurrence_id
        ).where(m.EventAlert.event_id.in_(event_ids)).subquery()
        for row in db.execute(select(ranked).where(ranked.c.position<=20)):
            stamp=row.recovery_at if row.status=='recovered' else row.source_at
            if stamp:
                try: episodes.setdefault(row.event_id,[]).append(parse_time(stamp))
                except HTTPException: pass
    return evidence,episodes


def _overview_issue(event,finding,instance,mapping,inv,task,evidence,episodes,db,review=None):
    observed=[evidence[identifier].observed_at for identifier in (finding.evidence_ids or [])
              if identifier in evidence and evidence[identifier].instance_id==event.instance_id] if finding else []
    if finding and finding.category=='evidence_correlation' and inv:
        observed_at=max(observed) if observed else inv.window_end
    else:
        observed.extend(episodes.get(event.id,[]))
        observed_at=max(observed) if observed else event.last_seen_at
    freshness=_report_freshness(db,event,inv,finding,observed_at=observed_at)
    presentation=_finding_presentation(event,finding,instance,mapping,inv,review)
    observation_state=presentation['observation_state'];summary=presentation['summary']
    # A whole-report headline does not review an unrelated numerical signal.
    if not summary and inv and inv.status in ('completed','partial') and inv.headline:
        summary='关联报告概览：'+inv.headline
    summary=_presentation_summary(summary,presentation,finding,freshness)
    if not summary:
        summary=('报告已形成；打开报告查看完整事实与证据。' if inv.status in ('completed','partial') else '分析尚未完成') if inv else '异常已登记，等待证据分析'
    return {'id':event.id,'event_id':event.id,'title':presentation['title'],'presentation_kind':presentation['presentation_kind'],'kind':finding.kind if finding else 'current',
        'status':event.status,'severity':event.severity,'summary':('待核验假设：' if finding and finding.category=='evidence_correlation' else '')+summary,
        'issue_category':_issue_category(finding),'report_freshness':freshness,'object_names':[instance.name],
        'instance_ids':[instance.id],'first_seen_at':iso(event.created_at),'updated_at':iso(event.last_seen_at),
        'investigation_id':inv.id if inv else None,'analysis_mode':task.mode or 'rules_only' if task else 'rules_only',
        'coverage_status':'complete' if inv and inv.coverage_complete is True else 'partial' if inv else 'unknown',
        'observation_state':observation_state,'finding_status':'hypothesis' if presentation['reviewed'] or finding and finding.category=='evidence_correlation' else 'conditional_prediction' if finding and finding.kind=='risk' and finding.category!='capacity_pressure' else 'observed',
        'report_status':inv.status if inv else 'not_started','report_error':task.error_code if task else None}


def overview(actor, **scope):
    from sqlalchemy.orm import load_only
    from .config import zabbix_only
    from .showcase import inspection_projection
    db=actor.db
    # Evaluate live grants once per action, including revocation/session checks.
    allowed_data=set(authorize(actor,'data.read').instance_ids)
    allowed_events=set(authorize(actor,'event.read').instance_ids)
    query=select(m.Instance).join(m.System,m.System.id==m.Instance.system_id).where(m.Instance.id.in_(allowed_data))
    for key,column in [('instance_id',m.Instance.id),('system_id',m.System.id),('department_id',m.System.department_id),('environment',m.System.environment)]:
        if scope.get(key): query=query.where(column==scope[key])
    instances={row.id:row for row in db.scalars(query.options(load_only(m.Instance.id,m.Instance.name,
        m.Instance.connection_mode,m.Instance.config_revision,m.Instance.enabled,m.Instance.lifecycle_state)))}
    mappings={row.instance_id:row for row in db.scalars(select(m.MonitoringMapping).where(m.MonitoringMapping.instance_id.in_(instances)))} if instances else {}
    current_ids={identifier for identifier,instance in instances.items() if not zabbix_only(db=db)
        or (instance.connection_mode=='zabbix' and identifier in mappings and mappings[identifier].layer=='group' and mappings[identifier].selected)}
    visible=current_ids&allowed_events
    rows=list(db.execute(select(m.Event,m.InspectionFinding).outerjoin(m.InspectionFinding,m.InspectionFinding.event_id==m.Event.id)
        .where(m.Event.instance_id.in_(visible),m.Event.status.notin_(TERMINAL_EVENTS))
        .order_by(m.Event.last_seen_at.desc(),m.Event.id).limit(200)))
    reports,tasks=_overview_reports(db,rows,allowed_data)
    evidence,episodes=_overview_observations(db,rows)
    reviews=_finding_reviews(db,rows,reports)
    issues=[_overview_issue(event,finding,instances[event.instance_id],mappings.get(event.instance_id),
        reports.get(event.id),tasks.get(reports[event.id].task_id) if event.id in reports else None,
        evidence,episodes,db,reviews.get(event.id)) for event,finding in rows]
    objects=[instance for identifier,instance in instances.items() if identifier in current_ids and instance.enabled and instance.lifecycle_state=='Active']
    missing=sum(capability['status']!='available' for instance in objects if instance.id in mappings for capability in mappings[instance.id].capabilities)
    return {'stats':{'current':sum(i['kind']=='current' and i['presentation_kind']=='issue' for i in issues),'risk':sum(i['kind']=='risk' and i['presentation_kind']=='issue' for i in issues),'objects':len(objects)},'issues':issues,'inspection':inspection_projection(db,visible),'coverage':{'status':'partial' if missing else 'unknown','missing_count':missing}}


def issue_detail(actor, identifier):
    event=actor.db.get(m.Event,identifier)
    if not event: raise PlatformError(404,'issue_not_found','问题不存在。')
    authorize(actor,'data.read',[event.instance_id]); authorize(actor,'event.read',[event.instance_id])
    finding=actor.db.scalar(select(m.InspectionFinding).where(m.InspectionFinding.event_id==identifier))
    inv=_latest(actor.db,event,finding)
    # A correlated report may contain other objects; authorize the complete frozen scope.
    if inv: get_investigation(actor,inv.id)
    result=issue_response(actor.db,event,finding)
    analysis_ids=set(actor.db.scalars(select(m.InvestigationResource.instance_id).where(m.InvestigationResource.investigation_id==inv.id))) if inv else {event.instance_id}
    analysis_objects=[{'id':obj.id,'name':obj.name,'engine':obj.engine} for obj in actor.db.scalars(select(m.Instance).where(m.Instance.id.in_(analysis_ids)).order_by(m.Instance.id))]
    object_names={obj['id']:obj['name'] for obj in analysis_objects}
    result.update(event_subject={'id':event.instance_id,'name':actor.db.get(m.Instance,event.instance_id).name},analysis_instance_ids=sorted(analysis_ids),analysis_objects=analysis_objects)
    report=(inv.report or {}) if inv and inv.status in ('completed','partial') else None
    nodes,edges,charts=[],[],[]
    if inv and inv.manifest_id:
        manifest=actor.db.get(m.EvidenceManifest,inv.manifest_id)
        if not manifest or not set(manifest.instance_ids).issubset(analysis_ids): raise PlatformError(409,'report_scope_inconsistent','报告冻结范围与调查授权范围不一致。')
        from .tools import project_metric_window
        for obj in analysis_objects:
            nodes.append({'id':obj['id'],'instance_id':obj['id'],'object_name':obj['name'],'label':obj['name'],'kind':'registered','summary':'已授权的报告分析对象','evidence_ids':[]})
        for item in actor.db.scalars(select(m.ManifestItem).where(m.ManifestItem.manifest_id==inv.manifest_id,m.ManifestItem.item_type.in_(['prometheus_window','time_series_window'])).limit(192)):
            if item.instance_id not in analysis_ids: continue
            projected=project_metric_window(item,inv.window_start,inv.window_end)
            if not projected: continue
            payload=item.payload or {}; metric_id='manifest:'+item.id
            object_name=payload.get('object_name') or object_names[item.instance_id]
            chart_title=object_name+' · '+(projected.get('title') or str(payload.get('template')))
            metric_key=payload['template']
            node_label=chart_title
            if metric_key=='zabbix_item_numeric':
                frozen_item=payload.get('item') or {}
                metric_key=frozen_item.get('metric_semantic') or {'dbops.mysql.lock_waits':'lock_waits'}.get(frozen_item.get('key'),metric_key)
                node_label=str(frozen_item.get('name') or projected.get('title') or metric_key)+' · '+str(frozen_item.get('host_name') or frozen_item.get('hostid') or object_name)
            charts.append({'id':metric_id,'instance_id':item.instance_id,'object_name':object_name,'metric_key':metric_key,'title':chart_title,'unit':projected['unit'],'source':projected['source'],'points':projected['points'],'gaps':payload.get('gaps',[]),'window':{'from':iso(inv.window_start),'to':iso(inv.window_end)},'coverage':projected['coverage']})
            nodes.append({'id':metric_id,'instance_id':item.instance_id,'object_name':object_name,'metric_key':metric_key,'label':node_label,'kind':'observed','summary':'冻结的实际指标窗口','evidence_ids':[metric_id]})
            from .analysis_groups import frozen_group_graph
            owner_node=frozen_group_graph(payload,item.instance_id,nodes,edges) or item.instance_id
            edges.append({'from':owner_node,'to':metric_id,'relationship':'指标归属','kind':'registered','evidence_ids':[metric_id],'provenance':'manifest_item.instance_id'})
            for relation in payload.get('registered_relationships',[]):
                if not isinstance(relation,dict) or relation.get('from') not in analysis_ids or relation.get('to') not in analysis_ids or relation.get('kind')!='registered': continue
                for object_id in (relation['from'],relation['to']):
                    if not any(n['id']==object_id for n in nodes):
                        target=actor.db.get(m.Instance,object_id)
                        if target: nodes.append({'id':object_id,'label':target.name,'kind':'registered','summary':'监控配置中登记的对象关系','evidence_ids':[]})
                if relation not in edges: edges.append(relation)
        raw_items={item.evidence_id:item for item in actor.db.scalars(select(m.ManifestItem).where(m.ManifestItem.manifest_id==inv.manifest_id,m.ManifestItem.instance_id.in_(analysis_ids),m.ManifestItem.evidence_id.is_not(None)))}
        valid_refs=set(raw_items) | {c['id'] for c in charts}
        for category,kind in [('facts','observed'),('hypotheses','hypothesis'),('findings','hypothesis')]:
            for index,entry in enumerate((report or {}).get(category,[])):
                refs=[r for r in entry.get('evidence_ids',[]) if r in valid_refs]
                if not refs: continue
                nid=f'{category}:{index}'
                nodes.append({'id':nid,'label':entry['statement'],'kind':kind,'summary':entry['statement'],'evidence_ids':refs})
                for ref in refs:
                    if not any(n['id']==ref for n in nodes):
                        raw=raw_items[ref];source=(raw.payload or {}).get('source','unknown');owner=object_names[raw.instance_id]
                        nodes.append({'id':ref,'instance_id':raw.instance_id,'object_name':owner,'source':source,'evidence_category':_evidence_category(source),'label':owner+' · '+source,'kind':'observed','summary':'冻结采集证据；观测时间 '+str((raw.payload or {}).get('observed_at','unknown')),'evidence_ids':[ref]})
                    edges.append({'from':ref,'to':nid,'relationship':'支持观察' if kind=='observed' else '用于候选解释，尚未证明因果','kind':kind,'evidence_ids':[ref],'provenance':'investigation.report.'+category})
    frozen_task=actor.db.get(m.Task,inv.task_id) if inv and inv.task_id else None
    signals=((frozen_task.payload or {}).get('options',{}).get('inspection',{}).get('signals',[]) if frozen_task else [])
    for index,signal in enumerate(signals):
        if signal.get('calculation',{}).get('kind')=='conditional_prediction':
            nodes.append({'id':'conditional-risk:'+str(index),'label':LABELS['cpu_trend'],'kind':'conditional_prediction','summary':prediction_summary(signal['calculation']),'calculation':signal['calculation'],'evidence_ids':signal.get('evidence_ids',[])})
    checks=mapping_for(actor.db,event.instance_id)
    history=list(actor.db.scalars(select(m.Investigation).where(m.Investigation.event_id==identifier).order_by(m.Investigation.created_at.desc()).limit(50)))
    linked_ids={(transition.details or {}).get('investigation_id') for transition in actor.db.scalars(select(m.EventTransition).where(m.EventTransition.event_id==identifier,m.EventTransition.action=='inspection_report').order_by(m.EventTransition.occurred_at.desc()).limit(100))}
    existing_ids={h.id for h in history}
    history.extend(actor.db.scalars(select(m.Investigation).where(m.Investigation.id.in_(linked_ids-existing_ids)).order_by(m.Investigation.created_at.desc()).limit(50)))
    if inv and inv.id not in {h.id for h in history}: history.insert(0,inv)
    visible_history=[]
    for h in history:
        try: get_investigation(actor,h.id)
        except HTTPException: continue
        t=actor.db.get(m.Task,h.task_id) if h.task_id else None
        visible_history.append({'id':h.id,'created_at':iso(h.created_at),'status':h.status,'mode':(t.payload or {}).get('mode','rules_only') if t else 'rules_only'})
    result.update(window={'from':iso(inv.window_start),'to':iso(inv.window_end)} if inv else None,report=report,nodes=nodes,edges=edges,charts=charts,checks=checks.capabilities if checks else [],next_checks=(report or {}).get('next_readonly_checks',[]),unknowns=(report or {}).get('unknowns',['尚无已完成的证据分析']),refutations=(report or {}).get('refutations',[]),history=visible_history)
    return result



def policy_status(actor, policy):
    if policy is None: return {"status":"not_configured","reason":"尚未配置自动巡检","runs":[]}
    runs=[]
    for dispatch in actor.db.scalars(select(m.InspectionDispatch).where(m.InspectionDispatch.policy_id==policy.id).order_by(m.InspectionDispatch.created_at.desc()).limit(20)):
        try: get_investigation(actor,dispatch.investigation_id)
        except HTTPException: continue
        inv=actor.db.get(m.Investigation,dispatch.investigation_id)
        task=actor.db.get(m.Task,dispatch.task_id)
        runs.append({"id":inv.id,"task_id":task.id,"created_at":iso(inv.created_at),"trigger":dispatch.trigger,"status":inv.status,"mode":(task.payload or {}).get("mode"),"error_code":task.error_code})
    status,reason=effective_policy_state(actor.db,policy)
    return {"status":status,"reason":reason,"last_completed_at":iso(policy.last_completed_at),"next_due_at":iso(policy.next_ai_at) if policy.enabled else None,"runs":runs}
