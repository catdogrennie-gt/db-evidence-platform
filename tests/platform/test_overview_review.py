"""Dashboard counts obey the same complete-source scope as event details."""

from datetime import timedelta

import pytest

from platform_app import models as m
from platform_app.common import PlatformError
from platform_app.db import utcnow
from platform_app.routes.observability import overview_response
from tests.platform.test_events import db, graph


@pytest.mark.parametrize("foreign_source", ["manifest", "manual_change"])
def test_dashboard_excludes_whole_event_before_count_and_top_twenty(db, foreign_source):
    actor, a, b, _ = graph(db)
    visible = m.Event(instance_id=a.id, title="visible open event")
    hidden = m.Event(instance_id=a.id, title="foreign scope must stay hidden")
    closed = m.Event(instance_id=a.id, title="closed must not inflate inbox", status="closed")
    db.add_all([visible, hidden, closed])
    db.flush()
    stamp = utcnow()
    if foreign_source == "manifest":
        manifest = m.EvidenceManifest(
            owner_type="event",
            owner_id=hidden.id,
            instance_ids=[a.id, b.id],
            window_start=stamp - timedelta(minutes=1),
            window_end=stamp,
            manifest_sha256="0" * 64,
            retain_until=stamp + timedelta(days=1),
        )
        db.add(manifest)
        db.flush()
        hidden.manifest_id = manifest.id
    else:
        db.add(
            m.ManualChange(
                instance_id=b.id,
                event_id=hidden.id,
                created_by=actor.user_id,
                event_at=stamp,
                description="foreign source fixture",
            )
        )
    db.commit()
    result = overview_response(actor, system_id=a.system_id, instance_id=a.id)
    assert result["counts"]["open_events"] == 1
    assert [event["id"] for event in result["events"]] == [visible.id]
    assert hidden.title not in str(result) and closed.title not in str(result)


def test_dashboard_does_not_convert_event_scope_scan_failure_to_zero(db, monkeypatch):
    actor, *_ = graph(db)

    def exhausted(*args, **kwargs):
        raise PlatformError(503, "event_scope_scan_limit", "Narrow the selected window.")

    monkeypatch.setattr("platform_app.events.list_events", exhausted)
    with pytest.raises(PlatformError) as failure:
        overview_response(actor)
    assert failure.value.status_code == 503


def test_dashboard_metadata_administrator_never_reads_events(db, monkeypatch):
    from platform_app.access import ROLE_PERMISSIONS, actor_for_user

    actor, a, _, grant = graph(db)
    db.get(m.Role, grant.role_id).permissions = sorted(ROLE_PERMISSIONS["platform_admin"])
    db.add(m.Event(instance_id=a.id, title="diagnostic title must not appear"))
    db.commit()

    def forbidden(*args, **kwargs):
        raise AssertionError("Control-plane metadata must not read diagnostic events")

    monkeypatch.setattr("platform_app.events.list_events", forbidden)
    result = overview_response(actor_for_user(db, actor.user_id))
    assert result["metadata_only"] is True
    assert result["events"] == [] and result["counts"]["open_events"] is None


def test_dashboard_excludes_archived_primary_assets_even_if_history_is_authorized(db):
    actor, a, b, grant = graph(db)
    db.add(
        m.UserGrant(
            user_id=actor.user_id, role_id=grant.role_id, scope_type="instance", scope_id=b.id
        )
    )
    b.lifecycle_state = "Archived"
    db.add_all(
        [
            m.Event(instance_id=a.id, title="visible active"),
            m.Event(instance_id=b.id, title="archived history"),
        ]
    )
    db.commit()
    result = overview_response(actor)
    assert result["counts"]["registered"] == 1
    assert result["counts"]["open_events"] == 1
    assert all(event["instance_id"] == a.id for event in result["events"])
