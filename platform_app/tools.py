"""Bounded historical evidence tools. No target connector, SQL or shell access."""

from __future__ import annotations

import hashlib
import math
import re
import json
from collections import Counter

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from .access import authorize
from .common import PlatformError, iso, parse_time
from .models import Evidence, MetricFrame, ToolCall, ManifestItem

TOOL_SOURCES = {
    "get_lock_graph": {"mysql.lock_waits", "mysql.transactions", "mysql.sessions"},
    "get_statement_digests": {"mysql.statement_digest"},
    "get_schema_metadata": {"mysql.tables", "mysql.indexes", "mysql.configuration"},
    "search_log_events": {
        "mysql.error_log",
        "mysql.slow_log",
        "mysql.slow_log_table",
        "mysql.slow_log_file",
        "host.error_log",
        "host.slow_log",
    },
    "get_evidence": None,
    "get_metric_window": set(),
}
LOCK_FIELDS = {
    "waiting_trx_id",
    "blocking_trx_id",
    "waiting_thread_id",
    "blocking_thread_id",
    "TRX_ID",
    "TRX_MYSQL_THREAD_ID",
    "TRX_ROWS_LOCKED",
    "TRX_ROWS_MODIFIED",
    "THREAD_ID",
    "PROCESSLIST_ID",
    "PROCESSLIST_TIME",
}
DIGEST_FIELDS = {
    "COUNT_STAR",
    "SUM_TIMER_WAIT",
    "SUM_LOCK_TIME",
    "SUM_ROWS_SENT",
    "SUM_ROWS_EXAMINED",
    "SUM_CREATED_TMP_TABLES",
    "SUM_CREATED_TMP_DISK_TABLES",
    "delta_count",
    "delta_timer_wait",
}
METRIC_FIELDS = {
    "Threads_connected",
    "Threads_running",
    "Questions",
    "Queries",
    "Uptime",
    "Connections",
    "Slow_queries",
    "Aborted_connects",
    "Max_used_connections",
    "Innodb_buffer_pool_read_requests",
    "Innodb_buffer_pool_reads",
    "Innodb_data_read",
    "Innodb_data_written",
    "Innodb_row_lock_current_waits",
    "Innodb_row_lock_waits",
    "Innodb_row_lock_time",
    "transactions",
    "lock_waits",
    "sessions",
    "connections",
    "threads_running",
    "questions_per_second",
    "queries_per_second",
    "buffer_pool_hit_ratio",
    "row_lock_waits_per_second",
    "row_lock_time_ms_per_second",
    "cpu_percent",
    "memory_used_percent",
    "load1",
    "memory_available_bytes",
    "memory_total_bytes",
}
from .metrics_derivation import REGISTRY as _METRIC_REGISTRY

METRIC_FIELDS.update(_METRIC_REGISTRY)
CONFIG_NUMERIC = {
    "max_connections",
    "innodb_buffer_pool_size",
    "innodb_flush_log_at_trx_commit",
    "sync_binlog",
    "long_query_time",
    "slow_query_log",
    "performance_schema",
}


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    instance_id: str
    from_time: str | None = Field(default=None, alias="from")
    to_time: str | None = Field(default=None, alias="to")
    limit: int = Field(default=30, ge=1, le=100)
    evidence_ids: list[str] | None = Field(default=None, max_length=100)


def tool_definitions(names=None):
    schema = ToolArguments.model_json_schema(by_alias=True)
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": "Read only already-frozen historical evidence; missing observations are unknown.",
                "parameters": schema,
            },
        }
        for name in TOOL_SOURCES
        if names is None or name in names
    ]


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def code(value, default="unknown"):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", value)
        else default
    )


def _numeric_fields(row, keys):
    return {key: number(row[key]) for key in keys if key in row and number(row[key]) is not None}


def project_evidence(row: Evidence) -> dict:
    """Build from field allowlists; never sanitize and forward an entire payload."""
    payload = row.payload if isinstance(row.payload, dict) else {}
    raw_rows = payload.get("rows", [])
    raw_rows = (
        [r for r in raw_rows[:100] if isinstance(r, dict)] if isinstance(raw_rows, list) else []
    )
    data = {"row_count": len(raw_rows)}
    if row.source in ("mysql.lock_waits", "mysql.transactions", "mysql.sessions"):
        data["rows"] = [_numeric_fields(r, LOCK_FIELDS) for r in raw_rows]
    elif row.source == "mysql.statement_digest":
        data["rows"] = []
        for item in raw_rows:
            projected = _numeric_fields(item, DIGEST_FIELDS)
            digest = item.get("DIGEST", "")
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{16,64}", digest):
                projected["digest"] = digest
            text = item.get("DIGEST_TEXT", "")
            # SQL keywords retain shape without sending identifiers, literals, comments or SQL text.
            projected["statement_shape"] = re.findall(
                r"\b(?:SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|JOIN|GROUP|ORDER|LIMIT|HAVING|UNION|DISTINCT)\b",
                str(text).upper(),
            )[:30]
            data["rows"].append(projected)
    elif row.source in ("mysql.tables", "mysql.indexes"):
        data["rows"] = [
            _numeric_fields(
                r,
                {
                    "TABLE_ROWS",
                    "DATA_LENGTH",
                    "INDEX_LENGTH",
                    "DATA_FREE",
                    "NON_UNIQUE",
                    "SEQ_IN_INDEX",
                    "CARDINALITY",
                },
            )
            for r in raw_rows
        ]
        data["metadata_names_omitted"] = True
    elif row.source == "mysql.configuration":
        data["variables"] = {
            r["VARIABLE_NAME"]: number(r.get("VARIABLE_VALUE"))
            for r in raw_rows
            if r.get("VARIABLE_NAME") in CONFIG_NUMERIC
            and number(r.get("VARIABLE_VALUE")) is not None
        }
    elif row.source in TOOL_SOURCES["search_log_events"]:
        events = payload.get("events", [])
        events = (
            [v for v in events[:100] if isinstance(v, dict)] if isinstance(events, list) else []
        )
        data = {
            "event_count": len(events),
            "severity_counts": dict(
                Counter(code(v.get("severity", str(v.get("priority", "unknown")))) for v in events)
            ),
            "error_codes": list(
                dict.fromkeys(code(v.get("code", v.get("error_code"))) for v in events)
            )[:20],
            "text_omitted": True,
        }
    elif row.source in ("mysql.status", "mysql.global_status"):
        data["values"] = {
            r.get("VARIABLE_NAME", r.get("Variable_name")): number(
                r.get("VARIABLE_VALUE", r.get("Value"))
            )
            for r in raw_rows
            if r.get("VARIABLE_NAME", r.get("Variable_name")) in METRIC_FIELDS
            and number(r.get("VARIABLE_VALUE", r.get("Value"))) is not None
        }
    else:
        data["payload_omitted"] = True
    coverage = row.coverage if isinstance(row.coverage, dict) else {}
    availability = code(coverage.get("availability"))
    if availability not in ("available", "degraded", "unknown"):
        data = {"observation_unavailable": True}
    return {
        "evidence_id": row.id,
        "instance_id": row.instance_id,
        "source": code(row.source),
        "source_mode": row.source_mode,
        "observed_at": iso(row.observed_at),
        "coverage": {
            "complete": coverage.get("complete") is True,
            "truncated": bool(row.truncated),
            "availability": availability,
            "gaps": [code(v) for v in coverage.get("gaps", [])[:20]]
            if isinstance(coverage.get("gaps", []), list)
            else [],
        },
        "data": data,
    }


def project_metric_window(row, start, end):
    from .metrics import METRIC_TEMPLATES

    payload = row.payload or {}
    definition = METRIC_TEMPLATES.get(payload.get("template"))
    if definition is None or payload.get("owner_collector") != definition.owner_collector:
        return None
    points = []
    for point in payload.get("points", [])[:10000]:
        if not isinstance(point, dict):
            continue
        try:
            stamp = parse_time(point.get("time"))
        except Exception:
            continue
        if not start <= stamp < end:
            continue
        validity = code(point.get("validity"))
        value = number(point.get("value")) if validity == "valid" else None
        points.append({"time": iso(stamp), "value": value, "validity": validity})
    gaps = sorted({p["validity"] for p in points if p["value"] is None})
    return {
        "evidence_id": "manifest:" + row.id,
        "manifest_item_id": row.id,
        "instance_id": row.instance_id,
        "template": definition.metric_id,
        "collector": definition.owner_collector,
        "unit": definition.unit,
        "source": "prometheus_resampled",
        "points": points,
        "coverage": {"complete": bool(points) and not gaps, "gaps": gaps},
        "payload_sha256": row.payload_sha256,
    }


class ToolBroker:
    def __init__(
        self,
        actor,
        investigation,
        instance_ids,
        start,
        end,
        *,
        allowed_evidence_ids,
        allowed_metric_run_ids=(),
        max_calls=6,
        live=False,
        check_running=None,
        max_result_bytes=6000,
        allowed_tools=None,
    ):
        self.actor = actor
        self.investigation = investigation
        self.instance_ids = frozenset(instance_ids)
        self.start = start
        self.end = end
        self.evidence_ids = frozenset(allowed_evidence_ids)
        self.metric_ids = frozenset(allowed_metric_run_ids)
        self.max_calls = min(max_calls, 6)
        self.calls = 0
        self.live = live
        self.safety_check = check_running
        self.max_result_bytes = min(6000, max(1000, max_result_bytes))
        self.allowed_tools = frozenset(allowed_tools) if allowed_tools is not None else None

    def call(self, name: str, arguments: dict) -> dict:
        db = self.actor.db
        sequence = (
            db.scalar(
                select(func.max(ToolCall.sequence)).where(
                    ToolCall.investigation_id == self.investigation.id
                )
            )
            or 0
        ) + 1
        # Record only approved parameter fields after validation; rejected arbitrary SQL/text is not persisted.
        audit = ToolCall(
            investigation_id=self.investigation.id,
            sequence=sequence,
            tool=code(name),
            parameters={},
            status="denied",
        )
        db.add(audit)
        try:
            if name not in TOOL_SOURCES or (
                self.allowed_tools is not None and name not in self.allowed_tools
            ):
                raise PlatformError(403, "tool_not_allowed", "工具不在只读白名单内。")
            try:
                args = ToolArguments.model_validate(arguments)
            except ValidationError:
                raise PlatformError(
                    422, "invalid_tool_arguments", "工具参数无效或包含未允许字段。"
                ) from None
            if self.calls >= self.max_calls:
                raise PlatformError(409, "tool_budget_exhausted", "工具调用预算已耗尽。")
            self.calls += 1
            if self.safety_check:
                self.safety_check()
            authorize(self.actor, "data.read", self.instance_ids)
            allowed_collectors = None
            if self.live:
                from .investigations import require_investigation_access

                policies = require_investigation_access(
                    self.actor, self.instance_ids, "live_redacted"
                )
                allowed_collectors = policies.get(args.instance_id, {}).get("allowed_sources", [])
            if args.instance_id not in self.instance_ids:
                raise PlatformError(404, "resource_not_found", "资源不存在。")
            lower = parse_time(args.from_time) if args.from_time else self.start
            upper = parse_time(args.to_time) if args.to_time else self.end
            if lower < self.start or upper > self.end or lower >= upper:
                raise PlatformError(403, "tool_window_exceeded", "工具不能扩大冻结历史窗口。")
            if args.evidence_ids is not None and not set(args.evidence_ids).issubset(
                self.evidence_ids
            ):
                raise PlatformError(404, "resource_not_found", "资源不存在。")
            audit.parameters = {
                "instance_id": args.instance_id,
                "from": iso(lower),
                "to": iso(upper),
                "limit": args.limit,
            }
            if name == "get_metric_window":
                frozen = (
                    list(
                        db.scalars(
                            select(ManifestItem)
                            .where(
                                ManifestItem.manifest_id == self.investigation.manifest_id,
                                ManifestItem.instance_id == args.instance_id,
                                ManifestItem.item_type == "prometheus_window",
                            )
                            .limit(192)
                        )
                    )
                    if self.investigation.manifest_id
                    else []
                )
                query = select(MetricFrame).where(
                    MetricFrame.instance_id == args.instance_id,
                    MetricFrame.run_id.in_(self.metric_ids),
                    MetricFrame.observed_at >= lower,
                    MetricFrame.observed_at < upper,
                )
                if allowed_collectors is not None:
                    query = query.where(MetricFrame.collector.in_(allowed_collectors))
                frames = db.scalars(
                    query.order_by(MetricFrame.observed_at, MetricFrame.run_id).limit(args.limit)
                )
                items = []
                for frame in () if frozen else frames:
                    derived = frame.derived_values or {}
                    values = derived.get("values", derived)
                    items.append(
                        {
                            "run_id": frame.run_id,
                            "instance_id": frame.instance_id,
                            "collector": code(frame.collector),
                            "observed_at": iso(frame.observed_at),
                            "values": _numeric_fields(values, METRIC_FIELDS),
                            "rate_validity": code(frame.rate_validity),
                        }
                    )
                if frozen:
                    order = {
                        "lock_waits": 0,
                        "transactions": 1,
                        "connections": 2,
                        "threads_running": 3,
                        "questions_per_second": 4,
                    }
                    frozen.sort(
                        key=lambda r: (
                            order.get((r.payload or {}).get("template"), 10),
                            (r.payload or {}).get("template", ""),
                        )
                    )
                    for row in frozen[: args.limit]:
                        if (
                            allowed_collectors is not None
                            and (row.payload or {}).get("owner_collector") not in allowed_collectors
                        ):
                            continue
                        projected = project_metric_window(row, lower, upper)
                        if projected is not None:
                            items.append(projected)
            else:
                query = select(Evidence).where(
                    Evidence.instance_id == args.instance_id,
                    Evidence.id.in_(
                        args.evidence_ids if args.evidence_ids is not None else self.evidence_ids
                    ),
                    Evidence.observed_at >= lower,
                    Evidence.observed_at < upper,
                )
                if allowed_collectors is not None:
                    from .models import CollectionRun

                    query = query.join(CollectionRun, Evidence.run_id == CollectionRun.id).where(
                        CollectionRun.collector.in_(allowed_collectors)
                    )
                if TOOL_SOURCES[name] is not None:
                    query = query.where(Evidence.source.in_(TOOL_SOURCES[name]))
                items = [
                    project_evidence(r)
                    for r in db.scalars(
                        query.order_by(Evidence.observed_at, Evidence.id).limit(args.limit)
                    )
                ]
            limited = []
            truncated = False
            for item in items:
                item = dict(item)
                while (
                    len(
                        json.dumps(
                            limited + [item], ensure_ascii=False, separators=(",", ":")
                        ).encode()
                    )
                    > self.max_result_bytes
                ):
                    field = "points" if "points" in item else None
                    if field and item[field]:
                        item[field] = item[field][:-1]
                        truncated = True
                    else:
                        break
                if (
                    len(
                        json.dumps(
                            limited + [item], ensure_ascii=False, separators=(",", ":")
                        ).encode()
                    )
                    > self.max_result_bytes
                ):
                    truncated = True
                    break
                limited.append(item)
            items = limited
            audit.status = "succeeded"
            gaps = sorted(
                {gap for item in items for gap in item.get("coverage", {}).get("gaps", [])}
            )
            if not items:
                gaps.append("no_frozen_observations")
            if truncated:
                gaps.append("tool_result_limit")
            result = {
                "items": items,
                "coverage": {
                    "complete": bool(items)
                    and not gaps
                    and all(i.get("coverage", {}).get("complete") for i in items),
                    "gaps": gaps,
                    "truncated": truncated,
                    "limit": args.limit,
                },
                "from": iso(lower),
                "to": iso(upper),
            }
            audit.result_ref = "sha256:" + hashlib.sha256(str(result).encode()).hexdigest()
            db.flush()
            return result
        except Exception:
            db.flush()
            # A denied request is itself durable audit evidence. The enclosing
            # investigation failure rolls back its result transaction afterwards.
            db.commit()
            raise
