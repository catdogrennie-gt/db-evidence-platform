"""Fixed read-only projection and expiring collection cadence, never AI dispatch."""
from datetime import timedelta
from pathlib import Path
import copy
import hmac
import math
import re

from fastapi import HTTPException
from sqlalchemy import case, select

from . import models as m
from .access import actor_for_user, authorize, current_permissions
from .common import PlatformError, iso, parse_time
from .db import utcnow
from .scheduling import db_now
from .report_presentation import report_presentation

FAST_COLLECTION = 10
FAST_SIGNAL = 5
LEASE_SECONDS = 45
READ_ACTIONS = {'data.read', 'event.read', 'knowledge.read'}


def settings(config):
    cfg = getattr(config, 'showcase', None) or {}
    if cfg.get('enabled'):
        if not all(isinstance(cfg.get(k), str) and cfg[k] for k in ('instance_id','policy_id','reader_user_id','token_path')):
            raise ValueError('Incomplete showcase configuration')
        path = Path(cfg['token_path'])
        if not path.is_absolute() or any((p/'.git').exists() for p in (path.parent,*path.parents)):
            raise ValueError('Showcase token must be outside source repositories')
        targets=cfg.get('targets',[])
        if not isinstance(targets,list) or len(targets)>20: raise ValueError('Invalid fixed targets')
        keys=set()
        for target_cfg in targets:
            if (not isinstance(target_cfg,dict) or not re.fullmatch(r'[a-z][a-z0-9-]{0,39}',str(target_cfg.get('key','')))
                    or target_cfg['key'] in keys or not all(isinstance(target_cfg.get(k),str) and target_cfg[k] for k in ('instance_id','policy_id','reader_user_id'))
                    or target_cfg.get('projection_mode','numeric') not in ('numeric','locks')):
                raise ValueError('Invalid fixed target')
            keys.add(target_cfg['key'])
        if cfg.get('fast_ai_seconds') is not None and (type(cfg['fast_ai_seconds']) is not int or not 30<=cfg['fast_ai_seconds']<=300):
            raise ValueError('Invalid temporary AI cadence')
    return cfg


def select_target(config,key):
    cfg=settings(config)
    selected=next((value for value in cfg.get('targets',[]) if value['key']==key),None)
    if selected is None: raise PlatformError(404,'showcase_target_not_found','展示对象不存在。')
    result=copy.copy(config)
    result.showcase={**{k:v for k,v in cfg.items() if k!='targets'},**selected}
    settings(result)
    return result


def target_catalog(db,config):
    from .zabbix_items import safe_text
    result=[]
    for cfg in settings(config).get('targets',[]):
        selected=select_target(config,cfg['key'])
        _,instance,_,_,_=target(db,selected)
        result.append({'key':cfg['key'],'name':safe_text(instance.name),
                       'projection_mode':cfg.get('projection_mode','numeric')})
    return {'targets':result}


def authenticate(config, authorization):
    cfg = settings(config)
    if not cfg.get('enabled'):
        raise PlatformError(404,'showcase_disabled','展示桥未启用。')
    try:
        expected = Path(cfg['token_path']).read_text(encoding='utf-8-sig').strip()
        if len(expected) < 32: raise ValueError('invalid')
    except (OSError,ValueError):
        raise PlatformError(503,'showcase_unavailable','展示桥配置暂不可用。') from None
    supplied = authorization[7:] if isinstance(authorization,str) and authorization.startswith('Bearer ') else ''
    if not hmac.compare_digest(supplied.encode(),expected.encode()):
        raise PlatformError(401,'showcase_authentication_required','需要展示服务凭据。')
    return cfg


def target(db,config,*,lock=False):
    cfg=settings(config)
    if not cfg.get('enabled'): raise PlatformError(404,'showcase_disabled','展示桥未启用。')
    policy=db.scalar(select(m.InspectionPolicy).where(m.InspectionPolicy.id==cfg['policy_id']).with_for_update()) if lock else db.get(m.InspectionPolicy,cfg['policy_id'])
    instance=db.get(m.Instance,cfg['instance_id'])
    mapping=db.scalar(select(m.MonitoringMapping).where(m.MonitoringMapping.instance_id==cfg['instance_id']))
    schedule=db.scalar(select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id==cfg['instance_id'],m.CollectionSchedule.collector=='monitoring').with_for_update()) if lock else db.scalar(select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id==cfg['instance_id'],m.CollectionSchedule.collector=='monitoring'))
    if not (policy and set(policy.instance_ids)=={cfg['instance_id']} and instance and instance.is_lab and instance.enabled and instance.lifecycle_state=='Active' and instance.connection_mode=='zabbix' and mapping and mapping.layer=='group' and mapping.selected and schedule and schedule.enabled):
        raise PlatformError(409,'showcase_target_unavailable','固定展示对象或巡检策略不可用。')
    try:
        reader=actor_for_user(db,cfg['reader_user_id'])
        authorize(reader,'data.read',[instance.id]); authorize(reader,'event.read',[instance.id])
        if not current_permissions(reader).issubset(READ_ACTIONS): raise ValueError('reader_write_permission')
    except (HTTPException,ValueError):
        raise PlatformError(409,'showcase_target_unavailable','固定只读身份不可用。') from None
    return cfg,instance,policy,schedule,reader


def state_key(instance_id): return 'showcase-cadence:'+instance_id


def _state(db,instance_id):
    row=db.get(m.PlatformSetting,state_key(instance_id))
    return row, dict(row.value or {}) if row else {}


def _save(db,key,value):
    row=db.get(m.PlatformSetting,key)
    if row is None: row=m.PlatformSetting(key=key,value=value);db.add(row)
    else: row.value=value;row.version+=1


def _unchanged(state,policy,schedule):
    return (policy.version==state.get('policy_version') and schedule.config_revision==state.get('schedule_revision') and policy.signal_interval_seconds==FAST_SIGNAL and schedule.interval_seconds==FAST_COLLECTION and (not state.get('fast_ai_seconds') or policy.ai_interval_seconds==state['fast_ai_seconds']))


def _restore(state,policy,schedule,now):
    if policy and policy.version==state.get('policy_version') and policy.signal_interval_seconds==FAST_SIGNAL:
        policy.signal_interval_seconds=state['baseline_signal_seconds']
        policy.next_signal_at=min(policy.next_signal_at,now+timedelta(seconds=policy.signal_interval_seconds))
    if policy and state.get('fast_ai_seconds') and policy.version==state.get('policy_version') and policy.ai_interval_seconds==state['fast_ai_seconds']:
        policy.ai_interval_seconds=state['baseline_ai_seconds']
        policy.next_ai_at=now+timedelta(seconds=policy.ai_interval_seconds)
    if schedule and schedule.config_revision==state.get('schedule_revision') and schedule.interval_seconds==FAST_COLLECTION:
        schedule.interval_seconds=state['baseline_collection_seconds']
        schedule.next_due_at=min(schedule.next_due_at,now+timedelta(seconds=schedule.interval_seconds))


def pacing_projection(db,instance,policy,schedule,now=None):
    now=now or db_now(db)
    _,state=_state(db,instance.id)
    active=bool(state.get('active') and state.get('expires_at') and parse_time(state['expires_at'])>now and _unchanged(state,policy,schedule))
    return dict(active=active,expires_at=state.get('expires_at') if active else None,collection_seconds=schedule.interval_seconds,signal_seconds=policy.signal_interval_seconds,ai_seconds=policy.ai_interval_seconds,baseline_ai_seconds=state.get('baseline_ai_seconds',policy.ai_interval_seconds),baseline_collection_seconds=state.get('baseline_collection_seconds',schedule.interval_seconds),baseline_signal_seconds=state.get('baseline_signal_seconds',policy.signal_interval_seconds),reason='administrator_changed' if state.get('suspended') else None)


def set_pacing(db,config,enabled,*,now=None):
    cfg,instance,policy,schedule,_=target(db,config,lock=True)
    now=now or db_now(db)
    _,state=_state(db,instance.id)
    if state.get('active') and (not _unchanged(state,policy,schedule) or parse_time(state['expires_at'])<=now):
        changed=not _unchanged(state,policy,schedule)
        _restore(state,policy,schedule,now)
        state.update(active=False,expires_at=None,suspended=changed)
    if not enabled:
        if state.get('active'): _restore(state,policy,schedule,now)
        state.update(active=False,expires_at=None,suspended=False)
    elif not state.get('suspended'):
        if not state.get('active'):
            state=dict(policy_id=policy.id,schedule_id=schedule.id,baseline_collection_seconds=schedule.interval_seconds,baseline_signal_seconds=policy.signal_interval_seconds,policy_version=policy.version,schedule_revision=schedule.config_revision,suspended=False)
            if cfg.get('fast_ai_seconds'):
                state.update(baseline_ai_seconds=policy.ai_interval_seconds,fast_ai_seconds=cfg['fast_ai_seconds'])
        state.update(active=True,expires_at=iso(now+timedelta(seconds=LEASE_SECONDS)))
        schedule.interval_seconds=FAST_COLLECTION
        policy.signal_interval_seconds=FAST_SIGNAL
        schedule.next_due_at=min(schedule.next_due_at,now)
        policy.next_signal_at=min(policy.next_signal_at,now)
        if state.get('fast_ai_seconds'):
            policy.ai_interval_seconds=state['fast_ai_seconds']
            policy.next_ai_at=min(policy.next_ai_at,now+timedelta(seconds=policy.ai_interval_seconds))
    _save(db,state_key(instance.id),state)
    db.flush()
    return pacing_projection(db,instance,policy,schedule,now)


def reconcile_pacing(db,config,*,now=None):
    # Expiry is cleanup, independent of the public capability or reader still
    # being enabled. Otherwise withdrawing access would strand fast cadence.
    now=now or db_now(db)
    rows=list(db.scalars(select(m.PlatformSetting).where(m.PlatformSetting.key.like('showcase-cadence:%'))))
    for row in rows:
        state=dict(row.value or {})
        if not state.get('active'): continue
        policy=db.scalar(select(m.InspectionPolicy).where(m.InspectionPolicy.id==state.get('policy_id')).with_for_update())
        schedule=db.scalar(select(m.CollectionSchedule).where(m.CollectionSchedule.id==state.get('schedule_id')).with_for_update())
        db.refresh(row); state=dict(row.value or {})
        if not state.get('active'): continue
        changed=not (policy and schedule and _unchanged(state,policy,schedule))
        if changed or parse_time(state['expires_at'])<=now:
            _restore(state,policy,schedule,now)
            state.update(active=False,expires_at=None,suspended=changed)
            _save(db,row.key,state)


def lock_snapshot_valid(db,row,now,*,max_age_seconds=90):
    if row is None or row.source!='mysql.lock_waits' or row.source_mode!='live': return False
    c=row.coverage or {}; rows=(row.payload or {}).get('rows')
    if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows) or c.get('availability')!='available' or not 0<=(now-row.observed_at).total_seconds()<=max_age_seconds: return False
    if c.get('transport')!='zabbix': return c.get('complete') is True and not row.truncated
    mapping=db.scalar(select(m.MonitoringMapping).where(m.MonitoringMapping.instance_id==row.instance_id))
    instance=db.get(m.Instance,row.instance_id)
    reasons=set(c.get('reason_codes') or c.get('gaps') or [])
    return bool(mapping and instance and mapping.selected and mapping.layer=='group' and any(str(item.get('hostid'))==str(c.get('hostid')) and str(item.get('itemid'))==str(c.get('itemid')) for item in mapping.items) and c.get('source_id')==mapping.source_id and c.get('mapping_version')==mapping.version and c.get('config_revision')==instance.config_revision and c.get('snapshot_contract')=='db-evidence-lock-snapshot-v2' and c.get('snapshot_complete') is True and c.get('snapshot_truncated') is False and type(c.get('snapshot_total_row_count')) is int and c['snapshot_total_row_count']==len(rows)<=50 and not reasons-{'history_truncated'})


def latest_lock(db,instance_id):
    return db.scalar(select(m.Evidence).where(m.Evidence.instance_id==instance_id,m.Evidence.source=='mysql.lock_waits',m.Evidence.source_mode=='live').order_by(m.Evidence.observed_at.desc(),m.Evidence.id.desc()).limit(1))


def current_locks(db,instance_id,now):
    """Latest per selected item; only a complete aggregate may prove recovery."""
    mapping=db.scalar(select(m.MonitoringMapping).where(m.MonitoringMapping.instance_id==instance_id))
    refs={(str(i['hostid']),str(i['itemid'])) for i in mapping.items if i.get('enhancement')=='mysql.lock_waits'} if mapping and mapping.layer=='group' else set()
    rows=list(db.scalars(select(m.Evidence).where(m.Evidence.instance_id==instance_id,m.Evidence.source=='mysql.lock_waits',m.Evidence.source_mode=='live',m.Evidence.observed_at>=now-timedelta(seconds=90)).order_by(m.Evidence.observed_at.desc(),m.Evidence.id.desc()).limit(1000)))
    latest={}
    for row in rows:
        c=row.coverage or {}; key=(str(c.get('hostid')),str(c.get('itemid')))
        if refs and key not in refs:continue
        if key not in latest:latest[key]=row
        if not refs:break  # Legacy direct collectors have one snapshot.
    good=[r for r in latest.values() if lock_snapshot_valid(db,r,now)]
    complete=bool(good) and len(good)==len(refs or latest)
    count=sum(len(r.payload['rows']) for r in good)
    usable=complete or count>0
    gaps=sorted({str(g) for r in latest.values() for g in ((r.coverage or {}).get('reason_codes') or (r.coverage or {}).get('gaps') or []) if isinstance(g,str)})
    if not complete:gaps.append('current_snapshot_unavailable')
    stamp=max((r.observed_at for r in latest.values()),default=None)
    return dict(usable=usable,complete=complete,count=count if usable else None,evidence_ids=[r.id for r in good],observed_at=stamp,gaps=gaps)


def inspection_task_projection(db,visible,policy):
    """Inspect at most 20 dispatches without fetching their reports/payloads."""
    rows=list(db.execute(select(
        m.InspectionDispatch.instance_ids,m.Investigation.id.label('investigation_id'),
        m.Investigation.status.label('investigation_status'),m.Task.id.label('task_id'),
        m.Task.status.label('task_status'),m.Task.created_at,m.Task.started_at,m.Task.error_code
    ).select_from(m.InspectionDispatch).outerjoin(m.Task,m.Task.id==m.InspectionDispatch.task_id)
      .outerjoin(m.Investigation,m.Investigation.id==m.InspectionDispatch.investigation_id)
      .where(m.InspectionDispatch.policy_id==policy.id)
      .order_by(m.InspectionDispatch.created_at.desc()).limit(20)))
    identifiers={r.investigation_id for r in rows if r.investigation_id}
    resources={identifier:set() for identifier in identifiers}
    if identifiers:
        for inv_id,instance_id in db.execute(select(m.InvestigationResource.investigation_id,
                m.InvestigationResource.instance_id).where(m.InvestigationResource.investigation_id.in_(identifiers))):
            resources[inv_id].add(instance_id)
    selected=None
    for row in rows:
        if (not set(row.instance_ids).issubset(visible) or not row.task_id or not row.investigation_id
                or not resources[row.investigation_id] or not resources[row.investigation_id].issubset(visible)):
            continue
        if row.task_status in ('queued','running','retry_wait'): return row
        if selected is None: selected=row
    return selected


def inspection_projection(db,visible,*,policy=None,now=None,collection_context=None):
    now=now or db_now(db)
    if policy is None:
        candidates=db.scalars(select(m.InspectionPolicy).order_by(m.InspectionPolicy.enabled.desc(),m.InspectionPolicy.updated_at.desc()).limit(200))
        policy=next((p for p in candidates if p.instance_ids and set(p.instance_ids).issubset(visible)),None)
    empty=dict(status='not_configured',reason='尚未配置自动巡检',last_completed_at=None,next_due_at=None,next_ai_at=None,mode=None,last_checked_at=None,next_check_at=None,last_observed_at=None,collection_seconds=None,signal_seconds=None,task=None,pacing={'active':False,'expires_at':None})
    if policy is None:return empty
    check=db.get(m.PlatformSetting,'inspection-status:'+policy.id)
    mappings=[collection_context[1]] if collection_context else list(db.scalars(select(m.MonitoringMapping).where(m.MonitoringMapping.instance_id.in_(visible))))
    stamps=[r.last_observed_at for r in mappings if r.last_observed_at]
    result={**empty,'status':policy.status,'reason':policy.reason,'last_completed_at':iso(policy.last_completed_at),'next_due_at':iso(policy.next_ai_at) if policy.enabled else None,'mode':policy.mode,'last_checked_at':(check.value or {}).get('last_checked_at') if check else None,'next_check_at':iso(policy.next_signal_at) if policy.enabled else None,'last_observed_at':iso(max(stamps)) if stamps else None,'signal_seconds':policy.signal_interval_seconds}
    from .intelligence import effective_policy_state
    result['status'],result['reason']=effective_policy_state(db,policy,now=now)
    task=inspection_task_projection(db,visible,policy)
    if task:
        result['task']=dict(id=task.task_id,status=task.investigation_status,created_at=iso(task.created_at),started_at=iso(task.started_at),error_code=task.error_code)
        if task.task_status in ('queued','running','retry_wait'):result['status']=task.investigation_status
    if len(visible)==1:
        identifier=next(iter(visible))
        instance=collection_context[0] if collection_context else db.get(m.Instance,identifier)
        schedule=collection_context[2] if collection_context else db.scalar(select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id==identifier,m.CollectionSchedule.collector=='monitoring'))
        if schedule:
            result['collection_seconds']=schedule.interval_seconds
            result['pacing']=collection_context[3] if collection_context else pacing_projection(db,instance,policy,schedule,now)
    result['next_ai_at']=result['next_due_at']
    return result


def snapshot(db,config):
    if settings(config).get('projection_mode')=='numeric': return numeric_snapshot(db,config)
    cfg,instance,policy,schedule,reader=target(db,config)
    now=db_now(db)
    current=current_locks(db,instance.id,now); valid=current['usable']
    observation=dict(status=('problem' if current['count'] else 'normal') if valid else 'unknown',observed_at=iso(current['observed_at']),lock_count=current['count'],source='zabbix',complete=current['complete'],gaps=current['gaps'])
    report=None;charts=[];matches=False
    from .investigations import get_investigation, get_frozen_metric
    from .zabbix_items import safe_text
    invs=db.scalars(select(m.Investigation).join(m.InvestigationResource,m.InvestigationResource.investigation_id==m.Investigation.id).where(m.InvestigationResource.instance_id==instance.id,m.Investigation.status.in_(['completed','partial'])).order_by(m.Investigation.created_at.desc()).limit(20))
    for candidate in invs:
        resources=set(db.scalars(select(m.InvestigationResource.instance_id).where(m.InvestigationResource.investigation_id==candidate.id)))
        if resources!={instance.id}:continue
        try:inv=get_investigation(reader,candidate.id)
        except HTTPException:continue
        task=db.get(m.Task,inv.task_id) if inv.task_id else None
        finding=db.scalar(select(m.InspectionFinding).where(m.InspectionFinding.instance_id==instance.id,m.InspectionFinding.category=='lock_wait'))
        first=(finding.calculation or {}).get('episode_started_at') if finding else None
        matches=bool(valid and current['count'] and first and inv.window_start<=parse_time(first)<inv.window_end and finding.latest_investigation_id==inv.id)
        report=dict(id=inv.id,status=inv.status,mode=(task.payload or {}).get('mode','rules_only') if task else 'rules_only',created_at=iso(inv.created_at),error_code=task.error_code if task else None,executive_summary=(inv.report or {}).get('executive_summary'),report_presentation=report_presentation(inv),coverage=(inv.report or {}).get('coverage',{}),current_for_episode=matches,**{'from':iso(inv.window_start),'to':iso(inv.window_end)})
        items=list(db.scalars(select(m.ManifestItem).where(m.ManifestItem.manifest_id==inv.manifest_id,m.ManifestItem.instance_id==instance.id,m.ManifestItem.item_type.in_(['time_series_window','prometheus_window'])).limit(100))) if inv.manifest_id else []
        def rank(item):
            payload=item.payload or {};meta=payload.get('item') or {};key=str(meta.get('key') or payload.get('template') or '').lower()
            return (0 if 'lock' in key else 1 if 'cpu' in key else 2 if any(s in key for s in ('quer','question','qps')) else 3,item.id)
        for item in sorted(items,key=rank):
            if len(charts)==3:break
            try:projected=get_frozen_metric(reader,inv.id,item.id)
            except HTTPException:continue
            if not any(p.get('value') is not None for p in projected.get('points',[])):continue
            payload=item.payload or {};meta=payload.get('item') or {}
            charts.append(dict(id=item.id,title=safe_text(meta.get('name') or payload.get('template')),unit=projected['unit'],source=projected['source'],points=projected['points'],window={'from':iso(inv.window_start),'to':iso(inv.window_end)},coverage=projected.get('coverage',{}),host_name=safe_text(meta.get('host_name')),itemid=str(meta.get('itemid',''))))
        break
    return dict(enabled=True,server_time=iso(now),target={'instance_id':instance.id,'name':instance.name},pacing=pacing_projection(db,instance,policy,schedule,now),observation=observation,inspection=inspection_projection(db,{instance.id},policy=policy),report=report,report_matches_current_observation=matches,charts=charts)


def report_covers_episode(finding,identifier,start,end):
    first=(finding.calculation or {}).get('episode_started_at')
    if finding.latest_investigation_id!=identifier or not first:return False
    try:stamp=parse_time(first)
    except (HTTPException,TypeError,ValueError,AttributeError):return False
    return start<=stamp<end


def numeric_report_identifiers(db,instance_id,active):
    """Prefer an authorized episode diagnosis over newer unrelated history.

    Inspect lightweight metadata first so a bound report cannot disappear behind
    the 20-report history limit, without deserializing unshown report payloads.
    The caller still authorizes the entire chosen report and exact target scope.
    """
    linked={finding.latest_investigation_id for finding in active if finding.latest_investigation_id}
    preferred=set()
    if linked:
        for identifier,start,end in db.execute(select(m.Investigation.id,
                m.Investigation.window_start,m.Investigation.window_end).where(
                    m.Investigation.id.in_(linked),m.Investigation.status.in_(['completed','partial']))):
            if any(report_covers_episode(finding,identifier,start,end) for finding in active):
                preferred.add(identifier)
    statement=select(m.Investigation.id).join(m.InvestigationResource,
        m.InvestigationResource.investigation_id==m.Investigation.id).where(
            m.InvestigationResource.instance_id==instance_id,m.Investigation.status.in_(['completed','partial']))
    if preferred:statement=statement.order_by(case((m.Investigation.id.in_(preferred),0),else_=1))
    return db.scalars(statement.order_by(m.Investigation.created_at.desc()).limit(20))


def numeric_snapshot(db,config):
    """Actual selected measurements and reports; no knowledge of Lab switches."""
    from .zabbix_items import safe_item,safe_text
    from .investigations import get_investigation
    from .numeric_evidence import latest_numeric_evidence
    from .tools import project_metric_window
    cfg,instance,policy,schedule,reader=target(db,config)
    mapping=db.scalar(select(m.MonitoringMapping).where(m.MonitoringMapping.instance_id==instance.id))
    selected={(str(i.get('hostid')),str(i.get('itemid'))) for i in mapping.items if i.get('value_kind')=='numeric'}
    now=db_now(db); metrics=[]; seen=set(); live_charts=[]
    selected_items={(str(i.get('hostid')),str(i.get('itemid'))):i for i in mapping.items}
    numeric_candidates=list(latest_numeric_evidence(db,instance.id,mapping.source_id,selected))
    for row in numeric_candidates:
        payload=row.payload or {}; meta=payload.get('item') or {}; coverage=row.coverage or {}
        identity=(str(meta.get('hostid')),str(meta.get('itemid')))
        if identity not in selected or identity in seen: continue
        seen.add(identity)
        if (coverage.get('source_id')!=mapping.source_id or coverage.get('mapping_version')!=mapping.version
                or coverage.get('config_revision')!=instance.config_revision): continue
        points=[p for p in payload.get('rows',[]) if isinstance(p,dict)]
        latest=points[-1] if points else {}
        value=latest.get('value')
        valid=not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value)
        fresh=coverage.get('availability')=='available' and 0<=(now-row.observed_at).total_seconds()<=max(20,3*meta.get('interval',60))
        item=safe_item(meta)
        metrics.append({'itemid':identity[1],'hostid':identity[0],'name':item.get('name'),
            'host_name':item.get('host_name'),'value':value if valid else None,'unit':item.get('units'),
            'observed_at':iso(row.observed_at),'status':'fresh' if fresh and valid else 'stale' if valid else 'unknown',
            'value_semantics':item.get('value_semantics','unknown'),'evidence_id':row.id})
        if fresh and valid and len(live_charts)<8:
            live_charts.append({'id':row.id,'title':item.get('name'),'host_name':item.get('host_name'),
                'unit':item.get('units'),'source':'zabbix_live_retained_samples','itemid':identity[1],
                'points':[{'time':p.get('time'),'value':p.get('value') if isinstance(p.get('value'),(int,float)) and not isinstance(p.get('value'),bool) and math.isfinite(p['value']) else None} for p in points[-100:]],
                'window':{'from':points[0].get('time'),'to':points[-1].get('time')},'coverage':{'complete':coverage.get('complete') is True,'reason_codes':coverage.get('reason_codes',[])}})
    findings=list(db.scalars(select(m.InspectionFinding).join(m.Event,m.Event.id==m.InspectionFinding.event_id).where(m.InspectionFinding.instance_id==instance.id,m.Event.status.notin_(['resolved','closed','merged'])).order_by(m.InspectionFinding.updated_at.desc()).limit(20)))
    current_metrics={(entry['hostid'],entry['itemid']):entry for entry in metrics if entry['status']=='fresh'}
    def fresh_stamp(value,meta):
        try: stamp=parse_time(value) if isinstance(value,str) else value
        except (TypeError,ValueError,HTTPException): return False
        return bool(stamp and 0<=(now-stamp).total_seconds()<=max(20,3*meta.get('interval',60)))
    def current_provenance(provenance):
        return (provenance.get('source_id')==mapping.source_id
                and provenance.get('mapping_version')==mapping.version
                and provenance.get('config_revision')==instance.config_revision)
    capacity_facts={}
    def current_signal(finding):
        calculation=finding.calculation or {}
        if finding.category=='capacity_pressure':
            from .capacity_signals import current_capacity_fact
            fact=current_capacity_fact(db,finding,mapping,instance,now,candidates=numeric_candidates)
            if fact is not None:capacity_facts[finding.id]=fact
            return fact is not None
        if calculation.get('observation_state') in ('recovered','returned_to_baseline'): return False
        if finding.category=='baseline_change':
            identity=(str(calculation.get('hostid')),str(calculation.get('itemid')))
            metric=current_metrics.get(identity)
            if (not metric or metric.get('value_semantics') not in ('gauge','rate')
                    or not current_provenance(calculation)
                    or not fresh_stamp(calculation.get('observed_at'),selected_items[identity])): return False
            latest=db.get(m.Evidence,metric['evidence_id'])
            if not latest or set((latest.coverage or {}).get('reason_codes') or [])-{'history_truncated'}: return False
            from .health_signals import assess_change
            current_payload=latest.payload or {}
            checked=assess_change(current_payload.get('item') or {},current_payload.get('rows'),now,
                                  baseline=calculation.get('baseline'))
            if checked['state']!='changed': return False
        for reference in finding.evidence_ids or []:
            if reference.startswith('manifest:'):
                item=db.get(m.ManifestItem,reference[9:])
                inv=db.get(m.Investigation,finding.latest_investigation_id) if finding.latest_investigation_id else None
                if not item or not inv or item.manifest_id!=inv.manifest_id or item.instance_id!=instance.id: continue
                payload=item.payload or {}; meta=payload.get('item') or {}
                identity=(str(meta.get('hostid')),str(meta.get('itemid')))
                if identity not in current_metrics or not current_provenance(payload.get('provenance') or {}): continue
                projected=project_metric_window(item,inv.window_start,inv.window_end)
                if projected and any(p['value'] is not None and fresh_stamp(p['time'],meta) for p in projected['points']): return True
                continue
            evidence=db.get(m.Evidence,reference)
            if not evidence or evidence.instance_id!=instance.id or evidence.source_mode!='live': continue
            coverage=evidence.coverage or {}; meta=(evidence.payload or {}).get('item') or {}
            identity=(str(meta.get('hostid') or coverage.get('hostid')),str(meta.get('itemid') or coverage.get('itemid')))
            if (identity not in selected_items or coverage.get('availability')!='available'
                    or not current_provenance(coverage) or not fresh_stamp(evidence.observed_at,selected_items[identity])): continue
            if evidence.source.startswith('zabbix.item.numeric:') and identity not in current_metrics: continue
            if finding.category=='baseline_change' and identity!=(str(calculation.get('hostid')),str(calculation.get('itemid'))): continue
            return True
        return False
    active=[f for f in findings if current_signal(f)]
    from types import SimpleNamespace
    from .intelligence import _finding_presentation
    rule_findings=[]
    for finding in active:
        fact=capacity_facts.get(finding.id)
        if fact is None or len(rule_findings)>=6:continue
        # Present this read's checked operands without mutating the saved rule,
        # event, model report, or any frozen observation window.
        event=db.get(m.Event,finding.event_id)
        view=SimpleNamespace(category=finding.category,calculation=fact['calculation'])
        presentation=_finding_presentation(event,view,instance,mapping,None)
        rule_findings.append({'title':presentation['title'],'summary':presentation['summary'],
            'observed_at':fact['calculation']['observed_at'],
            'evidence_ids':fact['evidence_ids'],'status':'active'})
    signals=[{'id':f.id,'category':f.category,'kind':f.kind,'classification':(f.calculation or {}).get('classification','observed_change'),
              'observed_at':(f.calculation or {}).get('observed_at'),
              'episode_started_at':(f.calculation or {}).get('episode_started_at'),
              'hostid':str(f.calculation['hostid']) if (f.calculation or {}).get('hostid') is not None else None,
              'itemid':str(f.calculation['itemid']) if (f.calculation or {}).get('itemid') is not None else None,
              'event_id':f.event_id} for f in active]
    report=None;charts=[];matches=False
    for identifier in numeric_report_identifiers(db,instance.id,active):
        resources=set(db.scalars(select(m.InvestigationResource.instance_id).where(m.InvestigationResource.investigation_id==identifier)))
        if resources!={instance.id}: continue
        try: inv=get_investigation(reader,identifier)
        except HTTPException: continue
        task=db.get(m.Task,inv.task_id) if inv.task_id else None
        matching_ids={f.id for f in active if report_covers_episode(f,inv.id,inv.window_start,inv.window_end)}
        matches=bool(matching_ids)
        report={'id':inv.id,'status':inv.status,'mode':(task.payload or {}).get('mode','rules_only') if task else 'rules_only',
                'created_at':iso(inv.created_at),'error_code':task.error_code if task else None,
                'executive_summary':(inv.report or {}).get('executive_summary'),'report_presentation':report_presentation(inv),'coverage':(inv.report or {}).get('coverage',{}),
                'current_for_episode':matches,'matched_signals':[signal for signal in signals if signal['id'] in matching_ids],
                'from':iso(inv.window_start),'to':iso(inv.window_end)}
        # Fetch only identity metadata for ranking. Frozen windows can contain
        # thousands of points; avoid loading every unshown chart payload.
        candidates=list(db.execute(select(m.ManifestItem.id,m.ManifestItem.payload['item']['itemid'].as_string()).where(
            m.ManifestItem.manifest_id==inv.manifest_id,m.ManifestItem.instance_id==instance.id,
            m.ManifestItem.item_type=='time_series_window').limit(192))) if inv.manifest_id else []
        changed_ids={str((f.calculation or {}).get('itemid')) for f in active}
        candidates.sort(key=lambda entry:(str(entry[1]) not in changed_ids,entry[0]))
        for offset in range(0,len(candidates),6):
            identifiers=[entry[0] for entry in candidates[offset:offset+6]]
            entries={entry.id:entry for entry in db.scalars(select(m.ManifestItem).where(
                m.ManifestItem.id.in_(identifiers),m.ManifestItem.manifest_id==inv.manifest_id,
                m.ManifestItem.instance_id==instance.id,m.ManifestItem.item_type=='time_series_window'))}
            for identifier in identifiers:
                entry=entries.get(identifier)
                if entry is None: continue
                # get_investigation above authorized the complete frozen report
                # scope. The query also pins each item to this report and target.
                projected=project_metric_window(entry,inv.window_start,inv.window_end)
                if not projected or not any(p.get('value') is not None for p in projected.get('points',[])): continue
                meta=safe_item((entry.payload or {}).get('item') or {})
                charts.append({'id':entry.id,'title':meta.get('name'),'host_name':meta.get('host_name'),'itemid':meta.get('itemid'),
                    'unit':projected['unit'],'source':projected['source'],'points':projected['points'],
                    'window':{'from':iso(inv.window_start),'to':iso(inv.window_end)},'coverage':projected.get('coverage',{})})
                if len(charts)>=6: break
            if len(charts)>=6: break
        break
    fresh=[entry for entry in metrics if entry['status']=='fresh']
    observation={'status':'signals_present' if active else 'data_available' if fresh else 'unknown',
        'source':'zabbix','observed_at':max((entry['observed_at'] for entry in metrics),default=None),
        'complete':bool(selected) and len(fresh)==len(selected),'metrics':metrics[:64],
        'signals':signals,'meaning':'No signal is not proof of business health.',
        'gaps':[] if bool(selected) and len(fresh)==len(selected) else ['selected_samples_missing_or_stale']}
    pacing=pacing_projection(db,instance,policy,schedule,now)
    return {'enabled':True,'server_time':iso(now),'target':{'instance_id':instance.id,'name':safe_text(instance.name),'key':cfg.get('key')},
        'pacing':pacing,'observation':observation,'rule_findings':rule_findings,
        'inspection':inspection_projection(db,{instance.id},policy=policy,now=now,
            collection_context=(instance,mapping,schedule,pacing)),'report':report,
        'report_matches_current_observation':matches,'charts':charts,'live_charts':live_charts}
