"""Unacknowledged inbox is a scoped event view, not a notification sender."""

from datetime import timedelta
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from platform_app import models as m
from platform_app.access import actor_for_user
from platform_app.db import Base, create_db_engine, create_session_factory, utcnow
from platform_app.identity import current_actor
from platform_app.routes.inbox import router


@pytest.fixture
def inbox_graph():
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    db = factory()
    department = m.Department(name="inbox")
    db.add(department)
    db.flush()
    system = m.System(name="inbox", department_id=department.id)
    db.add(system)
    db.flush()
    a = m.Instance(
        name="A", system_id=system.id, endpoint="127.0.0.1", expected_server_uuid="inbox-a"
    )
    b = m.Instance(
        name="B", system_id=system.id, endpoint="127.0.0.1", expected_server_uuid="inbox-b"
    )
    user = m.User(username="inbox-reader", password_hash="unused")
    role = m.Role(name="inbox-role", permissions=["data.read", "event.read"])
    db.add_all([a, b, user, role])
    db.flush()
    db.add(m.UserGrant(user_id=user.id, role_id=role.id, scope_type="instance", scope_id=a.id))
    events = [
        m.Event(instance_id=a.id, title="Authorized open"),
        m.Event(instance_id=b.id, title="Hidden open"),
        m.Event(instance_id=a.id, title="Acknowledged", status="acknowledged"),
        m.Event(instance_id=a.id, title="Closed", status="closed"),
    ]
    db.add_all(events)
    db.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_actor] = lambda: actor_for_user(db, user.id)
    yield SimpleNamespace(
        db=db, a=a, b=b, events=events, role=role, user=user, system=system, client=TestClient(app)
    )
    db.close()
    engine.dispose()


def test_inbox_counts_only_current_authorized_unacknowledged_events(inbox_graph):
    g = inbox_graph
    response = g.client.get("/api/v1/events/inbox")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1 and data["suppressed_count"] == 0
    assert [row["title"] for row in data["items"]] == ["Authorized open"]
    assert "Hidden open" not in str(data) and "Closed" not in str(data) and data["as_of"]


def test_maintenance_suppression_is_explicit_and_expires_without_changing_event(inbox_graph):
    g = inbox_graph
    window = m.MaintenanceWindow(
        system_id=g.system.id,
        instance_id=g.a.id,
        created_by=g.user.id,
        starts_at=utcnow() - timedelta(minutes=1),
        ends_at=utcnow() + timedelta(minutes=1),
        reason="planned acceptance",
        policy={"suppress_notifications": True},
    )
    g.db.add(window)
    g.db.commit()
    first = g.client.get("/api/v1/events/inbox").json()
    assert (
        first["total"] == 0
        and first["suppressed_count"] == 1
        and first["suppressed"][0]["reason"] == "active_maintenance"
    )
    assert g.db.get(m.Event, g.events[0].id).status == "open"
    window.ends_at = utcnow() - timedelta(seconds=1)
    g.db.commit()
    second = g.client.get("/api/v1/events/inbox").json()
    assert second["total"] == 1 and second["suppressed_count"] == 0


def test_inbox_rejects_control_only_and_filters_mixed_source_objects_before_count(inbox_graph):
    g = inbox_graph
    g.db.add(
        m.ManualChange(
            instance_id=g.b.id,
            event_id=g.events[0].id,
            event_at=utcnow(),
            description="hidden source",
            created_by=g.user.id,
        )
    )
    g.db.commit()
    data = g.client.get("/api/v1/events/inbox").json()
    assert data["total"] == 0 and data["items"] == []
    g.role.permissions = ["platform.read"]
    g.db.commit()
    assert g.client.get("/api/v1/events/inbox").status_code == 403


def test_inbox_scope_filters_and_acknowledgement_change_current_count(inbox_graph):
    g = inbox_graph
    assert (
        g.client.get("/api/v1/events/inbox", params={"system_id": "unknown-system"}).json()["total"]
        == 0
    )
    assert g.client.get("/api/v1/events/inbox", params={"instance_id": g.b.id}).status_code == 404
    g.events[0].status = "acknowledged"
    g.events[0].version += 1
    g.db.commit()
    data = g.client.get("/api/v1/events/inbox").json()
    assert data["total"] == 0 and data["items"] == []


def test_inbox_environment_uses_system_ownership(inbox_graph):
    g = inbox_graph
    g.system.environment = "staging"
    g.db.commit()
    response = g.client.get("/api/v1/events/inbox", params={"environment": "staging"})
    assert response.status_code == 200 and response.json()["total"] == 1
    assert (
        g.client.get("/api/v1/events/inbox", params={"environment": "production"}).json()["total"]
        == 0
    )
