"""Fixed-template MySQL collectors. No caller can supply SQL or expressions."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import time
import uuid
from ..connectors import ConnectorError, remaining, safe_error_code
from ..metrics_derivation import derive, derive_digest_window
from .logs import collect_error_table, collect_slow_table, json_safe, redact_sql, result

BOOT_ID = str(uuid.uuid4())
STATUS_FIELDS = (
    "Uptime",
    "Questions",
    "Com_commit",
    "Com_rollback",
    "Threads_connected",
    "Threads_running",
    "Max_used_connections",
    "Connections",
    "Aborted_connects",
    "Aborted_clients",
    "Bytes_received",
    "Bytes_sent",
    "Innodb_buffer_pool_reads",
    "Innodb_buffer_pool_read_requests",
    "Innodb_buffer_pool_pages_total",
    "Innodb_buffer_pool_pages_free",
    "Innodb_buffer_pool_pages_dirty",
    "Innodb_data_reads",
    "Innodb_data_writes",
    "Innodb_data_read",
    "Innodb_data_written",
    "Innodb_row_lock_current_waits",
    "Innodb_row_lock_waits",
    "Innodb_row_lock_time",
)
VARIABLE_FIELDS = (
    "max_connections",
    "innodb_buffer_pool_size",
    "innodb_flush_log_at_trx_commit",
    "sync_binlog",
    "transaction_isolation",
    "read_only",
    "super_read_only",
    "log_bin",
    "binlog_format",
    "slow_query_log",
    "log_output",
    "long_query_time",
    "performance_schema",
    "version_comment",
    "character_set_server",
    "collation_server",
)
IDENTITY_SQL = "SELECT /*+ MAX_EXECUTION_TIME(2000) */ @@server_uuid AS server_uuid, @@version AS version, UTC_TIMESTAMP(6) AS server_observed_at, @@performance_schema AS performance_schema_enabled"
# MySQL 8.4 global_status omits some Com_* counters; SHOW exposes the server's
# authoritative global statement counters without querying any business rows.
STATUS_SQL = (
    "SHOW GLOBAL STATUS WHERE Variable_name IN (" + ",".join(["%s"] * len(STATUS_FIELDS)) + ")"
)
PROFILE_NAMES = {
    "identity_status",
    "runtime",
    "statement_digest",
    "schema_config",
    "replication",
    "error_log",
    "slow_log",
    "logs",
}


@dataclass
class CollectionBatch:
    observed_at: str
    server_observed_at: str | None = None
    server_uuid: str | None = None
    uptime_seconds: int | None = None
    source_version: str | None = None
    capability_results: list = field(default_factory=list)
    evidence_items: list = field(default_factory=list)
    metric_frame: dict | None = None

    def to_dict(self):
        return json_safe(asdict(self))


def _query(connection, sql, params=None, deadline=None):
    remaining(deadline)
    if hasattr(connection, "_read_timeout"):
        connection._read_timeout = remaining(deadline, 2)
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def _source(connection, source, sql, params=None, *, deadline=None, limit=200, coverage=None):
    try:
        rows = _query(connection, sql, params, deadline)
        truncated = len(rows) > limit
        rows = rows[:limit]
        for row in rows:
            for name in list(row):
                if (
                    name.lower() in ("trx_query", "query", "info", "digest_text")
                    and row[name] is not None
                ):
                    row[name] = redact_sql(row[name])
        return result(
            source,
            {"rows": rows, "row_count": len(rows)},
            truncated=truncated,
            coverage=dict(coverage or {}, complete=not truncated, limit=limit),
            availability="degraded" if truncated else "available",
        )
    except Exception as exc:
        code = safe_error_code(exc)
        return result(
            source,
            availability=code
            if code in ("permission_denied", "unsupported", "unreachable")
            else "degraded",
            error_code=code,
        )


def probe_mysql(connection, *, deadline=None):
    """Minimal preflight; no full collection and no live metric frame."""
    start = time.perf_counter()
    before = datetime.now(timezone.utc)
    row = _query(connection, IDENTITY_SQL, deadline=deadline)[0]
    elapsed = time.perf_counter() - start
    after = datetime.now(timezone.utc)
    server = row.get("server_observed_at")
    offset = None
    if isinstance(server, datetime):
        offset = (
            server.replace(tzinfo=timezone.utc) - (before + (after - before) / 2)
        ).total_seconds()
    capabilities = []
    for name, sql in (
        ("performance_schema", "SELECT @@performance_schema AS enabled"),
        ("slow_log", "SELECT @@slow_query_log AS enabled, @@log_output AS output"),
        ("metadata", "SELECT SCHEMA_NAME FROM information_schema.schemata LIMIT 21"),
        ("error_log", "SELECT LOGGED FROM performance_schema.error_log LIMIT 1"),
    ):
        item = _source(connection, "mysql." + name, sql, deadline=deadline, limit=20)
        capabilities.append(
            {
                "collector": name,
                "availability": item["availability"],
                "error_code": item["error_code"],
                "sample": item["payload"],
            }
        )
    return json_safe(
        dict(
            row,
            rtt_seconds=elapsed,
            clock_offset_seconds=offset,
            clock_risk=offset is not None and abs(offset) > 5,
            capabilities=capabilities,
        )
    )


def collect_mysql(connection, context, profile_name="identity_status"):
    if profile_name == "digest":
        profile_name = "statement_digest"
    if profile_name not in PROFILE_NAMES:
        raise ValueError("unsupported_collector")
    deadline = context.get("deadline", context.get("deadline_monotonic"))
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    monotonic_ns = time.perf_counter_ns()
    batch = CollectionBatch(observed_at)
    identity = _source(connection, "mysql.identity", IDENTITY_SQL, deadline=deadline, limit=1)
    batch.evidence_items.append(identity)
    if identity["availability"] != "available" or not identity["payload"].get("rows"):
        batch.capability_results = [
            {
                "collector": profile_name,
                "availability": identity["availability"],
                "error_code": identity["error_code"] or "identity_unavailable",
            }
        ]
        return batch
    info = identity["payload"]["rows"][0]
    ps_enabled = str(info.get("performance_schema_enabled", 1)).upper() not in ("0", "OFF", "FALSE")
    batch.server_uuid = info["server_uuid"]
    batch.source_version = info["version"]
    batch.server_observed_at = json_safe(info.get("server_observed_at"))
    expected = context.get("expected_server_uuid")
    if expected and expected != batch.server_uuid:
        raise ConnectorError("identity_mismatch")
    status = _source(
        connection,
        "mysql.global_status",
        STATUS_SQL,
        STATUS_FIELDS,
        deadline=deadline,
        limit=len(STATUS_FIELDS),
    )
    batch.evidence_items.append(status)
    values = {}
    for row in status["payload"].get("rows", []):
        try:
            values[row.get("VARIABLE_NAME", row.get("Variable_name"))] = int(
                row.get("VARIABLE_VALUE", row.get("Value"))
            )
        except (TypeError, ValueError):
            continue
    batch.uptime_seconds = values.get("Uptime")
    # Caller maintains independent counter epochs by source; uptime alone is not a reset detector.
    epoch = context.get("counter_epoch", context.get("source_epoch", 0))
    anchor = context.get("identity_epoch_anchor")
    if (
        profile_name in ("error_log", "slow_log", "logs")
        and anchor
        and anchor.get("server_uuid") == batch.server_uuid
    ):
        epoch = int(anchor.get("counter_epoch", epoch))
        old_uptime = anchor.get("uptime_seconds")
        if old_uptime is not None and batch.uptime_seconds is not None:
            restarted = batch.uptime_seconds < old_uptime
            if anchor.get("collector_boot_id") == context.get("collector_boot_id", BOOT_ID):
                elapsed = (monotonic_ns - anchor.get("observed_monotonic_ns", monotonic_ns)) / 1e9
                restarted = restarted or abs((batch.uptime_seconds - old_uptime) - elapsed) > 2
            if restarted:
                epoch += 1
    frame = {
        "instance_id": context.get("instance_id"),
        "owner_collector": profile_name,
        "collector": profile_name,
        "server_uuid": batch.server_uuid,
        "collector_boot_id": context.get("collector_boot_id", BOOT_ID),
        "observed_monotonic_ns": monotonic_ns,
        "observed_at": observed_at,
        "uptime_seconds": batch.uptime_seconds,
        "counter_epoch": epoch,
        "source_epoch": epoch,
        "raw_values": {},
        "availability": "available",
    }
    items = []
    if profile_name == "identity_status":
        frame["raw_values"] = values
        frame["availability"] = status["availability"]
    elif profile_name == "runtime":
        items.append(
            _source(
                connection,
                "mysql.transactions",
                "SELECT /*+ MAX_EXECUTION_TIME(2000) */ TRX_ID,TRX_STATE,TRX_STARTED,TRX_WAIT_STARTED,TRX_MYSQL_THREAD_ID,TRX_ROWS_LOCKED,TRX_ROWS_MODIFIED FROM information_schema.innodb_trx ORDER BY TRX_STARTED LIMIT 201",
                deadline=deadline,
            )
        )
        items.append(
            _source(
                connection,
                "mysql.lock_waits",
                "SELECT /*+ MAX_EXECUTION_TIME(2000) */ w.REQUESTING_ENGINE_TRANSACTION_ID AS waiting_trx_id,w.BLOCKING_ENGINE_TRANSACTION_ID AS blocking_trx_id,w.REQUESTING_THREAD_ID AS waiting_thread_id,w.BLOCKING_THREAD_ID AS blocking_thread_id,r.OBJECT_SCHEMA,r.OBJECT_NAME,r.INDEX_NAME,r.LOCK_TYPE,r.LOCK_MODE FROM performance_schema.data_lock_waits w LEFT JOIN performance_schema.data_locks r ON r.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID AND r.ENGINE=w.ENGINE LIMIT 201",
                deadline=deadline,
            )
        )
        items.append(
            _source(
                connection,
                "mysql.sessions",
                "SELECT /*+ MAX_EXECUTION_TIME(2000) */ t.THREAD_ID,t.PROCESSLIST_ID,t.PROCESSLIST_DB,t.PROCESSLIST_COMMAND,t.PROCESSLIST_TIME,t.PROCESSLIST_STATE,s.DIGEST,s.DIGEST_TEXT FROM performance_schema.threads t LEFT JOIN performance_schema.events_statements_current s ON s.THREAD_ID=t.THREAD_ID WHERE t.TYPE='FOREGROUND' ORDER BY t.PROCESSLIST_TIME DESC LIMIT 201",
                deadline=deadline,
            )
        )
        if not ps_enabled:
            items = [
                result(
                    item["source"],
                    availability="not_configured",
                    error_code="performance_schema_disabled",
                )
                if item["source"] in ("mysql.lock_waits", "mysql.sessions")
                else item
                for item in items
            ]
        frame["raw_values"] = {
            item["source"].split(".")[-1]: item["payload"].get("row_count")
            if item["availability"] == "available"
            else None
            for item in items
        }
        if any(item["availability"] != "available" for item in items):
            frame["availability"] = "degraded"
    elif profile_name == "statement_digest":
        consumers = _source(
            connection,
            "mysql.digest_consumers",
            "SELECT NAME,ENABLED FROM performance_schema.setup_consumers WHERE NAME='statements_digest'",
            deadline=deadline,
            limit=1,
        )
        items.append(consumers)
        enabled = consumers["payload"].get("rows", [])
        if not ps_enabled:
            item = result(
                "mysql.statement_digest",
                availability="not_configured",
                error_code="performance_schema_disabled",
            )
        elif enabled and enabled[0]["ENABLED"] == "NO":
            item = result(
                "mysql.statement_digest",
                availability="not_configured",
                error_code="digest_consumer_disabled",
            )
        else:
            schemas = context.get("selected_schemas", [])
            if len(schemas) > 20 or any(not isinstance(value, str) for value in schemas):
                raise ValueError("invalid_schema_selection")
            where = "WHERE DIGEST IS NOT NULL"
            params = None
            if schemas:
                where += " AND SCHEMA_NAME IN (" + ",".join("%s" for _ in schemas) + ")"
                params = tuple(schemas)
            item = _source(
                connection,
                "mysql.statement_digest",
                "SELECT /*+ MAX_EXECUTION_TIME(2000) */ SCHEMA_NAME,DIGEST,DIGEST_TEXT,COUNT_STAR,SUM_TIMER_WAIT,SUM_LOCK_TIME,SUM_ROWS_SENT,SUM_ROWS_EXAMINED,SUM_CREATED_TMP_TABLES,SUM_CREATED_TMP_DISK_TABLES,FIRST_SEEN,LAST_SEEN FROM performance_schema.events_statements_summary_by_digest "
                + where
                + " ORDER BY SUM_TIMER_WAIT DESC LIMIT 201",
                params,
                deadline=deadline,
                coverage={
                    "complete": False,
                    "reason": "top_n_cumulative_digest_not_slow_log",
                    "selected_schemas": schemas,
                    "collector_queries_included": True,
                },
            )
            item["coverage"]["complete"] = False
        items.append(item)
        frame["raw_values"] = {"rows": item["payload"].get("rows", [])}
        frame["availability"] = item["availability"]
    elif profile_name == "schema_config":
        schemas = context.get("selected_schemas", [])
        if len(schemas) > 20 or any(not isinstance(value, str) for value in schemas):
            raise ValueError("invalid_schema_selection")
        offset = max(0, min(1_000_000, int(context.get("metadata_offset", 0))))
        where = "TABLE_SCHEMA NOT IN ('mysql','sys','performance_schema','information_schema')"
        params = ()
        if schemas:
            where += " AND TABLE_SCHEMA IN (" + ",".join("%s" for _ in schemas) + ")"
            params = tuple(schemas)
        for source, columns, table, order in (
            (
                "tables",
                "TABLE_SCHEMA,TABLE_NAME,TABLE_TYPE,ENGINE,TABLE_ROWS,DATA_LENGTH,INDEX_LENGTH,DATA_FREE,TABLE_COLLATION",
                "tables",
                "TABLE_SCHEMA,TABLE_NAME",
            ),
            (
                "indexes",
                "TABLE_SCHEMA,TABLE_NAME,INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME,CARDINALITY,INDEX_TYPE",
                "statistics",
                "TABLE_SCHEMA,TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX",
            ),
            (
                "columns",
                "TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,DATA_TYPE,ORDINAL_POSITION,IS_NULLABLE",
                "columns",
                "TABLE_SCHEMA,TABLE_NAME,ORDINAL_POSITION",
            ),
        ):
            item = _source(
                connection,
                "mysql." + source,
                "SELECT /*+ MAX_EXECUTION_TIME(2000) */ "
                + columns
                + " FROM information_schema."
                + table
                + " WHERE "
                + where
                + " ORDER BY "
                + order
                + " LIMIT 2001 OFFSET %s",
                params + (offset,),
                deadline=deadline,
                limit=2000,
                coverage={
                    "estimate": True,
                    "selected_schemas": schemas,
                    "offset": offset,
                    "visibility": "account_metadata_visibility_not_proof_of_full_instance",
                },
            )
            item["coverage"]["next_offset"] = offset + 2000 if item["truncated"] else None
            items.append(item)
        items.append(
            _source(
                connection,
                "mysql.configuration",
                "SELECT VARIABLE_NAME,VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME IN ("
                + ",".join("%s" for _ in VARIABLE_FIELDS)
                + ")",
                VARIABLE_FIELDS,
                deadline=deadline,
                limit=len(VARIABLE_FIELDS),
            )
        )
    elif profile_name == "replication":
        item = _source(
            connection, "mysql.replication", "SHOW REPLICA STATUS", deadline=deadline, limit=200
        )
        if item["availability"] == "available":
            # Credentials, filesystem paths and raw remote messages are not collected.
            allowed = (
                "Channel_Name",
                "Source_UUID",
                "Source_Server_Id",
                "Replica_IO_Running",
                "Replica_SQL_Running",
                "Seconds_Behind_Source",
                "Last_IO_Errno",
                "Last_SQL_Errno",
                "SQL_Delay",
                "SQL_Remaining_Delay",
            )
            item["payload"]["rows"] = [
                {k: v for k, v in row.items() if k in allowed} for row in item["payload"]["rows"]
            ]
            item["coverage"]["complete"] = False
            item["coverage"]["topology"] = "replica_side_only"
            if not item["payload"]["rows"]:
                item["availability"] = "not_applicable"
                item["error_code"] = "replication_not_configured"
        items.append(item)
    if profile_name in ("error_log", "logs"):
        log_cursor = context.get("error_log_cursor")
        if log_cursor and log_cursor.get("source_epoch") != str(epoch):
            log_cursor = None
        items.append(
            collect_error_table(connection, log_cursor, deadline=deadline, source_epoch=str(epoch))
            if ps_enabled
            else result(
                "mysql.error_log",
                availability="not_configured",
                error_code="performance_schema_disabled",
            )
        )
    if profile_name in ("slow_log", "logs"):
        items.append(
            collect_slow_table(
                connection,
                context.get("slow_log_cursor"),
                deadline=deadline,
                source_epoch=str(epoch),
            )
        )
    batch.evidence_items.extend(items)
    for item in batch.evidence_items:
        batch.capability_results.append(
            {
                "collector": item["source"],
                "availability": item["availability"],
                "error_code": item["error_code"],
                "row_count": item["payload"].get(
                    "row_count", len(item["payload"].get("events", []))
                ),
                "truncated": item["truncated"],
            }
        )
    if profile_name in ("identity_status", "runtime", "statement_digest"):
        derived = derive(
            context.get("previous_frame"),
            frame,
            context.get("interval_seconds", 60 if profile_name == "statement_digest" else 30),
        )
        frame.update(
            derived_values=derived["values"],
            rate_validity=derived["validity"],
            rate_reasons=derived["reasons"],
            window_seconds=derived["window_seconds"],
            window_validity=derived["window_validity"],
            metric_validity=derived["metric_validity"],
        )
        if profile_name == "statement_digest":
            frame["digest_window"] = derive_digest_window(
                context.get("previous_frame"), frame, context.get("interval_seconds", 60)
            )
            for item in items:
                if item["source"] == "mysql.statement_digest":
                    item["payload"]["window"] = frame["digest_window"]
        batch.metric_frame = frame
    return batch
