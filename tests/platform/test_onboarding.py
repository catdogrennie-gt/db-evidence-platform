"""Onboarding contract regressions; isolated SQLite logic, no target SQL or sockets.

The fixed credentials below are test-only inputs encrypted under a temporary key.
These tests do not claim live MySQL connectivity or deployment readiness.
"""

from copy import deepcopy
import asyncio
from datetime import timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from platform_app.access import actor_for_user
from platform_app.api import create_app
from platform_app.common import PlatformError
from platform_app.db import Base, create_db_engine, create_session_factory, utcnow
from platform_app.identity import current_actor
from platform_app.inventory import (
    checked_test,
    create_connection_test,
    instance_response,
    register_instance,
    secret_for_request,
    stop_collection,
    test_response as serialize_test,
)
from platform_app.models import (
    CollectionSchedule,
    ConnectionTest,
    Department,
    Instance,
    PlatformSetting,
    Role,
    System,
    Task,
    User,
    UserGrant,
)
from platform_app.routes.inventory import router
from platform_app.scheduling import check_fence, claim, enqueue
from platform_app.secrets import SecretStore


@pytest.fixture
def graph(tmp_path):
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    db = factory()
    key = tmp_path / "key"
    key.write_bytes(Fernet.generate_key())
    config = SimpleNamespace(
        allowed_networks=["127.0.0.0/8"],
        allowed_db_ports=[3306, 3307],
        ssh_profiles=[],
        fernet_key_path=key,
        secrets_dir=tmp_path / "secrets",
    )
    department = Department(name="onboarding-owned")
    outside = Department(name="onboarding-outside")
    role = Role(name="onboarding-test-control", permissions=["asset.onboard", "asset.manage"])
    first = User(username="onboard-first", password_hash="unused")
    second = User(username="onboard-second", password_hash="unused")
    db.add_all([department, outside, role, first, second])
    db.flush()
    system = System(name="owned-empty", department_id=department.id)
    other_system = System(name="outside-empty", department_id=outside.id)
    db.add_all([system, other_system])
    db.flush()
    for user in (first, second):
        db.add(
            UserGrant(
                user_id=user.id, role_id=role.id, scope_type="department", scope_id=department.id
            )
        )
    db.add(
        PlatformSetting(
            key="collection_profiles",
            value={
                "items": [
                    {
                        "id": "standard-id",
                        "name": "standard",
                        "intervals": {"identity_status": 30, "runtime": 30},
                    }
                ]
            },
        )
    )
    db.commit()
    actor = actor_for_user(db, first.id)
    other_actor = actor_for_user(db, second.id)
    body = {
        "system_id": system.id,
        "name": "test-instance",
        "engine": "mysql",
        "connection": {"mode": "direct", "db_host": "127.0.0.1", "db_port": 3306},
        "credentials": {"username": "test-readonly", "password": "not-a-production-password"},
        "requested_collectors": ["identity_status", "runtime"],
        "selected_schemas": ["fixture_schema"],
        "collection_profile": "standard-id",
    }
    app = FastAPI()
    app.state.config = config
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_actor] = lambda: actor
    client = TestClient(app)
    yield SimpleNamespace(
        db=db,
        factory=factory,
        actor=actor,
        other_actor=other_actor,
        config=config,
        body=body,
        system=system,
        other_system=other_system,
        client=client,
    )
    db.close()
    engine.dispose()


def passed_test(g, *, server_uuid=None):
    task = create_connection_test(g.db, g.actor, g.config, deepcopy(g.body))
    test = g.db.get(ConnectionTest, task.payload["test_id"])
    test.status = "succeeded"
    test.server_uuid = server_uuid or str(uuid4())
    test.source_version = "8.0.44-test"
    test.results = {"capabilities": [{"collector": "identity_status", "availability": "available"}]}
    task.status, task.finished_at = "succeeded", utcnow()
    g.db.commit()
    return test


def registered(g):
    test = passed_test(g)
    row = register_instance(g.db, g.actor, g.config, {**g.body, "test_id": test.id})
    g.db.commit()
    return row


def assert_code(error, status, code):
    assert error.value.status_code == status
    assert error.value.detail["code"] == code


def test_empty_system_onboarding_scope_and_no_cross_department(graph):
    g = graph
    task = create_connection_test(g.db, g.actor, g.config, deepcopy(g.body))
    assert task.status == "queued"
    with pytest.raises(HTTPException) as denied:
        create_connection_test(g.db, g.actor, g.config, {**g.body, "system_id": g.other_system.id})
    assert denied.value.status_code == 404


def test_preflight_idempotent_before_secret_or_test_creation(graph):
    g = graph
    one = create_connection_test(
        g.db, g.actor, g.config, deepcopy(g.body), idempotency_key="transport-retry"
    )
    g.db.commit()
    two = create_connection_test(
        g.db, g.actor, g.config, deepcopy(g.body), idempotency_key="transport-retry"
    )
    assert one.id == two.id
    assert g.db.scalar(select(func.count()).select_from(ConnectionTest)) == 1
    assert len(list(g.config.secrets_dir.glob("*.fernet"))) == 1
    changed = {
        **g.body,
        "credentials": {"username": "test-readonly", "password": "a-different-test-input"},
    }
    with pytest.raises(PlatformError) as conflict:
        create_connection_test(g.db, g.actor, g.config, changed, idempotency_key="transport-retry")
    assert_code(conflict, 409, "idempotency_conflict")


def test_secret_reference_not_bearer_and_cannot_cross_actor(graph):
    g = graph
    test = passed_test(g)
    body = {"credential_ref": test.secret_ref}
    with pytest.raises(PlatformError) as denied:
        secret_for_request(g.db, g.other_actor, SecretStore(g.config), body, g.system.id)
    assert_code(denied, 422, "credentials_required")
    ref, version = secret_for_request(g.db, g.actor, SecretStore(g.config), body, g.system.id)
    assert (ref, version) == (test.secret_ref, test.secret_version)


@pytest.mark.parametrize("mutation", ["host", "port", "tls", "ssh_ref", "system", "actor"])
def test_preflight_rejects_config_and_identity_tampering(graph, mutation):
    g = graph
    test = passed_test(g)
    changed = deepcopy(g.body)
    actor, system_id = g.actor, g.system.id
    if mutation == "host":
        changed["connection"]["db_host"] = "127.0.0.2"
    elif mutation == "port":
        changed["connection"]["db_port"] = 3307
    elif mutation == "tls":
        changed["connection"]["tls_mode"] = "disabled"
    elif mutation == "ssh_ref":
        changed["connection"]["ssh_profile_ref"] = "different-reference"
    elif mutation == "system":
        system_id = g.other_system.id
    elif mutation == "actor":
        actor = g.other_actor
    with pytest.raises(PlatformError) as mismatch:
        checked_test(g.db, actor, test.id, system_id, changed, g.config)
    assert mismatch.value.status_code in (404, 409)


def test_preflight_expired_failed_and_consumed_are_distinct_from_valid(graph):
    g = graph
    test = passed_test(g)
    for changes in (
        {"expires_at": utcnow() - timedelta(seconds=1)},
        {"status": "failed"},
        {"consumed_at": utcnow()},
    ):
        test.status, test.consumed_at, test.expires_at = (
            "succeeded",
            None,
            utcnow() + timedelta(minutes=1),
        )
        for key, value in changes.items():
            setattr(test, key, value)
        with pytest.raises(PlatformError) as invalid:
            checked_test(g.db, g.actor, test.id, g.system.id, g.body, g.config)
        assert_code(invalid, 409, "test_not_valid")


def test_registered_asset_identity_profile_and_no_credential_echo(graph):
    g = graph
    row = registered(g)
    assert row.lifecycle_state == "Ready" and row.enabled is False
    assert row.model_policy == "rules_only" and row.health == "unknown"
    assert row.selected_schemas == ["fixture_schema"]
    assert row.connection["_collection"]["collection_profile"] in ("standard-id", "standard")
    schedules = list(
        g.db.scalars(select(CollectionSchedule).where(CollectionSchedule.instance_id == row.id))
    )
    assert {r.collector for r in schedules} == {"identity_status", "runtime"}
    assert all(not r.enabled for r in schedules)
    response = instance_response(g.db, g.actor, row)
    assert "password" not in str(response) and "test-readonly" not in str(response)
    assert response["has_secret"] is True
    assert response["server_uuid"] == row.expected_server_uuid
    serialized = serialize_test(g.db.scalar(select(ConnectionTest)))
    assert "password" not in str(serialized) and "not-a-production-password" not in str(serialized)


def test_duplicate_uuid_conflict_does_not_reveal_asset_or_other_owner(graph):
    g = graph
    one = registered(g)
    second = passed_test(g, server_uuid=one.expected_server_uuid)
    with pytest.raises(PlatformError) as duplicate:
        register_instance(g.db, g.actor, g.config, {**g.body, "test_id": second.id})
    assert_code(duplicate, 409, "duplicate_database_identity")
    assert one.id not in str(duplicate.value.detail)


def test_pause_cancels_queued_and_blocks_inflight_fenced_results(graph):
    g = graph
    row = registered(g)
    row.enabled, row.lifecycle_state = True, "Active"
    running = enqueue(
        g.db,
        "collect_instance",
        actor_id=g.actor.user_id,
        instance_id=row.id,
        payload={"config_revision": row.config_revision},
    )
    g.db.commit()
    lease = claim(g.factory, "onboarding-test-worker")
    assert lease and lease.id == running.id
    pending = enqueue(
        g.db,
        "collect_instance",
        actor_id=g.actor.user_id,
        instance_id=row.id,
        payload={"config_revision": row.config_revision},
    )
    g.db.commit()
    revision = row.config_revision
    assert stop_collection(g.db, row) == 2
    g.db.commit()
    assert row.lifecycle_state == "Paused" and row.config_revision == revision + 1
    assert g.db.get(Task, pending.id).status == "cancelled"
    with pytest.raises(PlatformError) as fenced:
        check_fence(g.db, lease, config_revision=revision, allow_inactive=True)
    assert fenced.value.detail["code"] in (
        "task_cancelled",
        "configuration_changed",
        "stale_task_fence",
    )


def test_partial_draft_roundtrip_preserves_fields_without_secrets(graph):
    g = graph
    response = g.client.post(
        "/api/v1/connection-drafts", json={"system_id": g.system.id, "name": "partial"}
    )
    assert response.status_code == 201
    draft_id = response.json()["id"]
    response = g.client.patch(
        f"/api/v1/connection-drafts/{draft_id}",
        json={
            "system_id": g.system.id,
            "credentials": g.body["credentials"],
            "connection": {
                "mode": "direct",
                "db_host": "127.0.0.1",
                "db_port": 3306,
                "password": "must-drop",
            },
        },
    )
    assert response.status_code == 200
    saved = response.json()
    assert saved["name"] == "partial" and saved["has_secret"] is True
    assert "password" not in str(saved) and "not-a-production-password" not in str(saved)
    assert saved["credential_ref"]
    # Same organization management scope does not grant access to another author's draft.
    g.client.app.dependency_overrides[current_actor] = lambda: g.other_actor
    assert g.client.get(f"/api/v1/connection-drafts/{draft_id}").status_code == 404


def test_activate_same_key_replays_same_task_even_after_transition(graph):
    g = graph
    row = registered(g)
    payload = {"config_revision": row.config_revision}
    headers = {"Idempotency-Key": "activate-transport-retry"}
    one = g.client.post(f"/api/v1/instances/{row.id}/activate", json=payload, headers=headers)
    assert one.status_code == 202
    two = g.client.post(f"/api/v1/instances/{row.id}/activate", json=payload, headers=headers)
    assert two.status_code == 202, two.json()
    assert one.json()["id"] == two.json()["id"]


def test_lifecycle_api_uses_documented_snake_case_values(graph):
    g = graph
    row = registered(g)
    assert instance_response(g.db, g.actor, row)["lifecycle"] == "ready"


def test_registration_transport_replay_returns_same_asset_but_changed_body_rejected(graph):
    g = graph
    test = passed_test(g)
    body = {**g.body, "test_id": test.id}
    first = register_instance(g.db, g.actor, g.config, deepcopy(body))
    g.db.commit()
    replay = register_instance(g.db, g.actor, g.config, deepcopy(body))
    assert replay.id == first.id
    assert g.db.scalar(select(func.count()).select_from(Instance)) == 1
    with pytest.raises(PlatformError) as changed:
        register_instance(g.db, g.actor, g.config, {**body, "name": "different-body"})
    assert_code(changed, 409, "test_not_valid")


def test_activation_idempotency_key_cannot_cross_instance_or_payload(graph):
    g = graph
    one = registered(g)
    two = registered(g)
    headers = {"Idempotency-Key": "one-key-different-target"}
    assert (
        g.client.post(
            f"/api/v1/instances/{one.id}/activate",
            json={"config_revision": one.config_revision},
            headers=headers,
        ).status_code
        == 202
    )
    different_target = g.client.post(
        f"/api/v1/instances/{two.id}/activate",
        json={"config_revision": two.config_revision},
        headers=headers,
    )
    assert different_target.status_code == 409
    different_payload = g.client.post(
        f"/api/v1/instances/{one.id}/activate", json={"config_revision": 999}, headers=headers
    )
    assert different_payload.status_code == 409
    assert (
        g.db.scalar(select(func.count()).select_from(Task).where(Task.type == "activate_instance"))
        == 1
    )


def test_instance_secret_ownership_preflight_cannot_outlive_management_revocation(graph):
    g = graph
    row = registered(g)
    # This asset's original credential came from another operator, not this actor's input.
    original = g.db.scalar(select(ConnectionTest))
    original.created_by = g.other_actor.user_id
    g.db.commit()
    managed = {key: value for key, value in g.body.items() if key != "credentials"}
    managed.update(
        instance_id=row.id, config_revision=row.config_revision, credential_ref=row.credential_ref
    )
    create_connection_test(g.db, g.actor, g.config, managed)
    g.db.commit()
    role = g.db.scalar(select(Role).where(Role.name == "onboarding-test-control"))
    role.permissions = ["asset.onboard"]
    g.db.commit()
    current = actor_for_user(g.db, g.actor.user_id)
    escaped = {
        key: value
        for key, value in managed.items()
        if key not in ("instance_id", "config_revision")
    }
    with pytest.raises(HTTPException):
        create_connection_test(g.db, current, g.config, escaped)


def test_rotate_transport_retry_replays_before_creating_or_staging_another_secret(graph):
    g = graph
    row = registered(g)
    body = {
        "config_revision": row.config_revision,
        "credentials": {"username": "rotated-test-user", "password": "rotated-test-password"},
    }
    headers = {"Idempotency-Key": "rotate-transport-retry"}
    first = g.client.post(
        f"/api/v1/instances/{row.id}/credentials/rotate", json=body, headers=headers
    )
    assert first.status_code == 202, first.json()
    revision = row.config_revision
    secret_count = len(list(g.config.secrets_dir.glob("*.fernet")))
    replay = g.client.post(
        f"/api/v1/instances/{row.id}/credentials/rotate", json=body, headers=headers
    )
    assert replay.status_code == 202, replay.json()
    assert replay.json()["id"] == first.json()["id"] and row.config_revision == revision
    assert len(list(g.config.secrets_dir.glob("*.fernet"))) == secret_count


@pytest.mark.parametrize("source", ["host_profile_ref", "log_profile_ref"])
def test_source_reference_cannot_be_added_or_changed_after_preflight(graph, source):
    g = graph
    test = passed_test(g)
    with pytest.raises(PlatformError) as changed:
        checked_test(
            g.db,
            g.actor,
            test.id,
            g.system.id,
            {**g.body, source: "different-private-source"},
            g.config,
        )
    assert_code(changed, 409, "test_configuration_mismatch")


@pytest.mark.parametrize("acknowledgement", [None, False, "true", 1])
def test_partial_capability_requires_explicit_boolean_acceptance(graph, acknowledgement):
    g = graph
    test = passed_test(g)
    test.results = {
        "capabilities": [
            {"collector": "identity_status", "availability": "available"},
            {
                "collector": "host",
                "availability": "not_configured",
                "reason_code": "host_profile_required",
            },
        ]
    }
    g.db.commit()
    body = {**g.body, "test_id": test.id, "accept_capability_gaps": acknowledgement}
    with pytest.raises(PlatformError) as denied:
        register_instance(g.db, g.actor, g.config, body)
    assert_code(denied, 422, "capability_gaps_confirmation_required")
    assert g.db.scalar(select(func.count()).select_from(Instance)) == 0
    row = register_instance(g.db, g.actor, g.config, {**body, "accept_capability_gaps": True})
    assert row.lifecycle_state == "Ready" and row.health == "unknown"


def test_api_rejects_chunked_over_limit_body_before_route_or_json_parse(graph):
    g = graph
    g.config.public_origin = "http://127.0.0.1:18080"
    app = create_app(g.config, g.factory)
    received = []
    sent = []
    chunks = [b'{"password":"', b"x" * (1024 * 1024), b"y" * (1024 * 1024), b'"}']

    async def receive():
        index = len(received)
        received.append(index)
        return {"type": "http.request", "body": chunks[index], "more_body": index < len(chunks) - 1}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "root_path": "",
        "path": "/api/v1/auth/login",
        "raw_path": b"/api/v1/auth/login",
        "query_string": b"",
        "headers": [
            (b"host", b"127.0.0.1:18080"),
            (b"transfer-encoding", b"chunked"),
            (b"content-type", b"application/json"),
        ],
        "server": ("127.0.0.1", 18080),
        "client": ("127.0.0.1", 12345),
    }
    asyncio.run(app(scope, receive, send))
    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 413
    body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    error = json.loads(body)["error"]
    assert error["code"] == "request_too_large" and error["request_id"]
    assert len(received) == 3  # Stop reading as soon as the running bound is exceeded.
    assert "password" not in body.decode() and len(body) < 1024


@pytest.mark.parametrize("operation", ["capabilities/recheck", "credentials/rotate"])
def test_recheck_and_rotation_keep_tested_source_profile_bindings(graph, operation):
    g = graph
    g.body.update(
        host_profile_ref="private-host-reference", log_profile_ref="private-log-reference"
    )
    row = registered(g)
    body = {
        "config_revision": row.config_revision,
        "credentials": {"username": "rotated-readonly", "password": "test-new-password"},
    }
    response = g.client.post(f"/api/v1/instances/{row.id}/{operation}", json=body)
    assert response.status_code == 202, response.json()
    task = g.db.get(Task, response.json()["id"])
    test = g.db.get(ConnectionTest, task.payload["test_id"])
    assert test.config["host_profile_ref"] == "private-host-reference"
    assert test.config["log_profile_ref"] == "private-log-reference"


def test_revalidation_preserves_source_profiles_in_final_asset_configuration(graph):
    g = graph
    g.body.update(
        host_profile_ref="private-host-reference", log_profile_ref="private-log-reference"
    )
    row = registered(g)
    body = {key: value for key, value in g.body.items() if key != "credentials"}
    body.update(
        instance_id=row.id, config_revision=row.config_revision, credential_ref=row.credential_ref
    )
    task = create_connection_test(g.db, g.actor, g.config, body)
    test = g.db.get(ConnectionTest, task.payload["test_id"])
    test.status = "succeeded"
    test.server_uuid = row.expected_server_uuid
    test.results = {"capabilities": []}
    g.db.commit()
    response = g.client.post(
        f"/api/v1/instances/{row.id}/revalidate",
        json={"test_id": test.id, "config_revision": row.config_revision},
    )
    assert response.status_code == 200, response.json()
    assert row.connection["host_profile_ref"] == "private-host-reference"
    assert row.connection["log_profile_ref"] == "private-log-reference"


@pytest.mark.parametrize("operation", ["capabilities/recheck", "credentials/rotate"])
def test_instance_management_can_maintain_existing_asset_without_onboarding_new_assets(
    graph, operation
):
    g = graph
    row = registered(g)
    role = g.db.scalar(select(Role).where(Role.name == "onboarding-test-control"))
    role.permissions = ["asset.manage"]
    grant = g.db.scalar(select(UserGrant).where(UserGrant.user_id == g.actor.user_id))
    grant.scope_type, grant.scope_id = "instance", row.id
    g.db.commit()
    body = {
        "config_revision": row.config_revision,
        "credentials": {"username": "replacement-readonly", "password": "test-new-password"},
    }
    response = g.client.post(f"/api/v1/instances/{row.id}/{operation}", json=body)
    assert response.status_code == 202, response.json()
    task = g.db.get(Task, response.json()["id"])
    test_id = task.payload["test_id"]
    assert g.client.get(f"/api/v1/connections/tests/{test_id}").status_code == 200
    assert g.client.post("/api/v1/connections/test", json=g.body).status_code in (403, 404)
    role.permissions = []
    g.db.commit()
    assert g.client.get(f"/api/v1/connections/tests/{test_id}").status_code in (403, 404)


def test_preflight_credential_reference_survives_first_draft_save_without_plaintext(graph):
    g = graph
    test = passed_test(g)
    body = {key: value for key, value in g.body.items() if key != "credentials"}
    response = g.client.post(
        "/api/v1/connection-drafts", json={**body, "credential_ref": test.secret_ref}
    )
    assert response.status_code == 201, response.json()
    saved = response.json()
    assert saved["has_secret"] is True and saved["credential_ref"] == test.secret_ref
    restored = g.client.get(f"/api/v1/connection-drafts/{saved['id']}").json()
    assert restored["credential_ref"] == test.secret_ref and "password" not in str(restored)


def test_new_onboarding_draft_cannot_erase_existing_asset_credential_origin(graph):
    g = graph
    test = passed_test(g)
    body = {key: value for key, value in g.body.items() if key != "credentials"}
    draft = g.client.post(
        "/api/v1/connection-drafts", json={**body, "credential_ref": test.secret_ref}
    ).json()
    row = register_instance(g.db, g.actor, g.config, {**g.body, "test_id": test.id})
    g.db.commit()
    denied = g.client.post(
        "/api/v1/connection-drafts",
        json={**body, "credential_ref": row.credential_ref, "instance_id": row.id},
    )
    assert (
        denied.status_code == 422
        and denied.json()["detail"]["code"] == "existing_asset_draft_forbidden"
    )
    role = g.db.scalar(select(Role).where(Role.name == "onboarding-test-control"))
    role.permissions = ["asset.onboard"]
    g.db.commit()
    with pytest.raises(HTTPException):
        secret_for_request(
            g.db,
            actor_for_user(g.db, g.actor.user_id),
            SecretStore(g.config),
            {"credential_ref": draft["credential_ref"]},
            g.system.id,
        )
