"""Generic context coverage: frozen measurements, not scenario-name answers."""
import copy
from datetime import timedelta
import hashlib
import json

import pytest

from platform_app.common import iso
from platform_app.model_context import compact_initial, compact_numeric_evidence, refresh_snapshot_chronology
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_ai_failures import prepared_run, provider_response


def measured_item(index):
    return {'hostid': str(101 + index % 2), 'itemid': str(700 + index),
        'name': 'Measured dimension ' + str(index), 'key': 'measured.dimension_' + str(index),
        'host_name': 'Measured resource ' + str(index % 2), 'units': 'ms',
        'value_kind': 'numeric', 'value_type': 0, 'interval': 5,
        'interval_verified': True, 'history_semantics': 'periodic_samples',
        'poll_interval': 5, 'heartbeat_seconds': None,
        'value_semantics': 'gauge', 'semantics_source': 'zabbix_tag',
        'tags': [{'tag':'semantics','value':'gauge'}, {'tag':'component','value':'measurement'}]}


def projected_numeric():
    from datetime import datetime, timezone
    start = datetime(2026, 11, 12, 8, tzinfo=timezone.utc)
    return {'evidence_id':'actual-observation', 'instance_id':'resource-a',
        'source':'zabbix.item.numeric:actual-identity', 'source_mode':'live',
        'observed_at':iso(start + timedelta(seconds=495)),
        'coverage':{'complete':False, 'truncated':True, 'availability':'available',
                    'gaps':['history_truncated']},
        'data':{'row_count':100, 'item':measured_item(0),
            'rows':[{'time':iso(start + timedelta(seconds=5*i)), 'value': i % 12}
                    for i in range(100)]}}


def test_numeric_projection_preserves_true_count_times_and_explicit_display_omissions():
    item = projected_numeric()
    bundle = {'coverage':{}, 'evidence':[item], 'inspection_signals':[
        {'evidence_ids':[item['evidence_id']]}]}
    original = copy.deepcopy(bundle)
    retained = compact_numeric_evidence(item)
    data = retained['data']
    assert bundle == original
    assert data['sample_count'] == 100
    assert data['first_time'] == item['data']['rows'][0]['time']
    assert data['last_time'] == item['data']['rows'][-1]['time']
    assert data['rows'] == item['data']['rows'][:1] + item['data']['rows'][-3:]
    assert data['rows_omitted'] == 96
    assert data['projection_scope'] == 'retained_samples_subset_not_upstream_gap'
    assert retained['coverage']['gaps'] == ['history_truncated']
    assert retained['data']['item']['value_semantics'] == 'gauge'
    assert len(json.dumps(retained, ensure_ascii=False).encode()) < 1100


def test_tabular_numeric_and_signal_projection_decodes_losslessly_without_changing_frozen_values():
    item=projected_numeric(); signal={'evidence_ids':[item['evidence_id']],
        'calculation':{'baseline':{'from':'2026-11-12T07:00:00Z','median':1,'sample_count':31},
                       'recent':{'to':item['observed_at'],'median':3,'sample_count':3},
                       'diagnosis':'change_only_not_root_cause'}}
    bundle={'coverage':{},'evidence':[item],'inspection_signals':[signal]}
    before=copy.deepcopy(bundle);result=compact_initial(bundle)
    assert bundle==before
    encoded=result['evidence'][0]
    values=dict(zip(result['numeric_observation_columns'],encoded['numeric_observation']))
    times=result['numeric_time_dictionary']
    assert times[values['observed_at_ref']]==item['observed_at']
    assert values['data.sample_count']==100
    assert times[values['data.first_time_ref']]==item['data']['rows'][0]['time']
    assert [(times[p['time_ref']],p['value']) for p in values['data.rows']]==[
        (p['time'],p['value']) for p in item['data']['rows'][:1]+item['data']['rows'][-3:]]
    assert values['coverage.gaps']==item['coverage']['gaps']
    decoded=dict(zip(result['signal_calculation_columns'],result['inspection_signals'][0]['calculation']))
    assert decoded['baseline.sample_count']==31 and decoded['recent.sample_count']==3
    assert times[decoded['baseline.from_ref']]==signal['calculation']['baseline']['from']
    assert times[decoded['recent.to_ref']]==signal['calculation']['recent']['to']
    assert decoded['baseline.median']==1 and decoded['recent.median']==3
    assert decoded['diagnosis']==signal['calculation']['diagnosis']
    again=compact_initial(result)
    for key in ['evidence','numeric_observation_columns','numeric_time_dictionary','inspection_signals','signal_calculation_columns']:
        assert again[key]==result[key]


def test_numeric_curve_is_not_a_nonempty_snapshot_chronology():
    numeric = projected_numeric()
    lock = {'evidence_id':'lock-observation', 'instance_id':'resource-a',
        'source':'mysql.lock_waits', 'observed_at':numeric['observed_at'],
        'data':{'row_count':1, 'rows':[{'waiting_trx_id':12,'blocking_trx_id':13}]}}
    digest=copy.deepcopy(lock);digest.update(evidence_id='sql-statistics',source='mysql.statement_digest')
    context = refresh_snapshot_chronology({'evidence':[numeric, lock,digest]})
    assert [group['source'] for group in context['snapshot_chronology']['groups']] == ['mysql.lock_waits']


def test_numeric_summary_keeps_real_first_time_separate_from_count_and_extremes():
    from types import SimpleNamespace
    from platform_app.model_context import add_series, new_series
    item=projected_numeric();series=new_series()
    points=[dict(point,validity='valid') for point in item['data']['rows']]
    add_series(series,SimpleNamespace(id='frozen-window',instance_id='resource-a'),
        {'item':measured_item(0),'points':points,'coverage':{'complete':True}},
        [point['value'] for point in points])
    summary=dict(zip(series['columns'],series['rows'][0]))
    assert summary['sample_count']==100 and summary['max']==11
    assert summary['first_time']==points[0]['time']
    assert summary['last_time']==points[-1]['time']


def test_prompt_distinguishes_projection_gaps_quantities_and_tentative_mechanisms():
    from platform_app.investigations import _system_prompt_for_sources
    prompt=_system_prompt_for_sources({'zabbix.item.numeric:actual-identity'})
    for required in ['rows_omitted','sample_count不是max','每个量化子句','候选机制','内部字段名']:
        assert required in prompt


def test_lock_only_signal_encoding_also_has_its_columns_and_time_dictionary_contract():
    from platform_app.investigations import _system_prompt_for_sources
    context=compact_initial({'coverage':{},'evidence':[],
        'inspection_signals':[{'evidence_ids':['lock'],
            'calculation':{'row_count':1,'observed_at':'2026-11-12T08:00:00Z'}}]})
    assert context['signal_calculation_columns']
    prompt=_system_prompt_for_sources({'mysql.lock_waits'})
    assert 'signal_calculation_columns' in prompt and 'numeric_time_dictionary' in prompt


def test_budget_trim_preserves_unrepresented_dimensions_and_prunes_only_unused_dictionary_values():
    from platform_app.model_context import trim_numeric_summary
    context={'numeric_series':{'columns':['evidence_id','resource','hostid','itemid','first_time_ref'],
        'rows':[['manifest:trigger',0,'101','700',0],['manifest:normal',0,'101','701',1]],
        'first_time_dictionary':['2026-11-12T08:00:00Z','2026-11-12T08:01:00Z'],'omitted_count':0},
        'numeric_observation_columns':['data.numeric_series_ref'],
        'evidence':[{'evidence_id':'trigger','numeric_observation':['manifest:trigger']}]}
    assert trim_numeric_summary(context)
    series=context['numeric_series']
    assert series['rows']==[['manifest:normal',0,'101','701',0]]
    assert series['first_time_dictionary']==['2026-11-12T08:01:00Z']
    assert context['evidence'][0]['numeric_observation']==[None]
    assert series['omitted_count']==1


def test_same_local_item_identity_from_another_source_is_not_deduplicated_or_linked():
    item=projected_numeric();meta=item['data']['item']
    item['source']='zabbix.item.numeric:'+hashlib.sha256(
        f"source-b:{meta['hostid']}:{meta['itemid']}".encode()).hexdigest()[:24]
    context=compact_initial({'coverage':{},'evidence':[item],
        'numeric_series':{'rows':[['manifest:other',0,meta['hostid'],meta['itemid']]],
            'resources':[{'instance_id':item['instance_id'],'source_id':'source-a'}]}})
    assert context['evidence'][0]['evidence_id']==item['evidence_id']
    assert 'data.numeric_series_ref' not in context['numeric_observation_columns']


def numeric_summary_for(item, sample_count):
    from platform_app.model_context import SERIES_COLUMNS
    meta=item['data']['item'];source_id='selected-source'
    item['source']='zabbix.item.numeric:'+hashlib.sha256(
        f"{source_id}:{meta['hostid']}:{meta['itemid']}".encode()).hexdigest()[:24]
    values={'evidence_id':'manifest:window','resource':0,'hostid':meta['hostid'],
        'itemid':meta['itemid'],'sample_count':sample_count,'first':1 if sample_count else None,
        'last':1 if sample_count else None,'min':1 if sample_count else None,
        'max':1 if sample_count else None,'complete':sample_count>0,
        'reason_codes':[] if sample_count else ['item_unavailable']}
    return {'columns':list(SERIES_COLUMNS),'rows':[[values.get(key) for key in SERIES_COLUMNS]],
        'resources':[{'instance_id':item['instance_id'],'source_id':source_id}], 'omitted_count':0}


@pytest.mark.parametrize('trigger',[False,True])
def test_empty_matching_window_cannot_evict_or_alias_real_numeric_samples(trigger):
    item=projected_numeric();series=numeric_summary_for(item,0)
    bundle={'coverage':{'complete':False,'gaps':['item_unavailable']},'evidence':[item],
        'numeric_series':series,'inspection_signals':[{'evidence_ids':[item['evidence_id']]}] if trigger else []}
    before=copy.deepcopy(bundle);context=compact_initial(bundle)
    assert len(context['evidence'])==1, 'An empty window is not a substitute for observed samples'
    values=dict(zip(context['numeric_observation_columns'],context['evidence'][0]['numeric_observation']))
    assert values['data.sample_count']==100
    assert values.get('data.numeric_series_ref') is None
    assert context['coverage']['complete'] is False and context['coverage']['gaps']==['item_unavailable']
    columns=context['numeric_series']['columns'];row=context['numeric_series']['rows'][0]
    assert row[columns.index('sample_count')]==0 and row[columns.index('complete')] is False
    assert bundle==before
    assert compact_initial(context)==context


def test_nonempty_same_source_window_still_deduplicates_and_stays_idempotent():
    item=projected_numeric();series=numeric_summary_for(item,1)
    bundle={'coverage':{},'evidence':[item],'numeric_series':series}
    before=copy.deepcopy(bundle);context=compact_initial(bundle)
    assert context['evidence']==[] and len(context['numeric_series']['rows'])==1
    assert bundle==before
    assert compact_initial(context)==context


@pytest.mark.parametrize('identity_change',[{'source_id':None},{'source_id':'other-source'},
    {'instance_id':'other-resource'}])
def test_nonempty_window_from_another_or_unproven_identity_cannot_evict_raw(identity_change):
    item=projected_numeric();series=numeric_summary_for(item,1)
    series['resources'][0].update(identity_change)
    result=compact_initial({'coverage':{},'evidence':[item],'numeric_series':series})
    assert len(result['evidence'])==1
    values=dict(zip(result['numeric_observation_columns'],result['evidence'][0]['numeric_observation']))
    assert values.get('data.numeric_series_ref') is None and values['data.sample_count']==100


@pytest.mark.parametrize('bounds',[
    ('2026-11-12T08:00:10Z','2026-11-12T08:00:30Z'),
    ('2026-11-12T16:00:10+08:00','2026-11-12T16:00:30+08:00')])
def test_frozen_window_clips_points_before_counting_and_selecting_endpoints(bounds):
    item=projected_numeric();item['data']['rows'].append({'time':'bad-clock','value':999})
    bundle={'from':bounds[0],'to':bounds[1],'coverage':{},'evidence':[item],
        'inspection_signals':[{'evidence_ids':[item['evidence_id']]}]}
    before=copy.deepcopy(bundle);context=compact_initial(bundle)
    values=dict(zip(context['numeric_observation_columns'],context['evidence'][0]['numeric_observation']))
    times=context['numeric_time_dictionary']
    assert values['data.sample_count']==4
    assert values['data.rows_omitted']==0
    assert times[values['data.first_time_ref']]=='2026-11-12T08:00:10.000000Z'
    assert times[values['data.last_time_ref']]=='2026-11-12T08:00:25.000000Z'
    assert [(times[p['time_ref']],p['value']) for p in values['data.rows']]==[
        ('2026-11-12T08:00:10.000000Z',2),('2026-11-12T08:00:15.000000Z',3),
        ('2026-11-12T08:00:20.000000Z',4),('2026-11-12T08:00:25.000000Z',5)]
    assert values['coverage.gaps']==['history_truncated']
    assert bundle==before and compact_initial(context)==context


@pytest.mark.parametrize('bounds',[
    ('2026-11-12T09:00:00Z','2026-11-12T09:01:00Z'),('invalid','2026-11-12T09:01:00Z'),
    ('2026-11-12T09:01:00Z','2026-11-12T09:00:00Z'),(None,'2026-11-12T09:01:00Z'),
    (None,None)])
def test_missing_or_invalid_frozen_window_observation_is_unavailable_not_unbounded(bounds):
    item=projected_numeric();bundle={'from':bounds[0],'to':bounds[1],'coverage':{},
        'evidence':[item],'inspection_signals':[{'evidence_ids':[item['evidence_id']]}]}
    before=copy.deepcopy(bundle);context=compact_initial(bundle);actual=context['evidence'][0]
    if 'numeric_observation' in actual:
        values=dict(zip(context['numeric_observation_columns'],actual['numeric_observation']))
        assert values['data.observation_unavailable'] is True
        assert values['data.sample_count']==0 and values['data.rows']==[]
        assert values.get('data.first_time_ref') is None and values.get('data.last_time_ref') is None
    else:
        assert actual['data']['observation_unavailable'] is True
        assert actual['data']['rows']==[] and actual['data']['sample_count']==0
    assert bundle==before and compact_initial(context)==context


@pytest.mark.parametrize('prefix',['','wrong:'])
def test_manifest_reference_prefix_is_opaque_and_never_repaired(prefix):
    from platform_app.common import PlatformError
    from platform_app.investigations import validate_report, _system_prompt_for_sources, PROMPT_VERSION
    from tests.platform.test_report_summary import report,summary
    token='10000000-0000-0000-0000-000000000001';reference='manifest:'+token
    value=report();value['executive_summary']=summary(reference)
    assert validate_report(value,{reference},expected_version=PROMPT_VERSION)['executive_summary']==summary(reference)
    raw=report();raw['executive_summary']=summary(token)
    assert validate_report(raw,{token},expected_version=PROMPT_VERSION)['executive_summary']==summary(token)
    value['executive_summary']['phenomenon']['evidence_ids']=[prefix+token]
    with pytest.raises(PlatformError) as error:
        validate_report(value,{reference},expected_version=PROMPT_VERSION)
    assert error.value.detail['code']=='unverified_evidence_citation'
    prompt=_system_prompt_for_sources({'zabbix.item.numeric:actual-identity'})
    assert 'manifest:' in prompt and '不透明' in prompt and '前缀' in prompt
    assert '原本裸UUID也原样复制' in prompt


@pytest.mark.parametrize('field',['prerequisites','verification'])
def test_action_array_contract_accepts_lists_and_rejects_string_without_coercion(field):
    from platform_app.common import PlatformError
    from platform_app.investigations import validate_report, _system_prompt_for_sources, PROMPT_VERSION
    from tests.platform.test_report_summary import report
    for legal in ([],['Check a fresh observation']):
        value=report();value['executive_summary']['recommended_actions'][0][field]=legal
        actual=validate_report(value,{'real'},expected_version=PROMPT_VERSION)
        assert actual['executive_summary']['recommended_actions'][0][field]==legal
    value=report();value['executive_summary']['recommended_actions'][0][field]='Check a fresh observation'
    with pytest.raises(PlatformError) as error:
        validate_report(value,{'real'},expected_version=PROMPT_VERSION)
    assert error.value.detail['code']=='invalid_model_report'
    prompt=_system_prompt_for_sources({'zabbix.item.numeric:actual-identity'})
    assert 'prerequisites/verification必须为array[string]' in prompt
    assert 'evidence_ids/counterevidence_ids必须为array[string]' in prompt


def seed_dimensions(e, monkeypatch, frozen_payloads=None):
    from platform_app import models as m, evidence as evidence_module
    db=e['db']; baseline=e['evidence']; baseline.source='mysql.statement_digest'
    baseline.payload={'rows':[{'DIGEST':'a'*64, 'COUNT_STAR':12, 'SUM_TIMER_WAIT':12000}]}
    refs=[]
    for index in range(6):
        points=[{'time':iso(e['start']+timedelta(seconds=5*i)),
                 'value':index+1 if i<97 else (index+1)*10} for i in range(100)]
        row=m.Evidence(instance_id=e['a'].id, run_id=baseline.run_id,
            source='zabbix.item.numeric:'+hashlib.sha256(
                f"selected-monitor:{101+index%2}:{700+index}".encode()).hexdigest()[:24],
            observed_at=e['start']+timedelta(seconds=495),
            coverage={'availability':'available','complete':True}, payload_sha256='0'*64,
            source_mode='live', payload={'item':measured_item(index),'rows':points})
        db.add(row);db.flush();refs.append(row.id)
    db.commit()
    original=evidence_module.create_manifest
    def frozen_dimensions(*args,**kwargs):
        manifest=original(*args,**kwargs);db=args[0]
        for index in range(26):
            db.add(m.ManifestItem(manifest_id=manifest.id, instance_id=e['a'].id,
                item_type='time_series_window', payload_sha256='a'*64,
                payload={'template':'zabbix_item_numeric','owner_collector':'runtime',
                    'source':'zabbix_history','unit':'ms','item':measured_item(index),
                    'provenance':{'source_id':'selected-monitor','mapping_version':3,'config_revision':4},
                    'points':[{'time':iso(e['start']+timedelta(seconds=5*i)),
                        'value':index+1 if index>=6 or i<97 else (index+1)*10,
                        'validity':'valid'} for i in range(100)],
                    'coverage':{'complete':True}}))
        db.flush()
        if frozen_payloads is not None:
            from sqlalchemy import select
            for item in db.scalars(select(m.ManifestItem).where(m.ManifestItem.manifest_id==manifest.id)):
                frozen_payloads.append((item.id,copy.deepcopy(item.payload),item.payload_sha256))
        return manifest
    monkeypatch.setattr(evidence_module,'create_manifest',frozen_dimensions)
    return refs


def signals(e, refs):
    return [{'kind':'current', 'category':'baseline_change', 'instance_id':e['a'].id,
        'evidence_ids':[ref], 'calculation':{
            'state':'changed','observation_state':'active','hostid':str(101+index%2),
            'itemid':str(700+index),'source_id':'selected-monitor','config_revision':4,
            'mapping_version':3,'reference_key':'observed-reference:'+'a'*64,
            'episode_started_at':iso(e['end']-timedelta(seconds=30)),
            'observed_at':iso(e['end']-timedelta(seconds=5)),
            'baseline':{'from':iso(e['start']),'to':iso(e['start']+timedelta(seconds=480)),
                'sample_count':97,'median':index+1,'mad':0},
            'recent':{'from':iso(e['start']+timedelta(seconds=485)),
                'to':iso(e['start']+timedelta(seconds=495)),'sample_count':3,
                'median':(index+1)*10,'min':(index+1)*10,'max':(index+1)*10},
            'direction':'increase','threshold_distance':1e-9,
            'calculation':'median_mad_sustained_change_v1',
            'diagnosis':'change_only_not_root_cause','baseline_basis':'observed_reference_not_health'}}
        for index,ref in enumerate(refs)]


@pytest.mark.usefixtures('legacy_collection_compatibility')
def test_bundle_retains_each_actual_signal_dimension_before_redundant_metadata(ai_env,monkeypatch):
    from platform_app.evidence import create_manifest
    from platform_app.investigations import _bundle
    e=ai_env;refs=seed_dimensions(e,monkeypatch)
    inv,task,_,_=prepared_run(e, inspection={'signals':signals(e,refs)})
    manifest=create_manifest(e['db'],'investigation',inv.id,[e['a'].id],e['start'],e['end'])
    inv.manifest_id=manifest.id;e['db'].flush()
    bundle,*_=_bundle(e['db'],inv,manifest)
    actual={item['evidence_id'] for item in bundle['evidence']}
    assert set(refs).issubset(actual), 'A retained signal needs its real frozen projection, not only a name'
    assert len(bundle['inspection_signals'])==6


@pytest.mark.usefixtures('legacy_collection_compatibility')
def test_bundle_clips_trigger_history_before_its_first_numeric_compaction(ai_env,monkeypatch):
    from platform_app import models as m
    from platform_app.evidence import create_manifest
    from platform_app.investigations import _bundle
    e=ai_env;refs=seed_dimensions(e,monkeypatch);row=e['db'].get(m.Evidence,refs[0])
    row.payload=dict(row.payload,rows=[
        {'time':iso(e['start']-timedelta(seconds=5)),'value':900},
        {'time':iso(e['start']),'value':2},
        {'time':iso(e['start']+timedelta(seconds=5)),'value':3},
        {'time':iso(e['end']),'value':901},
        {'time':iso(e['end']+timedelta(seconds=5)),'value':902}])
    e['db'].commit();before=(copy.deepcopy(row.payload),copy.deepcopy(row.coverage),row.payload_sha256)
    inv,_,_,_=prepared_run(e,inspection={'signals':signals(e,refs)})
    manifest=create_manifest(e['db'],'investigation',inv.id,[e['a'].id],e['start'],e['end'])
    inv.manifest_id=manifest.id;e['db'].flush();bundle,*_=_bundle(e['db'],inv,manifest)
    actual=next(item for item in bundle['evidence'] if item['evidence_id']==refs[0])['data']
    assert actual['sample_count']==2
    assert actual['rows']==[{'time':iso(e['start']),'value':2},
        {'time':iso(e['start']+timedelta(seconds=5)),'value':3}]
    assert (row.payload,row.coverage,row.payload_sha256)==before


@pytest.mark.usefixtures('legacy_collection_compatibility')
def test_live_context_covers_signal_mechanisms_and_normal_dimensions_with_same_budget(ai_env,monkeypatch):
    from platform_app.investigations import run_investigation
    from tests.platform.test_intelligence import policy_env
    from platform_app import models as m
    e=ai_env;frozen=[];refs=seed_dimensions(e,monkeypatch,frozen)
    before=[(ref,copy.deepcopy(e['db'].get(m.Evidence,ref).payload),
             copy.deepcopy(e['db'].get(m.Evidence,ref).coverage),e['db'].get(m.Evidence,ref).payload_sha256)
            for ref in refs]
    policy=policy_env(e);policy.mode='live_redacted';policy.external_confirmed=True;e['db'].commit()
    _,_,lease,config=prepared_run(e, inspection={'policy_id':policy.id,
        'version':policy.version,'signals':signals(e,refs)})
    captured=[]
    def provider(config,payload,timeout):
        captured.append(payload)
        supplied=json.loads(payload['messages'][1]['content'])
        return provider_response(supplied['evidence'][0]['evidence_id'])
    result=run_investigation(e['factory'],lease,config,model_client=provider)
    assert result.status=='completed' and len(captured)==1,result.report
    payload=captured[0];context=json.loads(payload['messages'][1]['content'])
    series=context['numeric_series'];columns=series['columns']
    retained={dict(zip(columns,row))['itemid'] for row in series['rows']}
    cols=context['numeric_observation_columns']
    actual={item['numeric_observation'][cols.index('data.item.itemid')]
            for item in context['evidence'] if 'numeric_observation' in item}
    assert {str(700+i) for i in range(26)}.issubset(retained|actual), (len(retained),len(context['inspection_signals']),
        len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())+512)
    assert {str(700+i) for i in range(6,26)}.issubset(retained)
    assert series['omitted_count']==26-len(series['rows'])
    assert len(context['inspection_signals'])==6
    assert set(refs).issubset({item['evidence_id'] for item in context['evidence']})
    assert any(item.get('source')=='mysql.statement_digest' for item in context['evidence'])
    assert all(group['source']=='mysql.statement_digest'
               for group in context.get('snapshot_chronology',{}).get('groups',[]))
    assert len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())+512<=24000
    assert payload['max_tokens']==2000
    with e['factory']() as db:
        for ref,body,coverage,sha in before:
            row=db.get(m.Evidence,ref)
            assert (row.payload,row.coverage,row.payload_sha256)==(body,coverage,sha)
        for ref,body,sha in frozen:
            row=db.get(m.ManifestItem,ref)
            assert (row.payload,row.payload_sha256)==(body,sha)


@pytest.mark.usefixtures('legacy_collection_compatibility')
def test_wide_context_followup_keeps_complete_tool_group_and_truthful_remaining_budget(ai_env,monkeypatch):
    from platform_app.investigations import run_investigation
    from platform_app.model_context import context_refs
    from tests.platform.test_intelligence import policy_env
    e=ai_env;refs=seed_dimensions(e,monkeypatch)
    policy=policy_env(e);policy.mode='live_redacted';policy.external_confirmed=True;e['db'].commit()
    _,_,lease,config=prepared_run(e, inspection={'policy_id':policy.id,
        'version':policy.version,'signals':signals(e,refs)})
    config.model['max_calls']=2
    calls=[]
    def provider(config,payload,timeout):
        calls.append(copy.deepcopy(payload))
        context=json.loads(payload['messages'][1]['content'])
        supplied=context_refs(context)
        assert all(set(signal['evidence_ids']).issubset(supplied) for signal in context['inspection_signals'])
        assert len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())+512<=24000
        if len(calls)==1:
            return {'choices':[{'message':{'tool_calls':[{'id':'frozen-read','type':'function',
                'function':{'name':'get_evidence','arguments':json.dumps({'instance_id':e['a'].id,
                    'evidence_ids':[refs[0]]})}}]}}],
                'usage':{'prompt_tokens':10000,'completion_tokens':200}}
        assert payload['tools']==[] and payload['tool_choice']=='none'
        assert 10200+len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())+512+payload['max_tokens']<=32000
        assert any(message.get('tool_calls',[{}])[0].get('id')=='frozen-read'
                   for message in payload['messages'] if message['role']=='assistant')
        assert any(message.get('tool_call_id')=='frozen-read'
                   for message in payload['messages'] if message['role']=='tool')
        series=context['numeric_series']
        assert series['omitted_count']==26-len(series['rows'])
        for name in series['columns']:
            if not name.endswith('_ref'):continue
            dictionary=series[name[:-4]+'_dictionary'];index=series['columns'].index(name)
            assert all(type(row[index]) is int and 0<=row[index]<len(dictionary) for row in series['rows'])
        return provider_response(context['evidence'][0]['evidence_id'])
    result=run_investigation(e['factory'],lease,config,model_client=provider)
    assert result.status=='completed' and len(calls)==2,result.report
