"""A provenance drawer must not become an owner or raw-data bypass."""

from datetime import timedelta
import pytest
from fastapi import HTTPException
from tests.platform.test_events import db, graph, manual_event, observe
from platform_app import models as m
from platform_app.db import utcnow
from platform_app.evidence import create_manifest, digest


def test_manifest_checks_all_sources_owner_permission_and_revocation(db):
    from platform_app.routes.manifests import manifest_detail

    actor, a, b, grant = graph(db)
    now = utcnow()
    event = manual_event(db, a)
    manifest = create_manifest(db, "event", event.id, [a.id], now - timedelta(minutes=1), now)
    assert manifest_detail(manifest.id, actor=actor)["id"] == manifest.id
    role = db.get(m.Role, grant.role_id)
    role.permissions = ["data.read"]
    db.flush()
    with pytest.raises(HTTPException) as denied:
        manifest_detail(manifest.id, actor=actor)
    assert denied.value.status_code == 403
    role.permissions = ["data.read", "event.read"]
    db.flush()
    manifest.instance_ids = [a.id, b.id]
    db.flush()
    with pytest.raises(HTTPException) as denied:
        manifest_detail(manifest.id, actor=actor)
    assert denied.value.status_code == 404


def test_manifest_only_returns_bounded_safe_references_not_restricted_payload(db):
    from platform_app.routes.manifests import manifest_detail

    actor, a, _, _ = graph(db)
    now = utcnow()
    event = manual_event(db, a)
    _, item = observe(db, a, now - timedelta(seconds=10))
    item.sensitivity = "restricted"
    item.payload = {"sql_text": "do-not-project-private-marker"}
    item.payload_sha256 = digest(item.payload)
    db.flush()
    manifest = create_manifest(db, "event", event.id, [a.id], now - timedelta(minutes=1), now)
    response = manifest_detail(manifest.id, limit=1, actor=actor)
    assert len(response["items"]) == 1 and response["total"] == 2 and response["next_cursor"] == "1"
    assert "do-not-project-private-marker" not in str(response)
    assert all("payload" not in row for row in response["items"])
    assert response["coverage"]["coverage_complete"] is False
    with pytest.raises(HTTPException):
        manifest_detail(manifest.id, cursor="-1", actor=actor)


def test_unknown_manifest_owner_is_not_readable_just_with_data_read(db):
    from platform_app.routes.manifests import manifest_detail

    actor, a, _, _ = graph(db)
    now = utcnow()
    manifest = create_manifest(
        db, "future_owner", "unknown", [a.id], now - timedelta(minutes=1), now
    )
    with pytest.raises(HTTPException) as denied:
        manifest_detail(manifest.id, actor=actor)
    assert denied.value.status_code == 404
