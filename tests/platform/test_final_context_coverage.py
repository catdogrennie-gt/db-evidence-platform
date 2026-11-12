"""Final-round packing keeps actual evidence across independent dimensions."""
import copy
from datetime import timedelta
import json

import pytest

from platform_app import model_context as context
from platform_app.common import iso, parse_time
from tests.platform.test_context_dimension_coverage import measured_item, numeric_summary_for, projected_numeric


def encoded_size(value):
    return len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()) + 9500


def measured_context():
    series=context.new_series(); evidence=[]; signals=[]
    for index in range(26):
        item=projected_numeric();item['evidence_id']='observation-'+str(index)
        item['instance_id']='resource-'+str(index%2);item['data']['item']=measured_item(index)
        identity=numeric_summary_for(item,100)
        resource=identity['resources'][0]
        if resource not in series['resources']:series['resources'].append(resource)
        row=identity['rows'][0]
        row[0]='manifest:dimension-'+str(index);row[1]=series['resources'].index(resource)
        fields=dict(zip(series['columns'],row));fields.update(name='Measured dimension '+str(index),
            key='measured.dimension_'+str(index),unit='ms',first_time=item['data']['rows'][0]['time'],
            last_time=item['data']['rows'][-1]['time'],first=0,last=3,min=0,max=11,
            original_point_count=100,sampling_method='none',value_semantics='gauge',
            semantics_source='zabbix_tag',complete=True,missing_count=0,reason_codes=[])
        series['rows'].append([fields.get(k) for k in series['columns']])
        if index<6:
            evidence.append(item)
            signals.append({'instance_id':item['instance_id'],'evidence_ids':[item['evidence_id']],
                'kind':'current','category':'baseline_change','calculation':{
                    'observed_at':item['observed_at'],'episode_started_at':item['observed_at'],
                    'baseline':{'from':item['data']['rows'][0]['time'],'to':item['data']['rows'][10]['time'],
                                'median':1,'sample_count':11},
                    'recent':{'from':item['data']['rows'][-3]['time'],'to':item['data']['rows'][-1]['time'],
                              'median':3,'min':1,'max':3,'sample_count':3},
                    'diagnosis':'change_only_not_root_cause'}})
    bundle={'instance_ids':['resource-0','resource-1'],'from':'2026-11-12T08:00:00Z',
        'to':'2026-11-12T09:00:00Z','evidence':evidence,'numeric_series':series,
        'inspection_signals':signals,'coverage':{'complete':False,'gaps':['known_source_gap']}}
    packed=context.compact_initial(bundle)
    for _ in range(6):context.trim_numeric_summary(packed)
    return packed


def run_pack(initial,budget,protected=()):
    return context.compact_final_context(initial,max_bytes=budget,measure=encoded_size,
                                         protected_evidence_ids=protected)


def decoded_observations(bundle):
    columns=bundle['numeric_observation_columns']; dictionaries=bundle.get('numeric_observation_dictionaries',{})
    times=bundle['numeric_time_dictionary']; result={}
    for item in bundle['evidence']:
        if 'numeric_observation' not in item:continue
        row={}
        for key,value in zip(columns,item['numeric_observation']):
            base=key[:-4] if key.endswith('_ref') else key
            if key.endswith('_ref') and base in dictionaries:value=dictionaries[base][value];key=base
            elif key.endswith('_ref') and isinstance(value,int):value=times[value];key=base
            row[key]=value
        result[item['evidence_id']]=row
    return result


def test_final_pack_keeps_every_trigger_and_normal_dimension_before_display_detail():
    initial=measured_context(); before=copy.deepcopy(initial)
    budget=16500
    packed=run_pack(initial,budget)
    assert encoded_size(packed)<=budget
    assert context.context_refs(packed)==context.context_refs(initial)
    assert len(packed['inspection_signals'])==6
    assert initial==before
    assert run_pack(packed,budget)==packed
    decoded=decoded_observations(packed)
    assert len(decoded)==6
    originals=decoded_observations(initial)
    for ref,row in decoded.items():
        assert row['data.sample_count']==row['data.row_count']==100
        assert row['data.first_time']==originals[ref]['data.first_time']
        assert row['data.last_time']==originals[ref]['data.last_time']
        assert row['data.item.units']=='ms'
        assert row['data.item.value_semantics']=='gauge'
        assert row['data.item.semantics_source']=='zabbix_tag'
        assert row['coverage.gaps']==['history_truncated']
        assert row['data.rows_omitted']+len(row['data.rows'])==100
        for point in row['data.rows']:
            stamp=packed['numeric_time_dictionary'][point['time_ref']]
            assert parse_time(initial['from'])<=parse_time(stamp)<parse_time(initial['to'])


def test_tighter_final_pack_protects_signals_and_balances_remaining_normal_resources():
    initial=measured_context(); packed=run_pack(initial,15000)
    assert encoded_size(packed)<=15000
    assert {r['evidence_ids'][0] for r in initial['inspection_signals']}.issubset(context.context_refs(packed))
    assert len(packed['inspection_signals'])==6
    series=packed['numeric_series']; assert len(series['rows'])>=2
    assert {r[1] for r in series['rows']}=={0,1}
    assert packed['coverage']['gaps']==initial['coverage']['gaps']
    assert packed['coverage']['retained_evidence_count']==len(packed['evidence'])
    assert run_pack(packed,15000)==packed
    for position,name in enumerate(series['columns']):
        if name.endswith('_ref'):
            dictionary=series[name[:-4]+'_dictionary']
            assert all(0<=row[position]<len(dictionary) for row in series['rows'])
            assert {row[position] for row in series['rows']}==set(range(len(dictionary)))


@pytest.mark.parametrize('budget',[0,-1,100])
def test_protected_context_cannot_fit_stays_over_budget_without_pruning_or_raising(budget):
    initial=measured_context(); packed=run_pack(initial,budget)
    assert len(packed['inspection_signals'])==6
    assert {s['evidence_ids'][0] for s in initial['inspection_signals']}.issubset(context.context_refs(packed))
    assert encoded_size(packed)>budget
    assert run_pack(packed,budget)==packed


def test_final_pack_keeps_nonnumeric_families_and_actual_chronology_anchors():
    initial=measured_context()
    first={'evidence_id':'actual-lock','instance_id':'resource-0','source':'mysql.lock_waits',
        'observed_at':'2026-11-12T08:01:00Z','coverage':{'availability':'available'},
        'selection_reason':'nonempty_lock_snapshot',
        'data':{'row_count':1,'rows':[{'waiting_trx_id':7,'blocking_trx_id':8}]}}
    empty=copy.deepcopy(first);empty.update(evidence_id='later-empty',observed_at='2026-11-12T08:03:00Z')
    empty.pop('selection_reason');empty['data']={'row_count':0,'rows':[]}
    sql=copy.deepcopy(first);sql.update(evidence_id='actual-digest',source='mysql.statement_digest')
    sql.pop('selection_reason');sql['data']={'row_count':1,'rows':[{'digest':'a'*64,'delta_count':10}]}
    log=copy.deepcopy(first);log.update(evidence_id='actual-log',source='mysql.error_log')
    log.pop('selection_reason');log['data']={'event_count':1,'severity_counts':{'warning':1}}
    initial['evidence'].extend([first,empty,sql,log])
    packed=run_pack(initial,17000)
    assert encoded_size(packed)<=17000
    assert {'actual-lock','later-empty','actual-digest','actual-log'}.issubset(context.context_refs(packed))
    context.refresh_snapshot_chronology(packed)
    locks=next(g for g in packed['snapshot_chronology']['groups'] if g['source']=='mysql.lock_waits')
    assert locks['first_nonempty']['evidence_id']=='actual-lock'
    assert locks['empty_after_last']['evidence_ids']==['later-empty']


def test_final_pack_keeps_explicit_required_projection_even_without_signal():
    initial=measured_context(); ref=initial['numeric_series']['rows'][-1][0]
    packed=run_pack(initial,15000,[ref])
    assert ref in context.context_refs(packed)


def test_final_pack_preserves_distinct_required_source_identities_without_merging():
    initial=measured_context();series=initial['numeric_series'];first=copy.deepcopy(series['rows'][0])
    other=copy.deepcopy(first);other[0]='manifest:other-source'
    resource=copy.deepcopy(series['resources'][first[1]]);resource['source_id']='independent-source'
    series['resources'].append(resource);other[1]=len(series['resources'])-1
    series['rows'].append(other)
    packed=run_pack(initial,15500,[first[0],other[0]])
    assert {first[0],other[0]}.issubset(context.context_refs(packed))
    rows={row[0]:row for row in packed['numeric_series']['rows']}
    resources=packed['numeric_series']['resources']
    assert resources[rows[first[0]][1]]['source_id']!=resources[rows[other[0]][1]]['source_id']


def test_invalid_snapshot_time_cannot_displace_actual_latest_empty_anchor():
    def snapshot(ref,stamp,count):
        return {'evidence_id':ref,'instance_id':'resource-0','source':'mysql.lock_waits',
            'observed_at':stamp,'data':{'row_count':count,'rows':[{'blocking_trx_id':2}] if count else []}}
    initial={'evidence':[snapshot('nonempty','2026-11-12T08:01:00Z',1),
        snapshot('later-empty','2026-11-12T08:03:00Z',0),snapshot('unknown-time','unparseable',0)],
        'inspection_signals':[],'coverage':{}}
    packed=run_pack(initial,0)
    assert {'nonempty','later-empty'}.issubset(context.context_refs(packed))
    group=packed['snapshot_chronology']['groups'][0]
    assert group['empty_after_last']['evidence_ids']==['later-empty']


def test_normal_coverage_balances_verified_semantics_without_metric_name_rules():
    initial=measured_context();series=initial['numeric_series'];columns=series['columns']
    index=columns.index('value_semantics')
    for number,row in enumerate(series['rows']):row[index]=('gauge','rate','counter','unknown')[number%4]
    packed=run_pack(initial,15500)
    rows=packed['numeric_series']['rows'];cols=packed['numeric_series']['columns']
    assert {r[cols.index('value_semantics')] for r in rows}=={'gauge','rate','counter','unknown'}
    assert {r[1] for r in rows}=={0,1}
