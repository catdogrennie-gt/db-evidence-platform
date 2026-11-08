"""Terminal tasks must release domain state without undoing a successor."""

from uuid import uuid4
import pytest
from platform_app import models as m, worker
from platform_app.db import utcnow
from platform_app.scheduling import enqueue
from tests.platform.test_leases import lease_env


@pytest.mark.parametrize(
    "terminal,expected", [("cancelled", "cancelled"), ("failed", "failed"), ("expired", "failed")]
)
def test_terminal_preflight_is_not_left_running(lease_env, terminal, expected):
    factory, (a, _) = lease_env
    with factory.begin() as db:
        user = m.User(username="operator", password_hash="unused")
        db.add(user)
        db.flush()
        task = enqueue(db, "connection_test")
        task.status = terminal
        test = m.ConnectionTest(
            created_by=user.id,
            system_id=db.get(m.Instance, a).system_id,
            task_id=task.id,
            config_fingerprint="fixed",
            config={},
            secret_ref=str(uuid4()),
            expires_at=utcnow(),
            status="running",
        )
        db.add(test)
        db.flush()
        identifier = test.id
    worker.reconcile_interrupted(factory)
    with factory() as db:
        test = db.get(m.ConnectionTest, identifier)
        assert test.status == expected and test.results["error_code"] == "task_interrupted"


@pytest.mark.parametrize("successor", [None, "queued", "running", "lease_only"])
def test_terminal_activation_recovers_only_without_successor(lease_env, successor):
    factory, (a, _) = lease_env
    with factory.begin() as db:
        instance = db.get(m.Instance, a)
        instance.enabled = False
        instance.lifecycle_state = "Activating"
        old = enqueue(db, "activate_instance", instance_id=a, payload={"config_revision": 1})
        old.status = "cancelled"
        if successor:
            new = enqueue(
                db,
                "activate_instance" if successor != "lease_only" else "collect_instance",
                instance_id=a,
                payload={"config_revision": 1},
            )
            new.status = "succeeded" if successor == "lease_only" else successor
            if successor == "lease_only":
                db.get(m.InstanceLease, a).task_id = new.id
    worker.reconcile_interrupted(factory)
    with factory() as db:
        assert db.get(m.Instance, a).lifecycle_state == (
            "Ready" if successor is None else "Activating"
        )


def test_old_revision_failure_does_not_change_new_configuration(lease_env):
    factory, (a, _) = lease_env
    with factory.begin() as db:
        instance = db.get(m.Instance, a)
        instance.enabled = False
        instance.lifecycle_state = "Activating"
        instance.config_revision = 2
        task = enqueue(db, "activate_instance", instance_id=a, payload={"config_revision": 1})
        task.status = "failed"
    worker.reconcile_interrupted(factory)
    with factory() as db:
        assert db.get(m.Instance, a).lifecycle_state == "Activating"
