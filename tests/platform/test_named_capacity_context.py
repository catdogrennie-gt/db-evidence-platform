"""Capacity interpretation must not require a sparse heterogeneous table join.

These tests prove lossless request delivery, not live model reasoning quality.
"""
from copy import deepcopy
import json

import pytest

from platform_app import investigations as inv, models as m
from platform_app.evidence import digest
from platform_app.model_context import compact_initial, compact_final_context
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_ai_failures import prepared_run
from tests.platform.test_model_output_contract import response
from tests.platform.test_capacity_signals import assess, pair, metadata, points


def signal(now, used=33, refs=('used-observation', 'limit-observation'), instance='resource-a'):
    calculation=assess(now,*pair(now,used=(used,)*3))
    calculation.update(hostid='101',used_itemid='501',limit_itemid='502',
                       used_evidence_id=refs[0],limit_evidence_id=refs[1],
                       observation_state=calculation['state'])
    return {'kind':'risk','category':'capacity_pressure','instance_id':instance,
            'evidence_ids':list(refs),'calculation':calculation}


@pytest.mark.parametrize('used,state',[(33,'active'),(10,'recovered')])
def test_capacity_values_states_and_pair_times_remain_named(used,state):
    from platform_app.common import parse_time
    s=signal(parse_time('2026-11-13T05:04:53Z'),used)
    baseline={'category':'baseline_change','evidence_ids':['used-observation'],
              'calculation':{'baseline':{'median':10},'recent':{'median':used},
                             'diagnosis':'change_only_not_root_cause'}}
    bundle={'coverage':{},'evidence':[],'inspection_signals':[s,baseline]}
    original=deepcopy(bundle);result=compact_initial(bundle)
    actual=result['inspection_signals'][0]['calculation']
    assert isinstance(actual,dict), 'Capacity facts need explicit field names in the actual context'
    expected={k:v for k,v in s['calculation'].items()if k!='calculation'}
    # The existing compactor omits the internal calculation-version label;
    # measured values, the formula and its interpretation are still exact.
    assert actual==expected and actual['state']==state
    assert actual['warning_percent']==80 and actual['remaining']==40-used
    assert actual['used_evidence_id']=='used-observation'
    assert actual['limit_evidence_id']=='limit-observation'
    assert bundle==original
    encoded=result['inspection_signals'][1]['calculation']
    assert isinstance(encoded,list)
    decoded=dict(zip(result['signal_calculation_columns'],encoded))
    assert decoded['baseline.median']==10 and decoded['recent.median']==used
    assert compact_initial(result)==result


def test_protected_capacity_facts_survive_final_compaction_or_remain_over_budget():
    from platform_app.common import parse_time
    s=signal(parse_time('2026-11-13T05:04:53Z'))
    bundle={'coverage':{},'inspection_signals':[s],
            'evidence':[{'evidence_id':ref,'instance_id':'resource-a',
                         'source':'zabbix.item.numeric:'+ref,'data':{'rows':[]}}
                        for ref in s['evidence_ids']]}
    initial=compact_initial(bundle)
    measure=lambda value:len(json.dumps(value,separators=(',',':')).encode())
    result=compact_final_context(initial,max_bytes=100,measure=measure)
    assert measure(result)>100, 'Insufficient budget must not silently discard protected facts'
    expected={k:v for k,v in s['calculation'].items()if k!='calculation'}
    assert result['inspection_signals'][0]['calculation']==expected
    assert {i['evidence_id']for i in result['evidence']}==set(s['evidence_ids'])


@pytest.mark.usefixtures('legacy_collection_compatibility')
def test_actual_authorized_request_delivers_named_capacity_and_both_operands(ai_env):
    e=ai_env;now=e['evidence'].observed_at
    used=e['evidence'];used.source='zabbix.item.numeric:used'
    used.payload={'item':metadata('501','used'),'rows':points(now,(33,)*3)}
    used.payload_sha256=digest(used.payload)
    limit=m.Evidence(instance_id=used.instance_id,run_id=used.run_id,
        source='zabbix.item.numeric:limit',source_mode='live',observed_at=now,
        coverage={'complete':True},payload={'item':metadata('502','limit'),'rows':points(now,(40,)*3)})
    limit.payload_sha256=digest(limit.payload)
    e['db'].add(limit);e['db'].commit()
    s=signal(now,refs=(used.id,limit.id),instance=e['a'].id)
    from tests.platform.test_intelligence import policy_env
    policy=policy_env(e);policy.mode='live_redacted';policy.external_confirmed=True
    e['db'].commit()
    _,_,lease,config=prepared_run(e,inspection={'signals':[s],'policy_id':policy.id,'version':policy.version})
    requests=[]
    def provider(_config,payload,_timeout):
        context=json.loads(payload['messages'][1]['content'])
        calc=context['inspection_signals'][0]['calculation']
        assert isinstance(calc,dict)
        assert calc['warning_percent']==80 and calc['used']==33 and calc['limit']==40
        assert calc['classification']=='configured_threshold_risk_not_root_cause'
        assert calc['time_pairs']==s['calculation']['time_pairs']
        assert calc['used_evidence_id']==used.id and calc['limit_evidence_id']==limit.id
        assert context['instance_ids']==[e['a'].id]
        requests.append(payload)
        return response(used.id)
    result=inv.run_investigation(e['factory'],lease,config,model_client=provider)
    assert result.status=='completed'and len(requests)==1,result.report
    size=len(json.dumps(requests[0],ensure_ascii=False,separators=(',',':')).encode())+512
    assert size<=24000 and size+requests[0]['max_tokens']<=32000
