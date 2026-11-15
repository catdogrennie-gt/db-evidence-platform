"""Inspection latency must not stop the actual collection serve loop."""
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import pytest

from platform_app import worker, intelligence, showcase


def test_ready_collection_executes_while_inspection_sweep_is_blocked(monkeypatch):
    entered, release, collected = (threading.Event() for _ in range(3))
    disposed = threading.Event()
    handlers, errors = {}, []
    factory = SimpleNamespace(begin=lambda: nullcontext(None))
    engine = SimpleNamespace(dispose=disposed.set)

    class Maintenance:
        def __init__(self, *args):
            self.executor = ThreadPoolExecutor(max_workers=1)
        def tick(self):
            pass
        def close(self):
            self.executor.shutdown(wait=True, cancel_futures=True)

    monkeypatch.setattr(worker, "create_db_engine", lambda config: engine)
    monkeypatch.setattr(worker, "create_session_factory", lambda *args: factory)
    monkeypatch.setattr(worker, "ensure_default_rules", lambda db: None)
    monkeypatch.setattr(worker, "heartbeat", lambda *args: None)
    monkeypatch.setattr(worker, "renew", lambda *args: None)
    monkeypatch.setattr(worker, "schedule_due", lambda factory: [])
    monkeypatch.setattr(worker, "reconcile_interrupted", lambda factory: None)
    monkeypatch.setattr(worker, "MaintenanceRunner", Maintenance)
    monkeypatch.setattr(showcase, "reconcile_pacing", lambda *args: None)
    monkeypatch.setattr(worker.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler))

    claimed = []
    def claim(*args):
        if claimed:
            return None
        claimed.append("ready")
        return SimpleNamespace(id="already-ready-collection")

    def collect(*args):
        collected.set()
        handlers[worker.signal.SIGINT]()

    def blocked_sweep(factory):
        entered.set()
        assert release.wait(3), "test did not release inspection"
        return []

    monkeypatch.setattr(worker, "claim", claim)
    monkeypatch.setattr(worker, "execute", collect)
    monkeypatch.setattr(intelligence, "dispatch_due", blocked_sweep)
    def serve():
        try:
            worker.serve(SimpleNamespace(collection_workers=1, demo={}), no_bridge=True)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert entered.wait(2)
        ran_before_scan_finished = collected.wait(.5)
        assert not disposed.is_set(), "engine disposed while inspection still owns a session"
    finally:
        release.set()
        handler = handlers.get(worker.signal.SIGINT)
        if handler:
            handler()
        thread.join(3)
    assert not thread.is_alive()
    assert not errors
    assert disposed.is_set()
    assert ran_before_scan_finished, "ready collection was held behind inspection"


def test_single_flight_does_not_queue_sweeps_and_close_rejects_new_work():
    from platform_app.inspection_dispatch import InspectionDispatcher
    entered, release = threading.Event(), threading.Event()
    calls = []
    def scan(factory):
        calls.append(factory)
        entered.set()
        assert release.wait(2)
    runner = InspectionDispatcher("scoped-factory", dispatch=scan)
    try:
        assert runner.tick()
        assert entered.wait(1)
        for _ in range(100):
            assert runner.tick() is False
        assert calls == ["scoped-factory"]
        assert runner.summary()["status"] == "running"
    finally:
        release.set()
        runner.close()
    assert runner.tick() is False
    assert calls == ["scoped-factory"]


def test_failure_is_safe_observable_and_next_sweep_can_recover(monkeypatch, caplog):
    from platform_app import inspection_dispatch as module
    clock, calls = [0], []
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def scan(factory):
        calls.append(factory)
        if len(calls) == 1:
            raise OSError("token=DO-NOT-LOG")
        return ["natural-dispatch"]
    runner = module.InspectionDispatcher("factory", dispatch=scan)
    try:
        runner.tick()
        with pytest.raises(OSError):
            runner.future.result(timeout=1)
        state = runner.summary()
        assert state["status"] == "failed" and state["last_error_type"] == "OSError"
        assert "DO-NOT-LOG" not in str(state) + caplog.text
        assert runner.tick() is False
        clock[0] += 1
        assert runner.tick()
        assert runner.future.result(timeout=1) == ["natural-dispatch"]
        assert runner.summary()["last_error_type"] is None
        assert calls == ["factory", "factory"]
    finally:
        runner.close()


def test_once_waits_for_sweep_and_shutdown_drains_before_return():
    from platform_app.inspection_dispatch import InspectionDispatcher
    entered, release, returned, closed = (threading.Event() for _ in range(4))
    def scan(factory):
        entered.set()
        assert release.wait(2)
    runner = InspectionDispatcher(None, dispatch=scan)
    def once():
        runner.tick(wait=True)
        returned.set()
    thread = threading.Thread(target=once)
    thread.start()
    assert entered.wait(1)
    assert not returned.wait(.05)
    closer = threading.Thread(target=lambda: (runner.close(), closed.set()))
    closer.start()
    try:
        assert not closed.wait(.05)
    finally:
        release.set()
        thread.join(2)
        closer.join(2)
    assert returned.is_set() and closed.is_set()
    assert runner.tick() is False


def test_once_preserves_dispatch_exception_without_logging_secret(caplog):
    from platform_app.inspection_dispatch import InspectionDispatcher
    def scan(factory):
        raise ValueError("password=PRIVATE-VALUE")
    runner = InspectionDispatcher(None, dispatch=scan)
    try:
        with pytest.raises(ValueError):
            runner.tick(wait=True)
        assert runner.summary()["last_error_type"] == "ValueError"
        assert "PRIVATE-VALUE" not in caplog.text
    finally:
        runner.close()


def test_borrowed_executor_drains_queued_sweep_and_preserves_owner():
    from platform_app.inspection_dispatch import InspectionDispatcher
    executor = ThreadPoolExecutor(max_workers=1)
    occupied, release, finished = (threading.Event() for _ in range(3))
    blocker = executor.submit(lambda: (occupied.set(), release.wait(3)))
    assert occupied.wait(1)
    calls = []
    runner = InspectionDispatcher(None, executor=executor, dispatch=lambda factory: calls.append("sweep"))
    closer = None
    try:
        assert runner.tick()
        assert runner.summary()["queued"] is True
        for _ in range(100):
            assert runner.tick() is False
        closer = threading.Thread(target=lambda: (runner.close(), finished.set()))
        closer.start()
        assert not finished.wait(.05)
        release.set()
        closer.join(2)
        assert finished.is_set() and calls == ["sweep"]
        assert runner.summary()["queued"] is False
        assert runner.tick() is False
        assert executor.submit(lambda: "owner-still-usable").result(timeout=1) == "owner-still-usable"
    finally:
        release.set()
        if closer is not None:
            closer.join(2)
        runner.close()
        executor.shutdown(wait=True, cancel_futures=True)


def test_failed_borrowed_sweep_does_not_interrupt_owner_shutdown(caplog):
    from platform_app.inspection_dispatch import InspectionDispatcher
    executor = ThreadPoolExecutor(max_workers=1)
    def failure(factory):
        raise ValueError("password=PRIVATE-BORROWED")
    runner = InspectionDispatcher(None, executor=executor, dispatch=failure)
    try:
        with pytest.raises(ValueError):
            runner.tick(wait=True)
        runner.close()
        assert executor.submit(lambda: "maintenance").result(timeout=1) == "maintenance"
        assert runner.summary()["last_error_type"] == "ValueError"
        assert "PRIVATE-BORROWED" not in caplog.text
    finally:
        runner.close()
        executor.shutdown(wait=True, cancel_futures=True)
