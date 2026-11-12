"""Bounded historical evidence tools. No target connector, SQL or shell access."""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import json
from collections import Counter
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import object_session

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
IDENTITY_FIELDS = {
    'waiting_trx_id','blocking_trx_id','waiting_thread_id','blocking_thread_id',
    'TRX_ID','TRX_MYSQL_THREAD_ID','THREAD_ID','PROCESSLIST_ID',
}
DIGEST_FIELDS = {
    "COUNT_STAR",
    "SUM_TIMER_WAIT",
    "MAX_TIMER_WAIT",
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
METRIC_FIELDS.update({"host_cpu_percent", "host_disk_read_bytes_per_second", "host_disk_write_bytes_per_second", "probe_latency_ms", "probe_availability"})
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
    manifest_item_ids: list[str] | None = Field(default=None,max_length=32)


def tool_definitions(names=None):
    # The same verbose generated schema repeated per tool used to evict real
    # evidence. Keep the exact validation contract without titles/defaults.
    schema = {'type':'object','additionalProperties':False,'required':['instance_id'],'properties':{
        'instance_id':{'type':'string'},'from':{'type':'string'},'to':{'type':'string'},
        'limit':{'type':'integer','minimum':1,'maximum':100},
        'evidence_ids':{'type':'array','items':{'type':'string'},'maxItems':100},
        'manifest_item_ids':{'type':'array','items':{'type':'string'},'maxItems':32,
            'description':'Exact metric evidence_id (manifest:UUID); legacy bare UUID also accepted.'},
    }}
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": "Read authorized frozen evidence. Missing is unknown; metric windows accept exact manifest_item_ids.",
                "parameters": dict(schema,properties={key:value for key,value in schema['properties'].items() if (key!='manifest_item_ids' or name=='get_metric_window') and (key!='evidence_ids' or name!='get_metric_window')}),
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


def _identity_value(value):
    """Identity is not a measurement: never round decimal uint64s via float."""
    if isinstance(value,bool): return None
    if isinstance(value,str):
        if not re.fullmatch(r'[0-9]{1,20}',value): return None
        value=int(value)
    elif isinstance(value,float):
        # Already-inexact large floats cannot be reconstructed honestly.
        if not math.isfinite(value) or not value.is_integer() or not 0<=value<=9007199254740991: return None
        value=int(value)
    elif not isinstance(value,int): return None
    if not 0<=value<=18446744073709551615: return None
    # JSON consumers commonly use IEEE doubles. Keep larger IDs as decimal text.
    return value if value<=9007199254740991 else str(value)


def _numeric_fields(row, keys):
    result={}
    for key in keys:
        if key not in row: continue
        value=_identity_value(row[key]) if key in IDENTITY_FIELDS else number(row[key])
        if value is not None: result[key]=value
    return result


def _identifier_aliaser(row):
    """Secret-keyed aliases correlate authorized structure without sending names."""
    session = object_session(row)
    config = session.info.get('platform_config') if session is not None else None
    try:
        key = config.fernet_key_path.read_bytes().strip() if config is not None else None
    except OSError:
        key = None
    def alias(value):
        if not key or not isinstance(value, str) or not value or len(value) > 255:
            return None
        message = json.dumps(['evidence-identifier-v1', row.instance_id, value], ensure_ascii=False).encode()
        return 'id_' + hmac.new(key, message, hashlib.sha256).hexdigest()[:20]
    return alias, bool(key)


SQL_KEYWORDS = frozenset('SELECT INSERT UPDATE DELETE FROM WHERE JOIN GROUP ORDER LIMIT HAVING UNION DISTINCT AND OR NOT IN IS NULL LIKE BETWEEN EXISTS AS ON SET VALUES ASC DESC USING LEFT RIGHT INNER OUTER CROSS CASE WHEN THEN ELSE END BY OFFSET FOR LOCK SHARE KEY PRIMARY INDEX FORCE USE EXPLAIN'.split())
SQL_TOKEN = re.compile(r"/\*[\s\S]*?(?:\*/|$)|--[^\n]*|\#[^\n]*|'(?:''|\\.|[^'\\])*(?:'|$)|\"(?:\"\"|\\.|[^\"\\])*(?:\"|$)|`(?:``|[^`])*(?:`|$)|\b(?:0[xX][0-9a-fA-F]+|0[bB][01]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b|[a-zA-Z_$][a-zA-Z0-9_$]*|<=|>=|<>|!=|[(),.=<>+*/%?-]")


def _sql_structure(value, alias):
    result = []
    for match in SQL_TOKEN.finditer(str(value)[:4096]):
        token = match.group()
        if token.startswith(('/*', '--', '#')): continue
        if token.startswith(("'", '"')) or token[0].isdigit(): token = '?'
        elif token.startswith('`'): token = alias(token[1:-1].replace('``', '`')) or 'identifier'
        elif re.match(r'[a-zA-Z_$]', token): token = token.upper() if token.upper() in SQL_KEYWORDS else alias(token) or 'identifier'
        result.append(token)
    return ' '.join(result)[:2048]


def _structure_value(item, names):
    """Known field names only; conflicting spellings have no trusted winner."""
    values = [value for key, value in item.items() if isinstance(key, str) and key.upper() in names]
    if not values or not all(isinstance(value, str) and value == values[0] for value in values):
        return None
    return values[0]


def _structure_fields(item, alias):
    result = {}
    for output, names in {'schema_ref': ('OBJECT_SCHEMA', 'TABLE_SCHEMA', 'SCHEMA_NAME'), 'table_ref': ('OBJECT_NAME', 'TABLE_NAME'), 'index_ref': ('INDEX_NAME',), 'column_ref': ('COLUMN_NAME',)}.items():
        value = _structure_value(item, names)
        identifier = alias(value)
        if identifier: result[output] = identifier
    for name, values in {'LOCK_MODE': {'X','S','IX','IS','AUTO_INC','X,REC_NOT_GAP','S,REC_NOT_GAP','X,GAP','S,GAP'}, 'LOCK_STATUS': {'WAITING','GRANTED'}, 'LOCK_TYPE': {'RECORD','TABLE'}, 'TRX_STATE': {'RUNNING','LOCK WAIT','ROLLING BACK','COMMITTING'}, 'INDEX_TYPE': {'BTREE','HASH','FULLTEXT','RTREE'}}.items():
        value = _structure_value(item, (name,))
        if value in values: result[name] = value
    return result


def _verified_requested_lock_attributes(row, payload):
    """The verified v2 producer joins attributes on REQUESTING_ENGINE_LOCK_ID.

    Its current snapshot contract is independent of truncated history. Preserve
    those history gaps in coverage; never infer roles from an item name alone.
    """
    contract = 'db-evidence-lock-snapshot-v2'
    coverage = row.coverage if isinstance(row.coverage, dict) else {}
    item = payload.get('item') if isinstance(payload.get('item'), dict) else {}
    rows = payload.get('rows')
    count = coverage.get('snapshot_total_row_count')
    tags = item.get('tags') if isinstance(item.get('tags'), list) else []
    declared = [tag.get('value') for tag in tags
                if isinstance(tag, dict) and tag.get('tag') == 'sl.snapshot_contract']
    return (row.source == 'mysql.lock_waits'
            and item.get('enhancement') == 'mysql.lock_waits'
            and bool(declared) and all(value == contract for value in declared)
            and coverage.get('snapshot_contract') == contract
            and coverage.get('snapshot_complete') is True
            and coverage.get('snapshot_truncated') is False
            and type(count) is int and 0 <= count <= 50
            and isinstance(rows, list) and len(rows) == count
            and all(isinstance(value, dict) for value in rows))


def project_evidence(row: Evidence) -> dict:
    """Build from field allowlists; never sanitize and forward an entire payload."""
    payload = row.payload if isinstance(row.payload, dict) else {}
    raw_rows = payload.get("rows", [])
    raw_rows = (
        [r for r in raw_rows[:100] if isinstance(r, dict)] if isinstance(raw_rows, list) else []
    )
    data = {"row_count": len(raw_rows)}
    alias, aliases_available = _identifier_aliaser(row)
    if row.source.startswith('zabbix.item.'):
        from .zabbix_items import safe_item
        item=payload.get('item') if isinstance(payload.get('item'),dict) else {}
        data['item']=safe_item(item)
        if row.source.startswith('zabbix.item.numeric:') and item.get('value_kind')=='numeric':
            data['rows']=[{'time':iso(parse_time(r['time'])),'value':number(r.get('value'))} for r in raw_rows if r.get('time') and number(r.get('value')) is not None]
        else:
            data['text_omitted']=True
            data['value_kind']=item.get('value_kind') if item.get('value_kind') in ('text','log','unsupported') else 'unknown'
            data['reason']='raw_text_not_authorized_for_external_model'
    elif row.source in ("mysql.lock_waits", "mysql.transactions", "mysql.sessions"):
        data["rows"] = [_numeric_fields(r, LOCK_FIELDS) | _structure_fields(r, alias) for r in raw_rows]
        if _verified_requested_lock_attributes(row, payload):
            for projected in data['rows']:
                for name in ('LOCK_TYPE', 'LOCK_MODE', 'LOCK_STATUS'):
                    if name in projected:
                        projected['requested_' + name.lower()] = projected.pop(name)
    elif row.source == "mysql.statement_digest":
        data["rows"] = []
        units = payload.get("field_units") or {}
        data["timer_unit"] = "ps" if units.get("SUM_TIMER_WAIT") == "ps" or payload.get("timer_unit") == "picoseconds" else "unknown"
        data["snapshot_scope"] = code(payload.get("snapshot_scope"), "unknown")
        window = payload.get("window") or {}
        data["window"] = {"valid": window.get("valid") is True, "window_seconds": number(window.get("window_seconds")), "reason": code(window.get("reason")), "rows": [_numeric_fields(r, {"count_delta", "total_time_seconds_delta", "avg_latency_seconds"}) | ({"digest": r["DIGEST"]} if isinstance(r.get("DIGEST"), str) and re.fullmatch(r"[a-fA-F0-9]{16,64}", r["DIGEST"]) else {}) for r in window.get("rows", [])[:30] if isinstance(r, dict)]}
        for item in raw_rows:
            projected = _numeric_fields(item, DIGEST_FIELDS)
            projected.update(_structure_fields(item, alias))
            digest = item.get("DIGEST", "")
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{16,64}", digest):
                projected["digest"] = digest
            text = item.get("DIGEST_TEXT", "")
            projected['sql_shape'] = _sql_structure(text, alias)
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
            ) | _structure_fields(r, alias)
            for r in raw_rows
        ]
        data["metadata_names_omitted"] = True
        data['identifier_policy'] = 'secret_keyed_instance_aliases_v1' if aliases_available else 'names_omitted_no_alias_key'
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
    elif row.source == "linux.host":
        data["values"] = _numeric_fields(payload.get("values") or {}, METRIC_FIELDS)
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
    if isinstance(payload.get('item'),dict):
        from .zabbix_items import safe_item
        data['item']=safe_item(payload['item'])
        source_id=coverage.get('source_id')
        if isinstance(source_id,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}',source_id):
            data['item']['source_id']=source_id
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
    generic=payload.get('template')=='zabbix_item_numeric' and payload.get('owner_collector')=='monitoring' and isinstance(payload.get('item'),dict) and payload['item'].get('value_kind')=='numeric'
    if not generic and (definition is None or payload.get("owner_collector") != definition.owner_collector):
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
    original_coverage = payload.get('coverage') if isinstance(payload.get('coverage'), dict) else {}
    for gap in [*(payload.get('gaps') or []), *(original_coverage.get('gaps') or []), *(original_coverage.get('reason_codes') or [])][:100]:
        if isinstance(gap, str): safe_gap = code(gap)
        elif isinstance(gap, dict):
            safe_gap = {'reason_code': code(gap.get('reason_code', gap.get('reason')))}
            for field in ('from', 'to'):
                try: safe_gap[field] = iso(parse_time(gap[field]))
                except (KeyError, TypeError, ValueError): pass
        else: continue
        if safe_gap not in gaps: gaps.append(safe_gap)
    truncated = original_coverage.get('truncated') is True
    provenance = {key: value for key, value in (payload.get('provenance') or {}).items() if key in ('source_id','hostid','itemid','mapping_version','config_revision','transport') and isinstance(value, (str, int))}
    from .zabbix_items import safe_item,safe_text
    result = {
        "evidence_id": "manifest:" + row.id,
        "manifest_item_id": row.id,
        "instance_id": row.instance_id,
        "template": 'zabbix_item_numeric' if generic else definition.metric_id,
        "collector": 'monitoring' if generic else definition.owner_collector,
        "unit": safe_text(payload.get('unit')) if generic else definition.unit,
        "source": "zabbix_history" if row.item_type == "time_series_window" else "prometheus_resampled",
        "points": points,
        "coverage": {"complete": bool(points) and not gaps and not truncated and original_coverage.get('complete') is not False, "gaps": gaps, 'truncated': truncated},
        'provenance': provenance,
        'window': {'from': iso(start), 'to': iso(end)},
        "payload_sha256": row.payload_sha256,
    }
    if generic:
        result['item']=safe_item(payload['item'])
        from .zabbix_items import safe_relations
        result['registered_relationships']=safe_relations(payload)
        result['id']=safe_text(payload.get('id'),160)
        result['title']=result['item'].get('host_name','')+' / '+result['item'].get('name','')
        result['object_name']=safe_text(payload.get('object_name'))
    if isinstance(original_coverage.get('sampling'),dict):
        sampling={k:v for k,v in original_coverage['sampling'].items() if k in ('method','original_point_count','retained_point_count','original_valid_point_count','retained_valid_point_count','minimum_required_point_count','endpoints_preserved','gap_boundaries_preserved') and isinstance(v,(int,bool,str))}
        result['coverage']['sampling']=sampling
    return result


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
                                ManifestItem.item_type.in_(["prometheus_window", "time_series_window"]),
                            )
                            .limit(192)
                        )
                    )
                    if self.investigation.manifest_id
                    else []
                )
                if args.manifest_item_ids is not None:
                    wanted=set()
                    for reference in args.manifest_item_ids:
                        if reference.startswith('manifest:'):
                            identifier=reference[len('manifest:'):]
                            try:
                                if str(UUID(identifier))!=identifier:raise ValueError
                            except ValueError:
                                raise PlatformError(404,'resource_not_found','冻结指标引用不属于本调查对象。') from None
                            wanted.add(identifier)
                        else:
                            wanted.add(reference)
                    frozen=[row for row in frozen if row.id in wanted]
                    if wanted!={row.id for row in frozen}: raise PlatformError(404,'resource_not_found','冻结指标引用不属于本调查对象。')
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
                items = []
                for row in db.scalars(
                    query.order_by(Evidence.observed_at, Evidence.id).limit(args.limit)
                ):
                    projected = project_evidence(row)
                    if row.source.startswith('zabbix.item.numeric:'):
                        # observed_at bounds the snapshot, not its history rows.
                        # Clip before counting/sampling, including narrower tool windows.
                        from .model_context import compact_numeric_evidence
                        projected = compact_numeric_evidence(
                            projected, window_start=lower, window_end=upper
                        )
                    items.append(projected)
            limited = []
            truncated = False
            for item in items:
                item = dict(item)
                if 'points' in item and len(json.dumps(item,ensure_ascii=False,separators=(',',':')).encode())>self.max_result_bytes:
                    from .model_context import compact_tool_item
                    item=compact_tool_item(item)
                    truncated=True
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
                        item['coverage']=dict(item.get('coverage') or {},complete=False,truncated=True)
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
                {code(gap.get('reason_code',gap.get('reason'))) if isinstance(gap,dict) else code(gap)
                 for item in items for gap in item.get("coverage", {}).get("gaps", [])}
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
