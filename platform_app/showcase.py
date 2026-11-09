"""Fixed read-only projection and expiring collection cadence, never AI dispatch."""
from datetime import timedelta
from pathlib import Path
import hmac

from fastapi import HTTPException
from sqlalchemy import select

from . import models as m
from .access import actor_for_user, authorize, current_permissions
from .common import PlatformError, iso, parse_time
from .db import utcnow
from .scheduling import db_now

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
    return cfg


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
    return (policy.version==state.get('policy_version') and schedule.config_revision==state.get('schedule_revision') and policy.signal_interval_seconds==FAST_SIGNAL and schedule.interval_seconds==FAST_COLLECTION)


def _restore(state,policy,schedule,now):
    if policy and policy.version==state.get('policy_version') and policy.signal_interval_seconds==FAST_SIGNAL:
        policy.signal_interval_seconds=state['baseline_signal_seconds']
        policy.next_signal_at=min(policy.next_signal_at,now+timedelta(seconds=policy.signal_interval_seconds))
    if schedule and schedule.config_revision==state.get('schedule_revision') and schedule.interval_seconds==FAST_COLLECTION:
        schedule.interval_seconds=state['baseline_collection_seconds']
        schedule.next_due_at=min(schedule.next_due_at,now+timedelta(seconds=schedule.interval_seconds))


def pacing_projection(db,instance,policy,schedule,now=None):
    now=now or db_now(db)
    _,state=_state(db,instance.id)
    active=bool(state.get('active') and state.get('expires_at') and parse_time(state['expires_at'])>now and _unchanged(state,policy,schedule))
    return dict(active=active,expires_at=state.get('expires_at') if active else None,collection_seconds=schedule.interval_seconds,signal_seconds=policy.signal_interval_seconds,baseline_collection_seconds=state.get('baseline_collection_seconds',schedule.interval_seconds),baseline_signal_seconds=state.get('baseline_signal_seconds',policy.signal_interval_seconds),reason='administrator_changed' if state.get('suspended') else None)


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
        state.update(active=True,expires_at=iso(now+timedelta(seconds=LEASE_SECONDS)))
        schedule.interval_seconds=FAST_COLLECTION
        policy.signal_interval_seconds=FAST_SIGNAL
        schedule.next_due_at=min(schedule.next_due_at,now)
        policy.next_signal_at=min(policy.next_signal_at,now)
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


def inspection_projection(db,visible,*,policy=None):
    now=db_now(db)
    if policy is None:
        candidates=db.scalars(select(m.InspectionPolicy).order_by(m.InspectionPolicy.enabled.desc(),m.InspectionPolicy.updated_at.desc()).limit(200))
        policy=next((p for p in candidates if p.instance_ids and set(p.instance_ids).issubset(visible)),None)
    empty=dict(status='not_configured',reason='尚未配置自动巡检',last_completed_at=None,next_due_at=None,next_ai_at=None,mode=None,last_checked_at=None,next_check_at=None,last_observed_at=None,collection_seconds=None,signal_seconds=None,task=None,pacing={'active':False,'expires_at':None})
    if policy is None:return empty
    check=db.get(m.PlatformSetting,'inspection-status:'+policy.id)
    mappings=list(db.scalars(select(m.MonitoringMapping).where(m.MonitoringMapping.instance_id.in_(visible))))
    stamps=[r.last_observed_at for r in mappings if r.last_observed_at]
    result={**empty,'status':policy.status,'reason':policy.reason,'last_completed_at':iso(policy.last_completed_at),'next_due_at':iso(policy.next_ai_at) if policy.enabled else None,'mode':policy.mode,'last_checked_at':(check.value or {}).get('last_checked_at') if check else None,'next_check_at':iso(policy.next_signal_at) if policy.enabled else None,'last_observed_at':iso(max(stamps)) if stamps else None,'signal_seconds':policy.signal_interval_seconds}
    from .intelligence import effective_policy_state
    result['status'],result['reason']=effective_policy_state(db,policy,now=now)
    tasks=[]
    for dispatch in db.scalars(select(m.InspectionDispatch).where(m.InspectionDispatch.policy_id==policy.id).order_by(m.InspectionDispatch.created_at.desc()).limit(20)):
        if not set(dispatch.instance_ids).issubset(visible):continue
        task=db.get(m.Task,dispatch.task_id);inv=db.get(m.Investigation,dispatch.investigation_id)
        if task and inv:
            resources=set(db.scalars(select(m.InvestigationResource.instance_id).where(m.InvestigationResource.investigation_id==inv.id)))
            if not resources or not resources.issubset(visible):continue
            if task.status in ('queued','running','retry_wait'):
                tasks=[(task,inv)];break
            if not tasks:tasks.append((task,inv))
    if tasks:
        task,inv=tasks[0]
        result['task']=dict(id=task.id,status=inv.status,created_at=iso(task.created_at),started_at=iso(task.started_at),error_code=task.error_code)
        if task.status in ('queued','running','retry_wait'):result['status']=inv.status
    if len(visible)==1:
        identifier=next(iter(visible));instance=db.get(m.Instance,identifier)
        schedule=db.scalar(select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id==identifier,m.CollectionSchedule.collector=='monitoring'))
        if schedule:
            result['collection_seconds']=schedule.interval_seconds
            result['pacing']=pacing_projection(db,instance,policy,schedule,now)
    result['next_ai_at']=result['next_due_at']
    return result


def snapshot(db,config):
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
        report=dict(id=inv.id,status=inv.status,mode=(task.payload or {}).get('mode','rules_only') if task else 'rules_only',created_at=iso(inv.created_at),error_code=task.error_code if task else None,executive_summary=(inv.report or {}).get('executive_summary'),coverage=(inv.report or {}).get('coverage',{}),current_for_episode=matches,**{'from':iso(inv.window_start),'to':iso(inv.window_end)})
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
