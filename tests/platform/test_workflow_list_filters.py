"""User-visible list filters must apply before counting and pagination."""

from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from platform_app import models as m
from platform_app.common import iso
from platform_app.identity import current_actor
from tests.platform.test_events import db, graph
from tests.platform.test_ai_scope import ai_env


def client_for(actor, router):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_actor] = lambda: actor
    return TestClient(app)


def test_event_search_window_and_organization_filter_before_count(db):
    from platform_app.db import utcnow
    from platform_app.routes.events import router

    actor, a, b, _ = graph(db)
    stamp = utcnow() - timedelta(minutes=5)
    keep = m.Event(instance_id=a.id, title="literal 10% lookup", created_at=stamp)
    other = m.Event(instance_id=a.id, title="literal 100 lookup", created_at=stamp)
    late = m.Event(
        instance_id=a.id, title="literal 10% lookup", created_at=stamp + timedelta(minutes=2)
    )
    hidden = m.Event(instance_id=b.id, title="literal 10% lookup", created_at=stamp)
    db.add_all([keep, other, late, hidden])
    db.commit()
    client = client_for(actor, router)
    params = {"q": "10%", "from": iso(stamp), "to": iso(stamp + timedelta(minutes=1)), "limit": 1}
    value = client.get("/api/v1/events", params=params).json()
    assert value["total"] == 1 and [r["id"] for r in value["items"]] == [keep.id]
    assert value["next_cursor"] is None
    assert (
        client.get("/api/v1/events", params={**params, "system_id": "unknown"}).json()["total"] == 0
    )
    assert (
        client.get(
            "/api/v1/events", params={**params, "environment": "not-the-environment"}
        ).json()["total"]
        == 0
    )
    assert (
        client.get("/api/v1/events", params={**params, "from": "without-timezone"}).status_code
        == 422
    )


def test_alert_search_uses_real_rule_and_first_seen_time(db):
    from platform_app.db import utcnow
    from platform_app.routes.events import router
    from tests.platform.test_events import observe
    from platform_app.events import evaluate

    actor, a, _, _ = graph(db)
    stamp = utcnow() - timedelta(minutes=5)
    for seconds in (0, 30, 60):
        run, _ = observe(db, a, stamp + timedelta(seconds=seconds))
        evaluate(db, a.id, run.id, now=stamp + timedelta(seconds=seconds))
    db.commit()
    client = client_for(actor, router)
    params = {
        "q": "locks_present",
        "from": iso(stamp - timedelta(seconds=1)),
        "to": iso(stamp + timedelta(minutes=2)),
    }
    value = client.get("/api/v1/alerts", params=params).json()
    assert value["total"] == 1
    assert client.get("/api/v1/alerts", params={**params, "q": "unknown-rule"}).json()["total"] == 0
    assert (
        client.get("/api/v1/alerts", params={**params, "department_id": "unknown"}).json()["total"]
        == 0
    )
    assert client.get("/api/v1/alerts", params={**params, "to": iso(stamp)}).json()["total"] == 0


def test_investigation_search_and_creation_window_do_not_leak_hidden_report(ai_env):
    from platform_app.routes.investigations import router

    env = ai_env
    db = env["db"]
    stamp = env["start"]
    keep = env["investigation"]
    keep.created_at = stamp
    keep.question = "why 10% lock?"
    late = m.Investigation(
        created_by=env["actor"].user_id,
        question="why 10% lock?",
        window_start=stamp,
        window_end=env["end"],
        permission_version=1,
        created_at=env["end"],
    )
    hidden = m.Investigation(
        created_by=env["actor"].user_id,
        question="why 10% lock?",
        window_start=stamp,
        window_end=env["end"],
        permission_version=1,
        created_at=stamp,
    )
    db.add_all([late, hidden])
    db.flush()
    db.add_all(
        [
            m.InvestigationResource(investigation_id=keep.id, instance_id=env["a"].id),
            m.InvestigationResource(investigation_id=late.id, instance_id=env["a"].id),
            m.InvestigationResource(investigation_id=hidden.id, instance_id=env["b"].id),
        ]
    )
    db.commit()
    client = client_for(env["actor"], router)
    params = {"q": "10%", "from": iso(stamp), "to": iso(stamp + timedelta(minutes=1)), "limit": 1}
    value = client.get("/api/v1/investigations", params=params).json()
    assert value["total"] == 1 and [r["id"] for r in value["items"]] == [keep.id]
    assert value["next_cursor"] is None
    assert (
        client.get("/api/v1/investigations", params={**params, "q": "unmatched"}).json()["total"]
        == 0
    )
    assert (
        client.get("/api/v1/investigations", params={**params, "from": "invalid"}).status_code
        == 422
    )


def test_direct_python_route_calls_keep_plain_defaults(db):
    from platform_app.routes.events import events, alerts
    from platform_app.routes.investigations import list_investigations

    actor, _, _, _ = graph(db)
    for route in (events, alerts, list_investigations):
        assert route(actor=actor)["total"] == 0


def test_event_list_rejects_mixed_scope_current_manifest_before_total(db):
    from platform_app.db import utcnow
    from platform_app.routes.events import router

    actor, a, b, _ = graph(db)
    stamp = utcnow()
    event = m.Event(instance_id=a.id, title="hidden mixed manifest")
    db.add(event)
    db.flush()
    manifest = m.EvidenceManifest(
        owner_type="event",
        owner_id=event.id,
        instance_ids=[a.id, b.id],
        window_start=stamp - timedelta(minutes=1),
        window_end=stamp,
        manifest_sha256="0" * 64,
        retain_until=stamp + timedelta(days=90),
    )
    db.add(manifest)
    db.flush()
    event.manifest_id = manifest.id
    db.commit()
    client = client_for(actor, router)
    response = client.get("/api/v1/events", params={"instance_id": a.id, "system_id": a.system_id})
    assert response.json()["total"] == 0
    assert event.title not in response.text and manifest.id not in response.text
    assert client.get("/api/v1/events/" + event.id).status_code == 404
