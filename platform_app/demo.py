"""Durable, globally serialized fixed demo; transport is worker-private."""

import logging
import threading
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select

from . import models as m
from .access import actor_for_user, authorize
from .common import PlatformError, iso
from .scheduling import db_now, enqueue

LOG = logging.getLogger("platform.demo")

DEFAULTS = dict(
    enabled=False,
    max_duration_seconds=180,
    cooldown_seconds=600,
    max_daily_runs=8,
    max_daily_ai_runs=4,
    mode="live_redacted",
    external_confirmed=False,
)
CLEAN_KEYS = ("sessions_closed", "locks_cleared", "dataset_unchanged")
ACTIVE = (
    "requested",
    "injecting",
    "active",
    "recovering",
    "cleanup_unknown",
    "awaiting_recovery",
    "recovery_unconfirmed",
)
QUESTION = "分析本轮隔离 Lab 的锁等待现象，结合冻结的 Zabbix 数据库与主机证据，说明可确认的直接机理、候选原因、反证和未知，并给出人工只读核对与恢复复验建议。实验负载是人工生成的单行事务阻塞，不代表生产事故。"


def settings(config):
    cfg = {**DEFAULTS, **(getattr(config, "demo", None) or {})}
    for key, low, high in [
        ("max_duration_seconds", 1, 180),
        ("cooldown_seconds", 600, 86400),
        ("max_daily_runs", 1, 8),
        ("max_daily_ai_runs", 0, 4),
    ]:
        value = cfg[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError("Invalid demo limits")
    if cfg["mode"] != "live_redacted":
        raise ValueError("Invalid demo mode")
    if cfg["enabled"]:
        if not all(
            cfg.get(k)
            for k in (
                "instance_id",
                "runner_user_id",
                "target_config_path",
                "lock_itemid",
                "source_id",
            )
        ):
            raise ValueError("Incomplete demo configuration")
        if not Path(cfg["target_config_path"]).is_absolute():
            raise ValueError("Demo target must be private absolute path")
        if cfg["external_confirmed"] is not True:
            raise ValueError("Demo model consent required")
    return cfg


def control(db):
    row = db.scalar(
        select(m.DemoControl)
        .where(m.DemoControl.id == 1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise PlatformError(503, "demo_not_initialized", "演示尚未初始化。")
    return row


def target(actor, cfg, action="data.read"):
    identifier = cfg.get("instance_id")
    if not identifier:
        raise PlatformError(503, "demo_disabled", "演示未启用。")
    authorize(actor, "data.read", [identifier])
    authorize(actor, action, [identifier])
    obj = actor.db.get(m.Instance, identifier)
    if obj is None:
        raise PlatformError(404, "resource_not_found", "资源不存在。")
    return obj


def budget(db, now):
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return dict(
        runs_used=db.scalar(
            select(func.count()).select_from(m.DemoRun).where(m.DemoRun.started_at >= day)
        )
        or 0,
        ai_runs_used=db.scalar(
            select(func.count()).select_from(m.DemoRun).where(m.DemoRun.ai_reserved_at >= day)
        )
        or 0,
        resets_at=iso(day + timedelta(days=1)),
    )


def request_start(actor, config):
    cfg = settings(config)
    obj = target(actor, cfg, "demo.operate")
    if not cfg["enabled"]:
        raise PlatformError(409, "demo_disabled", "演示已暂停。")
    if not obj.is_lab:
        raise PlatformError(403, "demo_lab_required", "仅可操作登记的隔离实验对象。")
    db = actor.db
    ctl = control(db)
    now = db_now(db)
    current = db.get(m.DemoRun, ctl.current_run_id) if ctl.current_run_id else None
    if current and current.status in ACTIVE:
        return current
    if ctl.blocked:
        raise PlatformError(409, "demo_cleanup_unconfirmed", "清理状态尚未确认。")
    if ctl.cooldown_until and ctl.cooldown_until > now:
        raise PlatformError(429, "demo_cooldown", "共享实验冷却中。")
    if budget(db, now)["runs_used"] >= cfg["max_daily_runs"]:
        raise PlatformError(429, "demo_daily_budget", "今日演示额度已用完。")
    mapping = db.scalar(
        select(m.MonitoringMapping).where(m.MonitoringMapping.instance_id == obj.id)
    )
    if (
        not mapping
        or not mapping.selected
        or mapping.layer != "group"
        or mapping.source_id != cfg["source_id"]
        or not any(str(i.get("itemid")) == str(cfg["lock_itemid"]) for i in mapping.items)
    ):
        raise PlatformError(409, "demo_mapping_invalid", "固定实验监控映射不可用。")
    selected = next(i for i in mapping.items if str(i.get("itemid")) == str(cfg["lock_itemid"]))
    lock_hostid = str(selected.get("hostid") or "")
    if not lock_hostid or (cfg.get("lock_hostid") and str(cfg["lock_hostid"]) != lock_hostid):
        raise PlatformError(409, "demo_mapping_invalid", "固定实验监控映射不可用。")
    row = m.DemoRun(
        id=str(uuid4()),
        instance_id=obj.id,
        requested_by=actor.user_id,
        started_at=now,
        mapping_version=mapping.version,
        config_revision=obj.config_revision,
        source_id=cfg["source_id"],
        lock_itemid=str(cfg["lock_itemid"]),
        lock_hostid=lock_hostid,
    )
    db.add(row)
    db.flush()
    ctl.current_run_id = row.id
    ctl.owner = None
    ctl.lease_until = None
    milestone(row, "requested", "已请求共享实验", now)
    return row


def request_stop(actor, config, run_id=None):
    cfg = settings(config)
    target(actor, cfg, "demo.operate")
    db = actor.db
    ctl = control(db)
    if run_id and run_id != ctl.current_run_id:
        raise PlatformError(409, "demo_stale_run", "该轮实验已结束，请刷新状态。")
    row = db.get(m.DemoRun, ctl.current_run_id) if ctl.current_run_id else None
    if row and row.status in ACTIVE:
        row.stop_requested = True
    return row


def milestone(run, key, label, now):
    if not any(s["key"] == key for s in run.steps or []):
        run.steps = [
            *(run.steps or []),
            dict(key=key, label=label, status="completed", at=iso(now), detail=None),
        ]


def proof(db, run, cfg, *, positive):
    boundary = run.fault_confirmed_at if positive else run.stopped_at
    if not boundary:
        return None
    mapping = db.scalar(
        select(m.MonitoringMapping).where(m.MonitoringMapping.instance_id == run.instance_id)
    )
    obj = db.get(m.Instance, run.instance_id)
    if (
        not mapping
        or mapping.version != run.mapping_version
        or obj.config_revision != run.config_revision
    ):
        return None
    now = db_now(db)
    rows = db.scalars(
        select(m.Evidence)
        .where(
            m.Evidence.instance_id == run.instance_id,
            m.Evidence.source == "mysql.lock_waits",
            m.Evidence.source_mode == "live",
            m.Evidence.observed_at >= boundary,
            m.Evidence.observed_at <= now,
            m.Evidence.observed_at >= now - timedelta(seconds=90),
        )
        .order_by(m.Evidence.observed_at.desc())
        .limit(100)
    )
    for row in rows:
        c = row.coverage or {}
        payload = row.payload or {}
        observed = payload.get("rows")
        if c.get("availability") != "available":
            continue
        # Snapshot completeness is distinct from retained history coverage.
        reasons = set(c.get("reason_codes") or c.get("gaps") or [])
        snapshot_ok = (
            c.get("snapshot_complete") is True
            and c.get("snapshot_contract") == "db-evidence-lock-snapshot-v2"
            and c.get("snapshot_truncated") is False
            and type(c.get("snapshot_total_row_count")) is int
            and isinstance(observed, list)
            and c["snapshot_total_row_count"] == len(observed)
            and len(observed) <= 50
        )
        if not snapshot_ok or reasons - {"history_truncated"}:
            continue
        if row.truncated or c.get("complete") is not True:
            if reasons != {"history_truncated"}:
                continue
        if (
            c.get("transport") != "zabbix"
            or c.get("source_id") != run.source_id
            or str(c.get("itemid")) != run.lock_itemid
            or str(c.get("hostid")) != run.lock_hostid
            or c.get("mapping_version") != run.mapping_version
            or c.get("config_revision") != run.config_revision
        ):
            continue
        if positive and run.cleanup_started_at and row.observed_at >= run.cleanup_started_at:
            continue
        if isinstance(observed, list) and bool(observed) == positive:
            return row
    return None


def project_run(db, row):
    if not row:
        return None
    result = {
        k: getattr(row, k)
        for k in (
            "id",
            "status",
            "phase",
            "fault_state",
            "stop_requested",
            "evidence_id",
            "investigation_id",
            "analysis_status",
            "error_code",
            "cleanup",
            "steps",
        )
    }
    for k in ("started_at", "expires_at", "stopped_at", "observed_at"):
        result[k] = iso(getattr(row, k))
    if row.investigation_id:
        investigation = db.get(m.Investigation, row.investigation_id)
        if investigation:
            result["analysis_status"] = investigation.status
    return result


def public_budget(db, now, cfg):
    counts = budget(db, now)
    return dict(
        used_runs=counts["runs_used"],
        remaining_runs=max(0, cfg["max_daily_runs"] - counts["runs_used"]),
        used_ai_runs=counts["ai_runs_used"],
        remaining_ai_runs=max(0, cfg["max_daily_ai_runs"] - counts["ai_runs_used"]),
        resets_at=counts["resets_at"],
    )


def snapshot(actor, config):
    cfg = settings(config)
    db = actor.db
    now = db_now(db)
    if not cfg.get("instance_id"):
        return dict(
            enabled=False,
            target=None,
            can_control=False,
            scenario="mysql_lock_wait",
            server_time=iso(now),
            limits={k: cfg[k] for k in DEFAULTS if k.startswith("max_") or k == "cooldown_seconds"},
            budget=public_budget(db, now, cfg),
            cooldown_until=None,
            run=None,
            report=None,
            history=[],
        )
    obj = target(actor, cfg)
    can_control = False
    try:
        authorize(actor, "demo.operate", [obj.id])
        can_control = bool(obj.is_lab)
    except HTTPException:
        pass
    rows = list(
        db.scalars(
            select(m.DemoRun)
            .where(m.DemoRun.instance_id == obj.id)
            .order_by(m.DemoRun.started_at.desc())
            .limit(10)
        )
    )
    ctl = db.get(m.DemoControl, 1)
    report = None
    from .investigations import get_investigation, investigation_detail

    for row in rows:
        if row.investigation_id:
            report = investigation_detail(actor, get_investigation(actor, row.investigation_id))
            break
    system = db.get(m.System, obj.system_id)
    return dict(
        enabled=cfg["enabled"],
        target=dict(display_name=obj.name, instance_id=obj.id, environment=system.environment),
        can_control=can_control,
        scenario="mysql_lock_wait",
        server_time=iso(now),
        limits={k: cfg[k] for k in DEFAULTS if k.startswith("max_") or k == "cooldown_seconds"},
        budget=public_budget(db, now, cfg),
        cooldown_until=iso(ctl.cooldown_until) if ctl else None,
        run=project_run(db, rows[0]) if rows else None,
        report=report,
        history=[project_run(db, r) for r in rows],
    )


class DemoController:
    """One dedicated loop, persisted injection claim before any transport I/O."""

    def __init__(self, factory, config, *, transport_factory=None, verify_idle=None):
        self.factory, self.config = factory, config
        self.cfg = settings(config)
        self.owner = str(uuid4())
        self.fence = None
        self.run_id = None
        self.transport = None
        self.negative_samples = 0
        self.stop_event = threading.Event()
        self.thread = None
        if transport_factory is None or verify_idle is None:
            from .demo_fault import FixedLabFault
            from .demo_fault import verify_idle as idle

            transport_factory = transport_factory or FixedLabFault
            verify_idle = verify_idle or idle
        self.transport_factory, self.verify_idle = transport_factory, verify_idle

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="demo-controller", daemon=True)
        self.thread.start()

    def _loop(self):
        try:
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception:
                    LOG.error(
                        "Demo controller dependency unavailable code=demo_dependency_unavailable"
                    )
                self.stop_event.wait(1)
        finally:
            self._close_transport()

    def _close_transport(self):
        if self.transport is not None:
            try:
                self.transport.stop()
            except Exception:
                LOG.error("Demo transport cleanup requires recovery code=demo_cleanup_unconfirmed")
            self.transport = None

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=20)
        self._close_transport()

    def _fenced(self, db):
        ctl = control(db)
        if (
            ctl.owner != self.owner
            or ctl.fence != self.fence
            or ctl.current_run_id != self.run_id
            or not ctl.lease_until
            or ctl.lease_until <= db_now(db)
        ):
            raise PlatformError(409, "demo_fenced", "演示控制租约已失效。")
        return ctl, db.get(m.DemoRun, self.run_id)

    def tick(self):
        action = None
        with self.factory.begin() as db:
            ctl = control(db)
            now = db_now(db)
            run = db.get(m.DemoRun, ctl.current_run_id) if ctl.current_run_id else None
            if not run or run.status not in ACTIVE:
                return
            mine = ctl.owner == self.owner and ctl.lease_until and ctl.lease_until > now
            if not mine:
                if ctl.lease_until and ctl.lease_until > now:
                    return
                ctl.fence += 1
                ctl.owner = self.owner
                ctl.lease_until = now + timedelta(seconds=30)
                self.fence, self.run_id = ctl.fence, run.id
                # Any already-claimed execution is recovery-only, never replayed.
                if run.status == "requested" and not run.stop_requested and self.cfg["enabled"]:
                    run.status, run.phase, run.fault_state = "injecting", "preparing", "unknown"
                    run.expires_at = now + timedelta(seconds=self.cfg["max_duration_seconds"])
                    ctl.blocked = True
                    action = "inject"
                else:
                    run.status, run.phase = "recovering", "cleanup"
                    run.cleanup_started_at = run.cleanup_started_at or now
                    action = "recover"
            else:
                self.fence, self.run_id = ctl.fence, run.id
                ctl.lease_until = now + timedelta(seconds=30)
                if run.status in ("awaiting_recovery", "recovery_unconfirmed"):
                    action = "observe"
                elif run.status in ("recovering", "cleanup_unknown"):
                    action = "recover"
                elif (
                    run.stop_requested
                    or not self.cfg["enabled"]
                    or not run.expires_at
                    or now >= run.expires_at
                ):
                    run.status, run.phase = "recovering", "cleanup"
                    run.cleanup_started_at = run.cleanup_started_at or now
                    action = "cleanup"
                elif run.status == "active":
                    action = "sample"
                else:
                    action = "recover"
        # No database transaction is held across remote I/O.
        try:
            if action == "inject":
                with self.factory.begin() as db:
                    _, run = self._fenced(db)
                    remaining = (run.expires_at - db_now(db)).total_seconds()
                    if run.stop_requested or remaining <= 0:
                        action = "cleanup"
                if action == "inject":
                    self.transport = self.transport_factory(
                        self.config, expires_in_seconds=remaining, expires_at=run.expires_at
                    )
                    result = self.transport.start()
                    with self.factory.begin() as db:
                        _, run = self._fenced(db)
                        now = db_now(db)
                        if result.get("active") is not True:
                            raise ValueError("unconfirmed")
                        run.status, run.phase, run.fault_state = (
                            "active",
                            "waiting_evidence",
                            "active",
                        )
                        run.fault_confirmed_at = now
                        self.negative_samples = 0
                        milestone(run, "fault_confirmed", "已确认实际锁等待", now)
                    return
            if action == "recover":
                with self.factory.begin() as db:
                    _, run = self._fenced(db)
                    if (
                        run.expires_at
                        and db_now(db) < run.expires_at + timedelta(seconds=5)
                        and self.transport is None
                    ):
                        return
            if action in ("cleanup", "recover"):
                result = (
                    self.transport.stop()
                    if self.transport is not None
                    else self.verify_idle(self.config)
                )
                self.transport = None
                with self.factory.begin() as db:
                    ctl, run = self._fenced(db)
                    now = db_now(db)
                    run.cleanup = {k: result.get(k) is True for k in CLEAN_KEYS}
                    run.cleanup_started_at = run.cleanup_started_at or now
                    if all(run.cleanup.values()):
                        run.stopped_at = now
                        run.fault_state = "cleared"
                        run.status, run.phase = "awaiting_recovery", "monitoring_recovery"
                        milestone(run, "direct_cleanup", "事务清理直接校验通过", now)
                        ctl.cooldown_until = now + timedelta(seconds=self.cfg["cooldown_seconds"])
                        ctl.blocked = False
                    else:
                        run.status, run.phase, run.fault_state, run.error_code = (
                            "cleanup_unknown",
                            "cleanup",
                            "unknown",
                            "demo_cleanup_unconfirmed",
                        )
                        ctl.blocked = True
            if action == "sample":
                result = self.transport.sample() if self.transport else {"active": False}
                if result.get("active") is not True:
                    with self.factory.begin() as db:
                        _, run = self._fenced(db)
                        self.negative_samples += 1
                        run.fault_state = "unknown"
                        if self.negative_samples >= 3:
                            run.stop_requested = True
                            run.error_code = "demo_fault_inactive"
                    return
                self.negative_samples = 0
                with self.factory.begin() as db:
                    _, run = self._fenced(db)
                    run.fault_state = "active"
            with self.factory.begin() as db:
                ctl, run = self._fenced(db)
                now = db_now(db)
                self._collect(db, run, now)
                if (
                    action == "sample"
                    and not run.investigation_id
                    and run.analysis_status == "not_started"
                ):
                    self._analyze(db, run, now)
                if run.status in ("awaiting_recovery", "recovery_unconfirmed") and proof(
                    db, run, self.cfg, positive=False
                ):
                    run.status, run.phase = "completed", "completed"
                    milestone(run, "monitoring_recovery", "Zabbix 新鲜空锁快照复验通过", now)
                    ctl.blocked = False
                    ctl.owner = None
                    ctl.lease_until = None
                elif run.status == "awaiting_recovery" and now > run.stopped_at + timedelta(
                    minutes=5
                ):
                    run.status, run.phase, run.error_code = (
                        "recovery_unconfirmed",
                        "monitoring_recovery",
                        "demo_monitoring_recovery_unconfirmed",
                    )
                    ctl.blocked = True
        except Exception:
            self._close_transport()
            try:
                with self.factory.begin() as db:
                    ctl, run = self._fenced(db)
                    run.status, run.phase, run.fault_state, run.error_code = (
                        "cleanup_unknown",
                        "cleanup",
                        "unknown",
                        "demo_controller_failed",
                    )
                    run.cleanup_started_at = run.cleanup_started_at or db_now(db)
                    ctl.blocked = True
            except Exception:
                LOG.error("Demo fenced status unavailable code=demo_status_unavailable")

    def _collect(self, db, run, now):
        if run.next_collection_at and now < run.next_collection_at:
            return
        pending = db.scalar(
            select(m.Task.id)
            .where(
                m.Task.instance_id == run.instance_id,
                m.Task.type == "collect_instance",
                m.Task.status.in_(["queued", "running", "retry_wait"]),
            )
            .limit(1)
        )
        if not pending:
            obj = db.get(m.Instance, run.instance_id)
            enqueue(
                db,
                "collect_instance",
                instance_id=run.instance_id,
                actor_id=self.cfg["runner_user_id"],
                payload={"config_revision": obj.config_revision, "collectors": ["monitoring"]},
                max_attempts=1,
            )
        run.next_collection_at = now + timedelta(seconds=10)

    def _analyze(self, db, run, now):
        evidence = proof(db, run, self.cfg, positive=True)
        if not evidence or evidence.observed_at >= now:
            return
        run.evidence_id, run.observed_at = evidence.id, evidence.observed_at
        milestone(run, "evidence", "已取得本轮 Zabbix 非空锁证据", now)
        # Caller holds the global control lock, so reservation and report linkage
        # commit atomically with the existing queue idempotency key.
        if budget(db, now)["ai_runs_used"] >= self.cfg["max_daily_ai_runs"]:
            run.analysis_status = "budget_exhausted"
            return
        from .investigations import create_investigation

        try:
            with db.begin_nested():
                runner = actor_for_user(db, self.cfg["runner_user_id"])
                report, _ = create_investigation(
                    runner,
                    [run.instance_id],
                    run.fault_confirmed_at - timedelta(minutes=5),
                    min(now, evidence.observed_at + timedelta(microseconds=1)),
                    QUESTION,
                    mode="live_redacted",
                    idempotency_key="demo:" + run.id,
                    options={
                        "max_tool_calls": 6,
                        "max_duration_seconds": 180,
                        "external_confirmed": True,
                        "evidence_ids": [evidence.id],
                        "demo": {"run_id": run.id, "evidence_id": evidence.id},
                    },
                )
                run.ai_reserved_at = now
                run.investigation_id = report.id
                run.analysis_status = "queued"
                run.phase = "analyzing"
                milestone(run, "analysis_queued", "已排队真实模型分析", now)
        except Exception:
            run.analysis_status = "failed"
            run.error_code = "demo_analysis_unavailable"
