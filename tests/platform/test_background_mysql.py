"""Opt-in, synthetic MySQL locks through the actual worker background wiring."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import time
from uuid import uuid4

import pytest
from sqlalchemy import Column, Integer, MetaData, Table
from sqlalchemy.engine import URL

from platform_app import worker, showcase, models as m
from platform_app.db import create_db_engine, create_session_factory


@pytest.fixture
def mysql_background_engine():
    path = os.environ.get("DBE_MYSQL_REGRESSION_CONFIG")
    if not path:
        pytest.skip("requires explicitly registered isolated MySQL regression schema")
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    assert cfg.get("test_only") is True
    assert cfg["database"].startswith("dbe_background_regression")
    engine = create_db_engine(URL.create("mysql+pymysql", username=cfg["user"],
        password=cfg["password"], host=cfg["host"], port=cfg["port"], database=cfg["database"]))
    m.PlatformSetting.__table__.create(engine, checkfirst=True)
    Table("regression_lock_targets", MetaData(), Column("id", Integer, primary_key=True)).create(engine, checkfirst=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("INSERT IGNORE INTO regression_lock_targets (id) VALUES (1),(2)")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.parametrize("opposite_order", [True, False], ids=["opposite-lock-order", "normal-same-order"])
def test_serve_background_writers_do_not_deadlock(mysql_background_engine, monkeypatch, opposite_order):
    engine = mysql_background_engine
    factory = create_session_factory(engine)
    real_maintenance, real_inspection = worker.MaintenanceRunner, worker.InspectionDispatcher
    first_lock, release, clock_entered = (threading.Event() for _ in range(3))
    handlers, runners, errors, server_errors = {}, {}, [], []
    job = "regression_clock_" + uuid4().hex[:12]

    def lock_pair(first, second, *, inspection=False):
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("SET SESSION innodb_lock_wait_timeout=3")
                connection.exec_driver_sql("SELECT id FROM regression_lock_targets WHERE id=%s FOR UPDATE", (first,))
                if inspection:
                    first_lock.set()
                    assert release.wait(4), "controller did not release synthetic lock"
                    # Separate writers can acquire opposite first locks here;
                    # a shared writer is allowed to complete before clock starts.
                    clock_entered.wait(.2)
                else:
                    clock_entered.set()
                connection.exec_driver_sql("SELECT id FROM regression_lock_targets WHERE id=%s FOR UPDATE", (second,))
        except Exception as error:
            original = getattr(error, "orig", error)
            code = original.args[0] if original.args and isinstance(original.args[0], int) else None
            errors.append((type(error).__name__, code))
            raise

    def maintenance(*args):
        runner = real_maintenance(factory, SimpleNamespace(), jobs={job: (15,
            lambda: lock_pair(1 if opposite_order else 2, 2 if opposite_order else 1))})
        runners["maintenance"] = runner
        return runner

    def inspection(*args, **kwargs):
        runner = real_inspection(factory, dispatch=lambda factory: lock_pair(2, 1, inspection=True), **kwargs)
        runners["inspection"] = runner
        return runner

    monkeypatch.setattr(worker, "create_db_engine", lambda config: engine)
    monkeypatch.setattr(worker, "create_session_factory", lambda *args: factory)
    monkeypatch.setattr(worker, "MaintenanceRunner", maintenance)
    monkeypatch.setattr(worker, "InspectionDispatcher", inspection)
    monkeypatch.setattr(worker, "ensure_default_rules", lambda db: None)
    monkeypatch.setattr(worker, "heartbeat", lambda *args: None)
    monkeypatch.setattr(worker, "schedule_due", lambda factory: [])
    monkeypatch.setattr(worker, "reconcile_interrupted", lambda factory: None)
    monkeypatch.setattr(worker, "claim", lambda *args: None)
    monkeypatch.setattr(showcase, "reconcile_pacing", lambda *args: None)
    monkeypatch.setattr(worker.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler))

    def serve():
        try:
            worker.serve(SimpleNamespace(collection_workers=1, demo={}), no_bridge=True)
        except BaseException as error:
            server_errors.append(type(error).__name__)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert first_lock.wait(3)
        deadline = time.monotonic() + 3
        while runners["maintenance"].future is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert runners["maintenance"].future is not None
        release.set()
        try:
            runners["inspection"].future.result(timeout=5)
        except Exception:
            # A SQL failure is recorded by the callback, then asserted below.
            pass
        runners["maintenance"].future.result(timeout=5)
        state = runners["maintenance"].summary()[job]
    finally:
        release.set()
        if worker.signal.SIGINT in handlers:
            handlers[worker.signal.SIGINT]()
        thread.join(6)
    assert not thread.is_alive()
    assert not server_errors
    assert not errors, f"real MySQL background transaction failure: {errors}"
    assert state["status"] == "available"
