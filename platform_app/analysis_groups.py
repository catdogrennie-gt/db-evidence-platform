"""Single-source, exact-item analysis objects reusing Instance authorization."""
from datetime import timedelta
from uuid import uuid4
import hashlib
import json
import time
from sqlalchemy import select
from . import models as m
from .access import authorize,authorize_system,scoped_instance
from .common import PlatformError,iso,parse_time
from .db import utcnow
from .zabbix_items import catalog,matches_tags,typed_history


def is_group(mapping): return mapping is not None and mapping.layer=='group' and mapping.hostid.startswith('group:')


def query_items(actor,source,body,config):
    from .monitoring import client_for
    authorize_system(actor,source.system_id,'asset.onboard')
    items,_=catalog(client_for(source,config),source,body.get('hostids'))
    search=str(body.get('search','')).casefold()
    items=[i for i in items if matches_tags(i['host_tags'],body.get('host_tags')) and matches_tags(i['tags'],body.get('item_tags')) and (not search or search in (i['name']+' '+i['key']+' '+i['host_name']).casefold())]
    offset=max(0,int(body.get('offset',0))); limit=min(200,max(1,int(body.get('limit',100))))
    return {'items':items[offset:offset+limit],'total':len(items),'offset':offset,'limit':limit,'next_offset':offset+limit if offset+limit<len(items) else None,'truncated':False}


def resolve(client,source,refs,*,strict=False):
    hosts={str(r['hostid']) for r in refs}; ids={str(r['itemid']) for r in refs}
    items,host_rows=catalog(client,source,hosts,ids)
    lookup={(i['hostid'],i['itemid']):i for i in items}
    if not hosts.issubset({str(h['hostid']) for h in host_rows}): raise PlatformError(403,'monitoring_scope_changed','所选主机已不在来源授权范围。')
    if strict and any((str(r['hostid']),str(r['itemid'])) not in lookup for r in refs): raise PlatformError(403,'monitoring_scope_changed','所选监控项已不在来源授权范围。')
    return [lookup.get((str(r['hostid']),str(r['itemid'])),dict(r,available=False,state='1',error='monitoring_item_missing',missing=True)) for r in refs],host_rows


def get_group(actor,identifier,action='data.read'):
    mapping=actor.db.get(m.MonitoringMapping,identifier)
    if not is_group(mapping): raise PlatformError(404,'analysis_group_not_found','联合分析对象不存在。')
    instance=scoped_instance(actor,mapping.instance_id,action=action)
    return mapping,instance,actor.db.get(m.MonitoringSource,mapping.source_id)


def group_response(db,mapping,instance,source):
    settings=mapping.relationships or {}; kinds={}
    for item in mapping.items: kinds[item['value_kind']]=kinds.get(item['value_kind'],0)+1
    schedule=db.scalar(select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id==instance.id,m.CollectionSchedule.collector=='monitoring'))
    return {'id':mapping.id,'instance_id':instance.id,'name':instance.name,'description':settings.get('description',''),'source_id':source.id,'source_name':source.name,'system_id':instance.system_id,'enabled':mapping.selected and instance.enabled,'config_revision':instance.config_revision,'version':mapping.version,'status':'paused' if not mapping.selected else source.status if source.status!='ready' else 'unknown' if mapping.last_observed_at is None else 'stale' if mapping.last_observed_at<utcnow()-timedelta(minutes=5) else 'ready','last_observed_at':iso(mapping.last_observed_at),'item_count':len(mapping.items),'host_count':len({i['hostid'] for i in mapping.items}),'value_kind_counts':kinds,'items':mapping.items,'hosts':settings.get('hosts',[]),'relations':settings.get('relations',[]),'interval_seconds':schedule.interval_seconds if schedule else 60}


def read_group(actor,identifier,config,refresh=True):
    from .monitoring import client_for
    mapping,instance,source=get_group(actor,identifier)
    result=group_response(actor.db,mapping,instance,source)
    if refresh:
        items,hosts=resolve(client_for(source,config),source,mapping.items)
        result['items']=items; result['hosts']=[{'hostid':str(h['hostid']),'name':h.get('name') or h.get('host'),'tags':h.get('tags',[])} for h in hosts]
    return result


def list_groups(actor,config,**scope):
    from .monitoring import client_for
    allowed=authorize(actor,'data.read').instance_ids
    items=[]; clients={}; deadline=time.monotonic()+10; refreshed=0
    for mapping in actor.db.scalars(select(m.MonitoringMapping).where(m.MonitoringMapping.layer=='group',m.MonitoringMapping.instance_id.in_(allowed))):
        instance=actor.db.get(m.Instance,mapping.instance_id); source=actor.db.get(m.MonitoringSource,mapping.source_id)
        system=actor.db.get(m.System,instance.system_id)
        if any(scope.get(k) and scope[k]!=v for k,v in [('instance_id',instance.id),('system_id',instance.system_id),('department_id',system.department_id),('environment',system.environment)]): continue
        try:
            if refreshed>=16 or time.monotonic()>=deadline: raise PlatformError(503,'monitoring_list_refresh_budget','列表元数据刷新预算已用尽。')
            refreshed+=1
            if source.id not in clients:
                clients[source.id]=client_for(source,config)
                if hasattr(clients[source.id],'deadline'): clients[source.id].deadline=min(clients[source.id].deadline,deadline)
            fresh,hosts=resolve(clients[source.id],source,mapping.items)
            item=group_response(actor.db,mapping,instance,source)
            item['items']=fresh; item['hosts']=[{'hostid':str(h['hostid']),'name':h.get('name') or h.get('host'),'tags':h.get('tags',[])} for h in hosts]
        except PlatformError as exc:
            item=group_response(actor.db,mapping,instance,source)
            item['status']='scope_changed' if exc.detail['code']=='monitoring_scope_changed' else 'unavailable'
            item['metadata_error']=exc.detail['code']
            item['metadata_fresh']=False
            item['items']=[dict(i,metadata_fresh=False,metadata_error=exc.detail['code']) for i in item['items']]
        items.append(item)
    return {'items':items,'total':len(items)}


def save_group(actor,config,body,identifier=None):
    from .monitoring import get_source,client_for
    refs=body.get('items',[])
    if not 1<=len(refs)<=32 or len({(r['hostid'],r['itemid']) for r in refs})!=len(refs) or len({r['itemid'] for r in refs})!=len(refs): raise PlatformError(422,'invalid_item_selection','必须选择1至32个不重复监控项。')
    source=get_source(actor,body['source_id']); mapping=None; instance=None
    if identifier:
        mapping,instance,old_source=get_group(actor,identifier,'asset.manage')
        if source.id!=old_source.id: raise PlatformError(409,'monitoring_identity_locked','已有组不可更换来源，请创建新对象。')
        # Same lock order as collection: Instance -> Mapping.
        instance=actor.db.scalar(select(m.Instance).where(m.Instance.id==instance.id).execution_options(populate_existing=True).with_for_update())
        mapping=actor.db.scalar(select(m.MonitoringMapping).where(m.MonitoringMapping.id==identifier).execution_options(populate_existing=True).with_for_update())
        if body.get('config_revision')!=instance.config_revision: raise PlatformError(409,'configuration_changed','配置版本已变化，请重新读取。')
    items,hosts=resolve(client_for(source,config),source,refs,strict=True)
    items.sort(key=lambda i:(i['hostid'],i['itemid']))
    selected_hosts={i['hostid'] for i in items}; relations=body.get('relations',[])
    if len(relations)>32 or any(r['from_hostid'] not in selected_hosts or r['to_hostid'] not in selected_hosts or r['from_hostid']==r['to_hostid'] or r['kind'] not in ('runs_on','depends_on','connects_to','related_to') for r in relations): raise PlatformError(422,'invalid_group_relation','关系必须连接已选的两个不同主机。')
    interval=body.get('interval_seconds',60)
    if not 10<=interval<=3600: raise PlatformError(422,'invalid_interval','采集周期必须为10至3600秒。')
    settings={'kind':'analysis_group','description':body.get('description',''),'hosts':[{'hostid':str(h['hostid']),'name':h.get('name') or h.get('host'),'tags':h.get('tags',[])} for h in hosts], 'relations':sorted(relations,key=lambda r:(r['from_hostid'],r['to_hostid'],r['kind'],r.get('label','')))}
    enabled=body.get('enabled',True)
    if mapping is None:
        instance=m.Instance(system_id=source.system_id,name=body['name'],endpoint='zabbix:group',connection_mode='zabbix',engine='analysis',expected_server_uuid=None,enabled=enabled,lifecycle_state='Active' if enabled else 'Paused')
        actor.db.add(instance); actor.db.flush(); actor.db.add(m.InstanceLease(instance_id=instance.id))
        mapping=m.MonitoringMapping(source_id=source.id,hostid='group:'+str(uuid4()),instance_id=instance.id,layer='group',items=[],relationships={})
        actor.db.add(mapping); actor.db.flush()
        schedule=m.CollectionSchedule(instance_id=instance.id,collector='monitoring',interval_seconds=interval,config_revision=instance.config_revision,enabled=enabled)
        actor.db.add(schedule)
    else:
        schedule=actor.db.scalar(select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id==instance.id,m.CollectionSchedule.collector=='monitoring').with_for_update())
        stable=lambda rows:[{k:v for k,v in i.items() if k not in ('lastclock','status','state','error','available')} for i in rows]
        changed=instance.name!=body['name'] or mapping.selected!=enabled or stable(mapping.items)!=stable(items) or {k:v for k,v in (mapping.relationships or {}).items() if k!='hosts'}!={k:v for k,v in settings.items() if k!='hosts'} or schedule is None or schedule.interval_seconds!=interval
        if changed: instance.config_revision+=1; mapping.version+=1
        if schedule is None: schedule=m.CollectionSchedule(instance_id=instance.id,collector='monitoring'); actor.db.add(schedule)
    instance.name=body['name']; instance.enabled=enabled; instance.lifecycle_state='Active' if enabled else 'Paused'
    mapping.items=items; mapping.relationships=settings; mapping.selected=enabled
    mapping.capabilities=[{'name':kind,'status':'available' if any(i['available'] and i['value_kind']==kind for i in items) else 'missing','reason':''} for kind in ('numeric','text','log')]
    schedule.interval_seconds=interval; schedule.enabled=enabled; schedule.config_revision=instance.config_revision
    actor.db.flush()
    return group_response(actor.db,mapping,instance,source)


def item_history(actor,identifier,itemid,start,end,limit,config):
    from .monitoring import client_for
    mapping,instance,source=get_group(actor,identifier)
    if not any(i['itemid']==itemid for i in mapping.items): raise PlatformError(404,'monitoring_item_not_selected','该监控项不属于此对象。')
    client=client_for(source,config); items,_=resolve(client,source,mapping.items); item=next(i for i in items if i['itemid']==itemid)
    if item['value_kind'] in ('text','log'): authorize(actor,'evidence.read_sensitive',[instance.id])
    rows,coverage=typed_history(client,item,start,end,min(1000,max(1,limit)))
    return {'item':item,'kind':item['value_kind'],'rows':rows,'coverage':coverage,'provenance':provenance(source,mapping,instance,item)}


def provenance(source,mapping,instance,item):
    return {'source_id':source.id,'hostid':item['hostid'],'itemid':item['itemid'],'mapping_version':mapping.version,'config_revision':instance.config_revision,'transport':'zabbix'}


def group_charts(db,instance,mapping,start,end,config):
    from .monitoring import client_for
    source=db.get(m.MonitoringSource,mapping.source_id); client=client_for(source,config)
    items,_=resolve(client,source,mapping.items); charts=[]
    for item in items:
        if item['value_kind']!='numeric': continue
        try: rows,coverage=typed_history(client,item,start,end,2000)
        except PlatformError as exc: rows=[]; coverage={'complete':False,'truncated':False,'reason_codes':[exc.detail['code']]}
        points=[]; gaps=[{'reason':r} for r in coverage['reason_codes']]; previous=None
        for row in rows:
            stamp=parse_time(row['time'])
            if item['interval_verified'] and (stamp-(previous if previous else start)).total_seconds()>item['interval']*1.5:
                missing=previous+timedelta(seconds=item['interval']) if previous else start
                points.append({'time':iso(missing),'value':None,'validity':'unknown_gap'}); gaps.append({'from':iso(missing),'to':iso(stamp),'reason':'missing_samples'})
            points.append(row)
            if row['value'] is None: gaps.append({'from':row['time'],'to':row['time'],'reason':'invalid_value'})
            previous=stamp
        if previous is None or item['interval_verified'] and (end-previous).total_seconds()>item['interval']*1.5:
            missing=previous+timedelta(seconds=item['interval']) if previous else start
            points.append({'time':iso(missing),'value':None,'validity':'unknown_gap'}); gaps.append({'from':iso(missing),'to':iso(end),'reason':'missing_samples'})
        if not item['interval_verified']: gaps.append({'reason':'sample_interval_unverified'})
        coverage['complete']=coverage['complete'] and not gaps
        charts.append({'id':f"zabbix:{source.id}:{item['hostid']}:{item['itemid']}",'title':item['host_name']+' / '+item['name'],'template':'zabbix_item_numeric','owner_collector':'monitoring','instance_id':instance.id,'unit':item['units'],'item':item,'source':'zabbix_history','points':points,'gaps':gaps,'coverage':coverage,'window':{'from':iso(start),'to':iso(end)},'provenance':provenance(source,mapping,instance,item),'registered_relationships':[dict(r,kind='registered',relationship_kind=r['kind'],provenance='user_registered',relationship='用户登记关联，不代表因果',evidence_ids=[]) for r in (mapping.relationships or {}).get('relations',[])]})
    for chart in charts: chart['group_hosts']=(mapping.relationships or {}).get('hosts',[])
    return charts


def frozen_group_graph(payload,instance_id,nodes,edges):
    """Only frozen selected composition supplies host identities and relations."""
    item=payload.get('item') or {}; source=(payload.get('provenance') or {}).get('source_id')
    if payload.get('template')!='zabbix_item_numeric' or not source: return None
    hosts={str(h['hostid']):h for h in payload.get('group_hosts',[]) if isinstance(h,dict) and h.get('hostid')}
    if item.get('hostid'): hosts.setdefault(str(item['hostid']),{'hostid':item['hostid'],'name':item.get('host_name')})
    identity=lambda hostid:'registered-host:'+instance_id+':'+source+':'+hostid
    for hostid,host in hosts.items():
        nid=identity(hostid)
        if any(n['id']==nid for n in nodes): continue
        nodes.append({'id':nid,'instance_id':instance_id,'hostid':hostid,'source_id':source,'label':host.get('name') or hostid,'kind':'registered','summary':'冻结的联合对象所选主机身份，不证明故障或因果','evidence_ids':[]})
        edges.append({'from':instance_id,'to':nid,'kind':'registered','relationship':'所选主机组成','provenance':'frozen_group_selection','evidence_ids':[]})
    for relation in payload.get('registered_relationships',[]):
        if not isinstance(relation,dict) or relation.get('provenance')!='user_registered' or str(relation.get('from_hostid')) not in hosts or str(relation.get('to_hostid')) not in hosts: continue
        edge={'from':identity(str(relation['from_hostid'])),'to':identity(str(relation['to_hostid'])),'kind':'registered','relationship_kind':relation.get('relationship_kind'),'relationship':relation.get('label') or '用户登记关联，不代表因果','provenance':'user_registered','evidence_ids':[]}
        if edge not in edges: edges.append(edge)
    return identity(str(item['hostid'])) if str(item.get('hostid')) in hosts else None


def lock_snapshot_metadata(item,parsed):
    """Verify one official snapshot without upgrading history coverage."""
    contract='db-evidence-lock-snapshot-v2'
    tags={t.get('tag'):t.get('value') for t in item.get('tags',[]) if isinstance(t,dict)}
    rows=parsed.get('rows')
    count=parsed.get('total_row_count')
    valid=(item.get('enhancement')=='mysql.lock_waits'
        and tags.get('sl.snapshot_contract')==contract
        and parsed.get('producer_contract')==contract
        and parsed.get('snapshot_complete') is True
        and parsed.get('snapshot_truncated') is False
        and type(count) is int and 0<=count<=50
        and isinstance(rows,list) and len(rows)==count
        and all(isinstance(row,dict) for row in rows))
    return {'snapshot_contract':contract if valid else None,
        'snapshot_complete':valid,'snapshot_truncated':not valid,
        'snapshot_total_row_count':count if valid else None}


def collect_group(db,instance,mapping,source,lease,config):
    from .monitoring import client_for,digest_window
    from .evidence import append_batch
    from .scheduling import check_fence,complete,db_now
    from .events import evaluate
    client=client_for(source,config); items,_=resolve(client,source,mapping.items)
    end=utcnow(); start=end-timedelta(minutes=10); entries=[]; observed=[]
    run=m.CollectionRun(task_id=lease.id,instance_id=instance.id,collector='monitoring',attempt=lease.attempt,config_revision=instance.config_revision,fence=lease.fence,status='running',started_at=db_now(db)); db.add(run); db.flush()
    for item in items:
        try: rows,coverage=typed_history(client,item,start,end,100 if item['value_kind']=='numeric' else 8,latest=True)
        except PlatformError as exc: rows=[]; coverage={'complete':False,'truncated':False,'reason_codes':[exc.detail['code']]}
        stamp=parse_time(rows[-1]['time']) if rows else end
        if rows: observed.append(stamp)
        fresh=bool(rows) and stamp>=end-timedelta(seconds=max(60,item['interval']*3))
        if not item['interval_verified']: coverage['reason_codes'].append('sample_interval_unverified')
        coverage.update(provenance(source,mapping,instance,item)); coverage['complete']=fresh and coverage['complete'] and item['interval_verified']; coverage['gaps']=coverage['reason_codes']
        identity=hashlib.sha256((source.id+':'+item['hostid']+':'+item['itemid']).encode()).hexdigest()[:24]
        entries.append({'source':'zabbix.item.'+item['value_kind']+':'+identity,'observed_at':iso(stamp),'payload':{'item':item,'rows':rows[-100:] if item['value_kind']=='numeric' else rows[-8:]},'availability':'available' if fresh else 'stale' if rows else 'unknown','coverage':coverage,'truncated':coverage['truncated']})
        if item.get('enhancement') and rows:
            try: parsed=json.loads(rows[-1]['value'])
            except (ValueError,TypeError): parsed=None
            if isinstance(parsed,dict) and isinstance(parsed.get('rows'),list):
                tags={t['tag']:t['value'] for t in item['tags']}
                spec={'field_units':{k[14:]:v for k,v in tags.items() if k.startswith('sl.field_unit.')},'snapshot_scope':tags.get('sl.snapshot_scope')}
                payload={'item':item,'rows':[r for r in parsed['rows'][:100] if isinstance(r,dict)],**spec,'cumulative_counters':parsed.get('cumulative_counters'),'counter_epoch':parsed.get('counter_epoch'),'scope':parsed.get('scope')}
                if item['enhancement']=='mysql.statement_digest': payload['window']=digest_window([(parse_time(r['time']),r['value']) for r in rows],spec)
                snapshot_coverage={**coverage,**lock_snapshot_metadata(item,parsed)} if item['enhancement']=='mysql.lock_waits' else coverage
                entries.append({'source':item['enhancement'],'observed_at':iso(stamp),'payload':payload,'sensitivity':'restricted','availability':'available' if fresh else 'stale','coverage':snapshot_coverage,'truncated':coverage['truncated']})
    check_fence(db,lease,config_revision=instance.config_revision)
    run.status='succeeded' if any(e['availability']=='available' for e in entries) else 'unavailable'; run.finished_at=db_now(db); run.row_count=len(entries)
    schedule=db.scalar(select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id==instance.id,m.CollectionSchedule.collector=='monitoring'))
    evidence_ids=append_batch(db,instance,run,{'observed_at':iso(end),'evidence_items':entries},interval_seconds=schedule.interval_seconds if schedule else 60,collector_version='zabbix-generic-1')
    mapping.items=items
    if observed: mapping.last_observed_at=max(observed)
    instance.last_success_at=mapping.last_observed_at
    # Successful metadata IO alone is not proof every selected history is healthy.
    upstream_errors=any('monitoring_upstream_unavailable' in e['coverage'].get('reason_codes',[]) for e in entries)
    source.status='failed' if upstream_errors else 'ready'; source.error_code='monitoring_upstream_unavailable' if upstream_errors else None
    if not upstream_errors: source.last_success_at=end
    evaluate(db,instance.id,run_id=run.id,now=run.finished_at)
    complete(db,lease,config_revision=instance.config_revision,result={'evidence_ids':evidence_ids})
    return {'evidence_ids':evidence_ids}
