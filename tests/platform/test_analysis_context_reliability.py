"""Regression coverage for real multi-source report failures, without live AI."""
import json
from datetime import timedelta

import pytest

from platform_app import investigations as inv, models as m
from platform_app.evidence import create_manifest, digest
from platform_app.model_context import compact_initial
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_ai_failures import prepared_run, provider_response
from tests.platform.test_model_output_contract import numeric_evidence, response


def test_context_limits_are_explicit_bounded_and_legacy_compatible():
    assert inv.context_limits({}) == (24000, 32000)
    assert inv.context_limits({'context_input_bytes':48000,'total_token_budget':96000}) == (48000,96000)
    for value in (True, '48000', -1, 64001):
        with pytest.raises(Exception):
            inv.context_limits({'context_input_bytes':value})


def test_explicit_input_budget_is_used_by_the_actual_request(ai_env,monkeypatch):
    ref=numeric_evidence(ai_env);_,_,lease,config=prepared_run(ai_env)
    config.model.update(context_input_bytes=48000,total_token_budget=96000)
    monkeypatch.setattr(inv,'SYSTEM_PROMPT','x'*26000)
    seen=[]
    def provider(_config,payload,_timeout):
        size=len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())+512
        assert 24000<size<=48000 and size+payload['max_tokens']<=96000
        seen.append(size);return response(ref)
    result=inv.run_investigation(ai_env['factory'],lease,config,model_client=provider)
    assert result.status=='completed' and len(seen)==1,result.report


def test_one_schema_retry_preserves_strict_metric_identity_and_uses_no_tools(ai_env):
    ref=numeric_evidence(ai_env)
    _,_,lease,config=prepared_run(ai_env)
    calls=[]
    finding={'intent':'review_issue','category':'correlation','instance_id':ai_env['a'].id,
             'dimension':{'kind':'metric','value':ref},'statement':'Review observed latency','evidence_ids':[ref]}
    def provider(_config,payload,_timeout):
        calls.append(payload)
        if len(calls)==1:return response(ref,finding=finding)
        assert payload['tool_choice']=='none' and payload['tools']==[]
        assert 'findings.0' in payload['messages'][-1]['content']
        assert '真实itemid' in payload['messages'][-1]['content']
        return response(ref,finding={**finding,'dimension':{'kind':'metric','value':'51543'}})
    result=inv.run_investigation(ai_env['factory'],lease,config,model_client=provider)
    assert result.status=='completed',result.report
    assert len(calls)==2
    assert result.report['findings'][0]['dimension']['value']=='51543'


def test_failed_schema_retry_stays_failed_after_two_total_calls(ai_env):
    ref=numeric_evidence(ai_env);_,_,lease,config=prepared_run(ai_env);calls=[]
    finding={'intent':'review_issue','category':'correlation','instance_id':ai_env['a'].id,
             'dimension':{'kind':'metric','value':ref},'statement':'Review','evidence_ids':[ref]}
    def provider(*args):calls.append(1);return response(ref,finding=finding)
    result=inv.run_investigation(ai_env['factory'],lease,config,model_client=provider)
    assert result.status=='failed' and result.report['error_code']=='invalid_model_report'
    assert len(calls)==2


def test_unverified_citation_never_receives_a_schema_retry(ai_env):
    ref=numeric_evidence(ai_env);_,_,lease,config=prepared_run(ai_env);calls=[]
    def provider(*args):calls.append(1);return response('not-supplied')
    result=inv.run_investigation(ai_env['factory'],lease,config,model_client=provider)
    assert result.status=='failed' and result.report['error_code']=='unverified_evidence_citation'
    assert len(calls)==1


def test_schema_retry_cannot_execute_tools_or_start_a_third_call(ai_env,monkeypatch):
    ref=numeric_evidence(ai_env);_,_,lease,config=prepared_run(ai_env);calls=[];tools=[]
    finding={'intent':'review_issue','category':'correlation','instance_id':ai_env['a'].id,
             'dimension':{'kind':'metric','value':ref},'statement':'Review','evidence_ids':[ref]}
    monkeypatch.setattr(inv.ToolBroker,'call',lambda *args:tools.append(args))
    def provider(*args):
        calls.append(1)
        if len(calls)==1:return response(ref,finding=finding)
        value=response(ref);value['choices'][0]['message']['tool_calls']=[{
            'id':'unexpected','function':{'name':'get_evidence','arguments':'{}'}}]
        return value
    result=inv.run_investigation(ai_env['factory'],lease,config,model_client=provider)
    assert result.status=='failed' and result.report['error_code']=='unexpected_model_tool_call'
    assert len(calls)==2 and tools==[]


def test_schema_retry_does_not_repair_unverified_citations(ai_env):
    ref=numeric_evidence(ai_env);_,_,lease,config=prepared_run(ai_env);calls=[]
    def provider(*args):
        calls.append(1)
        value=response(ref if len(calls)==1 else 'unprovided-reference')
        if len(calls)==1:
            body=json.loads(value['choices'][0]['message']['content'])
            body['executive_summary']['conclusion']='wrong shape'
            value['choices'][0]['message']['content']=json.dumps(body)
        return value
    result=inv.run_investigation(ai_env['factory'],lease,config,model_client=provider)
    assert result.status=='failed' and result.report['error_code']=='unverified_evidence_citation'
    assert len(calls)==2


@pytest.mark.parametrize('blocker',['permission','total_budget','deadline'])
def test_schema_retry_respects_revocation_deadline_and_total_budget(ai_env,blocker):
    from sqlalchemy import select
    from platform_app.db import utcnow
    ref=numeric_evidence(ai_env);_,_,lease,config=prepared_run(ai_env);calls=[]
    def provider(*args):
        calls.append(1);value=response(ref)
        body=json.loads(value['choices'][0]['message']['content']);body['executive_summary']['conclusion']='wrong shape'
        value['choices'][0]['message']['content']=json.dumps(body)
        if blocker=='total_budget':value['usage']={'prompt_tokens':31000,'completion_tokens':1000}
        else:
            with ai_env['factory'].begin()as db:
                if blocker=='permission':
                    role=db.scalar(select(m.Role).where(m.Role.name=='ai-test'));role.permissions=[]
                else:db.get(m.Task,lease.id).deadline_at=utcnow()-timedelta(seconds=1)
        return value
    result=inv.run_investigation(ai_env['factory'],lease,config,model_client=provider)
    assert result.status!='completed' and len(calls)==1,result.report


def test_raw_diagnostic_duplicate_cannot_evict_typed_sources(ai_env):
    e=ai_env;db=e['db'];old=e['evidence'];old.source='mysql.transactions';old.payload={'rows':[]};old.payload_sha256=digest(old.payload)
    for index,(query,source) in enumerate([('statements','mysql.statement_statistics'),('sessions','mysql.sessions'),('locks','mysql.lock_waits')]):
        item={'hostid':'101','itemid':str(400+index),'key':f'mysql.custom.query[DbA,,,"{query}"]',
              'value_kind':'text','value_type':4,'name':'Diagnostic snapshot',
              'tags':[{'tag':'dbe.contract','value':'mysql-diagnostics-v1'},{'tag':'dbe.query','value':query}]}
        stamp=e['start']+timedelta(minutes=2)
        coverage={'source_id':'same-source','snapshot_contract':'db-evidence-mysql-diagnostics-v1','availability':'available','complete':True}
        raw={'item':item,'rows':[{'time':str(stamp),'value':'secret raw text'}]}
        typed={'item':item,'rows':[{'digest':format(i,'064x'),'COUNT_STAR':10,'SUM_TIMER_WAIT':200000,'SUM_ROWS_EXAMINED':200}for i in range(20)]}
        for name,payload in [('zabbix.item.text:'+str(index),raw),(source,typed)]:
            db.add(m.Evidence(instance_id=e['a'].id,run_id=old.run_id,source=name,source_mode='live',observed_at=stamp,payload=payload,payload_sha256=digest(payload),coverage=coverage))
    db.flush();manifest=create_manifest(db,'investigation',e['investigation'].id,[e['a'].id],e['start'],e['end'])
    bundle,_,_,_=inv._bundle(db,e['investigation'],manifest)
    sources={x['source']for x in bundle['evidence']}
    assert {'mysql.statement_statistics','mysql.sessions','mysql.lock_waits'}<=sources
    assert not any(x.startswith('zabbix.item.text:')for x in sources)
    assert bundle['projection']['duplicate_raw_diagnostics_omitted']==3
    stats=next(x for x in bundle['evidence']if x['source']=='mysql.statement_statistics')
    assert stats['data']['row_count']==20 and stats['data']['rows_omitted']==16
    assert 'secret raw text' not in json.dumps(compact_initial(bundle))


@pytest.mark.parametrize('mismatch',['contract','definition','source_id','hostid','itemid','time'])
def test_unmatched_or_unverified_text_is_not_deduplicated(ai_env,mismatch):
    import copy
    e=ai_env;db=e['db'];old=e['evidence']
    item={'hostid':'101','itemid':'400','key':'mysql.custom.query[DbA,,,"sessions"]',
          'value_kind':'text','value_type':4,'name':'Diagnostic snapshot',
          'tags':[{'tag':'dbe.contract','value':'mysql-diagnostics-v1'},{'tag':'dbe.query','value':'sessions'}]}
    coverage={'source_id':'same-source','snapshot_contract':'db-evidence-mysql-diagnostics-v1','availability':'available'}
    raw_item=copy.deepcopy(item);raw_coverage=copy.deepcopy(coverage);stamp=e['start']+timedelta(minutes=2)
    if mismatch=='contract':coverage['snapshot_contract']='db-evidence-application-events-v1'
    elif mismatch=='definition':raw_item['tags']=[]
    elif mismatch=='source_id':raw_coverage['source_id']='different-source'
    elif mismatch in ('hostid','itemid'):raw_item[mismatch]='999'
    for source,meta,cov,observed in [('mysql.sessions',item,coverage,stamp),
            ('zabbix.item.text:raw',raw_item,raw_coverage,stamp+timedelta(seconds=1)if mismatch=='time'else stamp)]:
        payload={'item':meta,'rows':[]}
        db.add(m.Evidence(instance_id=e['a'].id,run_id=old.run_id,source=source,source_mode='live',observed_at=observed,payload=payload,payload_sha256=digest(payload),coverage=cov))
    db.flush();manifest=create_manifest(db,'investigation',e['investigation'].id,[e['a'].id],e['start'],e['end'])
    bundle,_,_,_=inv._bundle(db,e['investigation'],manifest)
    assert any(x['source']=='zabbix.item.text:raw'for x in bundle['evidence'])
    assert bundle['projection']['duplicate_raw_diagnostics_omitted']==0


def test_large_snapshot_budget_does_not_discard_non_zabbix_numeric_window(ai_env):
    from platform_app.common import iso
    e=ai_env;db=e['db'];old=e['evidence']
    for index in range(8):
        payload={'item':{'hostid':'101','itemid':str(index)},'rows':[
            {'digest':format(i,'064x'),'COUNT_STAR':10,'SUM_TIMER_WAIT':200000,'SUM_ROWS_EXAMINED':200,
             'SUM_ROWS_SENT':100,'SUM_NO_INDEX_USED':10,'SUM_LOCK_TIME':100}for i in range(20)]}
        db.add(m.Evidence(instance_id=e['a'].id,run_id=old.run_id,source='mysql.statement_statistics',source_mode='live',observed_at=e['start']+timedelta(minutes=2),payload=payload,payload_sha256=digest(payload),coverage={'availability':'available'}))
    db.flush();manifest=create_manifest(db,'investigation',e['investigation'].id,[e['a'].id],e['start'],e['end'])
    metric=m.ManifestItem(manifest_id=manifest.id,instance_id=e['a'].id,item_type='prometheus_window',
        payload={'template':'transactions','owner_collector':'runtime','unit':'transactions','source':'prometheus',
                 'points':[{'time':iso(e['start']+timedelta(minutes=2)),'value':10}], 'coverage':{'complete':True}})
    metric.payload_sha256=digest(metric.payload)
    db.add(metric);db.flush()
    bundle,_,_,_=inv._bundle(db,e['investigation'],manifest)
    snapshots=[x for x in bundle['evidence']if x['source']=='mysql.statement_statistics']
    assert len(json.dumps(snapshots).encode())>6500
    assert any(x['evidence_id']=='manifest:'+metric.id for x in bundle['evidence'])
