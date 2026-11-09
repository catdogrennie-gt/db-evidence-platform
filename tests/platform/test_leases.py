"""State-machine checks use SQLite; concurrency proof is explicitly real MySQL."""

from types import SimpleNamespace

from datetime import timedelta
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from sqlalchemy import select, delete
from platform_app import models as m
from platform_app.db import Base, create_db_engine, create_session_factory, utcnow
from platform_app.common import PlatformError
from platform_app.scheduling import (
    enqueue,
    claim,
    complete,
    check_fence,
    fail,
    renew,
    request_cancel,
)


@pytest.fixture
def lease_env():
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine, SimpleNamespace(collection_mode="legacy"))
    with factory.begin() as db:
        department = m.Department(name="lease state validation")
        db.add(department)
        db.flush()
        system = m.System(name="lease state validation", department_id=department.id)
        db.add(system)
        db.flush()
        a = m.Instance(
            name="A",
            system_id=system.id,
            endpoint="127.0.0.1",
            expected_server_uuid="test-a",
            enabled=True,
            lifecycle_state="Active",
        )
        b = m.Instance(
            name="B",
            system_id=system.id,
            endpoint="127.0.0.1",
            expected_server_uuid="test-b",
            enabled=True,
            lifecycle_state="Active",
        )
        db.add_all([a, b])
        db.flush()
        ids = (a.id, b.id)
        db.add_all([m.InstanceLease(instance_id=a.id), m.InstanceLease(instance_id=b.id)])
    yield factory, ids
    engine.dispose()


def add_task(factory, instance_id, **kwargs):
    with factory.begin() as db:
        return enqueue(db, "collection", instance_id=instance_id, **kwargs).id


def expire(factory, lease, *, instance=True, task=True):
    with factory.begin() as db:
        if task:
            db.get(m.Task, lease.id).lease_until = utcnow() - timedelta(seconds=1)
        if instance:
            db.get(m.InstanceLease, lease.instance_id).lease_until = utcnow() - timedelta(seconds=1)


def test_same_instance_serializes_but_other_instance_claims(lease_env):
    factory, (a, b) = lease_env
    first = add_task(factory, a)
    second = add_task(factory, a)
    third = add_task(factory, b)
    with factory.begin() as db:
        stamp = utcnow()
        for delay, identifier in ((3, first), (2, second), (1, third)):
            db.get(m.Task, identifier).due_at = stamp - timedelta(seconds=delay)
    owner = claim(factory, "worker-one")
    other = claim(factory, "worker-two")
    assert owner.id == first and other.id == third
    with factory() as db:
        assert db.get(m.Task, second).status == "queued"


def test_expired_worker_cannot_commit_and_successor_has_higher_fence(lease_env):
    factory, (a, _) = lease_env
    identifier = add_task(factory, a)
    old = claim(factory, "old")
    expire(factory, old)
    with factory.begin() as db:
        with pytest.raises(PlatformError) as out:
            complete(db, old, result={"obsolete": True})
        assert out.value.detail["code"] == "stale_task_fence"
    assert claim(factory, "new") is None  # recovery uses bounded retry delay
    with factory.begin() as db:
        db.get(m.Task, identifier).due_at = utcnow() - timedelta(seconds=1)
    new = claim(factory, "new")
    assert new.fence > old.fence and new.attempt == 2
    assert fail(factory, old, "obsolete_failure") is False
    with factory.begin() as db:
        complete(db, new, result={"owner": "new"})
    with factory() as db:
        task = db.get(m.Task, identifier)
        assert task.status == "succeeded" and task.result == {"owner": "new"}


def test_cancel_blocks_running_commit_and_queued_task_is_never_claimed(lease_env):
    factory, (a, _) = lease_env
    identifier = add_task(factory, a)
    lease = claim(factory, "worker")
    with factory.begin() as db:
        request_cancel(db, db.get(m.Task, identifier))
    with factory.begin() as db:
        with pytest.raises(PlatformError) as out:
            complete(db, lease)
        assert out.value.detail["code"] == "task_cancelled"
    assert fail(factory, lease, "cancelled")
    queued = add_task(factory, a)
    with factory.begin() as db:
        request_cancel(db, db.get(m.Task, queued))
    assert claim(factory, "other") is None


def test_deadline_and_configuration_change_block_commit(lease_env):
    factory, (a, _) = lease_env
    identifier = add_task(factory, a)
    lease = claim(factory, "worker")
    with factory.begin() as db:
        db.get(m.Instance, a).config_revision = 2
    with factory.begin() as db:
        with pytest.raises(PlatformError) as out:
            complete(db, lease, config_revision=1)
        assert out.value.detail["code"] == "configuration_changed"
    with factory.begin() as db:
        db.get(m.Task, identifier).deadline_at = utcnow() - timedelta(seconds=1)
    with factory.begin() as db:
        with pytest.raises(PlatformError) as out:
            complete(db, lease)
        assert out.value.detail["code"] == "task_deadline_exceeded"


def test_expired_instance_lease_alone_blocks_commit(lease_env):
    factory, (a, _) = lease_env
    add_task(factory, a)
    lease = claim(factory, "worker")
    expire(factory, lease, task=False)
    with factory.begin() as db:
        with pytest.raises(PlatformError) as out:
            complete(db, lease)
        assert out.value.detail["code"] == "stale_instance_fence"


def test_changed_instance_lease_owner_alone_blocks_commit(lease_env):
    factory, (a, _) = lease_env
    add_task(factory, a)
    lease = claim(factory, "worker")
    with factory.begin() as db:
        db.get(m.InstanceLease, a).leased_by = "different-worker"
    with factory.begin() as db:
        with pytest.raises(PlatformError):
            complete(db, lease)


def test_expired_worker_failure_is_also_a_blocked_commit(lease_env):
    factory, (a, _) = lease_env
    identifier = add_task(factory, a)
    lease = claim(factory, "worker")
    expire(factory, lease)
    assert fail(factory, lease, "late_failure") is False
    with factory() as db:
        assert db.get(m.Task, identifier).status == "running"


def test_old_failure_cannot_update_task_after_another_instance_fence_takes_over(lease_env):
    factory, (a, _) = lease_env
    identifier = add_task(factory, a)
    old = claim(factory, "old")
    expire(factory, old)
    # A successor may own another task on this instance before old-task recovery.
    with factory.begin() as db:
        row = db.get(m.InstanceLease, a)
        row.fence = old.fence + 1
        row.leased_by = "new"
        row.lease_until = utcnow() + timedelta(seconds=30)
    assert fail(factory, old, "obsolete_failure") is False
    with factory() as db:
        assert db.get(m.Task, identifier).error_code is None


def test_deadline_recovery_does_not_wait_for_longer_live_lease(lease_env):
    factory, (a, _) = lease_env
    identifier = add_task(factory, a)
    old = claim(factory, "deadline-old")
    with factory.begin() as db:
        db.get(m.Task, identifier).deadline_at = utcnow() - timedelta(seconds=1)
    successor = add_task(factory, a)
    new = claim(factory, "deadline-new")
    assert new is not None and new.id == successor and new.fence > old.fence
    with factory() as db:
        assert db.get(m.Task, identifier).status == "expired"
    with factory.begin() as db:
        with pytest.raises(PlatformError):
            complete(db, old, result={"obsolete": True})
    assert fail(factory, old, "late_failure") is False


def test_deadline_recovery_preserves_a_different_instance_owner(lease_env):
    from platform_app.scheduling import recover_expired

    factory, (a, _) = lease_env
    identifier = add_task(factory, a)
    old = claim(factory, "deadline-old")
    with factory.begin() as db:
        db.get(m.Task, identifier).deadline_at = utcnow() - timedelta(seconds=1)
        row = db.get(m.InstanceLease, a)
        row.fence = old.fence + 1
        row.leased_by = "successor"
        row.task_id = None
        expected = (row.fence, row.leased_by, row.lease_until)
    with factory.begin() as db:
        recover_expired(db, "collection")
    with factory() as db:
        assert db.get(m.Task, identifier).status == "expired"
        row = db.get(m.InstanceLease, a)
        assert (row.fence, row.leased_by, row.lease_until) == expected


def test_ai_deadline_recovery_never_queues_an_automatic_resend(lease_env):
    from platform_app.scheduling import recover_expired

    factory, _ = lease_env
    with factory.begin() as db:
        identifier = enqueue(db, "investigation", queue="ai").id
    old = claim(factory, "ai-old", "ai")
    with factory.begin() as db:
        db.get(m.Task, identifier).deadline_at = utcnow() - timedelta(seconds=1)
    with factory.begin() as db:
        recover_expired(db, "ai")
    with factory() as db:
        task = db.get(m.Task, identifier)
        assert task.status == "expired" and task.error_code == "task_deadline_exceeded"
    assert claim(factory, "ai-new", "ai") is None


def test_renew_moves_both_expiry_times_and_disabled_instance_blocks_commit(lease_env):
    factory, (a, _) = lease_env
    add_task(factory, a)
    lease = claim(factory, "worker")
    with factory.begin() as db:
        shortened = utcnow() + timedelta(seconds=10)
        db.get(m.Task, lease.id).lease_until = shortened
        db.get(m.InstanceLease, a).lease_until = shortened
    renew(factory, lease)
    with factory() as db:
        assert db.get(m.Task, lease.id).lease_until > shortened
        assert db.get(m.Task, lease.id).lease_until == db.get(m.InstanceLease, a).lease_until
    with factory.begin() as db:
        db.get(m.Instance, a).enabled = False
    with factory.begin() as db:
        with pytest.raises(PlatformError) as out:
            complete(db, lease)
        assert out.value.detail["code"] == "instance_not_active"


@pytest.mark.skipif(
    os.environ.get("DBA_PLATFORM_MYSQL_TEST") != "1",
    reason="requires private real MySQL and two connections; SQLite does not prove SKIP LOCKED",
)
@pytest.mark.parametrize(
    "worker_clock_skew", [False, True], ids=["skip_locked", "worker_clock_skew"]
)
def test_real_mysql_locked_task_is_skipped_and_fixture_is_removed(monkeypatch, worker_clock_skew):
    from platform_app.config import load_config

    engine = create_db_engine(load_config())
    factory = create_session_factory(engine, SimpleNamespace(collection_mode="legacy"))
    tag = uuid4().hex[:8]
    queue = "lease-test-" + tag
    instance_ids = []
    task_ids = []
    department_id = system_id = None
    try:
        with factory.begin() as db:
            department = m.Department(name="LEASE VALIDATION " + tag)
            db.add(department)
            db.flush()
            department_id = department.id
            system = m.System(
                name="LEASE VALIDATION " + tag,
                department_id=department.id,
                environment="validation",
            )
            db.add(system)
            db.flush()
            system_id = system.id
            for label in ("A", "B"):
                row = m.Instance(
                    name="LEASE VALIDATION " + label + " " + tag,
                    system_id=system.id,
                    endpoint="127.0.0.1",
                    port=1,
                    expected_server_uuid="lease-fixture-" + label + "-" + tag,
                    enabled=False,
                    lifecycle_state="Archived",
                    connection={"metadata_source": "validation"},
                )
                db.add(row)
                db.flush()
                instance_ids.append(row.id)
                db.add(m.InstanceLease(instance_id=row.id))
                task_ids.append(
                    enqueue(db, "validation_no_target_io", queue=queue, instance_id=row.id).id
                )
        with factory() as blocker:
            blocker.begin()
            blocker.scalar(select(m.Task).where(m.Task.id == task_ids[0]).with_for_update())
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(claim, factory, "mysql-validation-worker", queue)
                try:
                    claimed = future.result(timeout=5)
                finally:
                    blocker.rollback()
            assert claimed.id == task_ids[1]
        with factory.begin() as db:
            complete(
                db,
                claimed,
                result={"mode": "real_mysql_skip_locked_validation", "target_io": False},
                allow_inactive=True,
            )
        if not worker_clock_skew:
            from platform_app.scheduling import db_now

            old = claim(factory, "mysql-deadline-old", queue)
            with factory.begin() as db:
                db.get(m.Task, old.id).deadline_at = db_now(db) - timedelta(seconds=1)
                successor = enqueue(
                    db, "validation_no_target_io", queue=queue, instance_id=old.instance_id
                )
                task_ids.append(successor.id)
            recovered = claim(factory, "mysql-deadline-new", queue)
            assert recovered and recovered.id == successor.id and recovered.fence > old.fence
            with factory.begin() as db:
                assert db.get(m.Task, old.id).status == "expired"
                with pytest.raises(PlatformError):
                    complete(db, old, allow_inactive=True)
                complete(
                    db,
                    recovered,
                    result={"mode": "real_mysql_deadline_fence_validation", "target_io": False},
                    allow_inactive=True,
                )
        if worker_clock_skew:
            # A worker's local wall clock must not expire a still-valid task in
            # the control DB. The DB remains the lease/deadline authority.
            from platform_app import scheduling

            actual = utcnow()
            monkeypatch.setattr(scheduling, "utcnow", lambda: actual + timedelta(hours=1))
            next_task = claim(factory, "mysql-skew-validation-worker", queue)
            assert next_task is not None and next_task.id == task_ids[0]
            with factory.begin() as db:
                complete(
                    db,
                    next_task,
                    result={"mode": "real_mysql_worker_clock_skew_validation", "target_io": False},
                    allow_inactive=True,
                )
    finally:
        with factory.begin() as db:
            if instance_ids:
                db.execute(
                    delete(m.InstanceLease).where(m.InstanceLease.instance_id.in_(instance_ids))
                )
                db.execute(delete(m.Task).where(m.Task.id.in_(task_ids)))
                db.execute(delete(m.Instance).where(m.Instance.id.in_(instance_ids)))
            if system_id:
                db.execute(delete(m.System).where(m.System.id == system_id))
            if department_id:
                db.execute(delete(m.Department).where(m.Department.id == department_id))
        engine.dispose()
