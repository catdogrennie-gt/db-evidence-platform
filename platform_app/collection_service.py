"""Deterministic collection handlers with fenced, per-owner persistence.

Only the worker owns queue concurrency. No target writes or configurable SQL or
shell expressions cross this module's boundary.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import timedelta
import posixpath
import time
from uuid import uuid4

from sqlalchemy import select

from .access import actor_for_user, authorize, authorize_system, scoped_instance
from .audit import append_audit
from .common import PlatformError, json_safe
from .connectors import ConnectorError, open_mysql, open_ssh, remaining, safe_error_code
from .collectors.mysql import collect_mysql, probe_mysql, BOOT_ID, STATUS_SQL, STATUS_FIELDS
from .collectors.host import collect_host
from .collectors.logs import result, read_slow_file
from .evidence import append_batch, previous_frame_dict
from .events import evaluate
from .inventory import COLLECTORS, connection_fingerprint
from .metrics_derivation import derive, derive_digest_window
from .models import (
    Instance,
    ConnectionTest,
    CollectionRun,
    CollectionSchedule,
    MetricFrame,
    LogCursor,
    Evidence,
)
from .network_policy import validate_target, resolve_ssh_profile
from .scheduling import check_fence, complete, db_now, next_collection_due
from .secrets import SecretStore

SOURCES = {
    "identity_status": ["mysql.identity", "mysql.global_status"],
    "runtime": ["mysql.identity", "mysql.transactions", "mysql.lock_waits", "mysql.sessions"],
    "statement_digest": ["mysql.identity", "mysql.statement_digest"],
    "schema_config": [
        "mysql.identity",
        "mysql.tables",
        "mysql.columns",
        "mysql.indexes",
        "mysql.configuration",
    ],
    "replication": ["mysql.identity", "mysql.replication"],
    "error_log": ["mysql.identity", "mysql.error_log"],
    "slow_log": ["mysql.slow_log.file"],
    "host": ["linux.host"],
}
GOOD = {"available", "not_applicable"}


def _code(exc):
    if isinstance(exc, PlatformError):
        return exc.detail.get("code", "authorization_failed")
    return safe_error_code(exc)


def _private_profile(config, name, ref):
    value = next((p for p in getattr(config, name, []) if p.get("id") == ref), None)
    if not value:
        raise ConnectorError(name.rstrip("s") + "_not_found")
    return dict(value)


def _ssh(config, ref):
    profile = resolve_ssh_profile(config, ref)
    profile.setdefault("known_hosts", str(config.known_hosts_path))
    if profile.get("credential_ref"):
        value = SecretStore(config).resolve(profile["credential_ref"], purpose="ssh").value
        if not isinstance(value, dict):
            raise ConnectorError("ssh_credentials_invalid")
        profile.update(
            {k: v for k, v in value.items() if k in ("username", "password", "key_filename")}
        )
    return profile


def _target(config, connection, expected=None):
    profile = validate_target(config, connection)
    ref = profile.get("tls_profile_ref")
    if ref:
        tls = _private_profile(config, "tls_profiles", ref)
        # TLS identity overrides are trusted private profile data only;
        # validate_target has already discarded browser-supplied TLS names.
        if tls.get("server_name"):
            from .network_policy import _host

            profile["tls_server_name"] = _host(tls["server_name"])
        for key, aliases in {
            "tls_ca": ("tls_ca", "ca_file", "ca"),
            "tls_cert": ("tls_cert", "cert_file", "cert"),
            "tls_key": ("tls_key", "key_file", "key"),
        }.items():
            for alias in aliases:
                if tls.get(alias):
                    profile[key] = tls[alias]
                    break
    if expected:
        profile["expected_server_uuid"] = expected
    return profile


def _deadline(db, lease):
    budget = min(60.0, (lease.deadline_at - db_now(db)).total_seconds())
    if budget <= 0:
        raise ConnectorError("deadline_exceeded")
    return time.monotonic() + budget


def _preflight_authority(db, lease, test):
    check_fence(db, lease, allow_inactive=True)
    actor = actor_for_user(db, lease.actor_id)
    if (
        test.created_by != actor.user_id
        or test.task_id != lease.id
        or test.expires_at <= db_now(db)
    ):
        raise PlatformError(409, "connection_test_binding_invalid", "预检绑定已失效。")
    instance_id = lease.payload.get("instance_id")
    if instance_id:
        instance = scoped_instance(actor, instance_id, "asset.manage")
        if instance.config_revision != test.config_revision or instance.system_id != test.system_id:
            raise PlatformError(409, "configuration_changed", "实例配置已改变。")
    else:
        authorize_system(actor, test.system_id, "asset.onboard")
    expected = connection_fingerprint(
        test.system_id,
        test.config,
        test.secret_ref,
        test.secret_version,
        test.config_revision,
        instance_id,
    )
    if expected != test.config_fingerprint or lease.payload.get("fingerprint") != expected:
        raise PlatformError(409, "connection_test_binding_invalid", "预检配置绑定已改变。")
    return actor


def _source_precheck(config, safe, deadline, capabilities, steps):
    with ExitStack() as stack:
        clients = {}

        def client(ref):
            if ref not in clients:
                clients[ref] = stack.enter_context(open_ssh(_ssh(config, ref), deadline))
            return clients[ref]

        for kind, key, profiles in [
            ("host", "host_profile_ref", "host_profiles"),
            ("slow_log", "log_profile_ref", "log_profiles"),
        ]:
            if not safe.get(key):
                continue
            try:
                profile = _private_profile(config, profiles, safe[key])
                ssh = client(profile["ssh_profile_ref"])
                with ssh.open_sftp() as sftp:
                    sftp.get_channel().settimeout(remaining(deadline, 3))
                    if kind == "host":
                        values = {}
                        for path, label in [
                            ("/proc/sys/kernel/random/boot_id", "boot_id"),
                            ("/etc/hostname", "hostname"),
                        ]:
                            with sftp.open(path, "rb") as stream:
                                values[label] = (
                                    stream.read(257).decode("utf-8", errors="replace").strip()
                                )
                        expected = profile.get("expected_host_identity")
                        if expected and expected not in (values["boot_id"], values["hostname"]):
                            raise ConnectorError("host_identity_mismatch")
                    else:
                        canonical = sftp.normalize(profile["slow_log_path"])
                        allowed = [posixpath.normpath(p) for p in profile.get("allowed_paths", [])]
                        if canonical not in allowed:
                            raise ConnectorError("log_path_not_allowed")
                        sftp.stat(canonical)
                        with sftp.open(canonical, "rb") as stream:
                            stream.read(1)
                capabilities.append(
                    {
                        "collector": kind,
                        "availability": "available",
                        "reason_code": "preflight_access_only",
                        "complete": False,
                    }
                )
                steps.append({"step": kind, "status": "succeeded"})
            except Exception as exc:
                code = _code(exc)
                capabilities.append(
                    {"collector": kind, "availability": "unavailable", "reason_code": code}
                )
                steps.append({"step": kind, "status": "failed", "error_code": code})


def _probe_capabilities(connection, requested, probe, deadline):
    """Privilege probes return no business rows and do not claim full coverage."""
    checks = {
        "identity_status": [(STATUS_SQL, STATUS_FIELDS)],
        "runtime": [
            ("SELECT TRX_ID FROM information_schema.innodb_trx LIMIT 0", None),
            ("SELECT REQUESTING_THREAD_ID FROM performance_schema.data_lock_waits LIMIT 0", None),
            ("SELECT THREAD_ID FROM performance_schema.threads LIMIT 0", None),
        ],
        "statement_digest": [
            (
                "SELECT DIGEST,COUNT_STAR FROM performance_schema.events_statements_summary_by_digest LIMIT 0",
                None,
            )
        ],
        "schema_config": [
            ("SELECT TABLE_SCHEMA,TABLE_NAME FROM information_schema.tables LIMIT 0", None),
            (
                "SELECT TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME FROM information_schema.columns LIMIT 0",
                None,
            ),
            (
                "SELECT TABLE_SCHEMA,TABLE_NAME,INDEX_NAME FROM information_schema.statistics LIMIT 0",
                None,
            ),
            ("SELECT VARIABLE_NAME FROM performance_schema.global_variables LIMIT 0", None),
        ],
        "replication": [("SHOW REPLICA STATUS", None)],
        "error_log": [("SELECT LOGGED FROM performance_schema.error_log LIMIT 0", None)],
    }
    capabilities = []
    ps_enabled = str(probe.get("performance_schema_enabled", 1)).upper() not in (
        "0",
        "OFF",
        "FALSE",
    )
    for name in requested:
        if name not in checks:
            continue
        try:
            if not ps_enabled and name in ("runtime", "statement_digest", "error_log"):
                raise ConnectorError("performance_schema_disabled")
            rows = []
            for sql, params in checks[name]:
                if hasattr(connection, "_read_timeout"):
                    connection._read_timeout = remaining(deadline, 2)
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
                    rows = cursor.fetchall()
            capabilities.append(
                {
                    "collector": name,
                    "availability": "not_applicable"
                    if name == "replication" and not rows
                    else "available",
                    "reason_code": "replication_not_configured"
                    if name == "replication" and not rows
                    else "preflight_access_only",
                    "complete": False,
                }
            )
        except Exception as exc:
            capabilities.append(
                {
                    "collector": name,
                    "availability": "not_configured"
                    if _code(exc) == "performance_schema_disabled"
                    else "unavailable",
                    "reason_code": _code(exc),
                }
            )
    return capabilities


def handle_connection_test(session_factory, lease, config):
    with session_factory.begin() as db:
        test = db.get(ConnectionTest, lease.payload.get("test_id"))
        if not test:
            raise PlatformError(404, "not_found", "预检记录不存在。")
        _preflight_authority(db, lease, test)
        deadline = _deadline(db, lease)
        test.status = "running"
        safe = dict(test.config)
        ref = test.secret_ref
        expected = None
        if lease.payload.get("instance_id"):
            expected = db.get(Instance, lease.payload["instance_id"]).expected_server_uuid
    steps = []
    capabilities = []
    probe = {}
    error = None
    try:
        target = _target(config, safe["connection"], expected)
        secret = SecretStore(config).resolve(ref)
        with open_mysql(
            target, secret, deadline, ssh_profile_resolver=lambda r: _ssh(config, r)
        ) as connection:
            probe = probe_mysql(connection, deadline=deadline)
            if expected and probe.get("server_uuid") != expected:
                raise ConnectorError("identity_mismatch")
            if not str(probe.get("version", "")).startswith("8."):
                raise ConnectorError("unsupported_mysql_version")
            steps.append({"step": "database_identity", "status": "succeeded"})
            capabilities = _probe_capabilities(
                connection, safe.get("requested_collectors", []), probe, deadline
            )
    except Exception as exc:
        error = _code(exc)
        steps.append({"step": "database_identity", "status": "failed", "error_code": error})
    _source_precheck(config, safe, deadline, capabilities, steps)
    requested = safe.get("requested_collectors", [])
    for name in requested:
        if not any(c["collector"] == name for c in capabilities):
            capabilities.append(
                {
                    "collector": name,
                    "availability": "unknown",
                    "reason_code": "source_profile_not_configured"
                    if name in ("host", "slow_log")
                    else "database_preflight_failed",
                }
            )
    response = {
        "test_id": lease.payload["test_id"],
        "server_uuid": probe.get("server_uuid"),
        "server_version": probe.get("version"),
        "steps": steps,
        "capabilities": capabilities,
        "clock_offset_seconds": probe.get("clock_offset_seconds"),
        "clock_risk": probe.get("clock_risk"),
        "error_code": error,
        "preflight_only": True,
    }
    with session_factory.begin() as db:
        test = db.get(ConnectionTest, lease.payload["test_id"])
        actor = _preflight_authority(db, lease, test)
        test.status = "failed" if error else "succeeded"
        test.results = response
        test.server_uuid = probe.get("server_uuid") if not error else None
        test.source_version = probe.get("version")
        append_audit(
            db,
            actor.user_id,
            "connection.test.complete",
            "connection_test",
            test.id,
            result="failed" if error else "success",
            details={"error_code": error},
        )
        complete(
            db,
            lease,
            result=response,
            result_ref=test.id,
            status=test.status,
            error_code=error,
            allow_inactive=True,
        )
    return response


def _collection_authority(db, lease, revision, activation):
    check_fence(db, lease, config_revision=revision, allow_inactive=activation)
    instance = db.get(Instance, lease.instance_id)
    if activation and (instance.lifecycle_state != "Activating" or instance.enabled):
        raise PlatformError(409, "activation_state_changed", "资产激活状态已改变。")
    if activation and not lease.actor_id:
        raise PlatformError(403, "activation_actor_required", "激活需要当前授权的操作人。")
    if lease.actor_id:
        actor = actor_for_user(db, lease.actor_id)
        authorize(actor, "asset.manage" if activation else "collection.trigger", [instance.id])
    return instance


def _context(db, instance, name, deadline):
    schedule = db.scalar(
        select(CollectionSchedule).where(
            CollectionSchedule.instance_id == instance.id, CollectionSchedule.collector == name
        )
    )
    interval = schedule.interval_seconds if schedule else COLLECTORS[name]
    previous = db.scalar(
        select(MetricFrame)
        .where(MetricFrame.instance_id == instance.id, MetricFrame.collector == name)
        .order_by(MetricFrame.observed_at.desc())
        .limit(1)
    )
    gap = False
    if previous:
        gap = (
            db.scalar(
                select(CollectionRun.id)
                .where(
                    CollectionRun.instance_id == instance.id,
                    CollectionRun.collector == name,
                    CollectionRun.status.in_(["failed", "partial"]),
                    CollectionRun.started_at > previous.observed_at,
                )
                .limit(1)
            )
            is not None
        )
        previous_run = db.get(CollectionRun, previous.run_id)
        gap = gap or (previous_run is not None and previous_run.status == "failed")
        frame_source = {
            "identity_status": "mysql.global_status",
            "runtime": "mysql.lock_waits",
            "statement_digest": "mysql.statement_digest",
            "host": "linux.host",
        }.get(name)
        if frame_source:
            source = db.scalar(
                select(Evidence).where(
                    Evidence.run_id == previous.run_id, Evidence.source == frame_source
                )
            )
            gap = (
                gap or source is None or (source.coverage or {}).get("availability") != "available"
            )
    context = {
        "instance_id": instance.id,
        "expected_server_uuid": instance.expected_server_uuid,
        "server_uuid": instance.expected_server_uuid,
        "selected_schemas": instance.selected_schemas or [],
        "deadline": deadline,
        "collector_boot_id": BOOT_ID,
        "counter_epoch": previous.counter_epoch if previous else 0,
        "previous_frame": previous_frame_dict(previous, failed_since_previous=gap),
        "interval_seconds": interval,
    }
    if name in ("error_log", "slow_log"):
        anchor = db.scalar(
            select(MetricFrame)
            .where(
                MetricFrame.instance_id == instance.id, MetricFrame.collector == "identity_status"
            )
            .order_by(MetricFrame.observed_at.desc())
            .limit(1)
        )
        if anchor:
            context["identity_epoch_anchor"] = previous_frame_dict(anchor)
    for source, key in [
        ("mysql.error_log", "error_log_cursor"),
        ("mysql.slow_log.table", "slow_log_cursor"),
        ("mysql.slow_log.file", "slow_file_cursor"),
    ]:
        cursor = db.scalar(
            select(LogCursor).where(
                LogCursor.instance_id == instance.id, LogCursor.source == source
            )
        )
        if cursor:
            context[key] = dict(cursor.state)
    return context


def _failed_batch(name, expected, code):
    items = [
        result(
            source,
            availability="unreachable" if code == "unreachable" else "unavailable",
            error_code=code,
            coverage={"complete": False, "gaps": [code]},
        )
        for source in SOURCES[name]
    ]
    return {
        "observed_at": items[0]["observed_at"],
        "server_uuid": expected,
        "evidence_items": items,
        "capability_results": [
            {"collector": source, "availability": items[i]["availability"], "error_code": code}
            for i, source in enumerate(SOURCES[name])
        ],
    }


def _batch_status(batch):
    items = batch.get("evidence_items", [])
    # Repeated identity/status do not make the profile's real source successful.
    owned = [i for i in items if i["source"] not in ("mysql.identity", "mysql.global_status")]
    if not owned:
        owned = items
    good = sum(i.get("availability") in GOOD for i in owned)
    return "succeeded" if owned and good == len(owned) else "partial" if good else "failed"


def _core_ok(batch):
    return all(
        any(
            i["source"] == source
            and i.get("availability") == "available"
            and i.get("payload", {}).get("rows")
            for i in batch.get("evidence_items", [])
        )
        for source in ("mysql.identity", "mysql.global_status")
    )


def _derive_epoch(batch, context, name):
    frame = batch.get("metric_frame")
    previous = context.get("previous_frame")
    if not frame:
        return
    # ORM source_epoch is a string; compare the same representation on both
    # sides or every persisted DB frame would falsely look like a restart.
    if name != "host":
        frame["source_epoch"] = str(frame.get("counter_epoch", 0))
    if not previous:
        return
    interval = context["interval_seconds"]
    calculated = derive(previous, frame, interval)
    reset = any(
        reason in calculated["reasons"]
        for reason in (
            "server_restarted",
            "uptime_epoch_changed",
            "counter_decreased",
            "source_epoch_changed",
        )
    )
    if name == "identity_status":
        counters = (
            "Questions",
            "Com_commit",
            "Com_rollback",
            "Bytes_received",
            "Bytes_sent",
            "Innodb_data_reads",
            "Innodb_data_writes",
            "Innodb_buffer_pool_reads",
            "Innodb_buffer_pool_read_requests",
        )
        reset = reset or any(
            k in frame.get("raw_values", {})
            and k in previous.get("raw_values", {})
            and int(frame["raw_values"][k]) < int(previous["raw_values"][k])
            for k in counters
        )
    if name == "statement_digest":
        old = {
            (r.get("SCHEMA_NAME"), r.get("DIGEST")): r
            for r in previous.get("raw_values", {}).get("rows", [])
        }
        reset = reset or any(
            (r.get("SCHEMA_NAME"), r.get("DIGEST")) in old
            and any(
                int(r.get(k) or 0) < int(old[(r.get("SCHEMA_NAME"), r.get("DIGEST"))].get(k) or 0)
                for k in ("COUNT_STAR", "SUM_TIMER_WAIT")
            )
            for r in frame.get("raw_values", {}).get("rows", [])
        )
    if reset:
        frame["counter_epoch"] = int(previous["counter_epoch"]) + 1
        if name != "host":
            frame["source_epoch"] = str(frame["counter_epoch"])
        calculated = derive(previous, frame, interval)
    frame.update(
        derived_values=calculated["values"],
        rate_validity=calculated["validity"],
        rate_reasons=calculated["reasons"],
        window_seconds=calculated["window_seconds"],
        window_validity=calculated["window_validity"],
        metric_validity=calculated["metric_validity"],
    )
    if name == "statement_digest":
        frame["digest_window"] = derive_digest_window(previous, frame, interval)
        for item in batch.get("evidence_items", []):
            if item["source"] == "mysql.statement_digest":
                item["payload"]["window"] = frame["digest_window"]


def handle_collection(session_factory, lease, config):
    activation = lease.type == "activate_instance"
    revision = lease.payload.get("config_revision")
    if not isinstance(revision, int):
        raise PlatformError(422, "config_revision_required", "任务缺少配置版本。")
    with session_factory.begin() as db:
        instance = _collection_authority(db, lease, revision, activation)
        deadline = _deadline(db, lease)
        names = lease.payload.get("collectors")
        if names is None:
            names = list(
                db.scalars(
                    select(CollectionSchedule.collector).where(
                        CollectionSchedule.instance_id == instance.id
                    )
                )
            )
        if not isinstance(names, list) or not names or any(n not in COLLECTORS for n in names):
            raise PlatformError(422, "invalid_collectors", "采集器选择无效。")
        names = list(dict.fromkeys(names))
        if activation and "identity_status" not in names:
            names.insert(0, "identity_status")
        if "identity_status" in names:
            names.remove("identity_status")
            names.insert(0, "identity_status")
        connection = dict(instance.connection)
        credential_ref = instance.credential_ref
        expected = instance.expected_server_uuid
    run_ids = []
    evidence_ids = []
    statuses = {}
    core_success = False
    quarantined = False
    with ExitStack() as stack:
        database = None
        database_error = None
        ssh_clients = {}

        def ssh_for(ref):
            if ref not in ssh_clients:
                ssh_clients[ref] = stack.enter_context(open_ssh(_ssh(config, ref), deadline))
            return ssh_clients[ref]

        def database_for():
            nonlocal database, database_error
            if database_error:
                raise ConnectorError(database_error)
            if database is None:
                try:
                    target = _target(config, connection, expected)
                    secret = SecretStore(config).resolve(credential_ref)
                    database = stack.enter_context(
                        open_mysql(
                            target, secret, deadline, ssh_profile_resolver=lambda r: _ssh(config, r)
                        )
                    )
                except Exception as exc:
                    database_error = _code(exc)
                    raise
            return database

        for name in names:
            with session_factory.begin() as db:
                instance = _collection_authority(db, lease, revision, activation)
                context = _context(db, instance, name, deadline)
                started = db_now(db)
                started_mono = time.perf_counter()
                run = CollectionRun(
                    id=str(uuid4()),
                    task_id=lease.id,
                    instance_id=instance.id,
                    collector=name,
                    attempt=lease.attempt,
                    config_revision=revision,
                    fence=lease.fence,
                    started_at=started,
                    status="running",
                )
                db.add(run)
                db.flush()
                run_id = run.id
            try:
                remaining(deadline)
                if name == "host":
                    profile = _private_profile(
                        config, "host_profiles", connection.get("host_profile_ref")
                    )
                    item = collect_host(ssh_for(profile["ssh_profile_ref"]), context)
                    if profile.get("expected_host_identity") and profile[
                        "expected_host_identity"
                    ] not in (
                        item.get("payload", {}).get("values", {}).get("hostname"),
                        item.get("payload", {}).get("values", {}).get("host_boot_id"),
                    ):
                        raise ConnectorError("host_identity_mismatch")
                    frame = item.pop("metric_frame", None)
                    batch = {
                        "observed_at": item["observed_at"],
                        "server_uuid": expected,
                        "evidence_items": [item],
                        "metric_frame": frame,
                        "capability_results": [
                            {
                                "collector": "linux.host",
                                "availability": item["availability"],
                                "error_code": item.get("error_code"),
                            }
                        ],
                    }
                elif name == "slow_log" and connection.get("log_profile_ref"):
                    try:
                        profile = _private_profile(
                            config, "log_profiles", connection["log_profile_ref"]
                        )
                        ssh = ssh_for(profile["ssh_profile_ref"])
                        with ssh.open_sftp() as sftp:
                            sftp.get_channel().settimeout(remaining(deadline, 3))
                            item = read_slow_file(
                                sftp,
                                profile["slow_log_path"],
                                profile.get("allowed_paths", []),
                                context.get("slow_file_cursor"),
                                initial_mode=profile.get("initial_mode", "tail"),
                                deadline=deadline,
                            )
                    except Exception as exc:
                        item = _failed_batch(name, expected, _code(exc))["evidence_items"][0]
                    batch = {
                        "observed_at": item["observed_at"],
                        "server_uuid": expected,
                        "evidence_items": [item],
                        "capability_results": [
                            {
                                "collector": item["source"],
                                "availability": item["availability"],
                                "error_code": item.get("error_code"),
                            }
                        ],
                    }
                    if item.get("availability") not in GOOD:
                        # Preserve the FILE failure alongside a bounded TABLE
                        # fallback; TABLE late-arrival limits remain explicit.
                        try:
                            fallback = collect_mysql(database_for(), context, "slow_log")
                            if hasattr(fallback, "to_dict"):
                                fallback = fallback.to_dict()
                            if fallback.get("server_uuid") != expected:
                                raise ConnectorError("identity_mismatch")
                            for source in fallback.get("evidence_items", []):
                                source.setdefault("coverage", {})["fallback_from"] = (
                                    "mysql.slow_log.file"
                                )
                            batch["evidence_items"].extend(fallback.get("evidence_items", []))
                            batch["capability_results"].extend(
                                fallback.get("capability_results", [])
                            )
                        except Exception as exc:
                            if _code(exc) == "identity_mismatch":
                                raise
                            batch["evidence_items"].append(
                                result(
                                    "mysql.slow_log.table",
                                    availability="unavailable",
                                    error_code=_code(exc),
                                )
                            )
                else:
                    batch = collect_mysql(database_for(), context, name)
                    if hasattr(batch, "to_dict"):
                        batch = batch.to_dict()
                if batch.get("server_uuid") and batch["server_uuid"] != expected:
                    raise ConnectorError("identity_mismatch")
                _derive_epoch(batch, context, name)
            except Exception as exc:
                code = _code(exc)
                if code == "identity_mismatch":
                    quarantined = True
                    batch = None
                else:
                    batch = _failed_batch(name, expected, code)
            with session_factory.begin() as db:
                instance = _collection_authority(db, lease, revision, activation)
                run = db.get(CollectionRun, run_id)
                run.finished_at = db_now(db)
                run.duration_ms = (time.perf_counter() - started_mono) * 1000
                if quarantined:
                    run.status = "failed"
                    run.error_code = "identity_mismatch"
                    instance.lifecycle_state = "Quarantined"
                    instance.enabled = False
                    instance.health = "unknown"
                    for schedule in db.scalars(
                        select(CollectionSchedule).where(
                            CollectionSchedule.instance_id == instance.id
                        )
                    ):
                        schedule.enabled = False
                    append_audit(
                        db,
                        lease.actor_id,
                        "collection.identity_mismatch",
                        "instance",
                        instance.id,
                        result="failed",
                    )
                else:
                    run.status = _batch_status(batch)
                    owned = [
                        i
                        for i in batch.get("evidence_items", [])
                        if i["source"] not in ("mysql.identity", "mysql.global_status")
                    ]
                    if not owned:
                        owned = batch.get("evidence_items", [])
                    availability = (
                        "available"
                        if owned and all(i.get("availability") in GOOD for i in owned)
                        else (
                            "degraded"
                            if any(i.get("availability") in GOOD for i in owned)
                            else next((i.get("availability", "unknown") for i in owned), "unknown")
                        )
                    )
                    batch.setdefault("capability_results", []).append(
                        {
                            "collector": name,
                            "availability": availability,
                            "error_code": next(
                                (
                                    i.get("error_code")
                                    for i in owned
                                    if i.get("availability") not in GOOD
                                ),
                                None,
                            ),
                        }
                    )
                    run.error_code = next(
                        (
                            i.get("error_code")
                            for i in batch.get("evidence_items", [])
                            if i.get("error_code") and i.get("availability") not in GOOD
                        ),
                        None,
                    )
                    run.row_count = sum(
                        i.get("payload", {}).get(
                            "row_count", len(i.get("payload", {}).get("events", []))
                        )
                        for i in batch.get("evidence_items", [])
                    )
                    run.truncated = any(
                        i.get("truncated", False) for i in batch.get("evidence_items", [])
                    )
                    evidence_ids.extend(
                        append_batch(
                            db, instance, run, batch, interval_seconds=context["interval_seconds"]
                        )
                    )
                    if name == "identity_status":
                        core_success = _core_ok(batch)
                        instance.last_attempt_at = run.finished_at
                        if core_success:
                            instance.last_success_at = run.finished_at
                    schedule = db.scalar(
                        select(CollectionSchedule).where(
                            CollectionSchedule.instance_id == instance.id,
                            CollectionSchedule.collector == name,
                        )
                    )
                    if schedule:
                        schedule.consecutive_failures = (
                            0 if run.status == "succeeded" else schedule.consecutive_failures + 1
                        )
                        if run.status == "succeeded":
                            # The scheduler already reserved the next nominal
                            # phase. Transport/SQL duration must not move it.
                            # Manual/activation or overrun observations only
                            # realign an expired due; no missed sample is made up.
                            if schedule.next_due_at <= run.finished_at:
                                schedule.next_due_at = next_collection_due(
                                    instance, schedule, run.finished_at
                                )
                        else:
                            delay = min(
                                900,
                                context["interval_seconds"]
                                * (2 ** min(schedule.consecutive_failures, 4)),
                            )
                            schedule.next_due_at = run.finished_at + timedelta(seconds=delay)
                    db.flush()
                    evaluate(db, instance.id, run_id=run.id, now=run.finished_at)
                statuses[name] = run.status
                run_ids.append(run.id)
                if quarantined:
                    response = {
                        "run_ids": run_ids,
                        "evidence_ids": evidence_ids,
                        "collectors": statuses,
                        "error_code": "identity_mismatch",
                    }
                    complete(
                        db,
                        lease,
                        result=response,
                        status="failed",
                        error_code="identity_mismatch",
                        allow_inactive=True,
                        config_revision=revision,
                    )
            if quarantined:
                return response
    with session_factory.begin() as db:
        instance = _collection_authority(db, lease, revision, activation)
        if activation:
            instance.lifecycle_state = "Active" if core_success else "Ready"
            instance.enabled = core_success
            for schedule in db.scalars(
                select(CollectionSchedule).where(CollectionSchedule.instance_id == instance.id)
            ):
                schedule.enabled = core_success
                schedule.config_revision = revision
        if "identity_status" in statuses:
            instance.health = (
                "healthy"
                if core_success and all(v == "succeeded" for v in statuses.values())
                else "degraded"
                if core_success
                else "unreachable"
            )
        response = {
            "run_ids": run_ids,
            "evidence_ids": evidence_ids,
            "collectors": statuses,
            "partial": any(v != "succeeded" for v in statuses.values()),
        }
        failed = (activation and not core_success) or all(v == "failed" for v in statuses.values())
        complete(
            db,
            lease,
            result=response,
            status="failed" if failed else "succeeded",
            error_code="core_collection_failed" if activation and not core_success else None,
            allow_inactive=activation,
            config_revision=revision,
        )
        append_audit(
            db,
            lease.actor_id,
            "asset.activate.complete" if activation else "collection.complete",
            "instance",
            instance.id,
            result="failed" if failed else "success",
            details={"run_ids": run_ids},
        )
    return json_safe(response)
