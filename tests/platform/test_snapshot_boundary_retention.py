"""Adjacent observed empty samples must survive budget cuts, not prove recovery."""
from copy import deepcopy
from datetime import timedelta
import json
import pytest

from platform_app import model_context as context
from tests.platform.test_ai_scope import ai_env


def snapshot(ref, second, count, *, source_id='zabbix-a', itemid='11'):
    return {'evidence_id':ref,'instance_id':'asset-a','source':'mysql.lock_waits',
        'source_mode':'live','observed_at':f'2026-11-13T00:00:{second:02}Z',
        'coverage':{'availability':'available','complete':False,'gaps':['history_truncated']},
        'data':{'row_count':count,'rows':[{'waiting_trx_id':2}] if count else [],
                'item':{'source_id':source_id,'hostid':'101','itemid':itemid}}}


def samples():
    return [snapshot('far-before',0,0),snapshot('before',9,0),snapshot('first',10,1),
            snapshot('middle',11,1),snapshot('last',12,1),snapshot('after',13,0),
            snapshot('far-after',50,0)]


def test_boundary_refs_are_nearest_real_samples_and_do_not_mutate_input():
    rows=samples();before=deepcopy(rows)
    assert context.snapshot_boundary_refs(rows)=={'before','first','last','after'}
    assert rows==before
    assert context.snapshot_boundary_refs(list(reversed(rows)))=={'before','first','last','after'}


@pytest.mark.parametrize('field,value',[('instance_id','asset-b'),('source','mysql.sessions'),
    ('template','other-template'),('source_id','zabbix-b'),('hostid','102'),('itemid','12')])
def test_independent_identity_never_supplies_a_boundary(field,value):
    rows=[snapshot('first',10,1),snapshot('foreign',11,0)]
    target=rows[1]['data']['item'] if field in ('source_id','hostid','itemid') else rows[1]
    target[field]=value
    assert context.snapshot_boundary_refs(rows)=={'first'}


@pytest.mark.parametrize('change',['bad_time','missing_identity','unavailable','omitted','replay','boolean_count'])
def test_invalid_or_unknown_observations_cannot_be_temporal_anchors(change):
    rows=[snapshot('first',10,1),snapshot('bad',11,0)]
    if change=='bad_time':rows[1]['observed_at']='2026-11-13T00:00:11'
    if change=='missing_identity':rows[1]['data']['item'].pop('source_id')
    if change=='unavailable':rows[1]['data']['observation_unavailable']=True
    if change=='omitted':rows[1]['data']['text_omitted']=True
    if change=='replay':rows[1]['source_mode']='replay'
    if change=='boolean_count':rows[1]['data']['row_count']=False
    assert context.snapshot_boundary_refs(rows)=={'first'}


def test_all_empty_normal_history_has_no_abnormal_anchor_and_simultaneous_empty_is_not_after():
    assert context.snapshot_boundary_refs([snapshot('a',1,0),snapshot('b',2,0)])==set()
    assert context.snapshot_boundary_refs([snapshot('first',10,1),snapshot('same',10,0)])=={'first'}


def test_final_compression_keeps_boundaries_and_fails_closed_if_they_alone_do_not_fit():
    rows=samples()+[snapshot('noise-'+str(n),n,0,itemid='22')for n in range(30)]
    original={'evidence':rows,'coverage':{}}
    measure=lambda value:len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode())
    result=context.compact_final_context(original,max_bytes=4000,measure=measure)
    assert {'before','first','last','after'}<=context.context_refs(result)
    assert measure(result)<=4000
    tiny=context.compact_final_context(original,max_bytes=100,measure=measure)
    assert {'before','first','last','after'}<=context.context_refs(tiny)
    assert measure(tiny)>100


@pytest.mark.usefixtures('legacy_collection_compatibility')
def test_production_request_budget_cannot_drop_adjacent_empty_samples(ai_env):
    from platform_app import investigations as inv,models as m
    from platform_app.evidence import digest
    from tests.platform.test_ai_failures import prepared_run
    from tests.platform.test_model_output_contract import response
    e=ai_env;first=e['evidence'];first.observed_at=e['start']+timedelta(seconds=420)
    meta={'source_id':'zabbix-a','hostid':'101','itemid':'11'}
    first.payload={'rows':[{'waiting_trx_id':2,'blocking_trx_id':1}],'item':meta}
    first.coverage={'availability':'available','complete':True};first.payload_sha256=digest(first.payload)
    added={}
    for index in range(100):
        label='normal-'+str(index);stamp=e['start']+timedelta(seconds=index*5)
        payload={'rows':[],'item':meta}
        row=m.Evidence(instance_id=first.instance_id,run_id=first.run_id,source=first.source,
            source_mode='live',observed_at=stamp,payload=payload,payload_sha256=digest(payload),
            coverage={'availability':'available','complete':True,'source_id':'zabbix-a'})
        e['db'].add(row);added[index]=row
    first.coverage['source_id']='zabbix-a';e['db'].commit()
    _,_,lease,config=prepared_run(e);captured=[]
    def provider(_config,payload,_timeout):
        captured.append(payload);return response(first.id)
    result=inv.run_investigation(e['factory'],lease,config,model_client=provider)
    assert result.status=='completed' and len(captured)==1,result.report
    actual=json.loads(captured[0]['messages'][1]['content']);refs=context.context_refs(actual)
    assert {first.id,added[83].id,added[85].id}<=refs
    assert len(json.dumps(captured[0],ensure_ascii=False,separators=(',',':')).encode())+512<=24000
