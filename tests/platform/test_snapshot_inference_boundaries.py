"""Delivery properties, not a model-reasoning success assertion."""
from copy import deepcopy
from datetime import timedelta
import json
import pytest

from platform_app.model_context import refresh_snapshot_chronology,compact_initial,compact_final_context,bounded_tool_result
from tests.platform.test_snapshot_chronology import snap
from tests.platform.test_ai_scope import ai_env


def assert_boundary(chronology):
    assert chronology['sample_kind']=='point_in_time'
    assert chronology['sample_count_scope']=='retained_per_source_group'
    assert chronology['not_established_by_snapshots']==[
        'release_mechanism','continuous_duration','incident_count','numeric_series_population']


@pytest.mark.parametrize('later_count',[0,1])
def test_later_empty_or_active_observation_does_not_establish_release_method(later_count):
    rows=[snap('a','2026-11-13T00:00:00Z',1),snap('b','2026-11-13T00:01:00Z',later_count)]
    original=deepcopy(rows);context={'evidence':rows}
    refresh_snapshot_chronology(context)
    assert_boundary(context['snapshot_chronology'])
    group=context['snapshot_chronology']['groups'][0]
    assert group['sample_count']==2
    assert group['empty_after_last']['count']==int(later_count==0)
    assert context['evidence']==original
    assert 'recovered' not in json.dumps(context['snapshot_chronology'])


def test_scope_remains_explicit_through_compaction_and_separate_numeric_population():
    rows=[snap('a','2026-11-13T00:00:00Z',1),snap('b','2026-11-13T00:01:00Z',0)]
    numeric={'evidence_id':'curve','instance_id':'asset-a','source':'zabbix.item.numeric:count',
             'data':{'row_count':720,'sample_count':720,'first':0,'last':0,'min':0,'max':1}}
    initial=compact_initial({'evidence':rows+[numeric],'coverage':{}})
    final=compact_final_context(initial,max_bytes=24000,measure=lambda x:len(json.dumps(x).encode()))
    assert_boundary(final['snapshot_chronology'])
    assert final['snapshot_chronology']['groups'][0]['sample_count']==2
    tool=bounded_tool_result({'items':rows},6000)
    assert_boundary(tool['snapshot_chronology'])
    tiny=bounded_tool_result({'items':[rows[0]]},666)
    assert tiny['items'][0]['evidence_id']=='a'
    assert tiny['snapshot_chronology_omitted']=='tool_result_budget'


@pytest.mark.usefixtures('legacy_collection_compatibility')
def test_real_authorized_request_keeps_snapshot_contract_and_original_references(ai_env):
    from platform_app import investigations as inv,models as m
    from platform_app.evidence import digest
    from tests.platform.test_ai_failures import prepared_run
    from tests.platform.test_model_output_contract import response
    e=ai_env;first=e['evidence'];first.source='mysql.lock_waits'
    first.payload={'rows':[{'waiting_trx_id':11,'blocking_trx_id':12}],'row_count':1}
    first.payload_sha256=digest(first.payload)
    later=m.Evidence(instance_id=first.instance_id,run_id=first.run_id,source=first.source,source_mode='live',
        observed_at=first.observed_at+timedelta(seconds=15),coverage={'complete':True},payload={'rows':[],'row_count':0})
    later.payload_sha256=digest(later.payload);e['db'].add(later);e['db'].commit()
    _,_,lease,config=prepared_run(e)
    requests=[]
    def provider(_config,payload,_timeout):
        context=json.loads(payload['messages'][1]['content'])
        assert_boundary(context['snapshot_chronology'])
        refs={i['evidence_id']for i in context['evidence']}
        assert {first.id,later.id}<=refs
        assert context['instance_ids']==[e['a'].id]
        requests.append(payload);return response(first.id)
    result=inv.run_investigation(e['factory'],lease,config,model_client=provider)
    assert result.status=='completed'and len(requests)==1,result.report
