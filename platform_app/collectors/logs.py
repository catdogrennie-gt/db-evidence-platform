"""Bounded log readers. Cursor updates are returned, never committed here."""

import base64
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import posixpath
import re
import uuid
from ..connectors import remaining, safe_error_code

MAX_BYTES = 1024 * 1024
MAX_ROWS = 1000
PARSER_VERSION = "mysql-slow-v1"


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
            if value.tzinfo is None
            else value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        )
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def redact_sql(sql):
    """Conservative SQL shape, stripping comments and all string/numeric literals.

    This shape is evidence, never executable SQL. Identifier names remain scoped
    business evidence; callers still enforce instance/field authorization.
    """
    token = re.compile(
        r"'(?:''|\\.|[^'\\])*(?:'|$)|\"(?:\"\"|\\.|[^\"\\])*(?:\"|$)|`(?:``|[^`])*`|/\*[\s\S]*?(?:\*/|$)|--[^\n]*|\#[^\n]*|\b0[xX][0-9a-fA-F]+\b|\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b"
    )

    def replace(match):
        value = match.group()
        if value.startswith("`"):
            return value
        return " " if value.startswith(("/*", "--", "#")) else "?"

    return re.sub(r"\s+", " ", token.sub(replace, str(sql))).strip()


def redact_message(message):
    # Free-form log bodies may contain credentials/identifiers without quotes.
    # Store a category and hash only; raw server messages do not cross this boundary.
    text = str(message)
    categories = [
        word
        for word in (
            "error",
            "warning",
            "ready",
            "shutdown",
            "startup",
            "denied",
            "replication",
            "crash",
            "connection",
            "InnoDB",
        )
        if word.lower() in text.lower()
    ]
    return {
        "message_categories": categories,
        "message_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "message_redacted": True,
        "redaction_policy": "message-hash-only-v1",
    }


def result(
    source, payload=None, availability="available", coverage=None, error_code=None, truncated=False
):
    return {
        "source": source,
        "source_mode": "live",
        "availability": availability,
        "observed_at": utc_now(),
        "payload": json_safe(payload or {}),
        "coverage": coverage or {"complete": False},
        "truncated": truncated,
        "error_code": error_code,
    }


def _safe_failure(source, exc):
    code = safe_error_code(exc)
    availability = (
        code if code in ("permission_denied", "unsupported", "unreachable") else "degraded"
    )
    return result(source, availability=availability, error_code=code)


def _query(connection, sql, params=None, deadline=None):
    remaining(deadline)
    if hasattr(connection, "_read_timeout"):
        connection._read_timeout = remaining(deadline, 2)
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def _deduplicate(events, epoch, prior_seen):
    occurrences = Counter()
    seen = set(prior_seen)
    output = []
    for event in events:
        canonical = json.dumps(json_safe(event), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        occurrences[digest] += 1
        key = hashlib.sha256(f"{epoch}:{digest}:{occurrences[digest]}".encode()).hexdigest()
        if key not in seen:
            output.append(dict(event, event_id=key))
        seen.add(key)
    return output, sorted(seen)[-4000:]


def collect_error_table(connection, cursor=None, *, deadline=None, source_epoch="unknown"):
    cursor = cursor or {}
    try:
        boundary = _query(
            connection,
            "SELECT MIN(LOGGED) AS earliest, MAX(LOGGED) AS latest FROM performance_schema.error_log",
            deadline=deadline,
        )
        info = boundary[0] if boundary else {}
        since = cursor.get("last_server_event_at")
        if since:
            since = datetime.fromisoformat(since.replace("Z", "+00:00")).replace(
                tzinfo=None
            ) - timedelta(seconds=5)
        else:
            since = datetime(1970, 1, 1)
        rows = _query(
            connection,
            "SELECT /*+ MAX_EXECUTION_TIME(2000) */ LOGGED,THREAD_ID,PRIO,ERROR_CODE,SUBSYSTEM,DATA FROM performance_schema.error_log WHERE LOGGED >= %s ORDER BY LOGGED LIMIT 1001",
            (since,),
            deadline,
        )
        gaps = []
        if not cursor or cursor.get("source_epoch") != source_epoch:
            gaps.append("initial_or_epoch_changed")
        earliest = json_safe(info.get("earliest"))
        if (
            cursor.get("last_server_event_at")
            and earliest
            and cursor["last_server_event_at"] < earliest
        ):
            gaps.append("watermark_before_available_buffer")
        expired = _query(
            connection,
            "SELECT VARIABLE_NAME,VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME IN ('Error_log_buffered_bytes','Error_log_buffered_events','Error_log_expired_events')",
            deadline=deadline,
        )
        buffer_status = {row["VARIABLE_NAME"]: int(row["VARIABLE_VALUE"]) for row in expired}
        if buffer_status.get("Error_log_expired_events", 0) > cursor.get("expired_events", 0):
            gaps.append("buffer_expired_events")
        events = []
        used = 0
        for row in rows[:MAX_ROWS]:
            used += len(json.dumps(json_safe(row)).encode())
            if used > MAX_BYTES:
                break
            event = {k: json_safe(v) for k, v in row.items() if k != "DATA"}
            event.update(
                server_event_at=json_safe(row.get("LOGGED")), **redact_message(row.get("DATA", ""))
            )
            events.append(event)
        truncated = len(rows) > MAX_ROWS or used > MAX_BYTES
        events, seen = _deduplicate(
            events,
            source_epoch,
            cursor.get("seen_ids", []) if cursor.get("source_epoch") == source_epoch else [],
        )
        coverage = {
            "complete": False,
            "reason": "bounded_memory_ring_without_stable_global_event_id",
            "gaps": gaps,
            "earliest": earliest,
            "latest": json_safe(info.get("latest")),
            "overlap_seconds": 5,
            "buffer_status": buffer_status,
        }
        if truncated:
            coverage["gaps"].append("batch_limit")
        value = result(
            "mysql.error_log",
            {"events": events},
            coverage=coverage,
            truncated=truncated,
            availability="degraded" if truncated else "available",
        )
        # Never advance past an unconsumed truncated batch.
        latest = max(
            (e["server_event_at"] for e in events if e.get("server_event_at")),
            default=cursor.get("last_server_event_at"),
        )
        value["cursor"] = {
            "last_server_event_at": latest,
            "seen_ids": seen,
            "source_epoch": source_epoch,
            "expired_events": buffer_status.get("Error_log_expired_events", 0),
        }
        return value
    except Exception as exc:
        return _safe_failure("mysql.error_log", exc)


def collect_slow_table(
    connection, cursor=None, *, deadline=None, source_epoch="unknown", lookback_seconds=3600
):
    cursor = cursor or {}
    lookback_seconds = max(1, min(3600, int(lookback_seconds)))
    coverage = {
        "complete": False,
        "late_arrival_possible": True,
        "lookback_seconds": lookback_seconds,
        "reason": "start_time_window_cannot_cover_arbitrarily_late_completions",
        "query_budget_seconds": 2,
    }
    try:
        settings = _query(
            connection,
            "SELECT @@global.slow_query_log AS enabled, @@global.log_output AS output",
            deadline=deadline,
        )[0]
        if str(settings["enabled"]).upper() not in ("1", "ON", "TRUE"):
            return result(
                "mysql.slow_log.table",
                availability="not_configured",
                coverage=coverage,
                error_code="slow_log_disabled",
            )
        if "TABLE" not in str(settings["output"]).upper().split(","):
            return result(
                "mysql.slow_log.table",
                availability="not_configured",
                coverage=coverage,
                error_code="table_output_disabled",
            )
        columns = _query(
            connection,
            "SELECT COLUMN_NAME FROM information_schema.columns WHERE TABLE_SCHEMA='mysql' AND TABLE_NAME='slow_log'",
            deadline=deadline,
        )
        allowed = (
            "start_time",
            "query_time",
            "lock_time",
            "rows_sent",
            "rows_examined",
            "db",
            "thread_id",
            "sql_text",
        )
        selected = [
            name for name in allowed if name in {str(row["COLUMN_NAME"]).lower() for row in columns}
        ]
        if not {"start_time", "sql_text"} <= set(selected):
            return result(
                "mysql.slow_log.table",
                availability="unsupported",
                coverage=coverage,
                error_code="slow_log_columns_missing",
            )
        sql = (
            "SELECT /*+ MAX_EXECUTION_TIME(2000) */ "
            + ",".join("`" + name + "`" for name in selected)
            + " FROM mysql.slow_log WHERE start_time >= UTC_TIMESTAMP(6) - INTERVAL %s SECOND ORDER BY start_time LIMIT 1001"
        )
        rows = _query(connection, sql, (lookback_seconds,), deadline)
        events = []
        used = 0
        for row in rows[:MAX_ROWS]:
            used += len(json.dumps(json_safe(row)).encode())
            if used > MAX_BYTES:
                break
            event = {k: json_safe(v) for k, v in row.items() if k != "sql_text"}
            event.update(
                server_event_at=json_safe(row.get("start_time")),
                sql_shape=redact_sql(row.get("sql_text", "")),
                redaction_policy="sql-shape-v1",
                time_quality="server_session_utc",
            )
            events.append(event)
        events, seen = _deduplicate(
            events,
            source_epoch,
            cursor.get("seen_ids", []) if cursor.get("source_epoch") == source_epoch else [],
        )
        truncated = len(rows) > MAX_ROWS or used > MAX_BYTES
        coverage["reconciliation"] = "every_poll_requeries_entire_bounded_lookback"
        if truncated:
            coverage["gaps"] = ["batch_limit_or_time_watermark_density"]
        value = result(
            "mysql.slow_log.table",
            {"events": events},
            coverage=coverage,
            truncated=truncated,
            availability="degraded" if truncated else "available",
        )
        value["cursor"] = {
            "seen_ids": seen,
            "source_epoch": source_epoch,
            "reconciled_at": utc_now(),
        }
        return value
    except Exception as exc:
        return _safe_failure("mysql.slow_log.table", exc)


def _parse_slow_record(data, epoch, offset):
    text = data.decode("utf-8", errors="replace")
    banner = re.search(r"(?m)^.*mysqld, Version:.*$", text)
    if banner:
        text = text[: banner.start()]
    timing = re.search(
        r"# Query_time:\s*([\d.]+)\s+Lock_time:\s*([\d.]+)\s+Rows_sent:\s*(\d+)\s+Rows_examined:\s*(\d+)",
        text,
    )
    stamp = re.search(r"^# Time:\s*(\S+)", text, re.M)
    lines = [
        line
        for line in text.splitlines()
        if line and not line.startswith("#") and not line.startswith("SET timestamp=")
    ]
    sql = "\n".join(lines)
    if not timing or not sql.rstrip().endswith(";"):
        return None
    return {
        "event_id": hashlib.sha256(f"{epoch}:{offset}:".encode() + data).hexdigest(),
        "server_event_at": stamp.group(1) if stamp else None,
        "time_quality": "source_utc" if stamp and stamp.group(1).endswith("Z") else "uncertain",
        "query_time_seconds": float(timing.group(1)),
        "lock_time_seconds": float(timing.group(2)),
        "rows_sent": int(timing.group(3)),
        "rows_examined": int(timing.group(4)),
        "sql_shape": redact_sql(sql),
        "redaction_policy": "sql-shape-v1",
        "source_offset": offset,
    }


def read_slow_file(
    sftp,
    path,
    allowed_paths,
    cursor=None,
    *,
    initial_mode="tail",
    max_bytes=MAX_BYTES,
    file_identity=None,
    deadline=None,
):
    """SFTP file reader; fingerprint-only identity explicitly cannot prove no loss.

    file_identity may be a trusted (device,inode) value obtained by the fixed
    host protocol, never a client-supplied assertion of complete coverage.
    """
    cursor = dict(cursor or {})
    try:
        remaining(deadline)
        normalized = sftp.normalize(path)
        # Approved entries are canonical paths saved at preflight. Resolving
        # them again would let a changed symlink redefine the allowlist itself.
        allowed = {posixpath.normpath(value.replace("\\", "/")) for value in allowed_paths}
        if normalized not in allowed:
            return result(
                "mysql.slow_log.file",
                availability="permission_denied",
                error_code="path_not_allowed",
            )
        stat = sftp.stat(normalized)
        size = int(stat.st_size)
        gaps = []
        epoch = cursor.get("source_epoch") or str(uuid.uuid4())
        offset = int(
            cursor.get("offset", size if initial_mode == "tail" else max(0, size - MAX_BYTES))
        )
        imported = not bool(cursor) and initial_mode == "recent"
        identity_confidence = "device_inode" if file_identity is not None else "fingerprint_only"
        if not file_identity:
            gaps.append("sftp_no_stable_inode_rotation_cannot_be_proven_lossless")
        if not cursor:
            gaps.append("initial_tail_boundary" if not imported else "bounded_initial_import")
        with sftp.open(normalized, "rb") as stream:
            stream.seek(0)
            anchor_len = int(cursor.get("prefix_length", min(size, 256)))
            anchor = stream.read(anchor_len)
            prefix = hashlib.sha256(anchor).hexdigest()
            rotated = bool(
                cursor
                and cursor.get("file_identity") is not None
                and file_identity != cursor["file_identity"]
            )
            changed_prefix = bool(
                cursor.get("prefix_length", 0) and cursor.get("prefix_sha256") != prefix
            )
            if size < offset:
                gaps.append("copytruncate")
                rotated = True
            elif changed_prefix:
                gaps.append("file_replaced_or_prefix_changed")
                rotated = True
            if rotated:
                epoch = str(uuid.uuid4())
                offset = 0
                cursor.pop("tail_b64", None)
                gaps.append("rotation_old_file_not_reconciled")
            stream.seek(offset)
            block = stream.read(max(1, min(MAX_BYTES, int(max_bytes))))
            tail = base64.b64decode(cursor.get("tail_b64", ""))
            start_offset = offset - len(tail)
            data = tail + block
            if b"mysqld, Version:" in data:
                gaps.append("server_restart_banner_seen")
            positions = [m.start() for m in re.finditer(rb"(?m)^# Time: ", data)]
            events = []
            errors = 0
            consumed = 0
            for index, start in enumerate(positions):
                end = positions[index + 1] if index + 1 < len(positions) else len(data)
                record = data[start:end]
                event = _parse_slow_record(record, epoch, start_offset + start)
                if event is None and index == len(positions) - 1:
                    consumed = start
                    break
                if event is None:
                    errors += 1
                else:
                    events.append(event)
                consumed = end
                if len(events) >= MAX_ROWS:
                    break
            remainder = data[consumed:]
            if not positions and len(remainder) > 65536:
                errors += 1
                gaps.append("unparsed_or_oversized_record")
                remainder = b""
            if len(remainder) > 65536:
                errors += 1
                gaps.append("tail_buffer_limit")
                remainder = b""
            # If 1,000 records were consumed, preserve the unread bytes on the source.
            if len(events) >= MAX_ROWS and consumed < len(data):
                new_offset = start_offset + consumed
                remainder = b""
            else:
                new_offset = offset + len(block)
            stream.seek(0)
            anchor_len = min(size, 256)
            prefix = hashlib.sha256(stream.read(anchor_len)).hexdigest()
        truncated = new_offset < size or errors > 0
        if new_offset < size:
            gaps.append("batch_byte_or_row_limit_backlog")
        coverage = {
            "complete": False,
            "gaps": gaps,
            "identity_confidence": identity_confidence,
            "imported": imported,
            "cursor_start": offset,
            "cursor_end": new_offset,
            "file_size": size,
            "parser_version": PARSER_VERSION,
            "late_arrival_possible": False,
            "ingest_error_count": errors,
            "tail_bytes": len(remainder),
        }
        value = result(
            "mysql.slow_log.file",
            {"events": events},
            coverage=coverage,
            truncated=truncated,
            availability="degraded" if truncated else "available",
        )
        value["cursor"] = {
            "path": normalized,
            "file_identity": file_identity,
            "identity_confidence": identity_confidence,
            "offset": new_offset,
            "size": size,
            "prefix_sha256": prefix,
            "prefix_length": anchor_len,
            "tail_b64": base64.b64encode(remainder).decode(),
            "source_epoch": epoch,
            "parser_version": PARSER_VERSION,
        }
        return value
    except Exception as exc:
        return _safe_failure("mysql.slow_log.file", exc)
