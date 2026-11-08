"""Durable bounded queues with MySQL row locks and fenced instance leases."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select

from .common import PlatformError, iso, json_safe
from .db import utcnow
from .models import Instance, InstanceLease, Task

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "expired"})
LEASE_SECONDS = 90


def next_collection_due(instance, schedule, current):
    """Return a real future boundary on the immutable per-asset UTC phase.

    Normal jitter advances to the next nominal boundary. Misaligned schedules
    and a full missed period get one realignment at least one period away;
    missed observations are never caught up. Revisions preserve the asset
    created_at anchor; interval changes use the new period on the same anchor.
    This pure helper never makes an undued source eligible for a current task.
    """
    period = timedelta(seconds=schedule.interval_seconds)
    anchor = instance.created_at
    aligned = (schedule.next_due_at - anchor) % period == timedelta(0)
    if aligned and current - schedule.next_due_at < period:
        return anchor + (((current - anchor) // period) + 1) * period
    minimum = current + period
    steps = (minimum - anchor) // period
    boundary = anchor + steps * period
    return boundary if boundary >= minimum else boundary + period


def db_now(db):
    """Lease authority is the control database clock, never a worker's wall clock."""
    if db.get_bind().dialect.name == "mysql":
        return db.scalar(select(func.utc_timestamp(6)))
    return utcnow()  # Isolated SQLite tests have no independent database clock.


@dataclass(frozen=True)
class LeasedTask:
    id: str
    type: str
    queue: str
    actor_id: str | None
    instance_id: str | None
    payload: dict
    fence: int
    worker_id: str
    deadline_at: object
    attempt: int


def payload_digest(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(json_safe(payload), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def find_idempotent(
    db,
    task_type,
    actor_id,
    idempotency_key,
    payload,
    *,
    instance_id=None,
    request_hash_override=None,
):
    if not idempotency_key:
        return None
    if len(idempotency_key) > 160:
        raise PlatformError(422, "invalid_idempotency_key", "幂等键过长。")
    key = hashlib.sha256(f"{actor_id}:{task_type}:{idempotency_key}".encode()).hexdigest()
    old = db.scalar(select(Task).where(Task.idempotency_key == key))
    expected = request_hash_override or payload_digest(
        {"instance_id": instance_id, "payload": payload}
    )
    if old and old.request_hash != expected:
        raise PlatformError(409, "idempotency_conflict", "同一幂等键不能提交不同内容。")
    return old


def enqueue(
    db,
    task_type: str,
    *,
    actor_id=None,
    instance_id=None,
    payload=None,
    queue="collection",
    timeout_seconds=180,
    idempotency_key=None,
    max_attempts=3,
    parent_task_id=None,
    request_hash_override=None,
):
    payload = payload or {}
    digest = request_hash_override or payload_digest(
        {"instance_id": instance_id, "payload": payload}
    )
    key = None
    if idempotency_key:
        if len(idempotency_key) > 160:
            raise PlatformError(422, "invalid_idempotency_key", "幂等键过长。")
        key = hashlib.sha256(f"{actor_id}:{task_type}:{idempotency_key}".encode()).hexdigest()
        old = find_idempotent(
            db,
            task_type,
            actor_id,
            idempotency_key,
            payload,
            instance_id=instance_id,
            request_hash_override=request_hash_override,
        )
        if old:
            return old
    current = db_now(db)
    task = Task(
        id=str(uuid4()),
        type=task_type,
        queue=queue,
        actor_id=actor_id,
        instance_id=instance_id,
        payload=payload,
        request_hash=digest,
        status="queued",
        due_at=current,
        deadline_at=current + timedelta(seconds=timeout_seconds),
        attempt=0,
        fence=0,
        cancel_requested=False,
        max_attempts=max_attempts,
        idempotency_key=key,
        parent_task_id=parent_task_id,
        progress={},
    )
    db.add(task)
    db.flush()
    return task


def task_ref(task):
    return {
        "id": task.id,
        "type": task.type,
        "status": task.status,
        "created_at": iso(task.created_at),
        "poll_url": f"/api/v1/tasks/{task.id}",
    }


def task_detail(task):
    return {
        **task_ref(task),
        "instance_id": task.instance_id,
        "attempt": task.attempt,
        "started_at": iso(task.started_at),
        "finished_at": iso(task.finished_at),
        "cancel_requested": task.cancel_requested,
        "result_ref": task.result_ref,
        "result": json_safe(task.result or {}),
        "progress": task.progress or {},
        "error_code": task.error_code,
        "error_message": task.error_message,
    }


def recover_expired(db, queue: str):
    current = db_now(db)
    rows = db.scalars(
        select(Task)
        .where(
            Task.queue == queue,
            Task.status == "running",
            or_(Task.lease_until <= current, Task.deadline_at <= current),
        )
        .execution_options(populate_existing=True)
        .with_for_update(skip_locked=True)
        .limit(100)
    )
    for task in rows:
        if task.cancel_requested:
            task.status = "cancelled"
        elif task.deadline_at <= current:
            task.status = "expired"
            task.error_code = "task_deadline_exceeded"
        elif queue == "ai":
            # A network call may already be billable. Never silently resend.
            task.status = "failed"
            task.error_code = "worker_lost_call_outcome_unknown"
        elif task.attempt >= task.max_attempts:
            task.status = "failed"
            task.error_code = "attempts_exhausted"
        else:
            task.status = "retry_wait"
            task.due_at = current + timedelta(seconds=min(30, 2**task.attempt))
            task.error_code = "worker_lease_expired"
        if task.status in TERMINAL:
            task.finished_at = current
        if task.instance_id:
            owner = db.scalar(
                select(InstanceLease)
                .where(InstanceLease.instance_id == task.instance_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            # A deadline can precede the heartbeat-extended lease. Invalidate
            # exactly this owner; never clear a successor's valid lease.
            if (
                owner
                and owner.task_id == task.id
                and owner.fence == task.fence
                and owner.leased_by == task.leased_by
            ):
                owner.fence += 1
                owner.lease_until = owner.leased_by = owner.task_id = None
        task.leased_by = None
        task.lease_until = None


def claim(session_factory, worker_id: str, queue="collection") -> LeasedTask | None:
    with session_factory.begin() as db:
        recover_expired(db, queue)
        current = db_now(db)
        tasks = list(
            db.scalars(
                select(Task)
                .where(
                    Task.queue == queue,
                    Task.status.in_(["queued", "retry_wait"]),
                    Task.due_at <= current,
                )
                .order_by(Task.due_at, Task.id)
                .with_for_update(skip_locked=True)
                .limit(16)
            )
        )
        for task in tasks:
            if task.cancel_requested or task.deadline_at <= current:
                task.status = "cancelled" if task.cancel_requested else "expired"
                task.finished_at = current
                continue
            fence = task.fence + 1
            if task.instance_id:
                lease = db.scalar(
                    select(InstanceLease)
                    .where(InstanceLease.instance_id == task.instance_id)
                    .with_for_update()
                )
                if lease is None:
                    # Inventory normally creates this row with the instance.
                    lease = InstanceLease(instance_id=task.instance_id, fence=0)
                    db.add(lease)
                    db.flush()
                if lease.lease_until and lease.lease_until > current:
                    task.due_at = current + timedelta(seconds=2)
                    continue
                fence = lease.fence + 1
                lease.fence, lease.task_id, lease.leased_by = fence, task.id, worker_id
                lease.lease_until = current + timedelta(seconds=LEASE_SECONDS)
            task.fence, task.leased_by, task.lease_until = (
                fence,
                worker_id,
                current + timedelta(seconds=LEASE_SECONDS),
            )
            task.status, task.attempt = "running", task.attempt + 1
            task.started_at = current
            task.error_code = task.error_message = None
            return LeasedTask(
                task.id,
                task.type,
                task.queue,
                task.actor_id,
                task.instance_id,
                dict(task.payload or {}),
                fence,
                worker_id,
                task.deadline_at,
                task.attempt,
            )
    return None


def check_fence(db, lease: LeasedTask, *, config_revision=None, allow_inactive=False):
    """Call inside the SAME transaction that persists results and completes task."""
    current = db_now(db)
    task = db.scalar(
        select(Task)
        .where(Task.id == lease.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if (
        not task
        or task.status != "running"
        or task.fence != lease.fence
        or task.leased_by != lease.worker_id
        or not task.lease_until
        or task.lease_until <= current
    ):
        raise PlatformError(409, "stale_task_fence", "任务租约已失效，旧结果不能写入。")
    if task.cancel_requested:
        raise PlatformError(409, "task_cancelled", "任务已请求取消。")
    if task.deadline_at <= current:
        raise PlatformError(409, "task_deadline_exceeded", "任务超过截止时间。")
    if lease.instance_id:
        instance = db.scalar(
            select(Instance)
            .where(Instance.id == lease.instance_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        instance_lease = db.scalar(
            select(InstanceLease)
            .where(InstanceLease.instance_id == lease.instance_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (
            not instance
            or not instance_lease
            or instance_lease.fence != lease.fence
            or instance_lease.task_id != lease.id
            or instance_lease.leased_by != lease.worker_id
            or not instance_lease.lease_until
            or instance_lease.lease_until <= current
        ):
            raise PlatformError(409, "stale_instance_fence", "实例租约已改变。")
        if config_revision is not None and instance.config_revision != config_revision:
            raise PlatformError(
                409, "configuration_changed", "实例配置已更新，旧采集结果不能写入。"
            )
        if not allow_inactive and (not instance.enabled or instance.lifecycle_state != "Active"):
            raise PlatformError(409, "instance_not_active", "实例已停采或未激活。")
    return task


def renew(session_factory, lease: LeasedTask):
    with session_factory.begin() as db:
        task = check_fence(db, lease, allow_inactive=True)
        expiry = db_now(db) + timedelta(seconds=LEASE_SECONDS)
        task.lease_until = expiry
        if lease.instance_id:
            row = db.get(InstanceLease, lease.instance_id)
            row.lease_until = expiry


def complete(
    db,
    lease: LeasedTask,
    *,
    result=None,
    result_ref=None,
    status="succeeded",
    error_code=None,
    error_message=None,
    allow_inactive=False,
    config_revision=None,
):
    task = check_fence(db, lease, config_revision=config_revision, allow_inactive=allow_inactive)
    task.status, task.finished_at = status, db_now(db)
    task.result, task.result_ref = result or {}, result_ref
    task.error_code, task.error_message = error_code, error_message
    task.lease_until = None
    if lease.instance_id:
        row = db.get(InstanceLease, lease.instance_id)
        row.lease_until, row.leased_by, row.task_id = None, None, None
    return task


def fail(session_factory, lease: LeasedTask, code: str, message="任务失败，请查看分项状态。"):
    with session_factory.begin() as db:
        # Do not let an obsolete worker overwrite its successor's result.
        task = db.scalar(select(Task).where(Task.id == lease.id).with_for_update())
        current = db_now(db)
        if (
            not task
            or task.status != "running"
            or task.fence != lease.fence
            or task.leased_by != lease.worker_id
            or not task.lease_until
            or task.lease_until <= current
            or task.deadline_at <= current
        ):
            return False
        if lease.instance_id:
            row = db.scalar(
                select(InstanceLease)
                .where(InstanceLease.instance_id == lease.instance_id)
                .with_for_update()
            )
            if (
                not row
                or row.fence != lease.fence
                or row.task_id != lease.id
                or row.leased_by != lease.worker_id
                or not row.lease_until
                or row.lease_until <= current
            ):
                return False
        task.status = "cancelled" if task.cancel_requested else "failed"
        task.error_code, task.error_message, task.finished_at = code, message, current
        task.lease_until = None
        if lease.instance_id:
            row = db.get(InstanceLease, lease.instance_id)
            if row and row.fence == lease.fence:
                row.lease_until, row.leased_by, row.task_id = None, None, None
        return True


def request_cancel(db, task):
    if task.status in TERMINAL:
        return task
    task.cancel_requested = True
    if task.status in ("queued", "retry_wait"):
        task.status, task.finished_at = "cancelled", db_now(db)
    return task
