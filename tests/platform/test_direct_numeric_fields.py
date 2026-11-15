"""Direct fields reduce decoding ambiguity; not proof of model diagnosis."""
from copy import deepcopy
import json

from platform_app import model_context as context
from tests.platform.test_numeric_latest_sample import measured_bundle


def test_latest_sample_has_named_fields_in_first_request():
    original=measured_bundle(); before=deepcopy(original)
    result=context.compact_initial(original);series=result['numeric_series']
    point=series['rows'][0][series['columns'].index('latest_sample')]
    assert point=={'value':2300,'observed_at_ref':0}
    assert series['latest_sample_time_dictionary'][point['observed_at_ref']]=='2026-11-15T06:06:31Z'
    assert original==before


def test_raw_numeric_itemid_is_beside_reference_and_round_trips():
    item={'evidence_id':'observed-request','instance_id':'scope-a',
        'source':'zabbix.item.numeric:measured','data':{'item':{'hostid':'101','itemid':'501'},
        'rows':[{'value':2300,'time':'2026-11-15T06:06:31Z'}]}}
    initial=context.compact_initial({'coverage':{},'evidence':[item]})
    encoded=initial['evidence'][0]
    assert encoded['itemid']=='501'
    assert 'data.item.itemid' not in initial['numeric_observation_columns']
    decoded=context._unpack_final_tables(initial)['evidence'][0]
    assert decoded['data']['item']==item['data']['item']
    assert decoded['evidence_id']==item['evidence_id'] and decoded['instance_id']=='scope-a'
    assert initial['evidence'][0]['itemid']=='501'


def test_legacy_pair_is_normalized_without_changing_latest_value_or_clock():
    initial=context.compact_initial(measured_bundle());series=initial['numeric_series']
    pos=series['columns'].index('latest_sample')
    for row in series['rows']:
        point=row[pos]
        if isinstance(point,dict):row[pos]=[point['value'],point['observed_at_ref']]
    series['latest_sample_columns']=['value','observed_at_ref']
    normalized=context.compact_initial(initial)
    assert isinstance(normalized['numeric_series']['rows'][0][pos],dict)
    assert normalized['numeric_series']['rows'][0][pos]['value']==2300
    assert context.compact_initial(normalized)==normalized


def test_budget_trim_shortens_display_names_before_dropping_a_measured_dimension():
    bundle=measured_bundle();bundle['numeric_series']['rows'][0][context.SERIES_COLUMNS.index('name')]='A long human display label for a measured value with redundant presentation-only description'
    result=context.compact_initial(bundle);before=deepcopy(result)
    assert context.trim_numeric_summary(result)
    assert len(result['numeric_series']['rows'])==len(before['numeric_series']['rows'])
    assert result['numeric_series']['display_names_truncated'] is True
    assert len(json.dumps(result,ensure_ascii=False))<len(json.dumps(before,ensure_ascii=False))
    cols=result['numeric_series']['columns']
    for old,new in zip(before['numeric_series']['rows'],result['numeric_series']['rows']):
        assert all(a==b for name,a,b in zip(cols,old,new) if name!='name')


def test_same_itemid_on_two_instances_keeps_independent_scope_and_reference():
    original=[]
    for scope,value in [('scope-a',2300),('scope-b',1.2)]:
        original.append({'evidence_id':scope+'-observation','instance_id':scope,
            'source':'zabbix.item.numeric:'+scope,'data':{'item':{'hostid':'101','itemid':'501'},
            'rows':[{'value':value,'time':'2026-11-15T06:06:31Z'}]}})
    encoded=context.compact_initial({'coverage':{},'evidence':original})
    assert [item['itemid'] for item in encoded['evidence']]==['501','501']
    decoded=context._unpack_final_tables(encoded)['evidence']
    assert [item['instance_id'] for item in decoded]==['scope-a','scope-b']
    assert [item['evidence_id'] for item in decoded]==['scope-a-observation','scope-b-observation']
    assert [item['data']['rows'][0]['value'] for item in decoded]==[2300,1.2]


def test_missing_itemid_is_not_invented_from_reference_or_name():
    item={'evidence_id':'501','source':'zabbix.item.numeric:measured',
        'data':{'item':{'name':'501'},'rows':[{'value':1,'time':'2026-11-15T06:06:31Z'}]}}
    encoded=context.compact_initial({'coverage':{},'evidence':[item]})
    assert 'itemid' not in encoded['evidence'][0]
    assert 'itemid' not in context._unpack_final_tables(encoded)['evidence'][0]['data']['item']


def test_short_names_at_legacy_budget_keep_all_three_normal_dimensions():
    original=measured_bundle()
    for index,row in enumerate(original['numeric_series']['rows']):
        row[context.SERIES_COLUMNS.index('name')]=chr(65+index)
    encoded=context.compact_initial(original)
    size=lambda value:len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode())
    # Independently measured immutable31 budget for this exact short-name input.
    assert size(encoded)>1329
    assert context.trim_numeric_summary(encoded)
    assert size(encoded)<=1329
    assert len(encoded['numeric_series']['rows'])==3
    assert encoded['numeric_series']['omitted_count']==0
    assert context.context_refs(encoded)=={'manifest:request','manifest:db-call','manifest:later'}


def test_optional_lossless_fallback_never_grows_a_single_observation():
    item={'evidence_id':'measured','source':'zabbix.item.numeric:measured',
        'data':{'item':{'itemid':'501'},'rows':[{'value':1,'time':'2026-11-15T06:06:31Z'}]}}
    encoded=context.compact_initial({'coverage':{},'evidence':[item]})
    before=deepcopy(encoded)
    assert context._compact_direct_fields(encoded) is False
    assert encoded==before


def test_final_tries_lossless_encoding_before_thinning_any_raw_points():
    original=measured_bundle()
    for index,row in enumerate(original['numeric_series']['rows']):
        row[context.SERIES_COLUMNS.index('name')]=chr(65+index)
    original['evidence']=[{'evidence_id':'raw-request','instance_id':'scope-a',
        'source':'zabbix.item.numeric:measured','data':{'item':{'itemid':'501'},
        'rows':[{'value':value,'time':f'2026-11-15T06:06:{stamp}Z'}
                for value,stamp in [(1,10),(2,20),(3,30),(4,40)]]}}]
    initial=context.compact_initial(original)
    # Include the mandatory final envelope in both compared encodings.
    initial['coverage']['retained_evidence_count']=1
    compact=deepcopy(initial)
    assert context._compact_direct_fields(compact)
    size=lambda value:len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode())
    assert size(compact)<size(initial)
    packed=context.compact_final_context(initial,max_bytes=size(compact),measure=size)
    assert size(packed)<=size(compact)
    decoded=context._unpack_final_tables(packed)
    assert decoded['evidence'][0]['data']['rows']==original['evidence'][0]['data']['rows']
    assert context.context_refs(packed)==context.context_refs(initial)
