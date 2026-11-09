import pytest
from fastapi import HTTPException
from tests.platform.test_ai_scope import ai_env, broker


def prepared_run(env, mode="live_redacted", **options):
    from types import SimpleNamespace
    from platform_app.investigations import create_investigation
    from platform_app.scheduling import claim

    row, task = create_investigation(
        env["actor"],
        [env["a"].id],
        env["start"],
        env["end"],
        "why lock? secret-unlabelled-personal-data",
        mode=mode,
        options={"external_confirmed": True, "max_cost_usd": 0.05, **options},
    )
    env["db"].commit()
    config = SimpleNamespace(
        private_dir=env["tmp_path"],
        model={
            "provider": "test",
            "model": "test",
            "max_calls": 4,
            "max_tokens": 2000,
            "pricing": {"input_usd_per_million": 0.3, "output_usd_per_million": 1.2},
        },
    )
    return row, task, claim(env["factory"], "test-ai-worker", "ai"), config


def provider_response(evidence_id, **extra):
    import json
    from tests.platform.test_report_summary import summary

    return {
        "id": "test-response",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "facts": [
                                {
                                    "statement": "a lock edge was observed",
                                    "evidence_ids": [evidence_id],
                                }
                            ],
                            "hypotheses": [],
                            "refutations": [],
                            "unknowns": ["business impact not measured"],
                            "next_readonly_checks": [],
                            "executive_summary": summary(evidence_id),
                        }
                    ),
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 100},
        **extra,
    }


def test_external_policy_denial_never_calls_provider(ai_env):
    from platform_app.investigations import require_investigation_access

    ai_env["a"].model_policy = "rules_only"
    ai_env["db"].commit()
    with pytest.raises(HTTPException):
        require_investigation_access(ai_env["actor"], [ai_env["a"].id], "live_redacted")


def test_tool_call_budget_exhaustion(ai_env):
    tool = broker(ai_env, max_calls=1)
    tool.call("get_lock_graph", {"instance_id": ai_env["a"].id})
    with pytest.raises(HTTPException):
        tool.call("get_lock_graph", {"instance_id": ai_env["a"].id})


def test_hallucinated_evidence_is_not_accepted_as_fact():
    from platform_app.investigations import validate_report

    response = {
        "facts": [{"statement": "root cause proved", "evidence_ids": ["fabricated"]}],
        "hypotheses": [],
        "refutations": [],
        "unknowns": [],
        "next_readonly_checks": [],
    }
    with pytest.raises(HTTPException):
        validate_report(response, {"real-evidence"})


def test_cancel_and_permission_revoke_stop_next_provider_call(ai_env):
    from platform_app.investigations import check_running
    from platform_app.models import Task

    task = Task(
        type="investigation",
        queue="ai",
        actor_id=ai_env["actor"].user_id,
        status="running",
        cancel_requested=True,
    )
    ai_env["db"].add(task)
    ai_env["db"].commit()
    with pytest.raises(HTTPException):
        check_running(ai_env["actor"], ai_env["investigation"], task.id, "live_redacted")


def test_rules_investigation_finishes_without_external_call(ai_env):
    from platform_app.investigations import run_investigation

    row, task, lease, config = prepared_run(ai_env, mode="rules_only")

    def forbidden(*_):
        pytest.fail("rules_only must never invoke a provider")

    result = run_investigation(ai_env["factory"], lease, config, model_client=forbidden)
    assert result.status == "completed"
    assert result.conclusion_type == "rule"
    assert "executive_summary" not in result.report
    assert result.report["facts"][0]["evidence_ids"] == [ai_env["evidence"].id]


def test_live_report_is_persisted_with_real_call_audit_and_projected_input(ai_env):
    import json
    from sqlalchemy import select
    from platform_app.investigations import run_investigation
    from platform_app.models import ModelCall, Task

    row, task, lease, config = prepared_run(ai_env)
    captured = []

    def provider(config, payload, timeout):
        captured.append(json.dumps(payload))
        names = {t["function"]["name"] for t in payload["tools"]}
        assert "get_lock_graph" in names
        assert "get_statement_digests" not in names and "search_log_events" not in names
        return provider_response(ai_env["evidence"].id)

    result = run_investigation(ai_env["factory"], lease, config, model_client=provider)
    assert result.status == "completed" and result.conclusion_type == "live_model"
    assert len(captured) == 1
    assert result.report["executive_summary"]["joint_root_cause"]["status"] == "insufficient_evidence"
    assert result.report["prompt_version"] == "evidence-investigation-v3.5"
    assert all(
        s not in captured[0]
        for s in (
            "DO-NOT-EXFILTRATE",
            "private_table",
            "personal@example.invalid",
            "secret-unlabelled-personal-data",
        )
    )
    with ai_env["factory"]() as db:
        call = db.scalar(select(ModelCall).where(ModelCall.investigation_id == row.id))
        assert call.status == "succeeded" and call.input_tokens == 100 and call.cost > 0
        assert db.get(Task, task.id).result["investigation_id"] == row.id


def test_provider_timeout_is_unknown_charge_and_is_never_automatically_retried(ai_env):
    from sqlalchemy import select
    from platform_app.investigations import run_investigation, ModelTransportError
    from platform_app.models import ModelCall

    row, _, lease, config = prepared_run(ai_env)
    calls = []

    def timeout(*_):
        calls.append(1)
        raise ModelTransportError("model_call_outcome_unknown", True)

    result = run_investigation(ai_env["factory"], lease, config, model_client=timeout)
    assert result.status == "failed" and len(calls) == 1
    with ai_env["factory"]() as db:
        call = db.scalar(select(ModelCall).where(ModelCall.investigation_id == row.id))
        assert call.status == "outcome_unknown" and call.cost is None


@pytest.mark.parametrize("terminal", ["cancelled", "expired"])
def test_inflight_terminal_task_never_leaves_analyzing_domain_or_loses_unknown_cost(
    ai_env, terminal
):
    from sqlalchemy import select
    from platform_app.db import utcnow
    from platform_app.investigations import run_investigation, ModelTransportError
    from platform_app.models import ModelCall, Task, Investigation
    from datetime import timedelta

    row, task, lease, config = prepared_run(ai_env)
    calls = []

    def interrupted(*args):
        calls.append(1)
        with ai_env["factory"].begin() as db:
            current = db.get(Task, task.id)
            if terminal == "cancelled":
                current.status = "cancelled"
                current.cancel_requested = True
            else:
                current.deadline_at = utcnow() - timedelta(seconds=1)
        raise ModelTransportError("model_call_outcome_unknown", True)

    run_investigation(ai_env["factory"], lease, config, model_client=interrupted)
    with ai_env["factory"]() as db:
        assert db.get(Task, task.id).status == terminal
        assert db.get(Investigation, row.id).status == terminal
        call = db.scalar(select(ModelCall).where(ModelCall.investigation_id == row.id))
        assert call.status == "outcome_unknown" and call.cost is None
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["revoke", "cancel", "policy", "profile"])
def test_permission_cancel_and_policy_change_during_provider_call_stop_tools(ai_env, change):
    from sqlalchemy import delete, select
    from platform_app.investigations import run_investigation
    from platform_app.models import UserGrant, Task, Instance, ToolCall, PlatformSetting

    row, task, lease, config = prepared_run(ai_env)
    calls = []

    def provider(*_):
        calls.append(1)
        with ai_env["factory"]() as db:
            if change == "revoke":
                db.execute(delete(UserGrant).where(UserGrant.id == ai_env["grant"].id))
            elif change == "cancel":
                db.get(Task, task.id).cancel_requested = True
            elif change == "policy":
                db.get(Instance, ai_env["a"].id).policy_version += 1
            else:
                db.add(
                    PlatformSetting(
                        key="model_profile:default", value={"provider": "test", "model": "test"}
                    )
                )
            db.commit()
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call1",
                                "type": "function",
                                "function": {
                                    "name": "get_lock_graph",
                                    "arguments": '{"instance_id":"' + ai_env["a"].id + '"}',
                                },
                            }
                        ]
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        }

    result = run_investigation(ai_env["factory"], lease, config, model_client=provider)
    assert result.status in ("failed", "cancelled") and len(calls) == 1
    with ai_env["factory"]() as db:
        assert list(db.scalars(select(ToolCall).where(ToolCall.investigation_id == row.id))) == []


def test_prospective_cost_budget_prevents_network_call(ai_env):
    from platform_app.investigations import run_investigation

    row, _, lease, config = prepared_run(ai_env, max_cost_usd=0.000001)

    def forbidden(*_):
        pytest.fail("over-budget task must not invoke provider")

    result = run_investigation(ai_env["factory"], lease, config, model_client=forbidden)
    assert (
        result.status == "failed" and result.report["error_code"] == "model_cost_budget_exhausted"
    )


def test_denied_model_tool_request_remains_in_audit_after_task_failure(ai_env):
    from sqlalchemy import select
    from platform_app.investigations import run_investigation
    from platform_app.models import ToolCall

    row, _, lease, config = prepared_run(ai_env)

    def provider(*_):
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call1",
                                "type": "function",
                                "function": {
                                    "name": "run_shell",
                                    "arguments": '{"command":"leak-private-text"}',
                                },
                            }
                        ]
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        }

    result = run_investigation(ai_env["factory"], lease, config, model_client=provider)
    assert result.status == "failed"
    with ai_env["factory"]() as db:
        audit = db.scalar(select(ToolCall).where(ToolCall.investigation_id == row.id))
        assert audit is not None and audit.status == "denied"
        assert audit.parameters == {}


def test_investigation_idempotency_reuses_task_after_completion_and_conflicts_on_new_input(ai_env):
    from platform_app.investigations import create_investigation

    args = (ai_env["actor"], [ai_env["a"].id], ai_env["start"], ai_env["end"], "same question")
    first, task = create_investigation(*args, idempotency_key="stable-key")
    ai_env["db"].commit()
    task.status = "succeeded"
    ai_env["db"].commit()
    second, again = create_investigation(*args, idempotency_key="stable-key")
    assert second.id == first.id and again.id == task.id
    with pytest.raises(HTTPException) as out:
        create_investigation(*args[:-1], "different question", idempotency_key="stable-key")
    assert out.value.status_code == 409


def test_empty_external_source_allowlist_denies_and_system_instance_lists_intersect(ai_env):
    from platform_app.investigations import preview_investigation
    from platform_app.models import PlatformSetting

    setting = PlatformSetting(
        key="model_policy:instance:" + ai_env["a"].id,
        value={"mode": "live_redacted", "allowed_sources": []},
    )
    ai_env["db"].add(setting)
    ai_env["db"].commit()
    with pytest.raises(HTTPException):
        preview_investigation(
            ai_env["actor"],
            [ai_env["a"].id],
            ai_env["start"],
            ai_env["end"],
            "locks",
            "live_redacted",
        )
    setting.value = {"mode": "live_redacted", "allowed_sources": ["identity_status"]}
    ai_env["db"].commit()
    preview = preview_investigation(
        ai_env["actor"], [ai_env["a"].id], ai_env["start"], ai_env["end"], "locks", "live_redacted"
    )
    assert preview["evidence"] == []  # The fixture's runtime evidence is excluded.


def test_tool_context_pressure_finalizes_without_raising_hard_token_cap(ai_env, monkeypatch):
    import json
    from platform_app.investigations import run_investigation
    from platform_app.tools import ToolBroker

    row, task, lease, config = prepared_run(ai_env)

    def bounded_result(*args):
        return {
            "items": [
                {
                    "evidence_id": ai_env["evidence"].id,
                    "data": {
                        "rows": [{"waiting_trx_id": i, "blocking_trx_id": i + 1} for i in range(300)]
                    },
                }
            ],
            "coverage": {"complete": True, "gaps": []},
        }

    monkeypatch.setattr(ToolBroker, "call", bounded_result)
    calls = []

    def provider(config, payload, timeout):
        calls.append(payload)
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": f"call{i}",
                                    "type": "function",
                                    "function": {
                                        "name": "get_lock_graph",
                                        "arguments": json.dumps({"instance_id": ai_env["a"].id, "fixture_pressure": "x" * 930}),
                                    },
                                }
                                for i in range(6)
                            ]
                        }
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100},
            }
        assert payload["tool_choice"] == "none"
        assert (
            len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) + 512
            <= 24000
        )
        return provider_response(ai_env["evidence"].id)

    result = run_investigation(ai_env["factory"], lease, config, model_client=provider)
    assert result.status == "completed" and len(calls) == 2, (result.report, [(c["tool_choice"], len(json.dumps(c,ensure_ascii=False,separators=(",",":")).encode())+512) for c in calls])


@pytest.mark.parametrize('configured,expected', [(1000,1000),(2000,2000),(3000,3000),(5000,3000),(None,3000)])
def test_deepseek_final_json_budget_disables_default_thinking(ai_env,configured,expected):
    from platform_app.investigations import run_investigation

    row, task, lease, config = prepared_run(ai_env)
    config.model["provider"] = "DeepSeek"
    if configured is None: config.model.pop("max_tokens")
    else: config.model["max_tokens"] = configured

    def provider(config, payload, timeout):
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["max_tokens"] == expected
        return provider_response(ai_env["evidence"].id)

    result = run_investigation(ai_env["factory"], lease, config, model_client=provider)
    assert result.status == "completed"


@pytest.mark.usefixtures('legacy_collection_compatibility')
@pytest.mark.parametrize('triggered',[True,False])
def test_lock_observation_and_counterevidence_survive_initial_context_pressure(ai_env,monkeypatch,triggered):
    """A real trigger ID is not permission to cite an omitted observation."""
    import json
    from datetime import timedelta
    from platform_app import evidence as evidence_module
    from platform_app import models as m
    from platform_app.common import iso
    from platform_app.investigations import run_investigation
    from tests.platform.test_intelligence import policy_env
    from platform_app.tools import DIGEST_FIELDS

    e=ai_env;p=policy_env(e);p.mode='live_redacted';p.external_confirmed=True;e['db'].commit()
    for source,payload in [
        ('mysql.global_status',{'rows':[{'VARIABLE_NAME':'Threads_connected','VARIABLE_VALUE':5}]}),
        ('mysql.statement_digest',{'rows':[dict({key:12345 for key in DIGEST_FIELDS},DIGEST=str(index)*64,DIGEST_TEXT='SELECT * FROM example WHERE id = ?') for index in range(8)]}),
    ]:
        e['db'].add(m.Evidence(instance_id=e['a'].id,run_id=e['evidence'].run_id,source=source,observed_at=e['end']-timedelta(seconds=1),coverage={'availability':'available','complete':True},payload=payload,payload_sha256='a'*64))
    for stamp in (e['start']+timedelta(seconds=1),e['end']-timedelta(seconds=1)):
        e['db'].add(m.Evidence(instance_id=e['a'].id,run_id=e['evidence'].run_id,source='mysql.lock_waits',observed_at=stamp,coverage={'availability':'available','complete':True},payload={'rows':[]},payload_sha256='a'*64))
    e['db'].commit()
    marker={'policy_id':p.id,'version':p.version,'signals':[{'category':'lock_wait','kind':'current','instance_id':e['a'].id,'evidence_ids':[e['evidence'].id],'calculation':{'row_count':1}}]}
    row,task,lease,config=prepared_run(e,**({'inspection':marker} if triggered else {}))
    row.question='automatic comprehensive inspection';e['db'].commit()
    original=evidence_module.create_manifest
    def with_frozen_metrics(*args,**kwargs):
        manifest=original(*args,**kwargs)
        db=args[0]
        for index in range(100):
            db.add(m.ManifestItem(manifest_id=manifest.id,instance_id=e['a'].id,item_type='time_series_window',payload={'template':'zabbix_item_numeric','item':{'hostid':str(101+index%2),'itemid':str(index),'name':'Measured statistic '+str(index),'key':'measured.statistic.'+str(index)},'owner_collector':'host','source':'zabbix_history','unit':'%','points':[{'time':iso(e['start']),'value':10+index,'validity':'valid'}],'coverage':{'complete':True}},payload_sha256='a'*64))
        db.flush();return manifest
    monkeypatch.setattr(evidence_module,'create_manifest',with_frozen_metrics)
    calls=[]
    def provider(config,payload,timeout):
        calls.append(payload)
        return provider_response(e['evidence'].id)
    result=run_investigation(e['factory'],lease,config,model_client=provider)
    assert result.status=='completed' and len(calls)==1, result.report
    payload=calls[0];initial=json.loads(payload['messages'][1]['content'])
    actual={item['evidence_id']:item for item in initial['evidence']}
    signal_refs={ref for signal in initial['inspection_signals'] for ref in signal['evidence_ids']}
    assert signal_refs.issubset(actual)
    assert initial['numeric_series']['omitted_count'] > 0
    assert e['evidence'].id in actual and actual[e['evidence'].id]['data']['rows'][0]['blocking_trx_id']==13
    assert any(item['source']=='mysql.lock_waits' and item['data']['row_count']==0 and item['observed_at']>actual[e['evidence'].id]['observed_at'] for item in actual.values())
    assert len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())+512<=24000

@pytest.mark.parametrize('invalid', ['missing_summary','unknown_counterevidence','unapproved_change'])
def test_invalid_new_live_report_fails_without_rule_summary_fallback(ai_env, invalid):
    import json
    from platform_app.investigations import run_investigation
    row, _, lease, config = prepared_run(ai_env)
    def provider(*_):
        response = provider_response(ai_env['evidence'].id)
        data = json.loads(response['choices'][0]['message']['content'])
        if invalid == 'missing_summary': data.pop('executive_summary')
        elif invalid == 'unknown_counterevidence': data['executive_summary']['joint_root_cause']['counterevidence_ids']=['unauthorized']
        else: data['executive_summary']['recommended_actions'][0]['kind']='manual_change'
        response['choices'][0]['message']['content']=json.dumps(data)
        return response
    result = run_investigation(ai_env['factory'],lease,config,model_client=provider)
    assert result.status == 'failed'
    assert result.report['error_code'] in {'invalid_model_report','unverified_evidence_citation'}
    assert 'executive_summary' not in result.report and 'facts' not in result.report


def test_missing_provider_usage_stops_before_second_call(ai_env):
    import json
    from platform_app.investigations import run_investigation
    row, _, lease, config = prepared_run(ai_env)
    calls=[]
    def provider(*_):
        calls.append(1)
        return {'choices':[{'message':{'tool_calls':[{'id':'unknown-usage','type':'function','function':{'name':'get_lock_graph','arguments':json.dumps({'instance_id':ai_env['a'].id})}}]}}]}
    result=run_investigation(ai_env['factory'],lease,config,model_client=provider)
    assert result.status=='failed' and result.report['error_code']=='model_usage_unknown'
    assert len(calls)==1

@pytest.mark.parametrize('target', [24000,24001])
def test_round_byte_boundary_includes_512_overhead(ai_env,monkeypatch,target):
    import json
    from platform_app import investigations as inv, model_context
    from tests.platform.test_report_summary import summary
    row, _, lease, config=prepared_run(ai_env)
    config.model['max_calls']=1
    initial={'coverage':{'retained_evidence_count':0},'evidence':[],'numeric_series':{'rows':[]},'inspection_signals':[]}
    monkeypatch.setattr(model_context,'compact_initial',lambda _: __import__('copy').deepcopy(initial))
    monkeypatch.setattr(inv,'tool_definitions',lambda _: [])
    messages=[{'role':'system','content':''},{'role':'user','content':json.dumps(initial,ensure_ascii=False,separators=(',',':'))}]
    probe={'model':'test','messages':messages,'max_tokens':2000,'temperature':0.1,'tools':[],'tool_choice':'none'}
    base=len(json.dumps(probe,ensure_ascii=False,separators=(',',':')).encode())+512
    monkeypatch.setattr(inv,'SYSTEM_PROMPT','x'*(target-base))
    calls=[]
    def provider(config,payload,timeout):
        calls.append(payload)
        assert len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())+512==24000
        data={'facts':[],'hypotheses':[],'refutations':[],'unknowns':['no measurements'],'next_readonly_checks':[]}
        data['executive_summary']=summary()
        for key in ['phenomenon','conclusion']:
            data['executive_summary'][key].update(status='insufficient_evidence',statement='No frozen measurement was supplied',evidence_ids=[])
        data['executive_summary']['surface_causes']=[]
        data['executive_summary']['recommended_actions']=[]
        return {'choices':[{'message':{'content':json.dumps(data)}}], 'usage':{'prompt_tokens':100,'completion_tokens':100}}
    result=inv.run_investigation(ai_env['factory'],lease,config,model_client=provider)
    if target==24000:
        assert result.status=='partial' and len(calls)==1, result.report
    else:
        assert result.status=='failed' and result.report['error_code']=='model_token_budget_exhausted'
        assert not calls


def test_cumulative_32000_budget_still_blocks_followup(ai_env):
    import json
    from platform_app.investigations import run_investigation
    row, _, lease, config=prepared_run(ai_env)
    calls=[]
    def provider(*_):
        calls.append(1)
        return {'choices':[{'message':{'tool_calls':[{'id':'large-usage','type':'function','function':{'name':'get_lock_graph','arguments':json.dumps({'instance_id':ai_env['a'].id})}}]}}], 'usage':{'prompt_tokens':31000,'completion_tokens':0}}
    result=run_investigation(ai_env['factory'],lease,config,model_client=provider)
    assert result.status=='failed' and result.report['error_code']=='model_token_budget_exhausted'
    assert len(calls)==1

@pytest.mark.parametrize('known_usage',[True,False])
def test_followup_context_compacts_to_remaining_cumulative_budget(ai_env,monkeypatch,known_usage):
    """A 23.5k second request fits the per-round cap but needs 32k-budget compaction."""
    import copy
    import json
    from platform_app import model_context
    from platform_app.investigations import run_investigation
    from platform_app.tools import project_evidence
    from platform_app.models import ModelCall
    from sqlalchemy import select
    row,_,lease,config=prepared_run(ai_env)
    frozen_result={'items':[project_evidence(ai_env['evidence'])], 'coverage':{'complete':False,'gaps':['bounded_test_projection']}}
    monkeypatch.setattr(model_context,'prepare_tool_context',lambda result,max_bytes:copy.deepcopy(frozen_result))
    calls=[]
    def bound(payload):return len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())+512
    def provider(config,payload,timeout):
        calls.append(copy.deepcopy(payload))
        if len(calls)==1:
            tool={'id':'budget-settlement','type':'function','function':{'name':'get_lock_graph','arguments':json.dumps({'instance_id':ai_env['a'].id})}}
            message={'role':'assistant','content':'','tool_calls':[tool]}
            probe=copy.deepcopy(payload)
            probe['messages'] += [copy.deepcopy(message),{'role':'tool','tool_call_id':tool['id'],'content':json.dumps(frozen_result,ensure_ascii=False)}]
            padding=23500-bound(probe)
            assert padding>0 and bound(payload)<23700
            message['content']='x'*padding
            probe['messages'][-2]['content']=message['content']
            assert bound(probe)==23500
            response={'choices':[{'message':message}]}
            if known_usage:response['usage']={'prompt_tokens':6517,'completion_tokens':244}
            return response
        assert payload['tool_choice']=='none' and payload['tools']==[]
        assert bound(payload)<=24000 and 6517+244+bound(payload)+payload['max_tokens']<=32000
        return provider_response(ai_env['evidence'].id)
    result=run_investigation(ai_env['factory'],lease,config,model_client=provider)
    if known_usage:
        assert result.status=='completed' and len(calls)==2,result.report
    else:
        assert result.status=='failed' and result.report['error_code']=='model_usage_unknown'
        assert len(calls)==1
        with ai_env['factory']() as db:
            call=db.scalar(select(ModelCall).where(ModelCall.investigation_id==row.id))
            assert call.input_tokens is None and call.output_tokens is None and not call.response['usage_available']
