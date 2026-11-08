"""Task and export boundaries against isolated SQLite and temporary private files."""

from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from platform_app import models as m
from platform_app.access import actor_for_user
from platform_app.common import iso
from platform_app.db import Base, create_db_engine, create_session_factory, utcnow
from platform_app.identity import current_actor
from platform_app.routes.tasks import router
from platform_app.exports import handle_export
from platform_app.scheduling import claim, enqueue


@pytest.fixture
def graph(tmp_path):
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    db = factory()
    dep = m.Department(name="tasks")
    db.add(dep)
    db.flush()
    system = m.System(name="task-system", department_id=dep.id)
    db.add(system)
    db.flush()
    a = m.Instance(
        name="A",
        system_id=system.id,
        endpoint="127.0.0.1",
        expected_server_uuid="task-a",
        enabled=True,
        lifecycle_state="Active",
    )
    b = m.Instance(
        name="B",
        system_id=system.id,
        endpoint="127.0.0.1",
        expected_server_uuid="task-b",
        enabled=True,
        lifecycle_state="Active",
    )
    user = m.User(username="task-reader", password_hash="unused")
    other = m.User(username="task-other", password_hash="unused")
    role = m.Role(
        name="task-test",
        permissions=[
            "data.read",
            "task.manage",
            "collection.trigger",
            "export.create",
            "event.read",
            "investigation.create",
        ],
    )
    db.add_all([a, b, user, other, role])
    db.flush()
    grant = m.UserGrant(user_id=user.id, role_id=role.id, scope_type="instance", scope_id=a.id)
    db.add(grant)
    db.commit()
    actor = actor_for_user(db, user.id)
    config = SimpleNamespace(private_dir=tmp_path, model={})
    app = FastAPI()
    app.state.config = config
    app.state.session_factory = factory
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_actor] = lambda: actor
    yield SimpleNamespace(
        db=db,
        factory=factory,
        a=a,
        b=b,
        user=user,
        other=other,
        role=role,
        grant=grant,
        actor=actor,
        config=config,
        client=TestClient(app),
        system=system,
    )
    db.close()
    engine.dispose()


def task(g, kind="collect_instance", *, ids=None, author=True, status="queued", result=None):
    payload = {
        "instance_ids": ids or [g.a.id],
        "config_revision": 1,
        "collectors": ["identity_status"],
    }
    row = enqueue(
        g.db,
        kind,
        actor_id=g.user.id if author else g.other.id,
        payload=payload,
        instance_id=g.a.id if kind == "collect_instance" else None,
    )
    row.status = status
    row.result = result or {}
    g.db.commit()
    return row


def export_body(g, resource="evidence"):
    return {
        "instance_ids": [g.a.id],
        "resource": resource,
        "from": iso(utcnow() - timedelta(hours=1)),
        "to": iso(utcnow() - timedelta(seconds=1)),
        "format": "json",
    }


def evidence(g, *, restricted=False):
    source_task = task(g)
    run = m.CollectionRun(
        task_id=source_task.id, instance_id=g.a.id, collector="runtime", status="succeeded"
    )
    g.db.add(run)
    g.db.flush()
    row = m.Evidence(
        instance_id=g.a.id,
        run_id=run.id,
        source="mysql.sessions",
        observed_at=utcnow() - timedelta(minutes=1),
        sensitivity="restricted" if restricted else "redacted",
        payload={"rows": [{"secret_test_text": "local-only"}]},
        payload_sha256="a" * 64,
    )
    g.db.add(row)
    g.db.commit()
    return row


def test_author_cannot_read_mixed_scope_or_reuse_revoked_business_read(graph):
    g = graph
    mixed = task(g, "investigation", ids=[g.a.id, g.b.id])
    own = task(g)
    assert g.client.get(f"/api/v1/tasks/{mixed.id}").status_code == 404
    listed = g.client.get("/api/v1/tasks").json()
    assert listed["total"] == 1 and [r["id"] for r in listed["items"]] == [own.id]
    g.role.permissions = ["task.manage", "collection.trigger"]
    g.db.commit()
    assert g.client.get(f"/api/v1/tasks/{own.id}").status_code in (403, 404)
    assert g.client.get("/api/v1/tasks").json()["total"] == 0


def test_task_manager_requires_all_sources_even_for_other_author(graph):
    g = graph
    visible = task(g, author=False)
    mixed = task(g, "export", ids=[g.a.id, g.b.id], author=False)
    assert g.client.get(f"/api/v1/tasks/{visible.id}").status_code == 200
    assert g.client.get(f"/api/v1/tasks/{mixed.id}").status_code == 404
    g.role.permissions = ["data.read"]
    g.db.commit()
    assert g.client.get(f"/api/v1/tasks/{visible.id}").status_code in (403, 404)


def test_task_response_does_not_expose_payload_paths_or_unreviewed_result_fields(graph):
    g = graph
    row = task(
        g,
        result={
            "instance_id": g.a.id,
            "password": "must-not-show",
            "private_path": "D:/private/model-key",
        },
    )
    row.payload = {**row.payload, "credentials": {"password": "must-not-show"}}
    row.result_ref = "D:/private/model-key"
    row.progress = {"password": "must-not-show", "completed": 1}
    g.db.commit()
    response = g.client.get(f"/api/v1/tasks/{row.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["result"] == {"instance_id": g.a.id}
    assert (
        "must-not-show" not in str(body) and "D:/private" not in str(body) and "payload" not in body
    )


def test_cancel_and_retry_reauthorize_current_actions_and_create_new_task(graph):
    g = graph
    row = task(g)
    assert g.client.post(f"/api/v1/tasks/{row.id}/cancel", json={}).json()["status"] == "cancelled"
    retried = g.client.post(
        f"/api/v1/tasks/{row.id}/retry", json={}, headers={"Idempotency-Key": "retry-one"}
    )
    assert retried.status_code == 202, retried.json()
    new = g.db.get(m.Task, retried.json()["id"])
    assert new.parent_task_id == row.id and new.id != row.id
    g.role.permissions = ["data.read", "task.manage"]
    g.db.commit()
    assert g.client.post(f"/api/v1/tasks/{new.id}/cancel", json={}).status_code == 403


def test_connection_task_never_crosses_author_or_onboarding_scope(graph):
    g = graph
    g.role.permissions = ["asset.onboard", "task.manage"]
    g.grant.scope_type = "system"
    g.grant.scope_id = g.system.id
    g.db.commit()
    row = enqueue(
        g.db,
        "connection_test",
        actor_id=g.user.id,
        payload={"system_id": g.system.id, "test_id": "opaque-test"},
    )
    g.db.commit()
    assert g.client.get(f"/api/v1/tasks/{row.id}").status_code == 200
    row.actor_id = g.other.id
    g.db.commit()
    assert g.client.get(f"/api/v1/tasks/{row.id}").status_code == 404


def test_export_requires_current_full_resource_scope_and_fixed_format(graph):
    g = graph
    body = export_body(g)
    assert (
        g.client.post(
            "/api/v1/exports", json={**body, "instance_ids": [g.a.id, g.b.id]}
        ).status_code
        == 404
    )
    assert g.client.post("/api/v1/exports", json={**body, "format": "html"}).status_code == 422
    assert (
        g.client.post("/api/v1/exports", json={**body, "path": "D:/arbitrary"}).status_code == 422
    )
    g.role.permissions = ["data.read"]
    g.db.commit()
    assert g.client.post("/api/v1/exports", json=body).status_code == 403


def test_export_real_snapshot_is_private_and_download_checks_revocation(graph):
    g = graph
    source = evidence(g)
    response = g.client.post(
        "/api/v1/exports", json=export_body(g), headers={"Idempotency-Key": "one-export"}
    )
    assert response.status_code == 202, response.json()
    identifier = response.json()["id"]
    lease = claim(g.factory, "export-test-worker", queue="export")
    assert lease.id == identifier
    handle_export(g.factory, lease, g.config)
    g.db.expire_all()
    download = g.client.get(f"/api/v1/exports/{identifier}/download")
    assert download.status_code == 200, download.text
    data = download.json()
    assert data["items"][0]["id"] == source.id
    assert "local-only" in download.text
    public = g.client.get(f"/api/v1/tasks/{identifier}").json()
    assert "private_dir" not in str(public) and str(g.config.private_dir) not in str(public)
    assert public["result"]["count"] == 1
    g.role.permissions = ["data.read", "task.manage"]
    g.db.commit()
    assert g.client.get(f"/api/v1/exports/{identifier}/download").status_code == 403


def test_export_rechecks_before_generation_and_expired_download_is_gone(graph):
    g = graph
    response = g.client.post("/api/v1/exports", json=export_body(g))
    identifier = response.json()["id"]
    lease = claim(g.factory, "export-revoked-worker", queue="export")
    g.role.permissions = ["data.read"]
    g.db.commit()
    with pytest.raises(HTTPException) as denied:
        handle_export(g.factory, lease, g.config)
    assert denied.value.status_code == 403
    assert not (g.config.private_dir / "exports" / f"{identifier}.json").exists()
    g.role.permissions = ["data.read", "export.create", "task.manage"]
    g.db.commit()
    row = g.db.get(m.Task, identifier)
    row.status = "succeeded"
    row.result = {"export_id": identifier, "expires_at": iso(utcnow() - timedelta(seconds=1))}
    g.db.commit()
    assert g.client.get(f"/api/v1/exports/{identifier}/download").status_code == 410


def test_export_restricted_payload_is_redacted_without_sensitive_permission(graph):
    g = graph
    evidence(g, restricted=True)
    identifier = g.client.post("/api/v1/exports", json=export_body(g)).json()["id"]
    handle_export(g.factory, claim(g.factory, "export-redacted-worker", queue="export"), g.config)
    download = g.client.get(f"/api/v1/exports/{identifier}/download")
    assert download.status_code == 200 and "local-only" not in download.text
    assert download.json()["items"][0]["payload"]["redacted"] is True


def test_export_mixed_report_does_not_leak_other_instance_even_to_report_author(graph):
    g = graph
    now = utcnow() - timedelta(minutes=1)
    report = m.Investigation(
        created_by=g.user.id,
        question="mixed scope",
        permission_version=1,
        window_start=now - timedelta(minutes=1),
        window_end=now,
        status="completed",
        report={"secret_other_scope": "must-not-show"},
    )
    g.db.add(report)
    g.db.flush()
    g.db.add_all(
        [m.InvestigationResource(investigation_id=report.id, instance_id=i.id) for i in [g.a, g.b]]
    )
    g.db.commit()
    identifier = g.client.post("/api/v1/exports", json=export_body(g, "investigations")).json()[
        "id"
    ]
    handle_export(g.factory, claim(g.factory, "export-mixed-worker", queue="export"), g.config)
    response = g.client.get(f"/api/v1/exports/{identifier}/download")
    assert response.status_code == 200 and response.json()["items"] == []
    assert "must-not-show" not in response.text


def test_obsolete_export_attempt_cannot_delete_completed_snapshot(graph):
    g = graph
    identifier = g.client.post("/api/v1/exports", json=export_body(g)).json()["id"]
    lease = claim(g.factory, "export-first-worker", queue="export")
    handle_export(g.factory, lease, g.config)
    with pytest.raises(HTTPException):
        handle_export(g.factory, lease, g.config)
    response = g.client.get(f"/api/v1/exports/{identifier}/download")
    assert response.status_code == 200, response.text


def test_export_sensitive_download_requires_permission_still_held(graph):
    g = graph
    evidence(g, restricted=True)
    g.role.permissions = [*g.role.permissions, "evidence.read_sensitive"]
    g.db.commit()
    identifier = g.client.post("/api/v1/exports", json=export_body(g)).json()["id"]
    handle_export(g.factory, claim(g.factory, "export-sensitive-worker", queue="export"), g.config)
    assert "local-only" in g.client.get(f"/api/v1/exports/{identifier}/download").text
    g.role.permissions = [v for v in g.role.permissions if v != "evidence.read_sensitive"]
    g.db.commit()
    assert g.client.get(f"/api/v1/exports/{identifier}/download").status_code == 403


def test_export_row_budget_is_explicit_and_never_reported_complete(graph, monkeypatch):
    g = graph
    from platform_app import exports

    assert exports.MAX_ROWS == 5000
    monkeypatch.setattr(exports, "MAX_ROWS", 2)
    for _ in range(3):
        evidence(g)
    identifier = g.client.post("/api/v1/exports", json=export_body(g)).json()["id"]
    handle_export(g.factory, claim(g.factory, "export-bound-worker", queue="export"), g.config)
    document = g.client.get(f"/api/v1/exports/{identifier}/download").json()
    assert len(document["items"]) == 2 and document["count"] == 2
    assert document["truncated"] is True and document["coverage"]["complete"] is False


def test_unknown_model_cost_retry_needs_explicit_ack_and_new_investigation(graph):
    g = graph
    row = task(g, "investigation", status="failed")
    row.payload = {
        **row.payload,
        "from": iso(utcnow() - timedelta(minutes=5)),
        "to": iso(utcnow() - timedelta(minutes=1)),
        "question": "核对已采证据",
        "mode": "rules_only",
    }
    row.error_code = "model_call_outcome_unknown"
    g.db.commit()
    denied = g.client.post(f"/api/v1/tasks/{row.id}/retry", json={})
    assert (
        denied.status_code == 409
        and denied.json()["detail"]["code"] == "unknown_model_cost_acknowledgement_required"
    )
    new = g.client.post(f"/api/v1/tasks/{row.id}/retry", json={"acknowledge_unknown_cost": True})
    assert new.status_code == 202, new.json()
    assert new.json()["id"] != row.id
    created = g.db.get(m.Task, new.json()["id"])
    assert created.parent_task_id == row.id and created.input_ref


def test_queued_investigation_cancel_releases_pending_domain_state(graph):
    g = graph
    row = task(g, "investigation")
    report = m.Investigation(
        created_by=g.user.id,
        task_id=row.id,
        question="queued-only",
        permission_version=1,
        window_start=utcnow() - timedelta(minutes=5),
        window_end=utcnow() - timedelta(minutes=1),
        status="queued",
    )
    g.db.add(report)
    g.db.flush()
    row.input_ref = report.id
    g.db.commit()
    assert g.client.post(f"/api/v1/tasks/{row.id}/cancel", json={}).status_code == 200
    assert row.status == "cancelled" and report.status == "cancelled"


def test_queued_activation_cancel_returns_ready_without_enabling_collection(graph):
    g = graph
    g.role.permissions = [*g.role.permissions, "asset.manage"]
    g.db.commit()
    row = enqueue(
        g.db,
        "activate_instance",
        actor_id=g.user.id,
        instance_id=g.a.id,
        payload={"config_revision": g.a.config_revision},
    )
    g.a.lifecycle_state = "Activating"
    g.a.enabled = False
    g.db.commit()
    assert g.client.post(f"/api/v1/tasks/{row.id}/cancel", json={}).status_code == 200
    assert row.status == "cancelled" and g.a.lifecycle_state == "Ready" and g.a.enabled is False


def test_export_cleanup_deletes_only_expired_completed_files_and_keeps_task_history(graph):
    g = graph
    from platform_app.exports import purge_expired_exports

    identifier = g.client.post("/api/v1/exports", json=export_body(g)).json()["id"]
    handle_export(g.factory, claim(g.factory, "export-cleanup-worker", queue="export"), g.config)
    path = g.config.private_dir / "exports" / f"{identifier}.json"
    assert purge_expired_exports(g.factory, g.config)["expired_exports_deleted"] == 0
    row = g.db.get(m.Task, identifier)
    row.finished_at = utcnow() - timedelta(days=2)
    row.result = {**row.result, "expires_at": iso(utcnow() - timedelta(hours=1))}
    g.db.commit()
    assert purge_expired_exports(g.factory, g.config)["expired_exports_deleted"] == 1
    assert not path.exists() and g.db.get(m.Task, identifier).status == "succeeded"
