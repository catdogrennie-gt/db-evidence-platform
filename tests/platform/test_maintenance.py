"""Maintenance failures must stay separate from collection and remain visible."""

from datetime import timedelta
from types import SimpleNamespace
import threading

from tests.platform.test_events import db
from tests.platform.test_admin_scope import admin_graph
from platform_app import models as m
from platform_app.db import create_session_factory, utcnow
from platform_app.common import iso


def test_failed_maintenance_persists_safe_error_and_backoff_then_recovers(db):
    from platform_app.worker import MaintenanceRunner

    factory = create_session_factory(db.get_bind())

    def failure():
        raise OSError("password=DO-NOT-PERSIST mysql://private")

    runner = MaintenanceRunner(factory, SimpleNamespace(), jobs={"cleanup": (60, failure)})
    try:
        result = runner.run_job("cleanup")
        db.expire_all()
        value = db.get(m.PlatformSetting, "maintenance:cleanup").value
        assert value["status"] == "failed" and value["last_error_type"] == "OSError"
        assert "DO-NOT-PERSIST" not in str(value) and result["next_delay"] >= 30
        assert value["last_success_at"] is None and value["next_retry_at"]
        # A restart cannot bypass the persisted retry boundary.
        assert runner.run_job("cleanup")["deferred"] is True
        value["next_due_at"] = iso(utcnow() - timedelta(seconds=1))
        db.get(m.PlatformSetting, "maintenance:cleanup").value = dict(value)
        db.commit()
        runner.jobs["cleanup"] = (60, lambda: {"deleted": 2})
        runner.run_job("cleanup")
        db.expire_all()
        value = db.get(m.PlatformSetting, "maintenance:cleanup").value
        assert value["status"] == "available" and value["last_success_at"]
        assert value["last_error_type"] is None and value["consecutive_failures"] == 0
    finally:
        runner.close()


def test_maintenance_tick_never_starts_more_than_one_job(db):
    from platform_app.worker import MaintenanceRunner

    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocking():
        calls.append("first")
        entered.set()
        release.wait(3)
        return {}

    runner = MaintenanceRunner(
        create_session_factory(db.get_bind()),
        SimpleNamespace(),
        jobs={"first": (60, blocking), "second": (60, lambda: calls.append("second"))},
    )
    try:
        runner.tick()
        assert entered.wait(2)
        for _ in range(20):
            runner.tick()
        assert calls == ["first"]
    finally:
        release.set()
        runner.close()


def test_platform_status_exposes_cleanup_failure_and_disk_warning(admin_graph, monkeypatch):
    g = admin_graph
    g.db.add(
        m.PlatformSetting(
            key="maintenance:retention",
            value={
                "status": "failed",
                "last_attempt_at": iso(utcnow()),
                "last_success_at": None,
                "last_error_type": "PermissionError",
                "next_due_at": iso(utcnow() + timedelta(seconds=60)),
                "next_retry_at": iso(utcnow() + timedelta(seconds=60)),
                "consecutive_failures": 1,
            },
        )
    )
    retention = g.db.get(m.PlatformSetting, "retention")
    retention.value = {**retention.value, "min_free_disk_bytes": 500}
    g.db.commit()
    monkeypatch.setattr(
        "platform_app.routes.admin.shutil.disk_usage",
        lambda path: SimpleNamespace(free=100, total=1000),
    )
    response = g.client.get("/api/v1/platform-status")
    assert response.status_code == 200
    value = response.json()
    assert any(
        x["code"] == "maintenance_failed" and x["job"] == "retention" for x in value["warnings"]
    )
    assert any(x["code"] == "disk_space_below_threshold" for x in value["warnings"])
    assert value["maintenance"]["retention"]["last_error_type"] == "PermissionError"
