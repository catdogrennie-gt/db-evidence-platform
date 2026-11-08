"""Deliberate live collection acceptance, restricted to synthetic lab 3307/3308.

This harness observes the real background worker. It never runs collection
handlers inline, changes collection intervals, or operates the control server.
Reports must be written outside the source repository.
"""

import argparse
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import lab
from sqlalchemy import select
from platform_app import models as m
from platform_app.common import iso, json_safe
from platform_app.config import load_config
from platform_app.db import create_db_engine, create_session_factory, utcnow
from platform_app.collectors.logs import read_slow_file


def save(path, value):
    path = Path(path).resolve()
    if path.is_relative_to(ROOT):
        raise ValueError("Private report must remain outside source repository")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(json_safe(value), ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def target(factory, instance_id):
    cfg = lab.config()
    if cfg["source_port"] != 3307 or cfg["restore_port"] != 3308:
        raise ValueError("Dedicated A/B lab ports required")
    with factory() as db:
        instance = db.get(m.Instance, instance_id)
        if (
            not instance
            or instance.port not in (3307, 3308)
            or not instance.enabled
            or instance.lifecycle_state != "Active"
        ):
            raise ValueError("Active synthetic A/B asset required")
        role = "source" if instance.port == 3307 else "restore"
        if instance.expected_server_uuid != cfg[role + "_uuid"]:
            raise ValueError("Registered UUID gate failed")
        return role, instance.expected_server_uuid, instance.port


def backend_rows(factory, instance_ids, start, automated_only=True):
    with factory() as db:
        query = select(m.Evidence, m.Task).join(
            m.CollectionRun, m.CollectionRun.id == m.Evidence.run_id
        )
        query = query.join(m.Task, m.Task.id == m.CollectionRun.task_id).where(
            m.Evidence.instance_id.in_(instance_ids),
            m.Evidence.observed_at >= start,
            m.Task.queue == "collection",
            m.Evidence.source.in_(["mysql.slow_log.file", "mysql.global_status", "linux.host"]),
        )
        if automated_only:
            query = query.where(m.Task.actor_id.is_(None))
        rows = db.execute(query.order_by(m.Evidence.observed_at)).all()
        return [
            {
                "evidence_id": e.id,
                "run_id": e.run_id,
                "task_id": t.id,
                "instance_id": e.instance_id,
                "source": e.source,
                "task_origin": "scheduled" if t.actor_id is None else "manual_background_task",
                "observed_at": iso(e.observed_at),
                "ingested_at": iso(e.ingested_at),
                "availability": e.coverage.get("availability"),
                "coverage": e.coverage,
                "events": e.payload.get("events", []),
            }
            for e, t in rows
        ]


def long_query(factory, args):
    role, expected, port = target(factory, args.instance_id)
    with factory() as db:
        schedule = db.scalar(
            select(m.CollectionSchedule).where(
                m.CollectionSchedule.instance_id == args.instance_id,
                m.CollectionSchedule.collector == "slow_log",
            )
        )
        if not schedule or not schedule.enabled or schedule.interval_seconds != 60:
            raise ValueError("Default enabled 60 second FILE schedule required")
        other = list(
            db.scalars(
                select(m.Instance.id).where(
                    m.Instance.port.in_([3307, 3308]), m.Instance.enabled.is_(True)
                )
            )
        )
    token = uuid4().hex
    alias = "late_" + token[:16]
    literal = "synthetic_secret_" + token
    report = {
        "kind": "real_background_worker_file_late_arrival",
        "instance_id": args.instance_id,
        "port": port,
        "default_interval_seconds": 60,
        "sleep_seconds": 165,
        "query_alias": alias,
        "started_at": iso(utcnow()),
        "query": {"status": "connecting"},
        "observations": [],
        "raw_literal_logged": False,
    }

    def run():
        with lab.connection("reader", role, read_timeout=195, database=None) as connection:
            identity = lab.query(
                connection,
                "SELECT @@server_uuid AS uuid,@@slow_query_log AS enabled,@@log_output AS output",
            )[0]
            if (
                identity["uuid"] != expected
                or not identity["enabled"]
                or "FILE" not in identity["output"]
            ):
                raise ValueError("Live UUID/FILE gate failed")
            report["query"]["started_at"] = iso(utcnow())
            report["query"]["status"] = "running"
            save(args.output, report)
            start = time.perf_counter()
            lab.query(
                connection, f"SELECT SLEEP(165) AS {alias}, %s AS synthetic_marker", (literal,)
            )
            report["query"].update(
                status="completed",
                finished_at=iso(utcnow()),
                elapsed_seconds=time.perf_counter() - start,
            )
            save(args.output, report)

    start = utcnow()
    seen = set()
    completed_at = None
    first_event_at = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        deadline = time.monotonic() + 350
        while time.monotonic() < deadline:
            rows = backend_rows(factory, other, start)
            for row in rows:
                if row["evidence_id"] in seen:
                    continue
                seen.add(row["evidence_id"])
                event_matches = [
                    event for event in row["events"] if alias in event.get("sql_shape", "")
                ]
                row["matching_events"] = event_matches
                row.pop("events")
                row["query_status_when_seen"] = report["query"]["status"]
                if event_matches and first_event_at is None:
                    first_event_at = utcnow()
                report["observations"].append(row)
            report["raw_literal_logged"] = literal in json.dumps(report["observations"])
            save(args.output, report)
            if future.done():
                future.result()
                if completed_at is None:
                    completed_at = utcnow()
                if first_event_at and utcnow() - first_event_at >= timedelta(seconds=70):
                    break
                if utcnow() - completed_at >= timedelta(seconds=140):
                    break
            time.sleep(5)
    during = [
        r
        for r in report["observations"]
        if r["instance_id"] == args.instance_id
        and r["source"] == "mysql.slow_log.file"
        and r["query_status_when_seen"] == "running"
    ]
    matches = [r for r in report["observations"] if r.get("matching_events")]
    with factory() as db:
        events = list(
            db.scalars(
                select(m.LogEvent).where(
                    m.LogEvent.instance_id == args.instance_id,
                    m.LogEvent.source == "mysql.slow_log.file",
                    m.LogEvent.message_redacted.contains(alias),
                )
            )
        )
        report["persisted_log_event_ids"] = [e.id for e in events]
    report["result"] = {
        "intermediate_file_sample_count": len(during),
        "intermediate_file_evidence_ids": [r["evidence_id"] for r in during],
        "late_event_evidence_ids": [r["evidence_id"] for r in matches],
        "matched_event_count": sum(len(r["matching_events"]) for r in matches),
        "persisted_unique_event_count": len(events),
        "passed": len(during) >= 2
        and len(matches) == 1
        and len(events) == 1
        and not report["raw_literal_logged"],
    }
    report["finished_at"] = iso(utcnow())
    save(args.output, report)
    return report["result"]


def copytruncate(factory, args):
    role, expected, port = target(factory, args.instance_id)
    with lab.connection("reader", role, database=None) as connection:
        if lab.query(connection, "SELECT @@server_uuid AS uuid")[0]["uuid"] != expected:
            raise ValueError("Live UUID gate failed")
    token = uuid4().hex
    path = f"/opt/tdx-lab/{port}/collection-copytruncate-{token}.log"
    backup = path + ".before-truncate"

    def record(alias, padding):
        stamp = iso(utcnow())
        return (
            f"# Time: {stamp}\n# User@Host: synthetic[synthetic] @ localhost [] Id: 0\n"
            "# Query_time: 1.000000 Lock_time: 0.000000 Rows_sent: 1 Rows_examined: 0\n"
            f"SELECT '{padding}' AS {alias};\n"
        ).encode()

    before = b"".join(record("copy_before_" + str(n), "synthetic_" + ("x" * 300)) for n in range(3))
    after = record("copy_after_" + token[:12], "synthetic_after")
    with lab.ssh() as ssh:
        with ssh.open_sftp() as sftp:
            with sftp.open(path, "wx") as stream:
                stream.write(before)
            sftp.chmod(path, 0o600)
            first = read_slow_file(sftp, path, [path], initial_mode="recent")
            old_stat = lab.remote(ssh, f"stat -c '%d %i' -- {path}").decode().strip()
            with sftp.open(backup, "wx") as stream:
                stream.write(before)
            sftp.chmod(backup, 0o600)
            # Only this newly created synthetic fixture is truncated. Every
            # original byte remains in its exclusive backup; no existing
            # configured MySQL slow.log is truncated or deleted.
            with sftp.open(path, "r+b") as stream:
                stream.truncate(0)
                stream.seek(0)
                stream.write(after)
                stream.flush()
            second = read_slow_file(sftp, path, [path], first.get("cursor"))
            new_stat = lab.remote(ssh, f"stat -c '%d %i' -- {path}").decode().strip()
            with sftp.open(backup, "rb") as stream:
                preserved = stream.read()
    report = {
        "kind": "real_sftp_inline_adapter_copytruncate_new_synthetic_fixture",
        "background_worker_validation": False,
        "instance_id": args.instance_id,
        "port": port,
        "fixture_path": path,
        "preserved_backup": backup,
        "preserved_sha256": hashlib.sha256(preserved).hexdigest(),
        "before_size": len(before),
        "after_size": len(after),
        "same_device_inode": old_stat == new_stat,
        "before_event_count": len(first["payload"].get("events", [])),
        "after_event_count": len(second["payload"].get("events", [])),
        "before_epoch": first.get("cursor", {}).get("source_epoch"),
        "after_epoch": second.get("cursor", {}).get("source_epoch"),
        "coverage": second["coverage"],
        "availability": second["availability"],
        "existing_mysql_log_modified": False,
        "recorded_at": iso(utcnow()),
    }
    report["passed"] = (
        preserved == before
        and old_stat == new_stat
        and "copytruncate" in second["coverage"].get("gaps", [])
        and report["before_epoch"] != report["after_epoch"]
        and report["after_event_count"] == 1
        and not second["coverage"]["complete"]
    )
    save(args.output, report)
    return report


def isolation(factory, args):
    """Pause only verified synthetic B; remote watchdog survives local failure."""
    from platform_app.scheduling import enqueue
    from platform_app.access import actor_for_user, authorize

    role, expected, port = target(factory, args.instance_id)
    if port != 3308:
        raise ValueError("Isolation fault is restricted to synthetic B port3308")
    with lab.connection("reader", role, database=None) as connection:
        identity = lab.query(
            connection, "SELECT @@server_uuid AS uuid,@@port AS port,@@datadir AS datadir"
        )[0]
        if (
            identity["uuid"] != expected
            or identity["port"] != 3308
            or identity["datadir"] != "/opt/tdx-lab/3308/data/"
        ):
            raise ValueError("Live UUID/port/datadir gate failed")
    with factory() as db:
        a = db.scalar(
            select(m.Instance).where(m.Instance.port == 3307, m.Instance.enabled.is_(True))
        )
        if not a:
            raise ValueError("Healthy comparison A required")
        a_id = a.id
    token = uuid4().hex
    script = f"/opt/tdx-lab/3308/collection-watchdog-{token}.sh"
    log = script + ".log"
    report = {
        "kind": "real_background_worker_single_instance_sigstop_isolation",
        "instance_id": args.instance_id,
        "comparison_instance_id": a_id,
        "port": 3308,
        "planned_pause_seconds": 100,
        "watchdog_restore_after_seconds": 120,
        "watchdog_script": script,
        "watchdog_log": log,
        "started_at": iso(utcnow()),
        "observations": [],
    }
    with lab.ssh() as ssh:
        with ssh.open_sftp() as sftp:
            with sftp.open("/opt/tdx-lab/3308/mysql.pid", "rb") as stream:
                pid_text = stream.read(32).decode().strip()
            if not re.fullmatch(r"[1-9][0-9]{1,8}", pid_text):
                raise ValueError("PID gate failed")
            pid = int(pid_text)
            with sftp.open(f"/proc/{pid}/cmdline", "rb") as stream:
                command = stream.read(4096)
            if (
                b"mysqld" not in command
                or b"--defaults-file=/opt/tdx-lab/3308/my.cnf" not in command
            ):
                raise ValueError("PID command gate failed")
            with sftp.open(f"/proc/{pid}/stat", "rb") as stream:
                stat = stream.read(4096).decode().split()
            start_ticks = stat[21]
            if not start_ticks.isdigit() or stat[2] in ("T", "t", "Z"):
                raise ValueError("Initial process state gate failed")
            watchdog = (
                f"#!/bin/sh\nset -eu\nsleep 120\n"
                f"test -r /proc/{pid}/stat || exit 0\n"
                f"actual=$(awk '{{print $22}}' /proc/{pid}/stat)\n"
                f"test \"$actual\" = '{start_ticks}' || exit 0\n"
                f"kill -CONT {pid}\nprintf 'watchdog_continued_verified_pid\\n'\n"
            )
            with sftp.open(script, "wx") as stream:
                stream.write(watchdog.encode())
            sftp.chmod(script, 0o700)
            watchdog_pid = (
                lab.remote(ssh, f"nohup /bin/sh {script} >{log} 2>&1 </dev/null & echo $!")
                .decode()
                .strip()
            )
            if not watchdog_pid.isdigit():
                raise ValueError("Watchdog launch failed")
            lab.remote(ssh, f"kill -0 {int(watchdog_pid)}")
            report.update(
                pid=pid,
                pid_start_ticks=start_ticks,
                initial_process_state=stat[2],
                watchdog_pid=int(watchdog_pid),
            )
            save(args.output, report)
        stopped = False
        try:
            lab.remote(ssh, f"kill -STOP {pid}")
            stopped = True
            report["stopped_at"] = iso(utcnow())
            save(args.output, report)
            started = utcnow()
            until = time.monotonic() + 100
            while time.monotonic() < until:
                report["observations"] = backend_rows(
                    factory, [args.instance_id, a_id], started, automated_only=False
                )
                for row in report["observations"]:
                    row.pop("events", None)
                save(args.output, report)
                time.sleep(5)
        finally:
            if stopped:
                try:
                    lab.remote(ssh, f"kill -CONT {pid}")
                except Exception:
                    # A second independent SSH attempt complements the remote
                    # watchdog, which remains armed if this process disappears.
                    with lab.ssh() as restore:
                        lab.remote(restore, f"kill -CONT {pid}")
                report["resumed_at"] = iso(utcnow())
                save(args.output, report)
    with lab.connection("reader", role, database=None) as connection:
        if lab.query(connection, "SELECT @@server_uuid AS uuid")[0]["uuid"] != expected:
            raise ValueError("Recovery UUID gate failed")
    # Request a normal background task after recovery; the private control
    # record labels its deliberate origin. No handlers execute in this process.
    with factory.begin() as db:
        user = db.scalar(select(m.User).where(m.User.username == "dba"))
        actor = actor_for_user(db, user.id)
        authorize(actor, "collection.trigger", [args.instance_id])
        asset = db.get(m.Instance, args.instance_id)
        task = enqueue(
            db,
            "collect_instance",
            actor_id=user.id,
            instance_id=asset.id,
            payload={
                "collectors": ["identity_status", "runtime"],
                "config_revision": asset.config_revision,
            },
        )
        report["recovery_task_id"] = task.id
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        report["observations"] = backend_rows(
            factory, [args.instance_id, a_id], started, automated_only=False
        )
        for row in report["observations"]:
            row.pop("events", None)
        with factory() as db:
            task = db.get(m.Task, report["recovery_task_id"])
            status = task.status
        if status in ("succeeded", "failed", "cancelled", "expired"):
            break
        time.sleep(5)
    during = [r for r in report["observations"] if r["observed_at"] < report["resumed_at"]]
    a_ok = [
        r["evidence_id"]
        for r in during
        if r["instance_id"] == a_id
        and r["source"] == "mysql.global_status"
        and r["coverage"].get("collector") == "identity_status"
        and r["availability"] == "available"
    ]
    b_fail = [
        r["evidence_id"]
        for r in during
        if r["instance_id"] == args.instance_id
        and r["source"] == "mysql.global_status"
        and r["coverage"].get("collector") == "identity_status"
        and r["availability"] not in ("available", "not_applicable")
    ]
    report["result"] = {
        "A_fresh_core_evidence_ids": a_ok,
        "B_failed_core_evidence_ids": b_fail,
        "recovery_task_status": status,
        "passed": len(a_ok) >= 2 and len(b_fail) >= 1 and status == "succeeded",
    }
    with lab.ssh() as ssh:
        with ssh.open_sftp() as sftp:
            with sftp.open(f"/proc/{pid}/stat", "rb") as stream:
                final_stat = stream.read(4096).decode().split()
            with sftp.open(log, "rb") as stream:
                watchdog_log = stream.read(1024).decode()
    report["final_process_state"] = final_stat[2]
    report["same_pid_start_ticks"] = final_stat[21] == start_ticks
    report["watchdog_executed_guarded_continue"] = "watchdog_continued_verified_pid" in watchdog_log
    report["result"]["passed"] = (
        report["result"]["passed"]
        and report["same_pid_start_ticks"]
        and final_stat[2] not in ("T", "t", "Z")
    )
    report["finished_at"] = iso(utcnow())
    save(args.output, report)
    return report["result"]


def rotation(factory, args):
    role, expected, port = target(factory, args.instance_id)
    if port != 3307:
        raise ValueError("Configured-log rotation is restricted to synthetic A port3307")
    with lab.connection("reader", role, database=None) as connection:
        settings = lab.query(
            connection,
            "SELECT @@server_uuid AS uuid,@@slow_query_log_file AS path,@@slow_query_log AS enabled,@@log_output AS output",
        )[0]
    path = "/opt/tdx-lab/3307/slow.log"
    if (
        settings["uuid"] != expected
        or settings["path"] != path
        or not settings["enabled"]
        or "FILE" not in settings["output"]
    ):
        raise ValueError("Live UUID/configured FILE gate failed")
    with factory() as db:
        cursor = db.scalar(
            select(m.LogCursor).where(
                m.LogCursor.instance_id == args.instance_id,
                m.LogCursor.source == "mysql.slow_log.file",
            )
        )
        if not cursor:
            raise ValueError("Existing background FILE cursor required")
        old_epoch = cursor.source_epoch
    token = uuid4().hex
    preserved = path + ".rotated-validation-" + token
    alias = "rotation_after_" + token[:12]
    start = utcnow()
    report = {
        "kind": "real_background_worker_configured_file_rotation",
        "instance_id": args.instance_id,
        "source_path": path,
        "preserved_rotated_file": preserved,
        "before_epoch": old_epoch,
        "started_at": iso(start),
        "existing_data_deleted": False,
        "observations": [],
    }
    with lab.ssh() as ssh:
        with ssh.open_sftp() as sftp:
            if sftp.stat(path).st_size > 4 * 1024 * 1024:
                raise ValueError("Bounded lab preservation check exceeded4MiB")
            with sftp.open(path, "rb") as stream:
                before = stream.read(4 * 1024 * 1024 + 1)
            sftp.rename(path, preserved)
            report["renamed_at"] = iso(utcnow())
            save(args.output, report)
            try:
                lab.remote(
                    ssh,
                    "/opt/tdx-lab/mysql/bin/mysql --defaults-extra-file=/opt/tdx-lab/3307/client.cnf --batch --skip-column-names -e 'FLUSH SLOW LOGS'",
                    timeout=15,
                )
            except Exception:
                # Restore the original pathname only if no new file was made;
                # never overwrite either file or discard log bytes.
                try:
                    sftp.stat(path)
                except FileNotFoundError:
                    sftp.rename(preserved, path)
                raise
            report["flushed_at"] = iso(utcnow())
            with sftp.open(preserved, "rb") as stream:
                old_bytes = stream.read(4 * 1024 * 1024 + 1)
            report.update(
                preserved_size=len(old_bytes),
                preserved_sha256=hashlib.sha256(old_bytes).hexdigest(),
                original_prefix_preserved=old_bytes[: len(before)] == before,
                new_file_size=sftp.stat(path).st_size,
            )
            save(args.output, report)
    with lab.connection("reader", role, database=None) as connection:
        lab.query(connection, f"SELECT SLEEP(0.2) AS {alias}")
        settings_after = lab.query(
            connection,
            "SELECT @@server_uuid AS uuid,@@slow_query_log_file AS path,@@slow_query_log AS enabled,@@log_output AS output",
        )[0]
    report["settings_unchanged"] = settings == settings_after
    deadline = time.monotonic() + 160
    matches = []
    while time.monotonic() < deadline:
        rows = backend_rows(factory, [args.instance_id], start)
        for row in rows:
            row["matching_events"] = [e for e in row["events"] if alias in e.get("sql_shape", "")]
            row.pop("events")
        report["observations"] = rows
        matches = [r for r in rows if r.get("matching_events")]
        save(args.output, report)
        if matches:
            break
        time.sleep(5)
    with factory() as db:
        cursor = db.scalar(
            select(m.LogCursor).where(
                m.LogCursor.instance_id == args.instance_id,
                m.LogCursor.source == "mysql.slow_log.file",
            )
        )
        report["after_epoch"] = cursor.source_epoch
    gaps = [
        r
        for r in report["observations"]
        if r["source"] == "mysql.slow_log.file"
        and any(
            g in r["coverage"].get("gaps", [])
            for g in (
                "copytruncate",
                "file_replaced_or_prefix_changed",
                "rotation_old_file_not_reconciled",
            )
        )
    ]
    report["result"] = {
        "gap_evidence_ids": [r["evidence_id"] for r in gaps],
        "new_event_evidence_ids": [r["evidence_id"] for r in matches],
        "passed": report["original_prefix_preserved"]
        and report["settings_unchanged"]
        and old_epoch != report["after_epoch"]
        and bool(matches)
        and bool(gaps),
    }
    report["finished_at"] = iso(utcnow())
    save(args.output, report)
    return report["result"]


def lock_observations(factory, instance_id, start):
    with factory() as db:
        rows = db.execute(
            select(m.Evidence, m.Task)
            .join(m.CollectionRun, m.CollectionRun.id == m.Evidence.run_id)
            .join(m.Task, m.Task.id == m.CollectionRun.task_id)
            .where(
                m.Evidence.instance_id == instance_id,
                m.Evidence.source == "mysql.lock_waits",
                m.Evidence.observed_at >= start,
                m.Task.queue == "collection",
            )
            .order_by(m.Evidence.observed_at)
        ).all()
        return [
            {
                "evidence_id": e.id,
                "run_id": e.run_id,
                "task_id": t.id,
                "observed_at": iso(e.observed_at),
                "availability": e.coverage.get("availability"),
                "coverage": e.coverage,
                "rows": e.payload.get("rows", []),
            }
            for e, t in rows
        ]


def locks(factory, args):
    """Existing synthetic row, no row changes, watchdog closes both sessions."""
    role, expected, port = target(factory, args.instance_id)
    if port != 3307:
        raise ValueError("Lock validation is restricted to synthetic A port3307")
    if not re.fullmatch(r"[a-f0-9-]{36}", expected):
        raise ValueError("UUID format gate failed")
    hold = int(args.hold_seconds)
    guard = hold + 20
    control = Path(args.control_file).resolve() if args.control_file else None
    if control and control.is_relative_to(ROOT):
        raise ValueError("Lock control file must remain private outside repository")
    if control and not control.exists():
        save(control, {"ineffective_select_1": False, "release": False})
    token = uuid4().hex
    start = utcnow()
    watch_path = f"/opt/tdx-lab/3307/collection-lock-watchdog-{token}.sh"
    report = {
        "kind": "real_background_worker_existing_synthetic_row_lock",
        "instance_id": args.instance_id,
        "port": 3307,
        "source_mode": "live_controlled_synthetic",
        "hold_seconds": hold,
        "remote_rollback_seconds": guard,
        "row_data_changes": False,
        "started_at": iso(start),
        "before_window": {"from": iso(start - timedelta(seconds=150)), "to": iso(start)},
        "watchdog_script": watch_path,
        "private_control_file": str(control) if control else None,
        "observations": [],
        "event_ids": [],
    }
    with ExitStack() as stack:
        blocker = stack.enter_context(
            lab.connection("operator", role, read_timeout=20, database=None)
        )
        waiter = stack.enter_context(
            lab.connection("operator", role, read_timeout=hold + 40, database=None)
        )
        observer = stack.enter_context(
            lab.connection("reader", role, read_timeout=10, database=None)
        )
        connections = []
        for connection in (blocker, waiter, observer):
            row = lab.query(
                connection,
                "SELECT @@server_uuid AS uuid,@@port AS port,CONNECTION_ID() AS connection_id",
            )[0]
            if row["uuid"] != expected or row["port"] != 3307:
                raise ValueError("Live UUID/port gate failed")
            connections.append(int(row["connection_id"]))
        for connection in (blocker, waiter):
            lab.query(connection, "SET @tdx_validation_token=%s", (token,))
        before = lab.query(observer, "SELECT id,balance FROM evidence_lab.accounts WHERE id=1")
        if len(before) != 1:
            raise ValueError("Existing synthetic accounts row required; no DDL is permitted")
        lab.query(blocker, "SET SESSION innodb_lock_wait_timeout=5")
        lab.query(waiter, "SET SESSION innodb_lock_wait_timeout=%s", (hold + 40,))
        ssh = stack.enter_context(lab.ssh())
        lab.remote(ssh, "test -x /usr/bin/timeout")
        mysql = "/opt/tdx-lab/mysql/bin/mysql --defaults-extra-file=/opt/tdx-lab/3307/client.cnf --batch --skip-column-names --connect-timeout=2"
        owned = (
            "SELECT COUNT(*) FROM performance_schema.threads t JOIN performance_schema.user_variables_by_thread v "
            "ON v.THREAD_ID=t.THREAD_ID WHERE t.PROCESSLIST_ID=$cid AND t.PROCESSLIST_USER='lab_operator' "
            f"AND v.VARIABLE_NAME='tdx_validation_token' AND CONVERT(v.VARIABLE_VALUE USING utf8mb4)='{token}'"
        )
        watcher = (
            f"#!/bin/sh\nset -eu\nsleep {guard}\nmysql='{mysql}'\n"
            "actual=$(/usr/bin/timeout 3 $mysql -e 'SELECT @@server_uuid')\n"
            f"test \"$actual\" = '{expected}' || exit 0\n"
            f"for cid in {connections[0]} {connections[1]}; do\n"
            f' owned=$(/usr/bin/timeout 3 $mysql -e "{owned}")\n'
            ' if test "$owned" = 1; then /usr/bin/timeout 3 $mysql -e "KILL CONNECTION $cid"; fi\n'
            "done\nprintf 'guarded_lab_transaction_connections_closed\\n'\n"
        )
        # Prove the watchdog's ownership predicate now, before any row is locked.
        check = owned.replace("$cid", str(connections[0]))
        if lab.remote(ssh, f'/usr/bin/timeout 3 {mysql} -e "{check}"').decode().strip() != "1":
            raise ValueError("Remote transaction watchdog ownership predicate failed")
        with ssh.open_sftp() as sftp:
            with sftp.open(watch_path, "wx") as stream:
                stream.write(watcher.encode())
            sftp.chmod(watch_path, 0o700)
        watchdog_pid = (
            lab.remote(
                ssh, f"nohup /bin/sh {watch_path} >{watch_path}.log 2>&1 </dev/null & echo $!"
            )
            .decode()
            .strip()
        )
        if not watchdog_pid.isdigit():
            raise ValueError("Remote rollback watchdog failed")
        lab.remote(ssh, f"kill -0 {int(watchdog_pid)}")
        report.update(
            blocker_connection_id=connections[0],
            waiter_connection_id=connections[1],
            watchdog_pid=int(watchdog_pid),
            watchdog_ownership_predicate_verified=True,
            watchdog_armed_at=iso(utcnow()),
        )
        save(args.output, report)
        future = None
        pool = ThreadPoolExecutor(max_workers=1)

        def waiting():
            try:
                lab.query(waiter, "START TRANSACTION")
                lab.query(waiter, "SELECT id FROM evidence_lab.accounts WHERE id=1 FOR UPDATE")
                report["waiter_unblocked_at"] = iso(utcnow())
            finally:
                try:
                    lab.query(waiter, "ROLLBACK")
                except Exception:
                    pass

        try:
            lab.query(blocker, "START TRANSACTION")
            lab.query(blocker, "SELECT id FROM evidence_lab.accounts WHERE id=1 FOR UPDATE")
            report["blocker_held_at"] = iso(utcnow())
            future = pool.submit(waiting)
            chain = []
            chain_deadline = time.monotonic() + 10
            sql = (
                "SELECT w.REQUESTING_ENGINE_TRANSACTION_ID AS waiting_trx_id,w.BLOCKING_ENGINE_TRANSACTION_ID AS blocking_trx_id "
                "FROM performance_schema.data_lock_waits w JOIN performance_schema.threads b ON b.THREAD_ID=w.BLOCKING_THREAD_ID "
                "JOIN performance_schema.threads r ON r.THREAD_ID=w.REQUESTING_THREAD_ID WHERE b.PROCESSLIST_ID=%s AND r.PROCESSLIST_ID=%s"
            )
            while not chain and time.monotonic() < chain_deadline:
                chain = lab.query(observer, sql, (connections[0], connections[1]))
                if not chain:
                    time.sleep(0.25)
            if not chain:
                raise ValueError("Live lock chain was not established")
            report.update(
                blocked_at=iso(utcnow()),
                blocking_trx_id=str(chain[0]["blocking_trx_id"]),
                waiting_trx_id=str(chain[0]["waiting_trx_id"]),
            )
            report["blocked_window"] = {"from": report["blocked_at"], "to": None}
            print(
                json.dumps(
                    {
                        "phase": "blocked",
                        "blocked_at": report["blocked_at"],
                        "blocking_trx_id": report["blocking_trx_id"],
                        "planned_release_seconds": hold,
                    },
                    ensure_ascii=True,
                ),
                flush=True,
            )
            until = time.monotonic() + hold
            while time.monotonic() < until:
                report["observations"] = lock_observations(
                    factory, args.instance_id, start - timedelta(seconds=150)
                )
                with factory() as db:
                    episodes = list(
                        db.scalars(
                            select(m.AlertOccurrence)
                            .join(
                                m.ActiveAlert, m.ActiveAlert.id == m.AlertOccurrence.active_alert_id
                            )
                            .where(
                                m.AlertOccurrence.instance_id == args.instance_id,
                                m.AlertOccurrence.first_seen_at >= start,
                                m.ActiveAlert.rule_version == "locks_present:v1",
                            )
                        )
                    )
                    episode_ids = [e.id for e in episodes]
                    report["episode_ids"] = episode_ids
                    report["event_ids"] = (
                        list(
                            db.scalars(
                                select(m.EventAlert.event_id).where(
                                    m.EventAlert.alert_occurrence_id.in_(episode_ids)
                                )
                            )
                        )
                        if episode_ids
                        else []
                    )
                if control and control.exists():
                    try:
                        request = json.loads(control.read_text(encoding="utf-8-sig"))
                    except (OSError, ValueError):
                        request = {}
                    if request.get("ineffective_select_1") is True and not report.get(
                        "ineffective_diagnostic"
                    ):
                        with factory() as db:
                            plan = db.get(m.RemediationPlan, request.get("plan_id"))
                            approved = bool(
                                plan
                                and plan.status == "approved"
                                and plan.event_id in report["event_ids"]
                            )
                        if approved:
                            before_chain = lab.query(
                                observer, sql, (connections[0], connections[1])
                            )
                            diagnostic_start = iso(utcnow())
                            result = lab.query(observer, "SELECT 1 AS diagnostic_ok")
                            diagnostic_finish = iso(utcnow())
                            after_chain = lab.query(observer, sql, (connections[0], connections[1]))
                            report["ineffective_diagnostic"] = {
                                "source": "deliberate_private_cli_operator_request",
                                "fixed_template": "SELECT 1",
                                "plan_id": request["plan_id"],
                                "started_at": diagnostic_start,
                                "finished_at": diagnostic_finish,
                                "result": result,
                                "lock_chain_present_before": bool(before_chain),
                                "lock_chain_present_after": bool(after_chain),
                            }
                            report["ineffective_action"] = {
                                "executed_at": diagnostic_finish,
                                "plan_id": request["plan_id"],
                                "operation": "SELECT 1",
                                "blocking_trx_id": report["blocking_trx_id"],
                                "lock_still_present": bool(after_chain),
                            }
                            print(
                                json.dumps(
                                    {
                                        "phase": "ineffective_select_1_completed",
                                        **report["ineffective_diagnostic"],
                                    },
                                    ensure_ascii=True,
                                ),
                                flush=True,
                            )
                        else:
                            report["control_request_rejected"] = (
                                "approved_current_event_plan_required"
                            )
                    if request.get("release") is True:
                        report["release_reason"] = "explicit_private_operator_release_flag"
                        report["release_requested_at"] = iso(utcnow())
                        save(args.output, report)
                        break
                save(args.output, report)
                if future.done():
                    future.result()
                    raise ValueError("Waiter ended before planned release")
                time.sleep(5)
            if not report.get("release_reason"):
                report["release_reason"] = "fixed_maximum_hold_elapsed"
        finally:
            try:
                lab.query(blocker, "ROLLBACK")
                report["released_at"] = iso(utcnow())
            except Exception:
                report["release_fallback"] = "remote_guarded_watchdog_or_connection_close"
            if report.get("blocked_window"):
                report["blocked_window"]["to"] = report.get("released_at", iso(utcnow()))
            if future:
                try:
                    future.result(timeout=30)
                except Exception as exc:
                    report["waiter_completion_error"] = type(exc).__name__
            pool.shutdown(wait=True)
            save(args.output, report)
        after = lab.query(observer, "SELECT id,balance FROM evidence_lab.accounts WHERE id=1")
        report["row_data_unchanged"] = before == after
    # Observe recovery through at least three actual runtime samples.
    report["after_window"] = {"from": report.get("released_at"), "to": None}
    until = time.monotonic() + 140
    while time.monotonic() < until:
        report["observations"] = lock_observations(
            factory, args.instance_id, start - timedelta(seconds=150)
        )
        recovered = [
            r
            for r in report["observations"]
            if r["observed_at"] > report["released_at"]
            and r["availability"] == "available"
            and not r["rows"]
        ]
        save(args.output, report)
        if len(recovered) >= 3:
            break
        time.sleep(5)
    report["after_window"]["to"] = iso(utcnow())
    report["finished_at"] = iso(utcnow())
    blocked = [
        r
        for r in report["observations"]
        if any(str(v.get("blocking_trx_id")) == report["blocking_trx_id"] for v in r["rows"])
    ]
    report["result"] = {
        "blocked_evidence_ids": [r["evidence_id"] for r in blocked],
        "recovered_evidence_ids": [r["evidence_id"] for r in recovered],
        "passed": len(blocked) >= 3
        and len(recovered) >= 3
        and report["row_data_unchanged"]
        and bool(report["event_ids"]),
    }
    save(args.output, report)
    return report["result"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument(
        "--hold-seconds",
        type=int,
        choices=[250, 400],
        default=250,
        help="Only the explicitly authorized bounded lock windows",
    )
    parser.add_argument(
        "--control-file",
        help="Private fixed-action operator signal file for strict lock acceptance",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "phase", choices=["long-query", "copytruncate", "isolation", "rotation", "locks"]
    )
    args = parser.parse_args()
    if Path(args.output).resolve().is_relative_to(ROOT):
        parser.error("Private report must remain outside source repository")
    engine = create_db_engine(load_config(args.config))
    factory = create_session_factory(engine)
    try:
        print(
            json.dumps(
                {
                    "long-query": long_query,
                    "copytruncate": copytruncate,
                    "isolation": isolation,
                    "rotation": rotation,
                    "locks": locks,
                }[args.phase](factory, args),
                ensure_ascii=True,
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
