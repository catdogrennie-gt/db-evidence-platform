"""Admit only authorized, source-qualified points provided in the output request."""
from copy import deepcopy
from datetime import timedelta
import importlib
import importlib.util
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from tests.platform.test_ai_scope import ai_env
from tests.platform.test_ai_failures import prepared_run,provider_response


def prepare_case(e, *, count=1, truncated=False, alter=None, response_alter=None):
    from platform_app import models as m,zabbix_diagnostics as d
    from platform_app.common import iso
    from platform_app.evidence import digest
    from platform_app.investigations import run_investigation
    row=e['evidence']
    item={'hostid':'101','itemid':'900','key':'mysql.custom.query[DbA,,,locks]',
        'tags':[{'tag':'dbe.contract','value':'mysql-diagnostics-v1'},{'tag':'dbe.query','value':'locks'}]}
    pack={'producer_contract':d.CONTRACT,'query_name':'locks','rows':[{'waiting_trx_id':1,'blocking_trx_id':2}]*count,
        'returned_row_count':count+int(truncated),'snapshot_complete':not truncated,'snapshot_truncated':truncated}
    normalized=d.normalize(item,pack)
    origin=normalized['payload']['diagnostic_origin']
    origin['collection']={'source_id':'source-a','hostid':'101','itemid':'900',
        'mapping_version':1,'config_revision':1,'transport':'zabbix'}
    origin['observation']={'instance_id':row.instance_id,'source':'mysql.lock_waits','source_mode':'live','observed_at':iso(row.observed_at)}
    row.payload=normalized['payload'];row.source_mode='live';row.collector_version='zabbix-generic-1'
    row.coverage={**normalized['coverage'],'availability':'available','complete':False,
        'gaps':['history_truncated'],'source_id':'source-a','mapping_version':1,'config_revision':1}
    row.truncated=truncated
    run=e['db'].get(m.CollectionRun,row.run_id);run.collector='monitoring';run.config_revision=1
    if alter:alter(row)
    row.payload_sha256=digest(row.payload);e['db'].commit()
    investigation,_,lease,config=prepared_run(e);requests=[]
    def provider(_config,payload,_timeout):
        requests.append(deepcopy(payload));response=provider_response(row.id)
        if response_alter:response_alter(response,row.id)
        return response
    result=run_investigation(e['factory'],lease,config,model_client=provider)
    assert result.status=='completed',result.report
    e['db'].expire_all();e['db'].info['platform_config']=config
    e.update(result=e['db'].get(m.Investigation,result.id),config=config,requests=requests,
        call=e['db'].scalar(select(m.ModelCall).where(m.ModelCall.investigation_id==investigation.id)))
    return e


def ground(e):
    assert importlib.util.find_spec('platform_app.report_grounding') is not None,'source-qualified report admission missing'
    module=importlib.import_module('platform_app.report_grounding')
    return module.report_grounding(e['actor'],e['result'])


@pytest.mark.parametrize('count,truncated,scope',[(0,False,'exact_snapshot'),(1,False,'exact_snapshot'),(50,True,'at_least_projected_rows')])
def test_only_snapshot_point_values_gain_authority_and_history_gaps_are_not_zero(ai_env,count,truncated,scope):
    e=prepare_case(ai_env,count=count,truncated=truncated);before=deepcopy(e['result'].report)
    result=ground(e)
    assert result['version']=='report-grounding-v1' and result['status']=='point_facts_only'
    fact,=result['admitted_facts']
    assert fact['fact_id']==e['evidence'].id and fact['row_count']==count and fact['count_scope']==scope
    assert fact['kind']=='lock_snapshot_point' and fact['instance_id']==e['a'].id
    assert fact['source_identity']=={'source_id':'source-a','hostid':'101','itemid':'900','mapping_version':1,'config_revision':1}
    assert result['model_narrative_verification']=='unverified'
    assert set(result['not_established'])=={'continuous_duration','incident_count','release_mechanism','current_state'}
    assert result['verified_headline'] and e['result'].report==before


@pytest.mark.parametrize('change',['origin_missing','origin_version','producer_contract','producer_query','producer_count',
    'empty_incomplete','source_identity','item_identity','config_unknown','config_conflict','point_time','point_instance','point_mode','schema_unknown','collector_unknown'])
def test_fresh_hashes_cannot_upgrade_invalid_or_missing_source_proof(ai_env,change):
    def alter(row):
        origin=row.payload['diagnostic_origin']
        if change=='origin_missing':row.payload.pop('diagnostic_origin')
        if change=='origin_version':origin['version']='unknown'
        if change=='producer_contract':origin['producer']['contract']='unknown'
        if change=='producer_query':origin['producer']['query_name']='sessions'
        if change=='producer_count':origin['producer']['returned_row_count']=8
        if change=='empty_incomplete':origin['producer'].update(snapshot_complete=False,snapshot_truncated=True,returned_row_count=1)
        if change=='source_identity':origin['collection']['source_id']=None
        if change=='item_identity':origin['collection']['itemid']='901'
        if change=='config_unknown':origin['collection']['config_revision']=None
        if change=='config_conflict':origin['collection']['config_revision']=2
        if change=='point_time':origin['observation']['observed_at']='2020-01-01T00:00:00Z'
        if change=='point_instance':origin['observation']['instance_id']='foreign'
        if change=='point_mode':origin['observation']['source_mode']='replay'
        if change=='schema_unknown':row.schema_version='future'
        if change=='collector_unknown':row.collector_version='future'
    e=prepare_case(ai_env,count=0 if change=='empty_incomplete' else 1,alter=alter)
    result=ground(e)
    assert result['status']=='unverified' and result['admitted_facts']==[] and result['verified_headline'] is None
    assert result['reason_codes']


@pytest.mark.parametrize('change',['payload','member','root','expired','owner','audit','receipt','binding'])
def test_corruption_or_unconfirmed_delivery_denies_fact_admission(ai_env,change):
    from platform_app import models as m
    e=prepare_case(ai_env);db=e['db'];inv=e['result'];call=e['call']
    manifest=db.get(m.EvidenceManifest,inv.manifest_id)
    if change=='payload':e['evidence'].payload={'rows':[]}
    if change=='member':
        member=db.scalar(select(m.ManifestItem).where(m.ManifestItem.manifest_id==manifest.id,m.ManifestItem.evidence_id==e['evidence'].id));member.payload_sha256='f'*64
    if change=='root':manifest.manifest_sha256='f'*64
    if change=='expired':manifest.retain_until=inv.window_end-timedelta(days=1)
    if change=='owner':manifest.owner_id='foreign'
    if change=='audit':
        path=e['config'].private_dir/'model-audit'/inv.id/call.response['private_audit_ref'];path.write_text('{}',encoding='utf-8')
    if change=='receipt':call.response={**call.response,'request_delivery':{**call.response['request_delivery'],'status':'outcome_unknown'}}
    if change=='binding':call.response={**call.response,'report_output':{**call.response['report_output'],'normalized_report_sha256':'f'*64}}
    db.commit();result=ground(e)
    assert result['status']=='unverified' and result['admitted_facts']==[] and result['verified_headline'] is None


def test_mutable_coverage_cannot_rewrite_frozen_origin_identity(ai_env):
    e=prepare_case(ai_env);e['evidence'].coverage={'availability':'available','source_id':'other-source','mapping_version':99,'config_revision':99,'snapshot_complete':True}
    e['db'].commit();result=ground(e)
    fact,=result['admitted_facts']
    assert fact['source_identity']['source_id']=='source-a' and fact['source_identity']['config_revision']==1


def test_revocation_is_rechecked_before_counts_or_facts_are_returned(ai_env):
    e=prepare_case(ai_env);e['db'].delete(e['grant']);e['db'].commit()
    with pytest.raises(HTTPException):ground(e)


def test_revocation_during_private_audit_read_prevents_returning_facts(ai_env,monkeypatch):
    e=prepare_case(ai_env);module=importlib.import_module('platform_app.report_grounding')
    original=module.confirm_request_delivery
    def revoke(*args):
        e['db'].delete(e['grant']);e['db'].commit()
        return original(*args)
    monkeypatch.setattr(module,'confirm_request_delivery',revoke)
    with pytest.raises(HTTPException):ground(e)


def test_real_detail_projects_grounding_without_changing_persisted_model_report(ai_env):
    from platform_app.investigations import investigation_detail
    e=prepare_case(ai_env);before=deepcopy(e['result'].report)
    detail=investigation_detail(e['actor'],e['result'],include_audit=False)
    assert detail['report_grounding']['status']=='point_facts_only'
    assert detail['report_grounding']['admitted_facts'][0]['fact_id']==e['evidence'].id
    assert detail['report_presentation']['primary_basis']=='historical_source_points'
    assert detail['report_presentation']['primary_headline']==detail['report_grounding']['verified_headline']
    assert detail['report_presentation']['model_narrative_verification']=='unverified'
    assert detail['report']==before==e['result'].report
    assert 'private_audit_ref' not in json.dumps(detail)


def test_changed_report_during_audit_read_cannot_reuse_old_candidate_fact_binding(ai_env,monkeypatch):
    from pathlib import Path
    e=prepare_case(ai_env);original=Path.read_bytes;changed=False
    def change_report(path):
        nonlocal changed
        raw=original(path)
        if not changed and path.name==e['call'].response['private_audit_ref']:
            changed=True
            e['result'].report={**e['result'].report,'unknowns':['Changed while facts were being checked']}
            e['db'].commit()
        return raw
    monkeypatch.setattr(Path,'read_bytes',change_report)
    result=ground(e)
    assert changed and result['status']=='unverified'
    assert result['admitted_facts']==[] and result['verified_headline'] is None
    assert 'report_changed_during_validation' in result['reason_codes']


def test_frozen_point_omitted_from_actual_request_cannot_be_admitted(ai_env,monkeypatch):
    from platform_app import model_context
    original=model_context.compact_initial
    def omit_count(bundle):
        result=original(bundle)
        for item in result['evidence']:item.get('data',{}).pop('row_count',None)
        return result
    monkeypatch.setattr(model_context,'compact_initial',omit_count)
    e=prepare_case(ai_env);result=ground(e)
    assert result['status']=='unverified' and result['admitted_facts']==[]
    assert 'required_point_fields_not_delivered' in result['reason_codes']


def test_mutated_investigation_and_manifest_windows_cannot_rebind_an_old_request(ai_env):
    from platform_app import models as m
    e=prepare_case(ai_env);manifest=e['db'].get(m.EvidenceManifest,e['result'].manifest_id)
    e['result'].window_start-=timedelta(minutes=1);manifest.window_start=e['result'].window_start
    e['db'].commit()
    result=ground(e)
    assert result['status']=='unverified' and result['admitted_facts']==[]


@pytest.mark.parametrize('counterexample',['same_time_conflict','later_empty','other_item'])
def test_undelivered_frozen_points_only_veto_conflicts_never_supply_new_facts(ai_env,monkeypatch,counterexample):
    from platform_app import models as m,model_context
    from platform_app.evidence import digest
    from platform_app.common import iso
    def add_counterexample(row):
        payload=deepcopy(row.payload);payload['rows']=[]
        origin=payload['diagnostic_origin'];origin['producer'].update(returned_row_count=0,snapshot_row_count=0)
        stamp=row.observed_at+(timedelta(seconds=1) if counterexample=='later_empty' else timedelta())
        origin['observation']['observed_at']=iso(stamp)
        if counterexample=='other_item':
            payload['item']['itemid']='901';origin['collection']['itemid']='901'
        other=m.Evidence(instance_id=row.instance_id,run_id=row.run_id,source=row.source,
            source_mode='live',observed_at=stamp,collector_version='zabbix-generic-1',
            payload=payload,payload_sha256=digest(payload),coverage=deepcopy(row.coverage),truncated=False)
        ai_env['db'].add(other);ai_env['db'].flush();ai_env['hidden_id']=other.id
    original=model_context.compact_initial
    def omit_counterexample(bundle):
        result=original(bundle)
        result['evidence']=[item for item in result['evidence'] if item['evidence_id']!=ai_env['hidden_id']]
        return result
    monkeypatch.setattr(model_context,'compact_initial',omit_counterexample)
    e=prepare_case(ai_env,alter=add_counterexample);result=ground(e)
    assert all(fact['fact_id']!=e['hidden_id'] for fact in result['admitted_facts'])
    if counterexample=='same_time_conflict':
        assert result['admitted_facts']==[] and result['verified_headline'] is None
        assert 'conflicting_same_time_points' in result['reason_codes']
    else:
        assert [fact['fact_id']for fact in result['admitted_facts']]==[e['evidence'].id]
