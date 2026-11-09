from copy import deepcopy
import json
import pytest
from platform_app.model_context import refresh_snapshot_chronology, bounded_tool_result


def snap(ref,time,count,**changes):
    row={'evidence_id':ref,'instance_id':'asset-a','source':'trusted.snapshot','template':'template-a','observed_at':time,'data':{'row_count':count,'item':{'hostid':'h1','itemid':'i1'}}}
    row.update(changes);return row


def test_actual_order_is_not_input_priority_or_timestamp_text_order():
    rows=[snap('active','2026-11-09T05:49:17Z',1),snap('before','2026-11-09T13:09:17+08:00',0),snap('same','2026-11-09T00:49:17-05:00',0)]
    value={'evidence':rows}
    refresh_snapshot_chronology(value,sort=True)
    group=value['snapshot_chronology']['groups'][0]
    assert group['empty_before_first']['count']==1 and group['empty_before_first']['evidence_ids']==['before']
    assert group['empty_after_last']['count']==0 and group['empty_after_last']['evidence_ids']==[]
    assert group['empty_at_nonempty_time']['count']==1
    assert [r['evidence_id'] for r in value['evidence']]==['before','active','same']


@pytest.mark.parametrize('field,value',[('instance_id','asset-b'),('source','different.source'),('template','template-b')])
def test_boundaries_never_mix(field,value):
    value={'evidence':[snap('active','2026-11-09T05:49:00Z',1),snap('later','2026-11-09T06:00:00Z',0,**{field:value})]}
    refresh_snapshot_chronology(value)
    active=next(g for g in value['snapshot_chronology']['groups'] if g['first_nonempty'])
    assert active['empty_after_last']['count']==0


def test_item_boundaries_invalid_times_and_unknown_source_names():
    rows=[snap('active','2026-11-09T05:49:00Z',1),snap('bad','not-a-time',0),snap('naive','2026-11-09T06:00:00',0)]
    other=snap('other-item','2026-11-09T06:00:00Z',0);other['data']['item']['itemid']='i2';rows.append(other)
    noitem=snap('unknown-item','2026-11-09T06:00:00Z',0);noitem['data']['item']={};rows.append(noitem)
    value={'evidence':rows};refresh_snapshot_chronology(value)
    assert value['snapshot_chronology']['invalid_time_count']==2
    active=next(g for g in value['snapshot_chronology']['groups'] if g['first_nonempty'])
    assert active['empty_after_last']['count']==0
    assert len(value['snapshot_chronology']['groups'])==3


def test_recount_after_omission_caps_references_and_groups():
    rows=[snap('active','2026-11-09T05:49:00Z',1)]
    rows += [snap(f'early{i}',f'2026-11-09T05:{i:02}:00Z',0) for i in range(8)]
    rows += [snap(f'group{i}','2026-11-09T06:00:00Z',1,source=f'source{i}') for i in range(8)]
    value={'evidence':rows};refresh_snapshot_chronology(value)
    chronology=value['snapshot_chronology'];assert len(chronology['groups'])==4 and chronology['groups_omitted']==5
    group=next(g for g in chronology['groups'] if g['source']=='trusted.snapshot')
    assert group['empty_before_first']['count']==8
    assert len(group['empty_before_first']['evidence_ids'])==3 and group['empty_before_first']['refs_omitted']==5
    value['evidence']=[rows[0]];refresh_snapshot_chronology(value)
    group=value['snapshot_chronology']['groups'][0]
    assert group['empty_before_first']['count']==0 and group['empty_before_first']['evidence_ids']==[]


def test_tool_budget_counts_chronology_and_never_keeps_omitted_refs():
    rows=[snap('active','2026-11-09T05:49:00Z',1),snap('before','2026-11-09T05:09:00Z',0)]
    result=bounded_tool_result({'items':rows},1600)
    assert len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode())<=1600
    supplied={r['evidence_id'] for r in result['items']}
    def refs(v):
        if isinstance(v,dict):
            for k,x in v.items():
                if k=='evidence_ids': yield from x
                elif k=='evidence_id': yield x
                else: yield from refs(x)
        elif isinstance(v,list):
            for x in v: yield from refs(x)
    assert set(refs(result.get('snapshot_chronology',{})))<=supplied
    assert all('observed_at' in item for item in result['items'])

def test_small_tool_budget_preserves_evidence_before_optional_chronology():
    row=snap('active','2026-11-09T05:49:00Z',1)
    result=bounded_tool_result({'items':[row]},666)
    assert result['items'][0]['evidence_id']=='active'
    assert result['snapshot_chronology_omitted']=='tool_result_budget'
    assert len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode())<=666

def test_missing_or_omitted_payload_does_not_become_empty_snapshot():
    active=snap('active','2026-11-09T05:49:00Z',1)
    omitted=snap('omitted','2026-11-09T06:00:00Z',0);omitted['data']['text_omitted']=True
    missing=snap('missing','2026-11-09T06:01:00Z',0);missing['data']['observation_unavailable']=True
    value={'evidence':[active,omitted,missing]};refresh_snapshot_chronology(value)
    assert value['snapshot_chronology']['groups'][0]['sample_count']==1
    assert value['snapshot_chronology']['groups'][0]['empty_after_last']['count']==0

@pytest.mark.parametrize('snapshot',[False,True])
def test_tool_priority_survives_prepare_then_later_budget_compression(snapshot):
    from platform_app.model_context import prepare_tool_context
    lock_id='manifest:ffffffff-ffff-4fff-8fff-ffffffffffff'
    cpu_id='manifest:00000000-0000-4000-8000-000000000000'
    def numeric(ref,template):
        return {'evidence_id':ref,'instance_id':'asset-a','template':template,'source':'zabbix_history','points':[{'time':'2026-11-09T05:49:00Z','value':1}], 'item':{'hostid':'10687','itemid':'50957' if template=='lock_waits' else '50829','name':'Frozen database host measurement','key':'dbops.retained_measurement'}, 'coverage':{'complete':True}}
    lock=numeric(lock_id,'lock_waits');cpu=numeric(cpu_id,'host_cpu_percent')
    if snapshot:
        lock=snap(lock_id,'2026-11-09T05:49:00Z',1)
        cpu=snap(cpu_id,'2026-11-09T05:09:00Z',0)
        for row in [lock,cpu]: row['data']['rows']=[{'waiting_trx_id':12,'blocking_trx_id':13,'padding_for_fixture':'x'*200}]
    raw={'items':[lock,cpu]}
    direct=bounded_tool_result(raw,1000)
    assert direct['items'][0]['evidence_id']==lock_id
    assert len(direct['items'])==1
    prepared=prepare_tool_context(raw,6000)
    assert [i['evidence_id'] for i in prepared['items']]==[lock_id,cpu_id]
    compressed=bounded_tool_result(prepared,1000)
    assert [i['evidence_id'] for i in compressed['items']]==[i['evidence_id'] for i in direct['items']]


@pytest.mark.parametrize('guard',[
    'REQUESTING_ENGINE_LOCK_ID关联的LOCK_MODE属于请求侧',
    'data_locks/data_lock_waits没有持锁开始时间或持锁时长',
    'PROCESSLIST_ID不能直接当作P_S.THREAD_ID',
])
def test_final_prompt_preserves_role_duration_and_identity_boundaries(guard):
    from platform_app.investigations import SYSTEM_PROMPT
    assert guard in SYSTEM_PROMPT
