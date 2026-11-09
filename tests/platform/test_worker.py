"""Worker boundary checks; these do not simulate a live process restart."""

from types import SimpleNamespace

from datetime import timedelta
import pytest
from sqlalchemy import select
from platform_app import models as m, worker
from platform_app.db import utcnow
from platform_app.connectors import ConnectorError
from platform_app.scheduling import enqueue, claim, complete
from tests.platform.test_leases import lease_env, expire


def test_due_profiles_bundle_by_instance_in_fixed_order_without_catchup_storm(lease_env):
    factory, (a, b) = lease_env
    old = utcnow() - timedelta(days=2)
    with factory.begin() as db:
        for instance, name in [
            (a, "host"),
            (a, "runtime"),
            (a, "identity_status"),
            (b, "statement_digest"),
        ]:
            db.add(
                m.CollectionSchedule(
                    instance_id=instance, collector=name, next_due_at=old, config_revision=1
                )
            )
    created = worker.schedule_due(factory)
    assert len(created) == 2
    with factory() as db:
        tasks = {r.instance_id: r for r in db.scalars(select(m.Task))}
        assert tasks[a].payload["collectors"] == ["identity_status", "runtime", "host"]
        assert tasks[b].payload["collectors"] == ["statement_digest"]
        assert all(row.next_due_at > utcnow() for row in db.scalars(select(m.CollectionSchedule)))
    assert worker.schedule_due(factory) == []


def test_pending_bundle_and_stale_config_or_paused_asset_are_not_rescheduled(lease_env):
    factory, (a, b) = lease_env
    with factory.begin() as db:
        enqueue(db, "collect_instance", instance_id=a, payload={"config_revision": 1})
        db.add_all(
            [
                m.CollectionSchedule(instance_id=a, collector="identity_status"),
                m.CollectionSchedule(instance_id=b, collector="identity_status", config_revision=2),
            ]
        )
    assert worker.schedule_due(factory) == []
    with factory.begin() as db:
        schedule = db.scalar(
            select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id == b)
        )
        schedule.config_revision = 1
        db.get(m.Instance, b).lifecycle_state = "Paused"
    assert worker.schedule_due(factory) == []


def test_delayed_sources_rejoin_shared_asset_phase_without_early_collection_or_catchup(
    lease_env, monkeypatch
):
    from datetime import datetime

    factory, (a, _) = lease_env
    anchor = datetime(2026, 1, 1)
    current = anchor + timedelta(seconds=67)
    monkeypatch.setattr(worker, "db_now", lambda db: current)
    with factory.begin() as db:
        db.get(m.Instance, a).created_at = anchor
        db.add_all(
            [
                m.CollectionSchedule(
                    instance_id=a,
                    collector="identity_status",
                    interval_seconds=30,
                    next_due_at=anchor + timedelta(seconds=31),
                    config_revision=1,
                ),
                m.CollectionSchedule(
                    instance_id=a,
                    collector="runtime",
                    interval_seconds=30,
                    next_due_at=anchor + timedelta(seconds=42),
                    config_revision=1,
                ),
                m.CollectionSchedule(
                    instance_id=a,
                    collector="statement_digest",
                    interval_seconds=60,
                    next_due_at=anchor + timedelta(seconds=61),
                    config_revision=1,
                ),
            ]
        )
    tasks = worker.schedule_due(factory)
    assert len(tasks) == 1
    with factory.begin() as db:
        schedules = {r.collector: r for r in db.scalars(select(m.CollectionSchedule))}
        assert schedules["identity_status"].next_due_at == anchor + timedelta(seconds=120)
        assert schedules["runtime"].next_due_at == anchor + timedelta(seconds=120)
        assert schedules["statement_digest"].next_due_at == anchor + timedelta(seconds=180)
        for row in schedules.values():
            assert current + timedelta(seconds=row.interval_seconds) <= row.next_due_at
        db.get(m.Task, tasks[0]).status = "succeeded"
    # Scheduler jitter must not become the next cycle's permanent phase.
    current = anchor + timedelta(seconds=121)
    tasks = worker.schedule_due(factory)
    with factory.begin() as db:
        schedules = {r.collector: r for r in db.scalars(select(m.CollectionSchedule))}
        assert schedules["identity_status"].next_due_at == anchor + timedelta(seconds=150)
        assert schedules["runtime"].next_due_at == anchor + timedelta(seconds=150)
        db.get(m.Task, tasks[0]).status = "succeeded"


def test_interval_revision_uses_same_asset_anchor_and_excludes_obsolete_revision(
    lease_env, monkeypatch
):
    from datetime import datetime

    factory, (a, _) = lease_env
    anchor = datetime(2026, 1, 1)
    current = anchor + timedelta(seconds=67)
    monkeypatch.setattr(worker, "db_now", lambda db: current)
    with factory.begin() as db:
        instance = db.get(m.Instance, a)
        instance.created_at = anchor
        instance.config_revision = 2
        db.add_all(
            [
                m.CollectionSchedule(
                    instance_id=a,
                    collector="runtime",
                    interval_seconds=60,
                    next_due_at=anchor,
                    config_revision=2,
                ),
                m.CollectionSchedule(
                    instance_id=a,
                    collector="host",
                    interval_seconds=30,
                    next_due_at=anchor,
                    config_revision=1,
                ),
            ]
        )
    tasks = worker.schedule_due(factory)
    with factory() as db:
        assert db.get(m.Task, tasks[0]).payload["collectors"] == ["runtime"]
        row = db.scalar(
            select(m.CollectionSchedule).where(m.CollectionSchedule.collector == "runtime")
        )
        assert row.next_due_at == anchor + timedelta(seconds=180)


@pytest.mark.parametrize(
    "task_status,attempt", [("retry_wait", 1), ("succeeded", 2), ("running", 2)]
)
def test_abandoned_attempt_run_is_reconciled_even_when_task_retries(
    lease_env, task_status, attempt
):
    factory, (a, _) = lease_env
    with factory.begin() as db:
        task = enqueue(db, "collect_instance", instance_id=a, payload={"config_revision": 1})
        task.status = task_status
        task.attempt = attempt
        run = m.CollectionRun(
            task_id=task.id, instance_id=a, collector="runtime", attempt=1, status="running"
        )
        db.add(run)
        db.flush()
        run_id = run.id
    worker.reconcile_interrupted(factory)
    with factory() as db:
        run = db.get(m.CollectionRun, run_id)
        assert (
            run.status == "failed"
            and run.error_code == "task_interrupted"
            and run.finished_at is not None
        )


def test_reconcile_preserves_current_running_attempt_and_successful_observation(lease_env):
    factory, (a, _) = lease_env
    with factory.begin() as db:
        task = enqueue(db, "collect_instance", instance_id=a, payload={"config_revision": 1})
        task.status = "running"
        task.attempt = 2
        active = m.CollectionRun(
            task_id=task.id, instance_id=a, collector="runtime", attempt=2, status="running"
        )
        success = m.CollectionRun(
            task_id=task.id,
            instance_id=a,
            collector="identity_status",
            attempt=1,
            status="succeeded",
        )
        db.add_all([active, success])
        db.flush()
        ids = (active.id, success.id)
    worker.reconcile_interrupted(factory)
    with factory() as db:
        assert [db.get(m.CollectionRun, i).status for i in ids] == ["running", "succeeded"]


def test_handler_failure_has_safe_task_error_and_releases_instance_lease(
    lease_env, monkeypatch, caplog
):
    factory, (a, _) = lease_env
    with factory.begin() as db:
        enqueue(db, "collect_instance", instance_id=a, payload={"config_revision": 1})
    lease = claim(factory, "worker")

    def broken(*args):
        raise RuntimeError("SECRET_SQL_OR_PASSWORD")

    monkeypatch.setattr(worker, "handle_collection", broken)
    response = worker.execute(factory, lease, SimpleNamespace(collection_mode="legacy"))
    assert response == {"status": "failed", "error_code": "task_failed"}
    assert "SECRET_SQL_OR_PASSWORD" not in caplog.text
    with factory() as db:
        assert db.get(m.Task, lease.id).status == "failed"
        assert db.get(m.InstanceLease, a).task_id is None


def test_obsolete_worker_error_never_overwrites_successor_result(lease_env, monkeypatch):
    factory, (a, _) = lease_env
    with factory.begin() as db:
        enqueue(db, "collect_instance", instance_id=a, payload={"config_revision": 1})
    old = claim(factory, "old")
    expire(factory, old)
    assert claim(factory, "new") is None
    with factory.begin() as db:
        db.get(m.Task, old.id).due_at = utcnow() - timedelta(seconds=1)
    new = claim(factory, "new")
    with factory.begin() as db:
        complete(db, new, result={"owner": "successor"})

    def broken(*args):
        raise ConnectorError("unreachable")

    monkeypatch.setattr(worker, "handle_collection", broken)
    worker.execute(factory, old, SimpleNamespace(collection_mode="legacy"))
    with factory() as db:
        row = db.get(m.Task, old.id)
        assert row.status == "succeeded" and row.result == {"owner": "successor"}


def test_verification_partial_write_rolls_back_before_task_failure(lease_env, monkeypatch):
    from platform_app import remediation

    factory, (a, _) = lease_env
    with factory.begin() as db:
        enqueue(db, "verification", instance_id=a, payload={"verification_id": "fixture"})
    lease = claim(factory, "worker")

    def interrupted(db, lease):
        db.add(m.PlatformSetting(key="must_rollback", value={"not_committed": True}))
        db.flush()
        raise ConnectorError("timeout")

    monkeypatch.setattr(remediation, "handle_verification", interrupted)
    worker.execute(factory, lease, SimpleNamespace(collection_mode="legacy"))
    with factory() as db:
        assert db.get(m.PlatformSetting, "must_rollback") is None
        assert db.get(m.Task, lease.id).status == "failed"
