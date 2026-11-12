from datetime import timedelta
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from tests.platform.test_ai_scope import ai_env
from platform_app import models as m
from platform_app.db import utcnow


@pytest.fixture
def demo_env(ai_env):
    from platform_app.demo import settings

    e = ai_env
    db = e["db"]
    a = e["a"]
    a.is_lab = True
    a.connection_mode = "zabbix"
    role = db.get(m.Role, e["grant"].role_id)
    role.permissions = [*role.permissions, "demo.operate"]
    source = m.MonitoringSource(
        name="demo", url="https://monitor.invalid", secret_ref="test-only", system_id=a.system_id
    )
    db.add(source)
    db.flush()
    mapping = m.MonitoringMapping(
        source_id=source.id,
        hostid="group",
        instance_id=a.id,
        layer="group",
        selected=True,
        items=[{"itemid": "42", "hostid": "101", "hostid": "101"}],
    )
    db.add_all([mapping, m.DemoControl(id=1)])
    db.commit()
    e["config"] = SimpleNamespace(
        demo=dict(
            enabled=True,
            instance_id=a.id,
            runner_user_id=e["actor"].user_id,
            target_config_path="D:/private/demo.json",
            lock_itemid="42",
            source_id=source.id,
            external_confirmed=True,
        )
    )
    e["settings"] = settings(e["config"])
    e["mapping"] = mapping
    return e


def test_start_is_singleton_and_stale_stop_cannot_touch_new_run(demo_env):
    from platform_app.demo import request_start, request_stop

    e = demo_env
    first = request_start(e["actor"], e["config"])
    e["db"].commit()
    assert request_start(e["actor"], e["config"]).id == first.id
    with pytest.raises(HTTPException):
        request_stop(e["actor"], e["config"], "stale")
    assert not first.stop_requested
    request_stop(e["actor"], e["config"], first.id)
    assert first.stop_requested


def test_scope_and_lab_enforced(demo_env):
    from platform_app.demo import request_start

    e = demo_env
    e["a"].is_lab = False
    e["db"].commit()
    with pytest.raises(HTTPException):
        request_start(e["actor"], e["config"])


def test_unsafe_limits_rejected():
    from platform_app.demo import settings

    for values in (
        {"max_duration_seconds": 181},
        {"cooldown_seconds": 1},
        {"max_daily_runs": 9},
        {"max_daily_ai_runs": 5},
    ):
        with pytest.raises(ValueError):
            settings(SimpleNamespace(demo=values))


class Fault:
    starts = 0

    def __init__(self, config, **kwargs):
        pass

    def start(self):
        self.starts += 1
        return {"active": True}

    def sample(self):
        return {"active": True}

    def stop(self):
        return dict(sessions_closed=True, locks_cleared=True, dataset_unchanged=True)


def controller(e, **kw):
    from platform_app.demo import DemoController

    return DemoController(
        e["factory"],
        e["config"],
        transport_factory=Fault,
        verify_idle=lambda c: dict(
            sessions_closed=True, locks_cleared=True, dataset_unchanged=True
        ),
        **kw,
    )


def test_stop_before_injection_never_starts_transport(demo_env):
    from platform_app.demo import request_start, request_stop

    e = demo_env
    r = request_start(e["actor"], e["config"])
    request_stop(e["actor"], e["config"], r.id)
    e["db"].commit()
    c = controller(e)
    c.tick()
    e["db"].expire_all()
    r = e["db"].get(m.DemoRun, r.id)
    assert c.transport is None and r.fault_state == "cleared"


def test_expired_owner_is_recovery_only(demo_env):
    from platform_app.demo import request_start

    e = demo_env
    r = request_start(e["actor"], e["config"])
    e["db"].commit()
    c = controller(e)
    c.tick()
    with e["factory"].begin() as db:
        control = db.get(m.DemoControl, 1)
        control.lease_until = utcnow() - timedelta(seconds=1)
        db.get(m.DemoRun, r.id).expires_at = utcnow() - timedelta(seconds=10)
    successor = controller(e)
    successor.tick()
    e["db"].expire_all()
    assert successor.transport is None
    assert e["db"].get(m.DemoRun, r.id).fault_state == "cleared"
    c.close()


def test_old_wrong_source_and_missing_recovery_are_rejected(demo_env):
    from platform_app.demo import request_start, proof

    e = demo_env
    r = request_start(e["actor"], e["config"])
    r.fault_confirmed_at = utcnow() - timedelta(seconds=5)
    e["db"].flush()
    ev = e["evidence"]
    ev.observed_at = utcnow()
    ev.coverage = dict(
        availability="available",
        complete=True,
        transport="zabbix",
        source_id=e["settings"]["source_id"],
        itemid="42",
        hostid="101",
        snapshot_complete=True,
        snapshot_contract="db-evidence-lock-snapshot-v2",
        snapshot_truncated=False,
        snapshot_total_row_count=len(ev.payload["rows"]),
        mapping_version=1,
        config_revision=e["a"].config_revision,
    )
    e["db"].flush()
    assert proof(e["db"], r, e["settings"], positive=True).id == ev.id
    ev.coverage = {**ev.coverage, "source_id": "wrong"}
    e["db"].flush()
    assert proof(e["db"], r, e["settings"], positive=True) is None
    ev.coverage = {**ev.coverage, "source_id": e["settings"]["source_id"]}
    ev.observed_at = r.fault_confirmed_at - timedelta(seconds=1)
    e["db"].flush()
    assert proof(e["db"], r, e["settings"], positive=True) is None
    r.cleanup_started_at = utcnow()
    r.stopped_at = utcnow()
    ev.payload = {"rows": []}
    e["db"].flush()
    assert proof(e["db"], r, e["settings"], positive=False) is None


def test_cooldown_daily_limit_and_stop_while_paused(demo_env):
    from platform_app.demo import request_start, request_stop

    e = demo_env
    db = e["db"]
    r = request_start(e["actor"], e["config"])
    db.commit()
    r.status = "completed"
    r.fault_state = "cleared"
    ctl = db.get(m.DemoControl, 1)
    ctl.cooldown_until = utcnow() + timedelta(seconds=60)
    db.commit()
    with pytest.raises(HTTPException) as err:
        request_start(e["actor"], e["config"])
    assert err.value.detail["code"] == "demo_cooldown"
    ctl.cooldown_until = None
    e["config"].demo["max_daily_runs"] = 1
    db.commit()
    with pytest.raises(HTTPException) as err:
        request_start(e["actor"], e["config"])
    assert err.value.detail["code"] == "demo_daily_budget"
    r.status = "active"
    e["config"].demo["enabled"] = False
    db.commit()
    assert request_stop(e["actor"], e["config"], r.id).stop_requested


def test_deadline_cleanup_and_missing_monitoring_stays_unconfirmed(demo_env):
    from platform_app.demo import request_start

    e = demo_env
    r = request_start(e["actor"], e["config"])
    e["db"].commit()
    c = controller(e)
    c.tick()
    with e["factory"].begin() as db:
        db.get(m.DemoRun, r.id).expires_at = utcnow() - timedelta(seconds=1)
    c.tick()
    e["db"].expire_all()
    r = e["db"].get(m.DemoRun, r.id)
    assert r.fault_state == "cleared" and r.status == "awaiting_recovery"
    assert all(r.cleanup.values())
    with e["factory"].begin() as db:
        db.get(m.DemoRun, r.id).stopped_at = utcnow() - timedelta(minutes=6)
    c.tick()
    e["db"].expire_all()
    assert e["db"].get(m.DemoRun, r.id).status == "recovery_unconfirmed"
    assert e["db"].get(m.DemoControl, 1).blocked


def test_stale_controller_cannot_commit_or_clear_successor(demo_env):
    from platform_app.demo import request_start

    e = demo_env
    r = request_start(e["actor"], e["config"])
    e["db"].commit()
    c = controller(e)
    c.tick()
    with e["factory"].begin() as db:
        db.get(m.DemoControl, 1).fence += 1
    with e["factory"].begin() as db:
        with pytest.raises(HTTPException):
            c._fenced(db)
    c.close()


def fresh_evidence(e, r):
    ev = e["evidence"]
    ev.observed_at = utcnow() - timedelta(milliseconds=100)
    r.fault_confirmed_at = utcnow() - timedelta(seconds=2)
    ev.coverage = dict(
        availability="available",
        complete=True,
        transport="zabbix",
        source_id=r.source_id,
        itemid="42",
        hostid="101",
        snapshot_complete=True,
        snapshot_contract="db-evidence-lock-snapshot-v2",
        snapshot_truncated=False,
        snapshot_total_row_count=len(ev.payload["rows"]),
        mapping_version=r.mapping_version,
        config_revision=r.config_revision,
    )
    e["db"].commit()


def test_analysis_atomic_link_and_fixed_scope_no_repeat(demo_env, monkeypatch):
    from platform_app.demo import request_start

    e = demo_env
    r = request_start(e["actor"], e["config"])
    e["db"].commit()
    c = controller(e)
    c.tick()
    e["db"].expire_all()
    r = e["db"].get(m.DemoRun, r.id)
    fresh_evidence(e, r)
    calls = []

    def create(actor, ids, start, end, question, **kwargs):
        assert ids == [r.instance_id] and start < e["evidence"].observed_at < end <= utcnow()
        assert kwargs["idempotency_key"] == "demo:" + r.id and kwargs["mode"] == "live_redacted"
        calls.append(kwargs)
        report = m.Investigation(
            created_by=actor.user_id,
            question=question,
            window_start=start,
            window_end=end,
            permission_version=1,
        )
        actor.db.add(report)
        actor.db.flush()
        return report, None

    monkeypatch.setattr("platform_app.investigations.create_investigation", create)
    c.tick()
    c.tick()
    e["db"].expire_all()
    r = e["db"].get(m.DemoRun, r.id)
    assert len(calls) == 1 and r.investigation_id and r.ai_reserved_at and r.evidence_id
    assert len(list(e["db"].scalars(select(m.Task).where(m.Task.type == "collect_instance")))) == 1
    c.close()


def test_model_queue_failure_has_safe_code_and_no_retry(demo_env, monkeypatch):
    from platform_app.demo import request_start

    e = demo_env
    r = request_start(e["actor"], e["config"])
    e["db"].commit()
    c = controller(e)
    c.tick()
    e["db"].expire_all()
    r = e["db"].get(m.DemoRun, r.id)
    fresh_evidence(e, r)
    calls = []

    def fail(*a, **k):
        calls.append(1)
        raise RuntimeError("secret password/path")

    monkeypatch.setattr("platform_app.investigations.create_investigation", fail)
    c.tick()
    c.tick()
    e["db"].expire_all()
    r = e["db"].get(m.DemoRun, r.id)
    assert (
        len(calls) == 1
        and r.analysis_status == "failed"
        and r.error_code == "demo_analysis_unavailable"
    )
    assert not r.investigation_id and not r.ai_reserved_at
    assert r.status == "active"
    c.close()


def test_unknown_cleanup_blocks_new_start(demo_env):
    from platform_app.demo import request_start, request_stop

    e = demo_env
    r = request_start(e["actor"], e["config"])
    request_stop(e["actor"], e["config"], r.id)
    e["db"].commit()
    c = controller(e)
    c.verify_idle = lambda cfg: dict(
        sessions_closed=True, locks_cleared=False, dataset_unchanged=True
    )
    c.tick()
    e["db"].expire_all()
    assert e["db"].get(m.DemoRun, r.id).fault_state == "unknown"
    assert e["db"].get(m.DemoControl, 1).blocked
    assert request_start(e["actor"], e["config"]).id == r.id


def test_cross_scope_and_revoked_operate_cannot_control(demo_env):
    from platform_app.demo import request_start, snapshot

    e = demo_env
    e["config"].demo["instance_id"] = e["b"].id
    with pytest.raises(HTTPException):
        snapshot(e["actor"], e["config"])
    with pytest.raises(HTTPException):
        request_start(e["actor"], e["config"])
    e["config"].demo["instance_id"] = e["a"].id
    role = e["db"].get(m.Role, e["grant"].role_id)
    role.permissions = ["data.read"]
    e["db"].commit()
    assert snapshot(e["actor"], e["config"])["can_control"] is False
    with pytest.raises(HTTPException):
        request_start(e["actor"], e["config"])


def test_http_extra_fields_and_query_are_rejected(demo_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from platform_app.routes.demo import router
    from platform_app.identity import current_actor

    e = demo_env
    app = FastAPI()
    app.state.config = e["config"]
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_actor] = lambda: e["actor"]
    client = TestClient(app)
    for path, body in [
        ("/start", {"instance_id": "other"}),
        ("/start?sql=bad", {}),
        ("/stop", {"sql": "bad"}),
        ("/stop?run_id=other", {}),
    ]:
        assert client.post("/api/v1/demo" + path, json=body).status_code == 422
    assert client.post("/api/v1/demo/start", json={}).status_code == 200
    assert client.post("/api/v1/demo/stop", json={"run_id": "stale"}).status_code == 409


def test_snapshot_budget_contract(demo_env):
    from platform_app.demo import snapshot

    result = snapshot(demo_env["actor"], demo_env["config"])
    assert result["scenario"] == "mysql_lock_wait"
    assert result["budget"]["used_runs"] == 0 and result["budget"]["remaining_runs"] == 8
    assert result["budget"]["used_ai_runs"] == 0 and result["budget"]["remaining_ai_runs"] == 4


def test_ai_exhaustion_preserves_evidence_and_never_queues(demo_env, monkeypatch):
    from platform_app.demo import request_start

    e = demo_env
    e["config"].demo["max_daily_ai_runs"] = 0
    r = request_start(e["actor"], e["config"])
    e["db"].commit()
    c = controller(e)
    c.tick()
    e["db"].expire_all()
    r = e["db"].get(m.DemoRun, r.id)
    fresh_evidence(e, r)
    monkeypatch.setattr(
        "platform_app.investigations.create_investigation",
        lambda *a, **kw: pytest.fail("must not queue"),
    )
    c.tick()
    e["db"].expire_all()
    r = e["db"].get(m.DemoRun, r.id)
    assert r.analysis_status == "budget_exhausted" and r.evidence_id and not r.investigation_id
    c.close()


def test_expired_takeover_waits_until_absolute_deadline(demo_env):
    from platform_app.demo import request_start

    e = demo_env
    r = request_start(e["actor"], e["config"])
    e["db"].commit()
    c = controller(e)
    c.tick()
    with e["factory"].begin() as db:
        db.get(m.DemoControl, 1).lease_until = utcnow() - timedelta(seconds=1)
    successor = controller(e)
    checks = []
    successor.verify_idle = lambda cfg: (
        checks.append(1)
        or {k: True for k in ("sessions_closed", "locks_cleared", "dataset_unchanged")}
    )
    successor.tick()
    e["db"].expire_all()
    assert not checks and successor.transport is None and e["db"].get(m.DemoControl, 1).blocked
    c.close()


def test_complete_snapshot_marker_keeps_history_gap_but_proves_current_sample(demo_env):
    from platform_app.demo import request_start, proof

    e = demo_env
    r = request_start(e["actor"], e["config"])
    e["db"].commit()
    fresh_evidence(e, r)
    ev = e["evidence"]
    ev.truncated = True
    ev.coverage = {
        **ev.coverage,
        "hostid": "101",
        "complete": False,
        "reason_codes": ["history_truncated"],
        "snapshot_complete": True,
        "snapshot_contract": "db-evidence-lock-snapshot-v2",
        "snapshot_truncated": False,
        "snapshot_total_row_count": len(ev.payload["rows"]),
    }
    e["db"].flush()
    assert proof(e["db"], r, e["settings"], positive=True).id == ev.id
    for key, value in [
        ("hostid", "wrong"),
        ("snapshot_complete", False),
        ("snapshot_contract", "wrong"),
        ("snapshot_total_row_count", 900),
        ("reason_codes", ["history_truncated", "invalid_value"]),
    ]:
        old = ev.coverage
        ev.coverage = {**old, key: value}
        e["db"].flush()
        assert proof(e["db"], r, e["settings"], positive=True) is None
        ev.coverage = old


def test_snapshot_metadata_requires_explicit_trusted_producer():
    from platform_app.analysis_groups import lock_snapshot_metadata

    item = {
        "enhancement": "mysql.lock_waits",
        "tags": [{"tag": "sl.snapshot_contract", "value": "db-evidence-lock-snapshot-v2"}],
    }
    payload = {
        "producer_contract": "db-evidence-lock-snapshot-v2",
        "snapshot_complete": True,
        "snapshot_truncated": False,
        "total_row_count": 1,
        "rows": [{"waiting_trx_id": 1}],
    }
    assert lock_snapshot_metadata(item, payload)["snapshot_complete"]
    for key, value in [
        ("producer_contract", "wrong"),
        ("total_row_count", 2),
        ("snapshot_truncated", True),
        ("snapshot_complete", False),
    ]:
        assert not lock_snapshot_metadata(item, {**payload, key: value})["snapshot_complete"]
    assert not lock_snapshot_metadata({**item, "tags": []}, payload)["snapshot_complete"]


def test_demo_routes_use_session_csrf_and_origin(demo_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from platform_app.routes.demo import router
    from platform_app.identity import create_session

    e = demo_env
    db = e["db"]
    user = db.get(m.User, e["actor"].user_id)
    token, csrf, _ = create_session(db, user)
    db.commit()
    app = FastAPI()
    app.state.config = e["config"]
    app.state.config.public_origin = "http://127.0.0.1:18080"
    app.state.session_factory = e["factory"]
    app.include_router(router, prefix="/api/v1")
    client = TestClient(app, base_url="http://127.0.0.1:18080")
    assert client.get("/api/v1/demo").status_code == 401
    client.cookies.set("dba_session", token, path="/api/v1")
    assert client.get("/api/v1/demo").status_code == 200
    assert client.post("/api/v1/demo/start", json={}).status_code == 403
    assert (
        client.post(
            "/api/v1/demo/start",
            json={},
            headers={"Origin": "https://outside.invalid", "X-CSRF-Token": csrf},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/demo/start",
            json={},
            headers={"Origin": app.state.config.public_origin, "X-CSRF-Token": csrf},
        ).status_code
        == 200
    )


def test_monitoring_timeout_remains_recoverable_by_fresh_empty_sample(demo_env):
    from platform_app.demo import request_start, request_stop

    e = demo_env
    r = request_start(e["actor"], e["config"])
    request_stop(e["actor"], e["config"], r.id)
    e["db"].commit()
    c = controller(e)
    c.tick()
    with e["factory"].begin() as db:
        db.get(m.DemoRun, r.id).stopped_at = utcnow() - timedelta(minutes=6)
    c.tick()
    e["db"].expire_all()
    r = e["db"].get(m.DemoRun, r.id)
    assert r.status == "recovery_unconfirmed"
    fresh_evidence(e, r)
    ev = e["evidence"]
    ev.payload = {"rows": []}
    ev.coverage = {**ev.coverage, "snapshot_total_row_count": 0}
    e["db"].commit()
    c.tick()
    e["db"].expire_all()
    assert e["db"].get(m.DemoRun, r.id).status == "completed"
    assert not e["db"].get(m.DemoControl, 1).blocked


def test_transient_negative_sample_does_not_stop_confirmed_fault(demo_env):
    from platform_app.demo import request_start

    e = demo_env
    r = request_start(e["actor"], e["config"])
    e["db"].commit()
    c = controller(e)
    c.tick()
    values = iter([False, True, False, False, False])
    c.transport.sample = lambda: {"active": next(values)}
    c.tick()
    e["db"].expire_all()
    assert not e["db"].get(m.DemoRun, r.id).stop_requested
    assert e["db"].get(m.DemoRun, r.id).fault_state == "unknown"
    c.tick()
    e["db"].expire_all()
    assert e["db"].get(m.DemoRun, r.id).fault_state == "active"
    for _ in range(2):
        c.tick()
    e["db"].expire_all()
    assert not e["db"].get(m.DemoRun, r.id).stop_requested
    c.tick()
    e["db"].expire_all()
    assert e["db"].get(m.DemoRun, r.id).stop_requested
    c.tick()
    e["db"].expire_all()
    assert e["db"].get(m.DemoRun, r.id).fault_state == "cleared"


def prepared_demo_worker(e):
    from platform_app.demo import request_start
    from platform_app.scheduling import claim

    e["db"].get(m.CollectionRun, e["evidence"].run_id).collector = "monitoring"
    r = request_start(e["actor"], e["config"])
    e["db"].commit()
    c = controller(e)
    c.tick()
    e["db"].expire_all()
    r = e["db"].get(m.DemoRun, r.id)
    fresh_evidence(e, r)
    c.tick()
    e["db"].expire_all()
    r = e["db"].get(m.DemoRun, r.id)
    assert r.investigation_id
    lease = claim(e["factory"], "demo-proof-worker", "ai")
    config = SimpleNamespace(
        demo=e["config"].demo,
        private_dir=e["tmp_path"],
        model={"provider": "test", "model": "test", "max_calls": 1, "max_tokens": 1000},
    )
    return r, c, lease, config


def test_real_worker_payload_contains_exact_required_demo_proof(demo_env):
    import json
    from platform_app.investigations import run_investigation
    from tests.platform.test_ai_failures import provider_response

    e = demo_env
    r, c, lease, config = prepared_demo_worker(e)
    calls = []

    def provider(config, payload, timeout):
        calls.append(payload)
        initial = json.loads(payload["messages"][1]["content"])
        actual = [item for item in initial["evidence"] if item["evidence_id"] == r.evidence_id]
        assert actual and actual[0]["source"] == "mysql.lock_waits" and actual[0]["data"]["rows"]
        assert actual[0]["instance_id"] == r.instance_id
        return provider_response(r.evidence_id)

    result = run_investigation(e["factory"], lease, config, model_client=provider)
    assert len(calls) == 1 and result.status == "completed"
    with e["factory"]() as db:
        assert db.scalar(
            select(m.ManifestItem).where(
                m.ManifestItem.manifest_id == result.manifest_id,
                m.ManifestItem.evidence_id == r.evidence_id,
            )
        )
    c.close()


def test_demo_worker_policy_filtered_proof_never_calls_model(demo_env):
    from platform_app.investigations import run_investigation

    e = demo_env
    r, c, lease, config = prepared_demo_worker(e)
    e["db"].add(
        m.PlatformSetting(
            key="model_policy:instance:" + r.instance_id, value={"allowed_sources": ["host"]}
        )
    )
    e["db"].commit()
    calls = []
    result = run_investigation(
        e["factory"], lease, config, model_client=lambda *args: calls.append(args)
    )
    assert not calls and result.status == "failed"
    e["db"].expire_all()
    report = e["db"].get(m.Investigation, r.investigation_id)
    assert (
        report.status == "failed"
        and report.report["error_code"] == "demo_required_evidence_missing"
    )
    c.close()


def test_demo_worker_oversized_required_proof_never_calls_model(demo_env, monkeypatch):
    from platform_app.investigations import run_investigation
    from platform_app import model_context

    e = demo_env
    r, c, lease, config = prepared_demo_worker(e)
    original = model_context.compact_initial

    def oversized(bundle):
        initial = original(bundle)
        for item in initial["evidence"]:
            if item["evidence_id"] == r.evidence_id:
                item["data"]["test_padding"] = "x" * 50000
        return initial

    monkeypatch.setattr(model_context, "compact_initial", oversized)
    calls = []
    result = run_investigation(
        e["factory"], lease, config, model_client=lambda *args: calls.append(args)
    )
    assert not calls and result.status == "failed"
    e["db"].expire_all()
    report = e["db"].get(m.Investigation, r.investigation_id)
    assert (
        report.status == "failed"
        # Required proof remains protected. An impossible budget must fail
        # before sending anything, rather than silently deleting that proof.
        and report.report["error_code"] == "model_token_budget_exhausted"
    )
    c.close()


def test_new_run_injection_resets_prior_negative_samples(demo_env):
    from platform_app.demo import request_start

    e = demo_env
    r = request_start(e["actor"], e["config"])
    e["db"].commit()
    c = controller(e)
    c.negative_samples = 3
    c.tick()
    c.transport.sample = lambda: {"active": False}
    c.tick()
    e["db"].expire_all()
    assert c.negative_samples == 1 and not e["db"].get(m.DemoRun, r.id).stop_requested
    c.close()
