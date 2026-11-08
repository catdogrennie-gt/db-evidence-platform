from datetime import timedelta
import json

import pytest
from fastapi import HTTPException


@pytest.fixture
def ai_env(tmp_path):
    from platform_app.db import Base, create_db_engine, create_session_factory, utcnow
    from platform_app.models import (
        Department,
        System,
        Instance,
        User,
        Role,
        UserGrant,
        Task,
        CollectionRun,
        Evidence,
        Investigation,
    )
    from platform_app.access import actor_for_user

    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    db = factory()
    now = utcnow()
    start = now - timedelta(minutes=10)
    end = now - timedelta(minutes=1)
    department = Department(name="test")
    user = User(username="test-ai", password_hash="unused")
    role = Role(
        name="ai-test", permissions=["data.read", "investigation.create", "model.external_use"]
    )
    db.add_all([department, user, role])
    db.flush()
    system = System(department_id=department.id, name="test", model_policy="live_redacted")
    db.add(system)
    db.flush()
    a = Instance(
        system_id=system.id,
        name="a",
        endpoint="localhost",
        expected_server_uuid="a",
        model_policy="live_redacted",
    )
    b = Instance(
        system_id=system.id,
        name="b",
        endpoint="localhost",
        expected_server_uuid="b",
        model_policy="live_redacted",
    )
    db.add_all([a, b])
    db.flush()
    grant = UserGrant(user_id=user.id, role_id=role.id, scope_type="instance", scope_id=a.id)
    db.add(grant)
    task = Task(type="collection", instance_id=a.id)
    db.add(task)
    db.flush()
    run = CollectionRun(task_id=task.id, instance_id=a.id, collector="runtime")
    db.add(run)
    db.flush()
    evidence = Evidence(
        instance_id=a.id,
        run_id=run.id,
        source="mysql.lock_waits",
        observed_at=start + timedelta(minutes=1),
        payload={
            "rows": [
                {
                    "waiting_trx_id": 12,
                    "blocking_trx_id": 13,
                    "OBJECT_NAME": "private_table",
                    "password": "DO-NOT-EXFILTRATE",
                    "raw_log": "personal@example.invalid",
                }
            ]
        },
        payload_sha256="0" * 64,
        coverage={"complete": True},
    )
    late = Evidence(
        instance_id=a.id,
        run_id=run.id,
        source="mysql.lock_waits",
        observed_at=end + timedelta(seconds=1),
        payload={"rows": [{"waiting_trx_id": 99}]},
        payload_sha256="1" * 64,
    )
    investigation = Investigation(
        created_by=user.id,
        question="why lock?",
        window_start=start,
        window_end=end,
        permission_version=1,
    )
    db.add_all([evidence, late, investigation])
    db.commit()
    result = dict(
        db=db,
        factory=factory,
        engine=engine,
        actor=actor_for_user(db, user.id),
        a=a,
        b=b,
        system=system,
        grant=grant,
        evidence=evidence,
        late=late,
        investigation=investigation,
        start=start,
        end=end,
        tmp_path=tmp_path,
    )
    yield result
    db.close()
    engine.dispose()


def broker(env, **kwargs):
    from platform_app.tools import ToolBroker

    return ToolBroker(
        env["actor"],
        env["investigation"],
        [env["a"].id],
        env["start"],
        env["end"],
        allowed_evidence_ids={env["evidence"].id, env["late"].id},
        **kwargs,
    )


def test_broker_freezes_window_and_explicit_payload_allowlist(ai_env):
    result = broker(ai_env).call("get_lock_graph", {"instance_id": ai_env["a"].id})
    encoded = json.dumps(result)
    assert ai_env["evidence"].id in encoded
    assert ai_env["late"].id not in encoded
    assert "DO-NOT-EXFILTRATE" not in encoded and "private_table" not in encoded
    assert "personal@example.invalid" not in encoded
    assert result["items"][0]["data"]["rows"][0]["blocking_trx_id"] == 13


def test_tools_cannot_widen_instance_window_or_arguments(ai_env):
    from platform_app.common import iso

    tool = broker(ai_env)
    for args in (
        {"instance_id": ai_env["b"].id},
        {"instance_id": ai_env["a"].id, "to": iso(ai_env["end"] + timedelta(seconds=1))},
        {"instance_id": ai_env["a"].id, "sql": "SELECT secrets"},
    ):
        with pytest.raises(HTTPException):
            tool.call("get_evidence", args)
    with pytest.raises(HTTPException):
        tool.call("run_sql", {"instance_id": ai_env["a"].id})


def test_tool_rechecks_grants_after_each_call(ai_env):
    tool = broker(ai_env)
    tool.call("get_lock_graph", {"instance_id": ai_env["a"].id})
    ai_env["db"].delete(ai_env["grant"])
    ai_env["db"].commit()
    with pytest.raises(HTTPException):
        tool.call("get_lock_graph", {"instance_id": ai_env["a"].id})


def test_report_requires_every_resource_readable(ai_env):
    from platform_app.models import InvestigationResource
    from platform_app.investigations import get_investigation

    db = ai_env["db"]
    investigation = ai_env["investigation"]
    db.add_all(
        [
            InvestigationResource(investigation_id=investigation.id, instance_id=ai_env["a"].id),
            InvestigationResource(investigation_id=investigation.id, instance_id=ai_env["b"].id),
        ]
    )
    db.commit()
    with pytest.raises(HTTPException) as out:
        get_investigation(ai_env["actor"], investigation.id)
    assert out.value.status_code == 404


def test_http_rejects_more_than_manifest_asset_limit_before_enqueue(ai_env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from platform_app.identity import current_actor
    from platform_app.routes import investigations
    from types import SimpleNamespace

    app = FastAPI()
    app.state.config = SimpleNamespace(model={})
    app.include_router(investigations.router)
    app.dependency_overrides[current_actor] = lambda: ai_env["actor"]

    def must_not_enqueue(*args, **kwargs):
        raise AssertionError("Oversized historical bundle reached enqueue")

    monkeypatch.setattr(investigations, "create_investigation", must_not_enqueue)
    with TestClient(app) as client:
        response = client.post(
            "/investigations",
            json={
                "instance_ids": [str(i) for i in range(6)],
                "from": ai_env["start"].isoformat() + "Z",
                "to": ai_env["end"].isoformat() + "Z",
                "question": "bounded historical investigation",
            },
        )
    assert response.status_code == 422


def test_preview_matches_field_projection_and_keeps_free_text_local(ai_env):
    from platform_app.investigations import preview_investigation

    result = preview_investigation(
        ai_env["actor"],
        [ai_env["a"].id],
        ai_env["start"],
        ai_env["end"],
        "lock with private note secret-value-unlabelled",
        "live_redacted",
    )
    encoded = json.dumps(result)
    assert result["redaction"]["raw_question"] == "local_only"
    assert all(
        v not in encoded for v in ("secret-value-unlabelled", "DO-NOT-EXFILTRATE", "private_table")
    )
    assert ai_env["late"].id not in encoded


def test_metric_tool_reads_frozen_prometheus_item_and_preserves_nulls(ai_env):
    from platform_app.models import EvidenceManifest, ManifestItem
    from platform_app.common import iso
    from platform_app.tools import ToolBroker

    db = ai_env["db"]
    inv = ai_env["investigation"]
    manifest = EvidenceManifest(
        owner_type="investigation",
        owner_id=inv.id,
        instance_ids=[ai_env["a"].id],
        window_start=ai_env["start"],
        window_end=ai_env["end"],
        manifest_sha256="0" * 64,
        retain_until=ai_env["end"] + timedelta(days=90),
    )
    db.add(manifest)
    db.flush()
    inv.manifest_id = manifest.id
    frozen = ManifestItem(
        manifest_id=manifest.id,
        instance_id=ai_env["a"].id,
        item_type="prometheus_window",
        payload_sha256="1" * 64,
        payload={
            "template": "connections",
            "owner_collector": "identity_status",
            "effective_step_seconds": 30,
            "private_host": "MUST-NOT-EXTERNALIZE",
            "points": [
                {
                    "time": iso(ai_env["start"] + timedelta(seconds=1)),
                    "value": 7,
                    "validity": "valid",
                },
                {
                    "time": iso(ai_env["start"] + timedelta(seconds=31)),
                    "value": 999,
                    "validity": "stale",
                },
                {
                    "time": iso(ai_env["end"] + timedelta(seconds=1)),
                    "value": 123,
                    "validity": "valid",
                },
            ],
        },
    )
    db.add(frozen)
    db.commit()
    tool = ToolBroker(
        ai_env["actor"],
        inv,
        [ai_env["a"].id],
        ai_env["start"],
        ai_env["end"],
        allowed_evidence_ids=[],
    )
    result = tool.call("get_metric_window", {"instance_id": ai_env["a"].id})
    assert result["items"][0]["evidence_id"] == "manifest:" + frozen.id
    assert [p["value"] for p in result["items"][0]["points"]] == [7, None]
    assert result["items"][0]["source"] == "prometheus_resampled"
    assert "MUST-NOT-EXTERNALIZE" not in str(result)
    from platform_app.models import InvestigationResource
    from platform_app.investigations import get_frozen_metric

    first = InvestigationResource(investigation_id=inv.id, instance_id=ai_env["a"].id)
    hidden = InvestigationResource(investigation_id=inv.id, instance_id=ai_env["b"].id)
    db.add_all([first, hidden])
    db.commit()
    with pytest.raises(HTTPException) as denied:
        get_frozen_metric(ai_env["actor"], inv.id, frozen.id)
    assert denied.value.status_code == 404
    db.delete(hidden)
    db.commit()
    assert (
        get_frozen_metric(ai_env["actor"], inv.id, frozen.id)["evidence_id"]
        == "manifest:" + frozen.id
    )


def test_bundle_uses_manifest_coverage_and_redaction_is_not_a_data_gap(ai_env):
    from platform_app.models import EvidenceManifest, ManifestItem
    from platform_app.investigations import _bundle

    db = ai_env["db"]
    inv = ai_env["investigation"]
    manifest = EvidenceManifest(
        owner_type="investigation",
        owner_id=inv.id,
        instance_ids=[ai_env["a"].id],
        window_start=ai_env["start"],
        window_end=ai_env["end"],
        manifest_sha256="0" * 64,
        retain_until=ai_env["end"] + timedelta(days=90),
        sources=["prometheus_resampled"],
    )
    db.add(manifest)
    db.flush()
    db.add(
        ManifestItem(
            manifest_id=manifest.id,
            instance_id=ai_env["a"].id,
            item_type="coverage",
            payload_sha256="0" * 64,
            payload={
                "coverage_complete": True,
                "gaps": [],
                "prometheus_materialization": {"materialized": True, "complete": True},
            },
        )
    )
    db.commit()
    bundle, *_ = _bundle(db, inv, manifest)
    assert bundle["coverage"]["complete"] is True and bundle["coverage"]["gaps"] == []
    assert "names_sql_and_log_text_omitted" in bundle["projection"]["omitted_fields"]
