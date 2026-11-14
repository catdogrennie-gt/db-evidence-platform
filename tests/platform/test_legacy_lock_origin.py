"""The original lock producer needs prospective proof, never history repair."""
from copy import deepcopy
from datetime import timedelta,timezone
import json
import pytest
from sqlalchemy import select
from platform_app import models as m,zabbix_diagnostics as d
from platform_app.db import utcnow
from tests.platform.test_analysis_groups import group_env
from tests.platform.test_monitoring import monitoring_env
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_report_grounding import prepare_case,ground

CONTRACT='db-evidence-lock-snapshot-v2'

def item():
    return {'key':'dbops.mysql.lock_snapshot','enhancement':'mysql.lock_waits','hostid':'101','itemid':'900',
        'tags':[{'tag':'sl.snapshot_contract','value':CONTRACT}]}

def pack(count=0):
    return {'producer_contract':CONTRACT,'snapshot_complete':True,'snapshot_truncated':False,
        'total_row_count':count,'rows':[{'waiting_trx_id':1,'blocking_trx_id':2} for _ in range(count)]}

@pytest.mark.parametrize('count',[0,1,50])
def test_legacy_producer_is_exact_and_does_not_copy_upstream_origin(count):
    payload=pack(count);payload['diagnostic_origin']={'collection':{'source_id':'forged'}}
    producer=d.legacy_lock_producer(item(),payload)
    assert producer=={'contract':CONTRACT,'query_name':'locks','scope':'current_snapshot',
        'returned_row_count':count,'snapshot_row_count':count,'snapshot_complete':True,'snapshot_truncated':False}

@pytest.mark.parametrize('change',['key','enhancement','tags_missing','tags_conflict','producer','complete_type','incomplete','truncated_type','truncated','count_type','count_mismatch','too_many','rows_type','row_type'])
def test_legacy_missing_or_invalid_proof_cannot_become_a_capability(change):
    meta=item();payload=pack(1)
    if change=='key':meta['key']='arbitrary.lock_query'
    if change=='enhancement':meta['enhancement']='mysql.sessions'
    if change=='tags_missing':meta['tags']=[]
    if change=='tags_conflict':meta['tags'].append({'tag':'sl.snapshot_contract','value':'unknown'})
    if change=='producer':payload['producer_contract']='unknown'
    if change=='complete_type':payload['snapshot_complete']=1
    if change=='incomplete':payload['snapshot_complete']=False
    if change=='truncated_type':payload['snapshot_truncated']=0
    if change=='truncated':payload['snapshot_truncated']=True
    if change=='count_type':payload['total_row_count']=True
    if change=='count_mismatch':payload['total_row_count']=0
    if change=='too_many':payload=pack(51)
    if change=='rows_type':payload['rows']='unknown'
    if change=='row_type':payload['rows']=[None]
    assert d.legacy_lock_producer(meta,payload) is None

@pytest.mark.parametrize('count',[0,1])
def test_real_legacy_collection_freezes_actual_provenance(group_env,count):
    from platform_app.analysis_groups import save_group
    from platform_app.monitoring import handle_collection
    from platform_app.scheduling import enqueue,claim
    from platform_app.evidence import digest
    from platform_app.common import iso
    e=group_env;meta=e['fake'].items[2]
    meta.update(key_='dbops.mysql.lock_snapshot',tags=item()['tags'])
    payload=pack(count);payload['diagnostic_origin']={'collection':{'source_id':'forged'}}
    original=e['fake'].call
    def history(method,params):
        if method=='history.get':return [{'clock':str(int(utcnow().replace(tzinfo=timezone.utc).timestamp())-1),'value':json.dumps(payload),'ns':'0'}]
        return original(method,params)
    e['fake'].call=history
    group=save_group(e['actor'],e['config'],dict(e['body'],items=[{'hostid':'102','itemid':'13'}],relations=[]))
    enqueue(e['db'],'collect_instance',instance_id=group['instance_id'],payload={'config_revision':group['config_revision'],'collectors':['monitoring']});e['db'].commit()
    lease=claim(e['factory'],'legacy-collector','collection');handle_collection(e['factory'],lease,e['config']);e['db'].expire_all()
    row=e['db'].scalar(select(m.Evidence).where(m.Evidence.instance_id==group['instance_id'],m.Evidence.source=='mysql.lock_waits'))
    assert row is not None
    origin=row.payload.get('diagnostic_origin')
    assert origin is not None,'legacy producer proof was discarded before hashing'
    assert origin['producer']['contract']==CONTRACT
    assert origin['collection']=={'source_id':e['source'].id,'hostid':'102','itemid':'13','mapping_version':group['version'],'config_revision':group['config_revision'],'transport':'zabbix'}
    assert origin['observation']=={'instance_id':group['instance_id'],'source':'mysql.lock_waits','source_mode':'live','observed_at':iso(row.observed_at)}
    assert row.payload_sha256==digest(row.payload)
    changed=deepcopy(row.payload);changed['diagnostic_origin']['producer']['snapshot_complete']=False
    assert digest(changed)!=row.payload_sha256

def use_legacy(row):
    row.payload['item']=item()
    row.payload['diagnostic_origin']['producer']['contract']=CONTRACT

@pytest.mark.parametrize('count',[0,1,50])
def test_delivered_complete_legacy_point_can_be_admitted_without_verifying_model_prose(ai_env,count):
    e=prepare_case(ai_env,count=count,alter=use_legacy);before=deepcopy(e['result'].report)
    result=ground(e)
    assert result['status']=='point_facts_only'
    assert result['admitted_facts'][0]['row_count']==count
    assert result['admitted_facts'][0]['count_scope']=='exact_snapshot'
    assert result['model_narrative_verification']=='unverified' and e['result'].report==before

@pytest.mark.parametrize('change',['missing_origin','truncated','wrong_contract','unknown_key','conflicting_tag'])
def test_legacy_does_not_borrow_strict_protocol_capabilities_or_backfill_history(ai_env,change):
    def alter(row):
        use_legacy(row)
        if change=='missing_origin':row.payload.pop('diagnostic_origin')
        if change=='wrong_contract':row.payload['diagnostic_origin']['producer']['contract']=d.CONTRACT
        if change=='unknown_key':row.payload['item']['key']='unknown'
        if change=='conflicting_tag':row.payload['item']['tags'].append({'tag':'sl.snapshot_contract','value':'unknown'})
    e=prepare_case(ai_env,count=50 if change=='truncated'else 1,truncated=change=='truncated',alter=alter)
    result=ground(e)
    assert result['status']=='unverified' and result['admitted_facts']==[]
