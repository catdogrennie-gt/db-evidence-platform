"""Non-AI evidence preservation is exercised against the real purge boundary."""

from datetime import timedelta
from sqlalchemy import select, func
from tests.platform.test_events import db, graph, observe, manual_event
from platform_app import models as m
from platform_app.db import utcnow
from platform_app.evidence import create_manifest


def raw_material(db, instance, stamp):
    run, item = observe(db, instance, stamp)
    item.expires_at = utcnow() - timedelta(seconds=1)
    frame = m.MetricFrame(
        run_id=run.id,
        instance_id=instance.id,
        collector="runtime",
        server_uuid=instance.expected_server_uuid,
        collector_boot_id="test-boot",
        source_epoch="1",
        observed_monotonic_ns=1,
        counter_epoch=1,
        observed_at=stamp,
        uptime_seconds=1,
        raw_values={"lock_waits": 1},
        derived_values={"values": {"lock_waits": 1}},
        rate_validity="first_sample",
    )
    db.add(frame)
    db.flush()
    return item, frame


def purge(db, now, limit=1000):
    from platform_app import evidence

    helper = getattr(evidence, "purge_expired", None)
    assert callable(helper), (
        "Missing implementation: evidence.purge_expired(db, now=None, limit=1000); pins alone do not demonstrate retention cleanup safety."
    )
    return helper(db, now=now, limit=limit)


def test_non_ai_event_manifest_protects_evidence_and_raw_metric_frame_through_purge(db):
    _, a, _, _ = graph(db)
    now = utcnow()
    old = now - timedelta(days=15)
    protected, frame = raw_material(db, a, old)
    event = manual_event(db, a)
    manifest = create_manifest(
        db, "event", event.id, [a.id], old - timedelta(seconds=1), old + timedelta(seconds=1)
    )
    event.manifest_id = manifest.id
    ordinary, ordinary_frame = raw_material(db, a, old - timedelta(hours=1))
    db.flush()
    assert db.scalar(select(func.count()).select_from(m.Investigation)) == 0
    assert (
        db.scalar(
            select(func.count())
            .select_from(m.EvidencePin)
            .where(m.EvidencePin.evidence_id == protected.id)
        )
        == 1
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(m.ManifestItem)
            .where(m.ManifestItem.metric_run_id == frame.run_id)
        )
        == 1
    )
    protected_id = protected.id
    protected_run = frame.run_id
    ordinary_id = ordinary.id
    ordinary_run = ordinary_frame.run_id
    purge(db, now)
    db.expire_all()
    assert db.get(m.Evidence, protected_id) is not None
    assert db.get(m.MetricFrame, protected_run) is not None
    assert db.get(m.Evidence, ordinary_id) is None
    assert db.get(m.MetricFrame, ordinary_run) is None


def test_verification_without_ai_has_independent_manifest_and_survives_purge(db):
    from platform_app.remediation import create_verification
    from platform_app.common import iso

    actor, a, _, _ = graph(db)
    now = utcnow()
    start = now - timedelta(days=15)
    event = manual_event(db, a)
    ids = []
    for seconds in (0, 30, 60, 120, 150, 180):
        item, frame = raw_material(db, a, start + timedelta(seconds=seconds))
        ids.append((item.id, frame.run_id))
    task = create_verification(
        actor,
        event.id,
        {
            "expected_version": event.version,
            "template": "locks_absent",
            "before": {"from": iso(start), "to": iso(start + timedelta(seconds=61))},
            "after": {
                "from": iso(start + timedelta(seconds=120)),
                "to": iso(start + timedelta(seconds=181)),
            },
            "criteria": {"load_context": {"kind": "unknown"}},
        },
    )
    value = db.get(m.Verification, task.payload["verification_id"])
    manifest = db.get(m.EvidenceManifest, value.manifest_id)
    assert manifest.owner_type == "verification" and manifest.owner_id == value.id
    purge(db, now)
    db.expire_all()
    assert all(db.get(m.Evidence, eid) and db.get(m.MetricFrame, runid) for eid, runid in ids)


def test_expired_manifest_and_pins_release_rows_without_foreign_key_errors(db):
    _, a, _, _ = graph(db)
    now = utcnow()
    stamp = now - timedelta(days=100)
    item, frame = raw_material(db, a, stamp)
    manifest = create_manifest(
        db,
        "event",
        "expired-owner",
        [a.id],
        stamp - timedelta(seconds=1),
        stamp + timedelta(seconds=1),
    )
    manifest.retain_until = now - timedelta(seconds=1)
    for pin in db.scalars(select(m.EvidencePin).where(m.EvidencePin.manifest_id == manifest.id)):
        pin.retain_until = now - timedelta(seconds=1)
    item_id = item.id
    run_id = frame.run_id
    purge(db, now)
    db.expire_all()
    assert db.get(m.Evidence, item_id) is None
    assert db.get(m.MetricFrame, run_id) is None


def test_purge_budget_is_bounded_and_new_data_is_never_removed(db):
    _, a, _, _ = graph(db)
    now = utcnow()
    old = now - timedelta(days=15)
    ids = []
    for seconds in (0, 1, 2):
        item, _ = raw_material(db, a, old - timedelta(seconds=seconds))
        ids.append(item.id)
    _, fresh = observe(db, a, now)
    fresh.expires_at = now + timedelta(days=14)
    db.flush()
    fresh_id = fresh.id
    purge(db, now, limit=1)
    db.expire_all()
    remaining = sum(db.get(m.Evidence, identifier) is not None for identifier in ids)
    assert remaining >= 2
    assert db.get(m.Evidence, fresh_id) is not None
