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
                        "rows": [{"waiting_trx_id": i, "blocking_trx_id": i + 1} for i in range(30)]
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
                                        "arguments": json.dumps({"instance_id": ai_env["a"].id}),
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
            <= 12000
        )
        return provider_response(ai_env["evidence"].id)

    result = run_investigation(ai_env["factory"], lease, config, model_client=provider)
    assert result.status == "completed" and len(calls) == 2


def test_deepseek_final_json_budget_disables_default_thinking(ai_env):
    from platform_app.investigations import run_investigation

    row, task, lease, config = prepared_run(ai_env)
    config.model["provider"] = "DeepSeek"

    def provider(config, payload, timeout):
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["max_tokens"] == 2000
        return provider_response(ai_env["evidence"].id)

    result = run_investigation(ai_env["factory"], lease, config, model_client=provider)
    assert result.status == "completed"
