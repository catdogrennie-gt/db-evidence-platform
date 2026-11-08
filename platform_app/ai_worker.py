"""Independent AI queue process; the collection pool is never used here."""

from __future__ import annotations

import argparse
import os
import signal
import threading
import time
from uuid import uuid4

from .config import load_config
from .db import create_db_engine, create_session_factory, utcnow
from .investigations import run_investigation, reconcile_interrupted_investigations
from .models import WorkerHeartbeat
from .scheduling import claim, renew


def heartbeat(factory, worker_id):
    with factory.begin() as db:
        row = db.get(WorkerHeartbeat, (worker_id, "ai"))
        if row is None:
            db.add(
                WorkerHeartbeat(
                    worker_id=worker_id,
                    queue="ai",
                    last_seen_at=utcnow(),
                    details={"pid": os.getpid()},
                )
            )
        else:
            row.last_seen_at = utcnow()


def reconcile_interrupted(factory):
    """Expired AI calls are never silently retried after a worker disappears."""
    reconcile_interrupted_investigations(factory)


def serve(config, *, once=False):
    engine = create_db_engine(config)
    factory = create_session_factory(engine, config)
    worker_id = "ai-" + str(uuid4())
    stopped = threading.Event()
    current = [None]

    def stop(*_):
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    def keepalive():
        while not stopped.is_set():
            try:
                heartbeat(factory, worker_id)
                if current[0] is not None:
                    renew(factory, current[0])
            except Exception:
                # Do not log driver exceptions/DSNs; failed renew means fenced commit will be refused.
                pass
            stopped.wait(10)

    thread = threading.Thread(target=keepalive, name="ai-heartbeat", daemon=True)
    thread.start()
    try:
        while not stopped.is_set():
            try:
                lease = claim(factory, worker_id, queue="ai")
                reconcile_interrupted(factory)
                if lease is not None:
                    current[0] = lease
                    run_investigation(factory, lease, config)
                    current[0] = None
                elif not once:
                    stopped.wait(1)
                if once:
                    break
            except Exception:
                current[0] = None
                if once:
                    raise RuntimeError(
                        "AI worker dependency check or task processing failed"
                    ) from None
                stopped.wait(2)
    finally:
        stopped.set()
        thread.join(timeout=2)
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="Run the independent, bounded AI task queue")
    parser.add_argument("--config")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    serve(load_config(args.config), once=args.once)


if __name__ == "__main__":
    main()
