"""Delivery bookkeeping is exact-request provenance, never fact verification."""
from copy import deepcopy
import importlib
import importlib.util
import json

import pytest

from tests.platform.test_ai_scope import ai_env
from tests.platform.test_ai_failures import prepared_run, provider_response


def delivery():
    assert importlib.util.find_spec('platform_app.request_delivery') is not None, 'actual-request receipt implementation missing'
    return importlib.import_module('platform_app.request_delivery')


def point(ref='positive', count=1):
    return {'evidence_id':ref, 'instance_id':'asset-a', 'source':'mysql.lock_waits',
        'source_mode':'live', 'observed_at':'2026-11-13T06:53:47Z',
        'coverage':{'availability':'available','complete':False},
        'data':{'row_count':count, 'rows':[{'waiting_trx_id':1}] if count else [],
            'item':{'source_id':'source-a','hostid':'101','itemid':'11'}}}


def request(items=None):
    return {'model':'test','messages':[{'role':'system','content':'readonly'},
        {'role':'user','content':json.dumps({'evidence':items if items is not None else [point()]})}],
        'max_tokens':4000,'tools':[]}


def prepare(payload, sequence=1):
    return delivery().prepare_request_delivery(payload, investigation_id='investigation-a', manifest_id='manifest-a', sequence=sequence)


def test_prepared_receipt_reuses_exact_projection_without_changing_request_or_budget():
    payload=request([point(),point('empty',0)]); before=deepcopy(payload)
    receipt=prepare(payload)
    assert payload==before
    assert receipt['status']=='prepared'
    assert receipt['request_sha256']==delivery().request_sha256(payload)
    assert [p['evidence_id'] for p in receipt['observations']]==['positive','empty']
    assert receipt['observations'][1]['point']['row_count']==0
    assert receipt['observations'][1]['location']=={'message_index':1,'item_index':1}
    assert 'rows' not in json.dumps(receipt['observations'][0]['point'])
    assert 'admitted_facts' not in receipt and 'verified_headline' not in receipt


def test_receipts_do_not_union_earlier_request_facts_into_final_request():
    first=prepare(request([point(),point('empty',0)]))
    final=prepare(request([point()]),sequence=2)
    assert len(first['observations'])==2
    assert [p['evidence_id'] for p in final['observations']]==['positive']
    assert first['request_sha256']!=final['request_sha256']


def test_real_final_context_packer_does_not_leave_omitted_point_receipts():
    from platform_app.model_context import compact_final_context
    items=[]
    for index in range(80):
        item=point('empty-'+str(index),0)
        item['observed_at']=f'2026-11-13T06:{index//60:02}:{index%60:02}Z'
        items.append(item)
    positive=point('positive',1);positive['observed_at']='2026-11-13T06:00:40Z'
    # No simultaneous conflict is selected as an adjacent negative point.
    items[40]=positive
    original={'evidence':items,'coverage':{}}
    def measure(context):
        payload=request([]);payload['messages'][1]['content']=json.dumps(context,ensure_ascii=False,separators=(',',':'))
        return len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())+512
    packed=compact_final_context(original,max_bytes=5500,measure=measure)
    before=prepare(request(items))
    payload=request([]);payload['messages'][1]['content']=json.dumps(packed,ensure_ascii=False,separators=(',',':'))
    after=prepare(payload,sequence=2)
    refs={p['evidence_id']for p in after['observations']}
    removed={p['evidence_id']for p in before['observations']}-refs
    assert removed and not removed&refs
    assert {'positive','empty-39','empty-41'}<=refs
    assert measure(packed)<=5500


def test_only_actual_matched_readonly_tool_results_supply_projection_receipts():
    payload=request([])
    payload['messages'] += [
        {'role':'assistant','content':json.dumps({'evidence':[point('hallucinated')]}),
         'tool_calls':[{'id':'call-1','type':'function','function':{'name':'get_lock_graph','arguments':'{}'}}]},
        {'role':'tool','tool_call_id':'call-1','content':json.dumps({'items':[point('tool')]} )},
        {'role':'tool','tool_call_id':'foreign','content':json.dumps({'items':[point('unmatched')]} )},
        {'role':'user','content':json.dumps({'evidence':[point('feedback')]} )}]
    receipt=prepare(payload)
    assert [p['evidence_id'] for p in receipt['observations']]==['tool']
    assert receipt['observations'][0]['location']['message_index']==3


@pytest.mark.parametrize('change',['numeric','text_history','boolean_count','unavailable','missing_identity','overlong_identity'])
def test_projection_receipt_does_not_upgrade_other_populations_or_unknown_fields(change):
    item=point()
    if change=='numeric':item['source']='zabbix.item.numeric:counter'
    if change=='text_history':item['data']={'text_omitted':True,'history_sample_count':8}
    if change=='boolean_count':item['data']['row_count']=False
    if change=='unavailable':item['data']['observation_unavailable']=True
    if change=='missing_identity':item['data']['item'].pop('source_id')
    if change=='overlong_identity':item['data']['item']['source_id']='x'*1000
    receipt=prepare(request([item]))
    if change=='numeric':assert receipt['observations']==[]
    else:
        observed=receipt['observations'][0]['point']
        assert not observed.get('snapshot_complete')
        if change in ('text_history','boolean_count'):assert 'row_count' not in observed
        if change in ('missing_identity','overlong_identity'):assert observed['item'].get('source_id') is None
        if change=='unavailable':assert observed['observation_unavailable'] is True


def test_private_audit_confirms_exact_prepared_request_but_not_semantic_truth(tmp_path):
    payload=request(); receipt=prepare(payload)
    path=tmp_path/'audit.json';path.write_text(json.dumps({'request':payload,'response':{'choices':[]}}),encoding='utf-8')
    confirmed=delivery().confirm_request_delivery(receipt,payload,path)
    assert confirmed['status']=='response_received'
    assert confirmed['audit_sha256'] and receipt['status']=='prepared'
    assert confirmed['semantic_verification']=='not_performed'


@pytest.mark.parametrize('change',['request','receipt_field','receipt_identity','audit_request','audit_missing','audit_response'])
def test_receipt_or_audit_mismatch_cannot_confirm_delivery(tmp_path,change):
    payload=request();receipt=prepare(payload);audit={'request':deepcopy(payload),'response':{'choices':[]}}
    if change=='request':payload['messages'][1]['content']='{}'
    if change=='receipt_field':receipt['observations'][0]['point']['row_count']=0
    if change=='receipt_identity':receipt['manifest_id']='other-manifest'
    if change=='audit_request':audit['request']['model']='other'
    if change=='audit_response':audit.pop('response')
    path=tmp_path/'audit.json'
    if change!='audit_missing':path.write_text(json.dumps(audit),encoding='utf-8')
    result=delivery().confirm_request_delivery(receipt,payload,path)
    assert result['status']=='unconfirmed'
    assert result['semantic_verification']=='not_performed'


def test_real_call_persists_prepared_receipt_before_io_then_binds_private_audit(ai_env):
    from sqlalchemy import select
    from platform_app.investigations import run_investigation
    from platform_app.models import ModelCall
    row,_,lease,config=prepared_run(ai_env)
    def provider(_config,payload,_timeout):
        with ai_env['factory']() as db:
            call=db.scalar(select(ModelCall).where(ModelCall.investigation_id==row.id))
            receipt=call.response.get('request_delivery')
            assert receipt is not None, 'receipt must be persisted before network IO'
            assert receipt['status']=='prepared' and receipt['request_sha256']==call.input_sha256
        return provider_response(ai_env['evidence'].id)
    result=run_investigation(ai_env['factory'],lease,config,model_client=provider)
    assert result.status=='completed',result.report
    with ai_env['factory']() as db:
        call=db.scalar(select(ModelCall).where(ModelCall.investigation_id==row.id))
        receipt=call.response['request_delivery']
        assert receipt['status']=='response_received'
        assert receipt['semantic_verification']=='not_performed'
        assert receipt['request_sha256']==call.input_sha256
        binding=call.response.get('report_output')
        assert binding is not None, 'accepted report must be bound to its exact producing request'
        assert binding['request_sha256']==call.input_sha256
        assert binding['scope_ref']==call.scope_ref and binding['sequence']==call.sequence
        assert binding['hash_scope']=='validated_candidate_before_server_envelope'
        normalized={k:v for k,v in result.report.items() if k not in ('coverage','policy_versions','prompt_version')}
        assert binding['normalized_report_sha256']==delivery().request_sha256(normalized)
        assert binding['status']=='structure_accepted_unverified'


def test_unknown_transport_cannot_confirm_prepared_receipt(ai_env):
    from sqlalchemy import select
    from platform_app.investigations import run_investigation,ModelTransportError
    from platform_app.models import ModelCall
    row,_,lease,config=prepared_run(ai_env)
    def provider(*_):raise ModelTransportError('unknown',True)
    run_investigation(ai_env['factory'],lease,config,model_client=provider)
    with ai_env['factory']() as db:
        call=db.scalar(select(ModelCall).where(ModelCall.investigation_id==row.id))
        receipt=call.response.get('request_delivery')
        assert receipt is not None, 'unknown IO must retain prepared provenance'
        assert call.status=='outcome_unknown' and receipt['status']=='outcome_unknown'
        assert 'audit_sha256' not in receipt


def test_inventory_limit_is_explicit_and_never_truncates_the_model_request():
    payload=request([point(str(i)) for i in range(1030)]);before=deepcopy(payload)
    receipt=prepare(payload)
    assert receipt['projection_inventory_complete'] is False
    assert len(receipt['observations'])==1024 and payload==before


def test_readonly_tool_id_cannot_be_replayed_or_reused_by_unknown_tool():
    payload=request([])
    call={'id':'call-a','function':{'name':'get_lock_graph','arguments':'{}'}}
    result={'role':'tool','tool_call_id':'call-a','content':json.dumps({'items':[point('first')]})}
    payload['messages'] += [{'role':'assistant','tool_calls':[call]},result,deepcopy(result),
        {'role':'assistant','tool_calls':[{'id':'call-a','function':{'name':'unknown'}}]},
        {'role':'tool','tool_call_id':'call-a','content':json.dumps({'items':[point('spoof')]})}]
    assert [p['evidence_id'] for p in prepare(payload)['observations']]==['first']


@pytest.mark.parametrize('failure',['transport_failed','invalid_response','audit_missing'])
def test_nonconfirmed_call_paths_never_gain_delivery_authority(ai_env,monkeypatch,failure):
    from sqlalchemy import select
    from platform_app import investigations as inv
    from platform_app.models import ModelCall
    row,_,lease,config=prepared_run(ai_env)
    def provider(*_):
        if failure=='transport_failed':raise inv.ModelTransportError('rejected',False)
        if failure=='invalid_response':return []
        return provider_response(ai_env['evidence'].id)
    if failure=='audit_missing':monkeypatch.setattr(inv,'_write_private_audit',lambda *args:'missing.json')
    inv.run_investigation(ai_env['factory'],lease,config,model_client=provider)
    with ai_env['factory']() as db:
        call=db.scalar(select(ModelCall).where(ModelCall.investigation_id==row.id))
        receipt=call.response['request_delivery']
        assert receipt['status']=={'transport_failed':'failed','invalid_response':'outcome_unknown','audit_missing':'unconfirmed'}[failure]
        assert 'audit_sha256' not in receipt


def test_final_packed_round_has_its_own_persisted_inventory_and_bound(ai_env,monkeypatch):
    from sqlalchemy import select
    from platform_app import investigations as inv,model_context
    from platform_app.models import ModelCall
    from platform_app.tools import project_evidence
    row,_,lease,config=prepared_run(ai_env)
    ref=ai_env['evidence'].id;requests=[]
    tool_result={'items':[project_evidence(ai_env['evidence'])],'coverage':{'complete':False}}
    monkeypatch.setattr(model_context,'prepare_tool_context',lambda *args:deepcopy(tool_result))
    def bound(payload):return len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())+512
    def provider(_config,payload,_timeout):
        requests.append(deepcopy(payload))
        if len(requests)==1:
            message={'role':'assistant','content':'','tool_calls':[{'id':'call-budget','type':'function',
                'function':{'name':'get_lock_graph','arguments':json.dumps({'instance_id':ai_env['a'].id})}}]}
            probe=deepcopy(payload);probe['messages'] += [deepcopy(message),
                {'role':'tool','tool_call_id':'call-budget','content':json.dumps(tool_result,ensure_ascii=False)}]
            message['content']='x'*(23500-bound(probe))
            return {'choices':[{'message':message}], 'usage':{'prompt_tokens':6517,'completion_tokens':244}}
        assert payload['tools']==[] and payload['tool_choice']=='none'
        assert bound(payload)<=24000 and 6517+244+bound(payload)+payload['max_tokens']<=32000
        return provider_response(ref)
    result=inv.run_investigation(ai_env['factory'],lease,config,model_client=provider)
    assert result.status=='completed' and len(requests)==2,result.report
    with ai_env['factory']() as db:
        calls=list(db.scalars(select(ModelCall).where(ModelCall.investigation_id==row.id).order_by(ModelCall.sequence)))
        for index,call in enumerate(calls):
            receipt=call.response['request_delivery']
            assert receipt['sequence']==index+1 and receipt['status']=='response_received'
            assert receipt['request_sha256']==delivery().request_sha256(requests[index])==call.input_sha256
            expected=[(ref,1)] if index==0 else [(ref,1),(ref,3)]
            assert [(p['evidence_id'],p['location']['message_index']) for p in receipt['observations']]==expected
            assert all(p['point']['row_count']==1 for p in receipt['observations'])
        assert any(p['location']['message_index']>1 for p in calls[1].response['request_delivery']['observations'])
        assert 'report_output' not in calls[0].response
        assert calls[1].response.get('report_output') is not None, 'tool rounds cannot stand in for the report-producing request'
        assert calls[1].response['report_output']['request_sha256']==calls[1].input_sha256
