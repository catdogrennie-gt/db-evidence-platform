"""Single-flight inspection dispatch, isolated from collection queue claims."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging
import threading
import time

LOG = logging.getLogger("platform.worker")


class InspectionDispatcher:
    def __init__(self, factory, *, dispatch=None, executor=None):
        self.factory = factory
        self.dispatch = dispatch
        self.owns_executor = executor is None
        self.executor = executor if executor is not None else ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="inspection-dispatch")
        self.future = None
        self.lock = threading.RLock()
        self.closed = False
        self.next_due = 0.0
        self.state = {"status": "idle", "last_started_at": None, "last_completed_at": None,
                      "last_duration_seconds": None, "last_error_type": None}

    def _run(self):
        started = time.monotonic()
        stamp = lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.lock:
            self.state.update(status="running", last_started_at=stamp())
        error = None
        try:
            if self.dispatch is None:
                from .intelligence import dispatch_due
                return dispatch_due(self.factory)
            return self.dispatch(self.factory)
        except Exception as exc:
            error = type(exc).__name__
            LOG.error("Inspection dispatch failure %s", error)
            raise
        finally:
            with self.lock:
                self.state.update(status="failed" if error else "idle",
                    last_completed_at=stamp(), last_duration_seconds=time.monotonic() - started,
                    last_error_type=error)
                self.next_due = time.monotonic() + 1

    def tick(self, *, wait=False):
        """At most one sweep, with no backlog; only --once waits for it."""
        with self.lock:
            if self.closed:
                return False
            if self.future is not None and self.future.done():
                try:
                    self.future.result()
                except Exception:
                    if wait:
                        raise
                self.future = None
            if self.future is None:
                if time.monotonic() < self.next_due:
                    return False
                self.future = self.executor.submit(self._run)
            else:
                if not wait:
                    return False
            future = self.future
        if wait:
            future.result()
        return True

    def summary(self):
        with self.lock:
            queued = self.future is not None and not self.future.done() and not self.future.running()
            return {**self.state, "closed": self.closed, "queued": queued}

    def close(self):
        with self.lock:
            self.closed = True
            future = self.future
        # Complete transactions before the caller disposes its database engine.
        if self.owns_executor:
            self.executor.shutdown(wait=True, cancel_futures=True)
        elif future is not None:
            try:
                future.result()
            except Exception:
                # _run records the safe error type; shutdown must still drain
                # the executor owner and dispose database resources.
                pass
