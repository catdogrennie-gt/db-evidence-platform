"""Generic selection is exact, versioned, typed and scope checked."""
from datetime import timedelta, timezone
from types import SimpleNamespace
import json
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from tests.platform.test_monitoring import monitoring_env
from tests.platform.test_ai_scope import ai_env
from platform_app import models as m
from platform_app.db import utcnow


@pytest.fixture
def group_env(monitoring_env):
    e = monitoring_env
    role = e['db'].scalar(select(m.Role).where(m.Role.name == 'ai-test'))
    role.permissions = [*role.permissions, 'asset.manage', 'evidence.read_sensitive']
    e['db'].commit()
    fake = e['fake']; fake.calls = []; fake.allowed = True
    fake.hosts = [{'hostid': h, 'name': 'host-'+h, 'tags': [{'tag':'app','value':'orders'}]} for h in ('101','102')]
    fake.items = [dict(itemid=i, hostid=h, name='Same name', key_='custom.'+i, value_type=t, units=u, description='Selected telemetry', delay='60s', status='0', state='0', lastclock='100', tags=[{'tag':'component','value':'app'},{'tag':'component','value':'db'}]) for i,h,t,u in [('11','101','0','ms'),('12','102','3','B'),('13','102','4',''),('14','102','2',''),('15','101','5','')]]
    now = int(utcnow().replace(tzinfo=timezone.utc).timestamp())-60
    def call(method, params):
        fake.calls.append((method,params))
        if method == 'host.get': return [h for h in fake.hosts if (not params.get('hostids') or h['hostid'] in params['hostids']) and (not params.get('groupids') or fake.allowed)]
        if method == 'item.get': return [i for i in fake.items if (not params.get('hostids') or i['hostid'] in params['hostids']) and (not params.get('itemids') or i['itemid'] in params['itemids'])]
        if method == 'history.get':
            return [{'clock':str(now),'ns':'7','value': 'DO-NOT-SEND literal alice@example.com' if params['history'] in (2,4) else '17', 'severity':'3','source':'application','logeventid':'9'}]
        raise AssertionError(method)
    fake.call=call
    for task in e['db'].scalars(select(m.Task)): task.status='succeeded'
    e['db'].commit()
    e['body'] = dict(name='Orders',source_id=e['source'].id,description='Combined',items=[{'hostid':'101','itemid':'11'},{'hostid':'102','itemid':'12'},{'hostid':'102','itemid':'13'}],relations=[{'from_hostid':'101','to_hostid':'102','kind':'depends_on','label':'user relation'}],enabled=True,interval_seconds=60)
    return e


def test_catalog_preserves_duplicate_tags_and_unknown_types(group_env):
    from platform_app.analysis_groups import query_items
    e=group_env
    result=query_items(e['actor'],e['source'],{'item_tags':[{'tag':'component','value':'app','operator':'equals'}]},e['config'])
    assert result['total']==5
    assert result['items'][0]['tags']==e['fake'].items[0]['tags']
    assert {i['value_kind'] for i in result['items']}=={'numeric','text','log','unsupported'}
    assert all(i['selectable'] for i in result['items'])
    assert 'lastvalue' not in str(result)


def test_group_exact_membership_noop_and_config_fence(group_env):
    from platform_app.analysis_groups import save_group
    from platform_app.monitoring import select_hosts
    e=group_env; first=save_group(e['actor'],e['config'],e['body']); e['db'].commit()
    assert first['item_count']==3 and first['host_count']==2
    inst=e['db'].get(m.Instance,first['instance_id']); assert inst.engine=='analysis'
    e['fake'].items[0]['lastclock']='200'
    same=save_group(e['actor'],e['config'],dict(e['body'],config_revision=first['config_revision']),first['id'])
    assert same['config_revision']==first['config_revision'] and same['version']==first['version']
    changed=save_group(e['actor'],e['config'],dict(e['body'],name='New name',config_revision=same['config_revision']),first['id'])
    assert changed['config_revision']==first['config_revision']+1
    sched=e['db'].scalar(select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id==inst.id))
    assert sched.collector=='monitoring' and sched.config_revision==changed['config_revision']
    select_hosts(e['actor'],e['source'],[],e['config'])
    assert inst.enabled


@pytest.mark.parametrize('items', [[], [{'hostid':'101','itemid':'11'}]*2, [{'hostid':'102','itemid':'11'}], [{'hostid':'101','itemid':'999'}]])
def test_group_rejects_empty_duplicate_or_forged_refs(group_env,items):
    from platform_app.analysis_groups import save_group
    e=group_env
    with pytest.raises(HTTPException): save_group(e['actor'],e['config'],dict(e['body'],items=items))


def test_current_source_scope_rechecked_before_history_and_charts(group_env):
    from platform_app.analysis_groups import save_group, item_history
    from platform_app.monitoring import metric_charts
    e=group_env; group=save_group(e['actor'],e['config'],e['body']); e['db'].commit()
    e['source'].allowed_host_group_ids=['revoked']; e['fake'].allowed=False
    e['fake'].calls.clear(); end=utcnow()
    with pytest.raises(HTTPException): item_history(e['actor'],group['id'],'11',end-timedelta(minutes=5),end,100,e['config'])
    with pytest.raises(HTTPException): metric_charts(e['db'],e['db'].get(m.Instance,group['instance_id']),end-timedelta(minutes=5),end,e['config'])
    assert not any(method=='history.get' for method,_ in e['fake'].calls)


def test_generic_curves_stay_distinct_and_text_not_numeric(group_env):
    from platform_app.analysis_groups import save_group, item_history
    from platform_app.monitoring import metric_charts
    e=group_env; group=save_group(e['actor'],e['config'],e['body']); end=utcnow(); start=end-timedelta(minutes=5)
    charts=metric_charts(e['db'],e['db'].get(m.Instance,group['instance_id']),start,end,e['config'])
    assert len(charts)==2 and len({c['id'] for c in charts})==2
    assert all(c['template']=='zabbix_item_numeric' and c['item']['name']=='Same name' for c in charts)
    assert {c['provenance']['hostid'] for c in charts}=={'101','102'}
    history=item_history(e['actor'],group['id'],'13',start,end,100,e['config'])
    assert history['kind']=='text' and 'DO-NOT-SEND' in history['rows'][0]['value']
    with pytest.raises(HTTPException): item_history(e['actor'],group['id'],'14',start,end,100,e['config'])


def test_generic_evidence_and_frozen_projection_have_safe_meaning():
    from platform_app.tools import project_evidence, project_metric_window
    from platform_app.common import iso
    end=utcnow(); start=end-timedelta(minutes=1)
    meta={'hostid':'101','itemid':'11','name':'Latency','key':'app.latency','units':'ms','value_type':0,'value_kind':'numeric','description':'password hunter2 Authorization Bearer sk-example-secret','tags':[{'tag':'component','value':'orders'},{'tag':'api_token','value':'DO-NOT-SEND'},{'tag':'owner','value':'alice@example.com'}]}
    row=m.Evidence(id='e',instance_id='i',source='zabbix.item.numeric:11',source_mode='live',observed_at=start,coverage={'availability':'available','complete':True},truncated=False,payload={'item':meta,'rows':[{'time':iso(start),'value':17}], 'raw':'DO-NOT-SEND'})
    result=project_evidence(row)
    assert result['data']['item']['units']=='ms' and result['data']['rows'][0]['value']==17
    assert 'DO-NOT-SEND' not in json.dumps(result) and 'alice@example.com' not in json.dumps(result)
    assert 'hunter2' not in json.dumps(result) and 'sk-example-secret' not in json.dumps(result)
    row.item_type='time_series_window'; row.payload_sha256='a'*64
    row.payload={'item':meta,'template':'zabbix_item_numeric','owner_collector':'monitoring','unit':'ms','points':[{'time':iso(start),'value':17,'validity':'valid'}],'coverage':{'complete':False,'sampling':{'method':'original_extrema','original_point_count':20000}},'provenance':{'source_id':'s','hostid':'101','itemid':'11','mapping_version':1}}
    frozen=project_metric_window(row,start,end)
    assert frozen['item']['key']=='app.latency' and frozen['unit']=='ms'
    assert frozen['coverage']['sampling']['original_point_count']==20000
    assert 'DO-NOT-SEND' not in json.dumps(frozen)
    row.source='zabbix.item.text:13'; row.payload={'item':dict(meta,value_kind='text'),'rows':[{'value':'DO-NOT-SEND'}]}
    assert project_evidence(row)['data']['text_omitted'] is True
    assert 'DO-NOT-SEND' not in json.dumps(project_evidence(row))


@pytest.mark.parametrize('numeric_key', ['custom.11', 'dbops.mysql.lock_waits'])
def test_group_collection_freezes_generic_numeric_with_model_metadata(group_env,numeric_key):
    from platform_app.analysis_groups import save_group
    from platform_app.monitoring import handle_collection
    from platform_app.scheduling import enqueue,claim
    from platform_app.evidence import create_manifest
    from platform_app.investigations import create_investigation,_bundle,require_investigation_access
    e=group_env; e['fake'].items[0]['key_']=numeric_key; group=save_group(e['actor'],e['config'],e['body'])
    task=enqueue(e['db'],'collect_instance',instance_id=group['instance_id'],payload={'config_revision':group['config_revision'],'collectors':['monitoring']}); e['db'].commit()
    lease=claim(e['factory'],'group-test','collection'); assert lease.id==task.id
    handle_collection(e['factory'],lease,e['config']); e['db'].expire_all()
    rows=list(e['db'].scalars(select(m.Evidence).where(m.Evidence.instance_id==group['instance_id'])))
    assert len(rows)==3 and len({r.source for r in rows})==3
    assert next(r for r in rows if r.source.startswith('zabbix.item.text:')).sensitivity=='restricted'
    assert {params['itemids'][0] for method,params in e['fake'].calls if method=='history.get'}=={'11','12','13'}
    end=utcnow(); inv,_=create_investigation(e['actor'],[group['instance_id']],end-timedelta(minutes=10),end,'Generic evidence')
    manifest=create_manifest(e['db'],'investigation',inv.id,[group['instance_id']],inv.window_start,end)
    inv.manifest_id=manifest.id
    bundle,*_= _bundle(e['db'],inv,manifest)
    assert 'monitoring' in require_investigation_access(e['actor'],[group['instance_id']],'rules_only')[group['instance_id']]['allowed_sources']
    assert any(item['data'].get('item',{}).get('key')==numeric_key for item in bundle['evidence'])
    assert any(item['data'].get('metric')=='zabbix_item_numeric' and item['data'].get('item',{}).get('units')=='ms' for item in bundle['evidence'])
    assert 'DO-NOT-SEND' not in json.dumps(bundle)
    from platform_app.tools import ToolBroker
    tool=ToolBroker(e['actor'],inv,[group['instance_id']],inv.window_start,end,allowed_evidence_ids={r.id for r in rows})
    result=tool.call('get_metric_window',{'instance_id':group['instance_id']})
    assert result['items'] and result['coverage']['complete'] is False
    from platform_app.intelligence import issue_detail
    event=m.Event(instance_id=group['instance_id'],title='Review',source='manual'); e['db'].add(event); e['db'].flush()
    inv.event_id=event.id; inv.status='completed'; inv.report={'facts':[]}; e['db'].flush()
    detail=issue_detail(e['actor'],event.id)
    if numeric_key=='dbops.mysql.lock_waits': assert any(c['metric_key']=='lock_waits' for c in detail['charts'])
    assert len({c['title'] for c in detail['charts']})==2
    assert any('host-101' in c['title'] for c in detail['charts'])
    assert any(edge.get('provenance')=='user_registered' and edge.get('relationship_kind')=='depends_on' for edge in detail['edges'])
    assert any(item['data'].get('registered_relationships') for item in bundle['evidence'])
    live,*_=_bundle(e['db'],inv,manifest,{group['instance_id']:{'allowed_sources':['monitoring']}})
    denied,*_=_bundle(e['db'],inv,manifest,{group['instance_id']:{'allowed_sources':[]}})
    assert live['evidence'] and denied['evidence']==[]
    frozen=[(i.id,i.payload_sha256) for i in e['db'].scalars(select(m.ManifestItem).where(m.ManifestItem.manifest_id==manifest.id))]
    save_group(e['actor'],e['config'],dict(e['body'],name='Changed after freeze',relations=[],config_revision=group['config_revision']),group['id'])
    after=issue_detail(e['actor'],event.id)
    assert after['charts']==detail['charts'] and after['edges']==detail['edges']
    assert [(i.id,i.payload_sha256) for i in e['db'].scalars(select(m.ManifestItem).where(m.ManifestItem.manifest_id==manifest.id))]==frozen


def test_unknown_digest_epoch_literal_is_not_verification():
    from platform_app.monitoring import digest_window
    end=utcnow(); rows=[(end-timedelta(minutes=1),json.dumps({'counter_epoch':'unknown','rows':[{'DIGEST':'a'*64,'COUNT_STAR':1,'SUM_TIMER_WAIT':1}]})),(end,json.dumps({'counter_epoch':'unknown','rows':[{'DIGEST':'a'*64,'COUNT_STAR':2,'SUM_TIMER_WAIT':2}]}))]
    result=digest_window(rows,{'snapshot_scope':'server_lifetime','field_units':{'SUM_TIMER_WAIT':'ps'}})
    assert result['valid'] is False and result['reason']=='counter_epoch_unverified'


def test_raw_text_requires_sensitive_permission(group_env):
    from platform_app.analysis_groups import save_group,item_history
    e=group_env; group=save_group(e['actor'],e['config'],e['body'])
    role=e['db'].scalar(select(m.Role).where(m.Role.name=='ai-test')); role.permissions=[p for p in role.permissions if p!='evidence.read_sensitive']; e['db'].commit()
    end=utcnow()
    with pytest.raises(HTTPException): item_history(e['actor'],group['id'],'13',end-timedelta(minutes=5),end,100,e['config'])


def test_collection_retains_latest_dense_samples(group_env):
    from platform_app.analysis_groups import save_group
    from platform_app.monitoring import handle_collection
    from platform_app.scheduling import enqueue,claim
    e=group_env; e['fake'].items[0]['delay']='1s'
    group=save_group(e['actor'],e['config'],dict(e['body'],items=[{'hostid':'101','itemid':'11'}],relations=[]))
    now=int(utcnow().replace(tzinfo=timezone.utc).timestamp())-1; original=e['fake'].call
    def dense(method,params):
        if method!='history.get': return original(method,params)
        values=[{'clock':str(now-599+i),'value':'999' if i==599 else '1','ns':'0'} for i in range(600)]
        if params.get('sortorder')=='DESC': values.reverse()
        return values[:params['limit']]
    e['fake'].call=dense
    enqueue(e['db'],'collect_instance',instance_id=group['instance_id'],payload={'config_revision':group['config_revision'],'collectors':['monitoring']}); e['db'].commit()
    lease=claim(e['factory'],'dense-test','collection'); handle_collection(e['factory'],lease,e['config']); e['db'].expire_all()
    row=e['db'].scalar(select(m.Evidence).where(m.Evidence.instance_id==group['instance_id']))
    assert row.payload['rows'][-1]['value']==999
    assert row.coverage['availability']=='available' and row.truncated


def test_sparse_heartbeat_history_is_explicit_not_false_one_minute_gaps(group_env):
    from platform_app.analysis_groups import save_group
    from platform_app.monitoring import metric_charts
    e=group_env; e['fake'].items[0]['preprocessing']=[{'type':'20','params':'1h'}]
    group=save_group(e['actor'],e['config'],dict(e['body'],items=[{'hostid':'101','itemid':'11'}],relations=[]))
    item=group['items'][0]
    assert item['heartbeat_seconds']==3600 and item['poll_interval']==60 and item['interval']==3600
    end=utcnow(); chart=metric_charts(e['db'],e['db'].get(m.Instance,group['instance_id']),end-timedelta(minutes=15),end,e['config'])[0]
    assert chart['coverage']['complete'] is False
    assert 'sparse_history_no_value_change_or_missing' in chart['coverage']['reason_codes']
    assert not any(g.get('reason')=='missing_samples' for g in chart['gaps'])


def test_old_group_lease_rejected_before_any_upstream_read(group_env):
    from platform_app.analysis_groups import save_group
    from platform_app.monitoring import handle_collection
    from platform_app.scheduling import enqueue,claim
    e=group_env; group=save_group(e['actor'],e['config'],e['body'])
    enqueue(e['db'],'collect_instance',instance_id=group['instance_id'],payload={'config_revision':group['config_revision'],'collectors':['monitoring']}); e['db'].commit()
    lease=claim(e['factory'],'old-group','collection')
    save_group(e['actor'],e['config'],dict(e['body'],config_revision=group['config_revision'],enabled=False),group['id']); e['db'].commit()
    e['fake'].calls.clear()
    with pytest.raises(HTTPException): handle_collection(e['factory'],lease,e['config'])
    assert e['fake'].calls==[]


def test_group_list_scope_and_fresh_metadata(group_env):
    from platform_app.analysis_groups import save_group,list_groups
    e=group_env; group=save_group(e['actor'],e['config'],e['body']); e['fake'].items[0]['lastclock']='9876'
    assert list_groups(e['actor'],e['config'],system_id='other')['total']==0
    assert list_groups(e['actor'],e['config'],system_id=e['system'].id)['items'][0]['items'][0]['lastclock']=='9876'


def test_missing_item_remains_visible_without_hiding_other_curves(group_env):
    from platform_app.analysis_groups import save_group,read_group
    from platform_app.monitoring import metric_charts
    e=group_env; group=save_group(e['actor'],e['config'],e['body']); e['fake'].items=[i for i in e['fake'].items if i['itemid']!='12']
    detail=read_group(e['actor'],group['id'],e['config'])
    assert detail['item_count']==3 and next(i for i in detail['items'] if i['itemid']=='12')['available'] is False
    end=utcnow(); charts=metric_charts(e['db'],e['db'].get(m.Instance,group['instance_id']),end-timedelta(minutes=5),end,e['config'])
    assert len(charts)==2 and any(any(p['value']==17 for p in c['points']) for c in charts)
    assert next(c for c in charts if c['item']['itemid']=='12')['coverage']['complete'] is False


def test_source_scope_update_fences_bound_group_tasks(group_env):
    from platform_app.analysis_groups import save_group
    from platform_app.monitoring import save_source
    e=group_env; group=save_group(e['actor'],e['config'],e['body'])
    save_source(e['actor'],e['config'],{'allowed_host_group_ids':['22']},e['source'].id)
    instance=e['db'].get(m.Instance,group['instance_id']); assert instance.config_revision==group['config_revision']+1
    schedule=e['db'].scalar(select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id==instance.id))
    assert schedule.config_revision==instance.config_revision


def test_optional_digest_snapshot_preserves_units_without_inventing_epoch(group_env):
    from platform_app.analysis_groups import save_group
    from platform_app.monitoring import handle_collection
    from platform_app.scheduling import enqueue,claim
    from platform_app.tools import project_evidence
    e=group_env
    row=e['fake'].items[2]; row['key_']='dbops.mysql.statement_digest_snapshot'; row['tags']=[{'tag':'sl.field_unit.SUM_TIMER_WAIT','value':'ps'},{'tag':'sl.snapshot_scope','value':'counter_epoch_unverified'}]
    payload={'rows':[{'DIGEST':'a'*64,'SCHEMA_NAME':'private_schema','DIGEST_TEXT':'SELECT * FROM private_table WHERE token=secret','COUNT_STAR':100,'SUM_TIMER_WAIT':10000000000000,'MAX_TIMER_WAIT':5000000000000,'extra':'x'*300} for _ in range(20)],'cumulative_counters':True,'counter_epoch':None,'scope':'since_server_start_or_last_digest_reset','timer_unit':'picoseconds'}
    original=e['fake'].call
    def snapshots(method,params):
        if method=='history.get': return [{'clock':str(int(utcnow().replace(tzinfo=timezone.utc).timestamp())-60),'value':json.dumps(payload),'ns':'0'}]
        return original(method,params)
    e['fake'].call=snapshots
    group=save_group(e['actor'],e['config'],dict(e['body'],items=[{'hostid':'102','itemid':'13'}],relations=[]))
    enqueue(e['db'],'collect_instance',instance_id=group['instance_id'],payload={'config_revision':group['config_revision'],'collectors':['monitoring']}); e['db'].commit()
    lease=claim(e['factory'],'snapshot-group','collection'); handle_collection(e['factory'],lease,e['config']); e['db'].expire_all()
    evidence=e['db'].scalar(select(m.Evidence).where(m.Evidence.instance_id==group['instance_id'],m.Evidence.source=='mysql.statement_digest'))
    assert evidence is not None and evidence.sensitivity=='restricted'
    result=project_evidence(evidence)
    assert result['data']['timer_unit']=='ps' and result['data']['window']['valid'] is False
    assert result['data']['item']['itemid']=='13'
    assert 'private_table' not in json.dumps(result) and 'token=secret' not in json.dumps(result)


@pytest.mark.parametrize('missing_last,active_lock',[(False,False),(True,False),(False,True)])
@pytest.mark.parametrize('total_budget_pressure',[False,True])
def test_real_model_payload_keeps_large_group_numeric_context_and_frozen_tool(group_env,missing_last,active_lock,total_budget_pressure):
    from platform_app.analysis_groups import save_group
    from platform_app.monitoring import handle_collection
    from platform_app.scheduling import enqueue,claim
    from platform_app.investigations import create_investigation,run_investigation
    from tests.platform.test_ai_failures import provider_response
    e=group_env; base=e['fake'].items[0]
    e['fake'].items=[dict(base,itemid=str(100+i),hostid='101' if i<9 else '102',name='Measured application statistic '+str(i)+' padded description',key_='application.custom_metric_'+str(i)+'_retained_sample_count_long_key',value_type=0,description='password DO-NOT-SEND') for i in range(18)]
    e['fake'].items.extend(dict(base,itemid=str(118+i),hostid='102',value_type=4,key_='application.text_'+str(i)) for i in range(4))
    e['fake'].items[-1]['key_']='dbops.mysql.lock_snapshot'
    e['fake'].items[-2]['key_']='dbops.mysql.statement_digest_snapshot'
    original=e['fake'].call
    def with_snapshots(method,params):
        if missing_last and method=='history.get' and params['itemids'][0]=='117': return []
        if method=='history.get' and params['itemids'][0] in ('120','121'):
            lock_rows=[{'waiting_trx_id':12,'blocking_trx_id':13,'waiting_thread_id':14,'blocking_thread_id':15}] if active_lock and params['itemids'][0]=='121' else []
            return [{'clock':str(int(utcnow().replace(tzinfo=timezone.utc).timestamp())-60),'value':json.dumps({'rows':lock_rows,'counter_epoch':None}),'ns':'0'}]
        return original(method,params)
    e['fake'].call=with_snapshots
    group=save_group(e['actor'],e['config'],dict(e['body'],items=[{'hostid':i['hostid'],'itemid':i['itemid']} for i in e['fake'].items]))
    instance=e['db'].get(m.Instance,group['instance_id']); instance.model_policy='live_redacted'
    enqueue(e['db'],'collect_instance',instance_id=instance.id,payload={'config_revision':instance.config_revision,'collectors':['monitoring']}); e['db'].commit()
    lease=claim(e['factory'],'large-group-collector','collection'); handle_collection(e['factory'],lease,e['config']); e['db'].expire_all()
    end=utcnow(); inv,_=create_investigation(e['actor'],[instance.id],end-timedelta(minutes=15),end,'分析联合对象',mode='live_redacted',options={'external_confirmed':True,'max_tool_calls':2}); e['db'].commit()
    lease=claim(e['factory'],'large-group-ai','ai')
    config=SimpleNamespace(**vars(e['config'])); config.private_dir=e['tmp_path']; config.model={'provider':'test','model':'test','max_calls':2,'max_tokens':1000}
    e['factory'].configure(info={'platform_config':config})
    captured=[]; reference=[]
    def provider(config,payload,timeout):
        captured.append(json.loads(json.dumps(payload)))
        assert len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())+512<=24000
        assert 'DO-NOT-SEND' not in json.dumps(payload)
        if len(captured)==1:
            initial=json.loads(payload['messages'][1]['content']); series=initial['numeric_series']; columns=series['columns']; rows=series['rows']
            assert len(rows)==18
            assert len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())+512>12000
            assert {row[columns.index('hostid')] for row in rows}=={'101','102'}
            assert {row[columns.index('itemid')] for row in rows}.issubset({str(100+i) for i in range(18)})
            assert all(row[columns.index('last')]==(None if missing_last and row[columns.index('itemid')]=='117' else 17) for row in rows)
            assert all(series['last_time_dictionary'][row[columns.index('last_time_ref')]]
                       for row in rows if row[columns.index('sample_count')])
            assert series['omitted_count']==18-len(rows)
            if active_lock:
                locks=[item for item in initial['evidence'] if item.get('selection_reason')=='nonempty_lock_snapshot']
                assert locks and any(row.get('blocking_trx_id')==13 for item in locks for row in item['data'].get('rows',[]))
            else: assert len(rows)==18 and series['omitted_count']==0
            assert any(r['kind']=='depends_on' and r['provenance']=='user_registered' for r in series['registered_relationships'])
            reference.append(next(row[columns.index('evidence_id')] for row in reversed(rows) if row[columns.index('sample_count')]))
            return {'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'id':'frozen1','type':'function','function':{'name':'get_metric_window','arguments':json.dumps({'instance_id':instance.id,'manifest_item_ids':[reference[0].removeprefix('manifest:')]})}}]}}], 'usage':{'prompt_tokens':12000 if total_budget_pressure else 100,'completion_tokens':200 if total_budget_pressure else 100}}
        results=[json.loads(msg['content']) for msg in payload['messages'] if msg['role']=='tool']
        if total_budget_pressure:
            assert 12200+len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())+512+payload['max_tokens']<=32000
        assert results and results[0]['items']
        assert any(item['evidence_id']==reference[0] and (item.get('points') or item.get('summary')) for item in results[0]['items'])
        return provider_response(reference[0])
    result=run_investigation(e['factory'],lease,config,model_client=provider)
    assert result.status=='completed' and len(captured)==2, (result.report,len(captured),[(len(json.dumps(p,ensure_ascii=False,separators=(',',':')).encode())+512,len(json.loads(p['messages'][1]['content']).get('numeric_series',{}).get('rows',[])),len(p.get('tools',[]))) for p in captured])


@pytest.mark.parametrize('count',[32,33])
def test_group_item_budget_includes_unknown_keys(group_env,count):
    from platform_app.analysis_groups import save_group
    e=group_env; base=e['fake'].items[0]
    e['fake'].items=[dict(base,itemid=str(100+i),key_='unknown.'+str(i)) for i in range(count)]
    body=dict(e['body'],items=[{'hostid':'101','itemid':str(100+i)} for i in range(count)],relations=[])
    if count==33:
        with pytest.raises(HTTPException): save_group(e['actor'],e['config'],body)
    else:
        assert save_group(e['actor'],e['config'],body)['item_count']==32


def test_model_compaction_does_not_merge_source_local_item_ids():
    from platform_app.model_context import compact_initial
    bundle={'coverage':{},'numeric_series':{'rows':[['manifest:a',0,'101','11']],'resources':[{'instance_id':'group-a'}]},'evidence':[{'evidence_id':'b','instance_id':'group-b','source':'zabbix.item.numeric:b','data':{'item':{'hostid':'101','itemid':'11'}}}]}
    assert compact_initial(bundle)['evidence'][0]['evidence_id']=='b'


@pytest.mark.parametrize('body',[{}, {'collectors':['monitoring']}])
def test_manual_collect_route_queues_bound_group_profile_with_revision(group_env,body):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from platform_app.identity import current_actor
    from platform_app.routes.inventory import router
    from platform_app.analysis_groups import save_group
    e=group_env; group=save_group(e['actor'],e['config'],e['body'])
    role=e['db'].scalar(select(m.Role).where(m.Role.name=='ai-test')); role.permissions=[*role.permissions,'collection.trigger']; e['db'].commit()
    app=FastAPI(); app.state.config=e['config']; app.include_router(router,prefix='/api/v1'); app.dependency_overrides[current_actor]=lambda:e['actor']
    with TestClient(app) as client: result=client.post('/api/v1/instances/'+group['instance_id']+'/collect',json=body)
    assert result.status_code==202, result.json()
    task=e['db'].get(m.Task,result.json()['id'])
    assert task.payload=={'collectors':['monitoring'],'config_revision':group['config_revision']}
    assert task.actor_id==e['actor'].user_id


def test_monitoring_profile_is_not_available_to_unbound_direct_assets(group_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from platform_app.identity import current_actor
    from platform_app.routes.inventory import router
    e=group_env; e['a'].enabled=True; e['a'].lifecycle_state='Active'
    role=e['db'].scalar(select(m.Role).where(m.Role.name=='ai-test')); role.permissions=[*role.permissions,'collection.trigger']; e['db'].commit()
    app=FastAPI(); app.state.config=e['config']; app.include_router(router,prefix='/api/v1'); app.dependency_overrides[current_actor]=lambda:e['actor']
    with TestClient(app) as client:
        assert client.post('/api/v1/instances/'+e['a'].id+'/collect',json={'collectors':['monitoring']}).status_code==422
        e['a'].connection_mode='zabbix'; e['db'].commit()
        assert client.post('/api/v1/instances/'+e['a'].id+'/collect',json={'collectors':['monitoring']}).status_code==409


@pytest.mark.parametrize('source',['mysql.lock_waits','mysql.transactions','mysql.sessions'])
def test_mysql_identity_projection_keeps_uint64_decimal_exact(source):
    from platform_app.tools import project_evidence
    values={'waiting_trx_id':'18446744073709551615','blocking_trx_id':9007199254740993,'waiting_thread_id':'2364','blocking_thread_id':2365,'TRX_ID':'9007199254740995','TRX_MYSQL_THREAD_ID':'18446744073709551614','THREAD_ID':'9007199254740997','PROCESSLIST_ID':2366}
    evidence=m.Evidence(id='e',instance_id='i',source=source,source_mode='live',observed_at=utcnow(),coverage={'availability':'available','complete':True},truncated=False,payload={'rows':[dict(values,TRX_ROWS_LOCKED=7)]})
    row=project_evidence(evidence)['data']['rows'][0]
    for key,value in values.items():
        expected=int(value) if int(value)<=9007199254740991 else str(value)
        assert row[key]==expected and type(row[key]) is type(expected)
    assert row['TRX_ROWS_LOCKED']==7
    encoded=json.dumps(row)
    assert '18446744073709551615' in encoded and '9007199254740993' in encoded


@pytest.mark.parametrize('value',[True,False,-1,'1.5','9e18','-1',18446744073709551616,float(9007199254740993)])
def test_identity_projection_rejects_invalid_or_already_inexact_ids(value):
    from platform_app.tools import _numeric_fields
    assert 'waiting_trx_id' not in _numeric_fields({'waiting_trx_id':value},{'waiting_trx_id'})
