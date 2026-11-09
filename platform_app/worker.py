"""Independent collection scheduler, bounded pool, leases and metric bridge."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import logging
import os
import signal
import threading
import time
from uuid import uuid4

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import aliased

from .collection_service import handle_collection, handle_connection_test
from .common import PlatformError, iso, parse_time
from . import config as collection_config
from .config import load_config, require_legacy, group_collection_allowed
from .db import create_db_engine, create_session_factory, utcnow
from .evidence import purge_expired
from .events import clock_tick, ensure_default_rules
from .metrics_bridge import start_bridge
from .secrets import SecretUnavailable
from .models import (
    CollectionRun,
    CollectionSchedule,
    ConnectionTest,
    Instance,
    InstanceLease,
    MonitoringMapping,
    PlatformSetting,
    Task,
    WorkerHeartbeat,
)
from .scheduling import (
    check_fence,
    claim,
    complete,
    db_now,
    enqueue,
    fail,
    renew,
    next_collection_due,
)

LOG = logging.getLogger("platform.worker")
ORDER = [
    "identity_status",
    "runtime",
    "statement_digest",
    "schema_config",
    "replication",
    "error_log",
    "slow_log",
    "host",
]


def schedule_due(factory):
    """Group due profiles by asset to reuse one bounded target transport."""
    created = []
    with factory.begin() as db:
        current = db_now(db)
        query = (
            select(CollectionSchedule)
            .join(Instance, Instance.id == CollectionSchedule.instance_id)
            .where(
                CollectionSchedule.enabled.is_(True),
                CollectionSchedule.next_due_at <= current,
                Instance.enabled.is_(True),
                Instance.lifecycle_state == "Active",
            )
        )
        if collection_config.zabbix_only(db=db):
            # Retired rows must not occupy the bounded due-work window.
            query = query.join(
                MonitoringMapping, MonitoringMapping.instance_id == Instance.id
            ).where(
                Instance.connection_mode == "zabbix",
                MonitoringMapping.layer == "group",
                MonitoringMapping.selected.is_(True),
                CollectionSchedule.collector == "monitoring",
            )
        schedules = list(db.scalars(
            query.order_by(CollectionSchedule.next_due_at)
            .limit(100)
            .with_for_update(skip_locked=True)
        ))
        groups = {}
        for row in schedules:
            groups.setdefault(row.instance_id, []).append(row)
        for instance_id, rows in groups.items():
            instance = db.get(Instance, instance_id)
            if not group_collection_allowed(db, instance):
                continue
            if db.scalar(
                select(Task.id)
                .where(
                    Task.instance_id == instance_id,
                    Task.type.in_(["collect_instance", "activate_instance"]),
                    Task.status.in_(["queued", "retry_wait", "running"]),
                )
                .limit(1)
            ):
                continue
            valid = [row for row in rows if row.config_revision == instance.config_revision and group_collection_allowed(db, instance, collector=row.collector)]
            if not valid:
                continue
            collectors = sorted(
                {row.collector for row in valid},
                key=lambda value: ORDER.index(value) if value in ORDER else 100,
            )
            task = enqueue(
                db,
                "collect_instance",
                instance_id=instance_id,
                payload={"collectors": collectors, "config_revision": instance.config_revision},
                timeout_seconds=180,
            )
            created.append(task.id)
            for row in valid:
                # No catch-up storm after sleep/restart. Record a real gap and
                # schedule one observation, then the next normal period.
                row.next_due_at = next_collection_due(instance, row, current)
    return created


def heartbeat(factory, worker_id, active_count, maintenance=None):
    with factory.begin() as db:
        row = db.get(WorkerHeartbeat, (worker_id, "collection"))
        if row is None:
            row = WorkerHeartbeat(worker_id=worker_id, queue="collection", last_seen_at=db_now(db))
            db.add(row)
        row.last_seen_at = db_now(db)
        row.details = {"pid": os.getpid(), "active_tasks": active_count}
        if maintenance is not None:
            row.details["maintenance"] = maintenance.summary()


class MaintenanceRunner:
    """One bounded maintenance job at a time, separate from queue dispatch.

    Persisted UTC boundaries survive restarts; local scheduling uses monotonic
    time. Exception text is never stored or logged because it may contain SQL,
    paths or credentials. A failed status write also backs off locally.
    """

    def __init__(self, factory, config, *, jobs=None):
        self.factory, self.config = factory, config
        self.jobs = (
            jobs
            if jobs is not None
            else {
                "clock": (15, self._clock),
                "retention": (60, self._retention),
                "exports": (60, self._exports),
            }
        )
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="maintenance")
        self.future = None
        self.active_name = None
        self.due = {name: 0.0 for name in self.jobs}
        self.states = {}
        self.state_lock = threading.Lock()

    def _clock(self):
        with self.factory.begin() as db:
            clock_tick(db, now=db_now(db))
        return {}

    def _retention(self):
        with self.factory.begin() as db:
            return purge_expired(db, limit=1000)

    def _exports(self):
        from .exports import purge_expired_exports

        return purge_expired_exports(self.factory, self.config, limit=100)

    def _store(self, db, name, state):
        row = db.get(PlatformSetting, "maintenance:" + name)
        if row is None:
            db.add(PlatformSetting(key="maintenance:" + name, value=dict(state)))
        else:
            row.value, row.version = dict(state), row.version + 1
        with self.state_lock:
            self.states[name] = dict(state)

    def run_job(self, name):
        interval, callback = self.jobs[name]
        state = {}
        try:
            with self.factory.begin() as db:
                now = db_now(db)
                row = db.get(PlatformSetting, "maintenance:" + name)
                state = dict(row.value or {}) if row else {}
                boundary = state.get("next_due_at")
                if boundary and parse_time(boundary) > now:
                    with self.state_lock:
                        self.states[name] = state
                    return {
                        "next_delay": max(1, (parse_time(boundary) - now).total_seconds()),
                        "deferred": True,
                    }
                state = {
                    **state,
                    "status": "running",
                    "interval_seconds": interval,
                    "last_attempt_at": iso(now),
                    "last_success_at": state.get("last_success_at"),
                    "last_error_type": state.get("last_error_type"),
                    "next_due_at": iso(now + timedelta(seconds=interval)),
                    "next_retry_at": None,
                    "backlog": {
                        "status": "unknown",
                        "reason_code": "eligible_backlog_not_measured",
                    },
                }
                self._store(db, name, state)
            result = callback()
            result = result if isinstance(result, dict) else {}
            # Only explicit numeric counters leave the maintenance callback.
            counters = {
                str(k): v
                for k, v in result.items()
                if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10**12
            }
            with self.factory.begin() as db:
                now = db_now(db)
                state.update(
                    status="available",
                    last_success_at=iso(now),
                    last_error_type=None,
                    consecutive_failures=0,
                    next_due_at=iso(now + timedelta(seconds=interval)),
                    next_retry_at=None,
                    last_result=counters,
                )
                if name == "retention":
                    deleted = sum(v for k, v in counters.items() if k.endswith("_deleted"))
                    state["backlog"] = {
                        "status": "possible" if deleted >= 1000 else "unknown",
                        "reason_code": "batch_budget_reached"
                        if deleted >= 1000
                        else "eligible_backlog_not_measured",
                    }
                self._store(db, name, state)
            return {"next_delay": interval, "deferred": False}
        except Exception as exc:
            error_type = type(exc).__name__[:80]
            failures = min(1000, int(state.get("consecutive_failures", 0)) + 1)
            delay = min(300, 30 * 2 ** min(failures - 1, 4))
            LOG.error("Maintenance job failed job=%s error_type=%s", name, error_type)
            state.update(
                status="failed",
                last_error_type=error_type,
                consecutive_failures=failures,
                last_success_at=state.get("last_success_at"),
            )
            try:
                with self.factory.begin() as db:
                    now = db_now(db)
                    state.update(
                        next_due_at=iso(now + timedelta(seconds=delay)),
                        next_retry_at=iso(now + timedelta(seconds=delay)),
                    )
                    self._store(db, name, state)
            except Exception as status_exc:
                LOG.error("Maintenance status unavailable error_type=%s", type(status_exc).__name__)
                with self.state_lock:
                    self.states[name] = state
            return {"next_delay": delay, "deferred": False}

    def tick(self):
        if self.future is not None:
            if not self.future.done():
                return
            try:
                delay = self.future.result()["next_delay"]
            except Exception as exc:
                LOG.error("Maintenance executor failure error_type=%s", type(exc).__name__)
                delay = 30
            self.due[self.active_name] = time.monotonic() + delay
            self.future = self.active_name = None
        now = time.monotonic()
        ready = [name for name in self.jobs if self.due[name] <= now]
        if ready:
            self.active_name = min(ready, key=lambda name: self.due[name])
            self.future = self.executor.submit(self.run_job, self.active_name)

    def summary(self):
        with self.state_lock:
            return {
                name: {
                    "status": state.get("status", "unknown"),
                    "last_error_type": state.get("last_error_type"),
                }
                for name, state in self.states.items()
            }

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)


def reconcile_interrupted(factory):
    with factory.begin() as db:
        rows = db.scalars(
            select(CollectionRun)
            .join(Task, Task.id == CollectionRun.task_id)
            .where(
                CollectionRun.status == "running",
                or_(
                    Task.status.in_(["failed", "cancelled", "expired", "retry_wait", "succeeded"]),
                    CollectionRun.attempt < Task.attempt,
                ),
            )
            .limit(100)
            .with_for_update(skip_locked=True)
        )
        for run in rows:
            run.status, run.error_code, run.finished_at = "failed", "task_interrupted", db_now(db)
        tests = list(
            db.scalars(
                select(ConnectionTest)
                .join(Task, Task.id == ConnectionTest.task_id)
                .where(
                    ConnectionTest.status.in_(["queued", "running"]),
                    Task.status.in_(["failed", "cancelled", "expired", "succeeded"]),
                )
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        )
        for test in tests:
            task = db.get(Task, test.task_id)
            test.status = "cancelled" if task.status == "cancelled" else "failed"
            test.results = {
                **(test.results or {}),
                "error_code": task.error_code or "task_interrupted",
            }
        successor = aliased(Task)
        activations = list(
            db.scalars(
                select(Task)
                .join(Instance, Instance.id == Task.instance_id)
                .outerjoin(InstanceLease, InstanceLease.instance_id == Instance.id)
                .where(
                    Task.type == "activate_instance",
                    Task.status.in_(["failed", "cancelled", "expired"]),
                    Instance.lifecycle_state == "Activating",
                    Instance.enabled.is_(False),
                    Task.payload["config_revision"].as_integer() == Instance.config_revision,
                    or_(InstanceLease.task_id.is_(None), InstanceLease.task_id == Task.id),
                    ~exists(
                        select(successor.id).where(
                            successor.instance_id == Instance.id,
                            successor.type == "activate_instance",
                            successor.id != Task.id,
                            successor.status.in_(["queued", "retry_wait", "running"]),
                        )
                    ),
                )
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        )
        for task in activations:
            instance = db.scalar(
                select(Instance).where(Instance.id == task.instance_id).with_for_update()
            )
            if (
                instance.lifecycle_state != "Activating"
                or instance.enabled
                or instance.config_revision != (task.payload or {}).get("config_revision")
            ):
                continue
            if db.scalar(
                select(Task.id)
                .where(
                    Task.instance_id == instance.id,
                    Task.type == "activate_instance",
                    Task.id != task.id,
                    Task.status.in_(["queued", "retry_wait", "running"]),
                )
                .limit(1)
            ):
                continue
            owner = db.scalar(
                select(InstanceLease)
                .where(InstanceLease.instance_id == instance.id)
                .with_for_update()
            )
            if owner and owner.task_id and owner.task_id != task.id:
                continue
            instance.lifecycle_state, instance.health = "Ready", "unknown"
            for schedule in db.scalars(
                select(CollectionSchedule).where(CollectionSchedule.instance_id == instance.id)
            ):
                schedule.enabled = False


def execute(factory, lease, config):
    try:
        if lease.type == "connection_test":
            require_legacy(config)
            return handle_connection_test(factory, lease, config)
        if lease.type in ("activate_instance", "collect_instance"):
            with factory() as db:
                external = db.get(Instance, lease.instance_id)
                if not group_collection_allowed(db, external, config):
                    require_legacy(config)
                external = external is not None and external.connection_mode == "zabbix"
            if external:
                from .monitoring import handle_collection as handle_monitoring
                return handle_monitoring(factory, lease, config)
            return handle_collection(factory, lease, config)
        if lease.type == "verification":
            from .remediation import handle_verification

            with factory.begin() as db:
                check_fence(db, lease, allow_inactive=True)
                result = handle_verification(db, lease)
                complete(
                    db,
                    lease,
                    result={"verification_id": lease.payload["verification_id"], **result},
                    result_ref=f"/api/v1/verifications/{lease.payload['verification_id']}",
                    allow_inactive=True,
                )
                return result
        if lease.type == "export":
            from .exports import handle_export

            return handle_export(factory, lease, config)
        if lease.type == "explain":
            from .explain import handle_explain

            return handle_explain(factory, lease, config)
        raise PlatformError(422, "unsupported_task_type", "任务类型尚未启用。")
    except Exception as exc:
        code = (
            exc.detail.get("code", "task_failed")
            if isinstance(exc, PlatformError)
            else getattr(exc, "code", "task_failed")
        )
        # Task lifecycle, budgets and internal bugs do not describe upstream health.
        retrieval_failure = isinstance(exc, SecretUnavailable) or (
            isinstance(exc, PlatformError)
            and code in {"monitoring_upstream_unavailable", "monitoring_credential_unavailable"}
        )
        if lease.type in ("collect_instance", "activate_instance") and retrieval_failure:
            try:
                with factory.begin() as db:
                    check_fence(db, lease, config_revision=lease.payload.get("config_revision"))
                    from .monitoring import mapping_for
                    from .models import MonitoringSource
                    mapping = mapping_for(db, lease.instance_id)
                    if mapping:
                        source = db.get(MonitoringSource, mapping.source_id)
                        source.status, source.error_code = "failed", "monitoring_collection_failed"
            except PlatformError:
                pass  # An obsolete result cannot overwrite current source health.
        fail(factory, lease, code)
        LOG.error(
            "Task failed type=%s id=%s error_type=%s code=%s",
            lease.type,
            lease.id,
            type(exc).__name__,
            code,
        )
        return {"status": "failed", "error_code": code}


def serve(config, *, once=False, no_bridge=False):
    engine = create_db_engine(config)
    factory = create_session_factory(engine, config)
    worker_id = "collection-" + str(uuid4())
    stopped, lease_lock = threading.Event(), threading.Lock()
    active_leases = {}
    max_workers = max(1, min(8, int(getattr(config, "collection_workers", 4))))
    bridge = None

    def stop(*_):
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    with factory.begin() as db:
        ensure_default_rules(db)
    if not no_bridge:
        bridge = start_bridge(factory, config)
    maintenance = MaintenanceRunner(factory, config)
    demo_controller = None
    if (getattr(config, "demo", None) or {}).get("instance_id"):
        from .demo import DemoController
        demo_controller = DemoController(factory, config)
        demo_controller.start()

    def keepalive():
        while not stopped.is_set():
            with lease_lock:
                leases = list(active_leases.values())
            try:
                heartbeat(factory, worker_id, len(leases), maintenance)
                for lease in leases:
                    try:
                        renew(factory, lease)
                    except Exception:
                        # A stale/cancelled lease must fail its final commit.
                        pass
            except Exception as exc:
                LOG.error("Heartbeat dependency failure %s", type(exc).__name__)
            stopped.wait(10)

    thread = threading.Thread(target=keepalive, name="collection-heartbeat", daemon=True)
    thread.start()
    futures = {}
    try:
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="collector") as pool:
            while not stopped.is_set():
                try:
                    from .showcase import reconcile_pacing
                    try:
                        with factory.begin() as db:
                            reconcile_pacing(db, config)
                    except Exception as exc:
                        LOG.error("Cadence cleanup failure %s", type(exc).__name__)
                    schedule_due(factory)
                    from .intelligence import dispatch_due
                    dispatch_due(factory)
                    maintenance.tick()
                    reconcile_interrupted(factory)
                    for future in list(futures):
                        if future.done():
                            task_id = futures.pop(future)
                            with lease_lock:
                                active_leases.pop(task_id, None)
                            # execute handles operational failures; unexpected
                            # infrastructure errors must not kill other workers.
                            try:
                                future.result()
                            except Exception as exc:
                                LOG.error("Pool infrastructure failure %s", type(exc).__name__)
                    while len(futures) < max_workers:
                        lease = claim(factory, worker_id, "collection") or claim(
                            factory, worker_id, "export"
                        )
                        if lease is None:
                            break
                        with lease_lock:
                            active_leases[lease.id] = lease
                        future = pool.submit(execute, factory, lease, config)
                        futures[future] = lease.id
                    if once:
                        for future in list(futures):
                            future.result()
                        break
                except Exception as exc:
                    LOG.error("Scheduler dependency failure %s", type(exc).__name__)
                    if once:
                        raise
                stopped.wait(1)
    finally:
        stopped.set()
        thread.join(timeout=2)
        if demo_controller is not None:
            demo_controller.close()
        maintenance.close()
        if bridge is not None:
            bridge.shutdown()
            bridge.server_close()
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(
        description="Run persistent source collection and metric bridge"
    )
    parser.add_argument("--config")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-bridge", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    serve(load_config(args.config), once=args.once, no_bridge=args.no_bridge)


if __name__ == "__main__":
    main()
