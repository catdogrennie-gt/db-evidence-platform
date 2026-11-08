"""Shared HTTP contracts and bounded query helpers; no secret serialization."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select, tuple_


class PlatformError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str, details=None):
        super().__init__(status_code, {"code": code, "message": message, "details": details or {}})


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds") + "Z"


def json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, (UUID,)):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    # Decimal and similar numeric database values.
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    return value


def fields(row, names: tuple[str, ...] | list[str]) -> dict:
    """Serialize only a caller-reviewed allowlist; never ORM __dict__."""
    return {name: json_safe(getattr(row, name)) for name in names}


def parse_time(value: str | datetime | None, *, default: datetime | None = None) -> datetime:
    if value is None:
        if default is None:
            raise PlatformError(422, "invalid_time", "需要有效的 UTC 时间。")
        return default
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
        if parsed.tzinfo is None:
            raise ValueError("Timezone is required")
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError):
        raise PlatformError(422, "invalid_time", "时间必须包含时区。") from None


def time_window(start=None, end=None, *, max_days=14) -> tuple[datetime, datetime]:
    current = datetime.now(timezone.utc).replace(tzinfo=None)
    upper = parse_time(end, default=current)
    lower = parse_time(start, default=upper - timedelta(hours=1))
    if lower >= upper or upper - lower > timedelta(days=max_days):
        raise PlatformError(422, "invalid_time_range", f"时间范围必须递增且不超过 {max_days} 天。")
    if upper > current + timedelta(minutes=1):
        raise PlatformError(422, "future_time_range", "查询窗口不能超出当前时间。")
    return lower, upper


def bounded_limit(value: int) -> int:
    if not 1 <= value <= 100:
        raise PlatformError(422, "invalid_limit", "每页条数必须为 1–100。")
    return value


def encode_cursor(timestamp: datetime, identifier: str) -> str:
    raw = json.dumps([iso(timestamp), identifier], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        if len(value) > 256:
            raise ValueError("Cursor too large")
        timestamp, identifier = json.loads(
            base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        )
        UUID(identifier)
        return parse_time(timestamp), identifier
    except (ValueError, TypeError, json.JSONDecodeError):
        raise PlatformError(422, "invalid_cursor", "分页游标无效。") from None


def page_query(db, statement, model, *, limit=50, cursor=None, time_column="created_at"):
    """The caller MUST scope statement before calling this function."""
    bounded_limit(limit)
    total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0
    column = getattr(model, time_column)
    if cursor:
        stamp, identifier = decode_cursor(cursor)
        statement = statement.where(tuple_(column, model.id) < tuple_(stamp, identifier))
    rows = list(db.scalars(statement.order_by(column.desc(), model.id.desc()).limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(getattr(rows[-1], time_column), rows[-1].id) if more else None
    return rows, int(total), next_cursor
