"""Independent retention acceptance cases; all data is isolated unit data."""

from datetime import timedelta

from sqlalchemy import select

from tests.platform.test_events import db, graph, observe
from platform_app import models as m
from platform_app.common import iso
from platform_app.db import utcnow
from platform_app.evidence import append_batch, create_manifest, purge_expired


def test_unreferenced_expired_digest_snapshot_does_not_grow_forever(db):
    _, instance, _, _ = graph(db)
    now = utcnow()
    run, _ = observe(db, instance, now - timedelta(days=100), collector="statement_digest")
    snapshot = m.DigestSnapshot(
        instance_id=instance.id,
        run_id=run.id,
        observed_at=now - timedelta(days=100),
        digest="a" * 64,
        digest_text="SELECT ?",
    )
    db.add(snapshot)
    db.flush()
    identifier = snapshot.id
    purge_expired(db, now)
    db.expire_all()
    assert db.get(m.DigestSnapshot, identifier) is None


def test_not_applicable_replication_does_not_make_complete_frozen_window_partial(db, monkeypatch):
    from platform_app import metrics

    _, instance, _, _ = graph(db)
    now = utcnow()
    _, item = observe(
        db,
        instance,
        now - timedelta(minutes=1),
        source="mysql.replication",
        availability="not_applicable",
        rows=[],
    )
    item.coverage = {**item.coverage, "complete": False}
    db.info["platform_config"] = object()
    monkeypatch.setattr(
        metrics,
        "materialize_metric_window",
        lambda *args: {
            "items": [],
            "coverage": {"materialized": True, "complete": True, "reason_codes": []},
        },
    )
    manifest = create_manifest(
        db, "event", "single-server", [instance.id], now - timedelta(minutes=2), now
    )
    coverage = db.scalar(
        select(m.ManifestItem).where(
            m.ManifestItem.manifest_id == manifest.id, m.ManifestItem.item_type == "coverage"
        )
    )
    assert coverage.payload["gaps"] == []
    assert coverage.payload["coverage_complete"] is True


def test_cleanup_budget_applies_to_whole_transaction_not_each_table(db):
    _, instance, _, _ = graph(db)
    now = utcnow()
    _, item = observe(db, instance, now - timedelta(days=100))
    item.expires_at = now - timedelta(days=1)
    manifest = create_manifest(
        db,
        "event",
        "expired",
        [instance.id],
        item.observed_at - timedelta(seconds=1),
        item.observed_at + timedelta(seconds=1),
    )
    manifest.retain_until = now - timedelta(seconds=1)
    for pin in db.scalars(select(m.EvidencePin).where(m.EvidencePin.manifest_id == manifest.id)):
        pin.retain_until = manifest.retain_until
    db.flush()
    stats = purge_expired(db, now, limit=1)
    assert sum(value for name, value in stats.items() if name.endswith("_deleted")) <= 1


def test_new_evidence_honors_active_extended_retention(db):
    _, instance, _, _ = graph(db)
    now = utcnow()
    run, _ = observe(db, instance, now)
    db.add(m.PlatformSetting(key="retention", value={"evidence_days": 30}))
    db.flush()
    ids = append_batch(
        db,
        instance,
        run,
        {
            "observed_at": iso(now),
            "evidence_items": [
                {
                    "source": "mysql.identity",
                    "observed_at": iso(now),
                    "availability": "available",
                    "coverage": {"complete": True},
                    "payload": {},
                }
            ],
        },
    )
    item = db.get(m.Evidence, ids[0])
    assert item.expires_at >= now + timedelta(days=29)


def test_extending_retention_also_protects_existing_expired_rows(db):
    _, instance, _, _ = graph(db)
    now = utcnow()
    _, item = observe(db, instance, now - timedelta(days=15))
    item.expires_at = now - timedelta(days=1)
    db.add(m.PlatformSetting(key="retention", value={"evidence_days": 30}))
    db.flush()
    identifier = item.id
    purge_expired(db, now)
    db.expire_all()
    assert db.get(m.Evidence, identifier) is not None


def test_cleanup_failure_does_not_prevent_collection_queue_claims(db, monkeypatch):
    from types import SimpleNamespace
    from platform_app import worker
    from platform_app.db import create_session_factory

    factory = create_session_factory(db.get_bind())
    claimed = []
    monkeypatch.setattr(worker, "create_db_engine", lambda config: db.get_bind())
    monkeypatch.setattr(worker, "create_session_factory", lambda *args: factory)
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker, "ensure_default_rules", lambda session: None)
    monkeypatch.setattr(worker, "heartbeat", lambda *args: None)
    monkeypatch.setattr(worker, "schedule_due", lambda *args: None)
    monkeypatch.setattr(worker, "clock_tick", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "reconcile_interrupted", lambda *args: None)
    monkeypatch.setattr(worker, "claim", lambda *args: claimed.append(args[2]))

    def cleanup_failure(*args, **kwargs):
        raise OSError("isolated cleanup failure")

    monkeypatch.setattr(worker, "purge_expired", cleanup_failure)
    try:
        worker.serve(SimpleNamespace(collection_workers=1), once=True, no_bridge=True)
    except OSError:
        pass
    assert "collection" in claimed
