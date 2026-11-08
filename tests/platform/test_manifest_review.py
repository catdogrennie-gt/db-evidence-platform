"""Independent whole-manifest scope regressions, including inconsistent history."""

from datetime import timedelta
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from tests.platform.test_events import db, graph, manual_event, observe
from platform_app import models as m
from platform_app.db import utcnow
from platform_app.evidence import create_manifest
from platform_app.events import evaluate
from platform_app.routes.manifests import manifest_detail


@pytest.mark.parametrize("declared_foreign", [True, False])
def test_manifest_checks_all_item_sources_and_authoritative_evidence_before_count_or_page(
    db, declared_foreign
):
    actor, a, b, _ = graph(db)
    now = utcnow()
    event = manual_event(db, a)
    manifest = create_manifest(db, "event", event.id, [a.id], now - timedelta(minutes=1), now)
    _, foreign = observe(db, b, now - timedelta(seconds=10))
    db.add(
        m.ManifestItem(
            id="ffffffff-ffff-4fff-bfff-ffffffffffff",
            manifest_id=manifest.id,
            instance_id=b.id if declared_foreign else a.id,
            item_type="evidence",
            evidence_id=foreign.id,
            payload={"source": "mysql.lock_waits"},
            payload_sha256="f" * 64,
        )
    )
    db.flush()
    # The first page contains only the permitted coverage marker. A foreign
    # reference later in the frozen object must still deny before total leaks.
    with pytest.raises(HTTPException):
        manifest_detail(manifest.id, limit=1, actor=actor)


def test_alert_manifest_checks_actual_owner_instance_not_only_declared_manifest_scope(db):
    actor, a, b, _ = graph(db)
    start = utcnow() - timedelta(minutes=3)
    for seconds in (0, 30, 60):
        run, _ = observe(db, b, start + timedelta(seconds=seconds))
        evaluate(db, b.id, run.id, now=start + timedelta(seconds=seconds))
    occurrence = db.scalar(select(m.AlertOccurrence).where(m.AlertOccurrence.instance_id == b.id))
    assert occurrence is not None
    manifest = create_manifest(
        db, "alert", occurrence.id, [a.id], start - timedelta(minutes=1), start
    )
    with pytest.raises(HTTPException):
        manifest_detail(manifest.id, actor=actor)
