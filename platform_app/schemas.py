"""Small shared HTTP envelopes; domain routes own their detailed input models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer

T = TypeVar("T")


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_serializer("*", when_used="json", check_fields=False)
    def utc_dates(self, value: Any):
        if isinstance(value, datetime):
            return (
                value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
                if value.tzinfo is None
                else value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            )
        return value


class Page(APIModel, Generic[T]):
    items: list[T]
    total: int
    next_cursor: str | None = None


class TaskRef(APIModel):
    id: str
    type: str
    status: str
    created_at: datetime
    poll_url: str


class Capability(APIModel):
    collector: str
    availability: str
    reason_code: str | None = None
    checked_at: datetime


class EvidenceRef(APIModel):
    id: str
    instance_id: str
    run_id: str
    source: str
    source_mode: str
    observed_at: datetime
    ingested_at: datetime
    schema_version: str
    sensitivity: str
    coverage: dict = Field(default_factory=dict)
