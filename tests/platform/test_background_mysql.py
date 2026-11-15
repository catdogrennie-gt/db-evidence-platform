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


@pytest.mark.parametrize("blocked", [True, False], ids=["cadence-lock-wait", "normal-cadence"])
def test_cadence_lock_wait_does_not_block_ready_collection(mysql_background_engine, monkeypatch, blocked):
    """Actual serve/maintenance wiring, synthetic SQL lock, no transport or fault."""
    from platform_app import intelligence

    engine = mysql_background_engine
    factory = create_session_factory(engine)
    entered, collected = threading.Event(), threading.Event()
    handlers, errors, claimed = {}, [], []
    real_maintenance = worker.MaintenanceRunner
    job = "regression_cadence_" + uuid4().hex[:12]

    def maintenance(factory, config):
        runner = real_maintenance(factory, config)
        # Exercise the registered production callback, isolating this test's
        # persisted due boundary from other invocations and unrelated jobs.
        runner.jobs = {job: runner.jobs["cadence"]} if "cadence" in runner.jobs else {}
        runner.due = {name: 0 for name in runner.jobs}
        return runner

    holder = engine.connect()
    transaction = holder.begin()
    if blocked:
        holder.exec_driver_sql("SELECT id FROM regression_lock_targets WHERE id=1 FOR UPDATE")

    def cleanup(db, config):
        db.connection().exec_driver_sql("SET SESSION innodb_lock_wait_timeout=3")
        entered.set()
        db.connection().exec_driver_sql("SELECT id FROM regression_lock_targets WHERE id=1 FOR UPDATE")

    def claim(*args):
        if claimed or not entered.wait(.5):
            return None
        claimed.append(True)
        return SimpleNamespace(id="ready-synthetic-collection")

    def execute(*args):
        collected.set()
        handlers[worker.signal.SIGINT]()

    monkeypatch.setattr(worker, "create_db_engine", lambda config: engine)
    monkeypatch.setattr(worker, "create_session_factory", lambda *args: factory)
    monkeypatch.setattr(worker, "MaintenanceRunner", maintenance)
    monkeypatch.setattr(worker, "ensure_default_rules", lambda db: None)
    monkeypatch.setattr(worker, "heartbeat", lambda *args: None)
    monkeypatch.setattr(worker, "renew", lambda *args: None)
    monkeypatch.setattr(worker, "schedule_due", lambda factory: [])
    monkeypatch.setattr(worker, "reconcile_interrupted", lambda factory: None)
    monkeypatch.setattr(worker, "claim", claim)
    monkeypatch.setattr(worker, "execute", execute)
    monkeypatch.setattr(worker, "clock_tick", lambda *args, **kwargs: None)
    monkeypatch.setattr(intelligence, "dispatch_due", lambda factory: [])
    monkeypatch.setattr(showcase, "reconcile_pacing", cleanup)
    monkeypatch.setattr(worker.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler))

    def serve():
        try:
            worker.serve(SimpleNamespace(collection_workers=1, demo={}), no_bridge=True)
        except BaseException as error:
            errors.append(type(error).__name__)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert entered.wait(3), "cadence cleanup did not start"
        ready_ran_during_wait = collected.wait(.7)
    finally:
        transaction.rollback()
        holder.close()
        if worker.signal.SIGINT in handlers:
            handlers[worker.signal.SIGINT]()
        thread.join(5)
    assert not thread.is_alive()
    assert not errors
    assert ready_ran_during_wait, "ready collection was held behind cadence SQL lock"
