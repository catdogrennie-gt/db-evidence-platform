"""Typed model selections have no authority beyond qualified delivered points."""
from copy import deepcopy
import json
import pytest
from fastapi import HTTPException
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_report_grounding import prepare_case,ground
from tests.platform.test_report_summary import report


def select_response(facet='snapshot_point',assertion='selected'):
    def alter(response,identifier):
        message=response['choices'][0]['message'];value=json.loads(message['content'])
        value['fact_selections']=[{'fact_id':identifier,'facet':facet,'assertion':assertion}]
        message['content']=json.dumps(value)
    return alter


@pytest.mark.parametrize('facet',['continuous_duration','incident_count','release_mechanism','current_state'])
def test_real_model_output_preserves_unsupported_assertions_without_retry_or_fact_authority(ai_env,facet):
    e=prepare_case(ai_env,response_alter=select_response(facet));before=deepcopy(e['result'].report)
    result=ground(e)
    assert e['result'].status=='completed'
    assert len(e['requests'])==1,'No retry to select a favourable temporal assertion'
    [decision]=result['model_selections']['decisions']
    assert decision['status']=='not_admitted' and decision['reason']=='unsupported_fact_facet'
    assert result['model_selections']['admitted_point_ids']==[]
    assert len(result['admitted_facts'])==1,'Independent observed source point remains useful'
    assert result['model_narrative_verification']=='unverified'
    assert e['result'].report==before


@pytest.mark.parametrize('assertion',['selected','unknown'])
def test_real_point_selection_is_bounded_by_actual_source_admission(ai_env,assertion):
    e=prepare_case(ai_env,response_alter=select_response(assertion=assertion));result=ground(e)
    [decision]=result['model_selections']['decisions']
    assert decision['status']==('admitted_point' if assertion=='selected' else 'not_asserted')
    assert result['model_selections']['admitted_point_ids']==([e['evidence'].id] if assertion=='selected' else [])
    assert result['model_narrative_verification']=='unverified'


def test_actual_selection_does_not_restore_a_source_point_with_wrong_config(ai_env):
    e=prepare_case(ai_env,response_alter=select_response(),alter=lambda row:row.payload['diagnostic_origin']['collection'].update(config_revision=99))
    result=ground(e)
    assert result['admitted_facts']==[]
    [decision]=result['model_selections']['decisions']
    assert decision['reason']=='point_not_admitted' and decision['status']=='not_admitted'


def test_old_output_without_structured_selection_is_explicitly_not_provided(ai_env):
    e=prepare_case(ai_env);result=ground(e)
    assert result['model_selections']=={'status':'not_provided','decisions':[],'admitted_point_ids':[]}


def test_actual_request_explains_selection_without_appending_a_duplicate_fact_registry(ai_env):
    e=prepare_case(ai_env)
    prompt=e['requests'][0]['messages'][0]['content']
    assert 'fact_selections' in prompt and 'snapshot_point' in prompt
    assert 'fact_registry' not in json.dumps(e['requests'][0])


def test_selection_cannot_invent_references_values_or_server_authority():
    from platform_app.investigations import validate_report
    for entry,code in [
        ({'fact_id':'other','facet':'snapshot_point','assertion':'selected'},'unverified_evidence_citation'),
        ({'fact_id':'real','facet':'snapshot_point','assertion':'selected','row_count':999},'invalid_model_report'),
        ({'fact_id':'real','facet':'snapshot_point','assertion':'selected','verified':True},'invalid_model_report'),
    ]:
        value=report();value['fact_selections']=[entry]
        with pytest.raises(HTTPException) as error:validate_report(value,{'real'})
        assert error.value.detail['code']==code
