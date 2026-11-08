"""Structured control schema; public IDs are UUIDs, fact timestamps are UTC.

JSON columns hold extensible source payloads; ownership, lifecycle, time, keys and
query dimensions stay structured. Migrations are the deployment authority.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    CheckConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow

UTC = DateTime().with_variant(DATETIME(fsp=6), "mysql")
BIG = BigInteger().with_variant(Integer, "sqlite")


def uuidstr():
    return str(uuid4())


class IDMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuidstr)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTC, default=utcnow, onupdate=utcnow)


class Department(IDMixin, TimestampMixin, Base):
    __tablename__ = "departments"
    name: Mapped[str] = mapped_column(String(120), unique=True)
    active: Mapped[bool] = mapped_column(default=True)


class System(IDMixin, TimestampMixin, Base):
    __tablename__ = "systems"
    department_id: Mapped[str] = mapped_column(ForeignKey("departments.id"))
    name: Mapped[str] = mapped_column(String(120))
    environment: Mapped[str] = mapped_column(String(30), default="lab")
    active: Mapped[bool] = mapped_column(default=True)
    model_policy: Mapped[str] = mapped_column(String(30), default="rules_only")
    policy_version: Mapped[int] = mapped_column(default=1)
    __table_args__ = (
        UniqueConstraint("department_id", "name"),
        Index("ix_systems_department_active", "department_id", "active", "id"),
    )


class User(IDMixin, TimestampMixin, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(100), unique=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(default=True)
    permission_version: Mapped[int] = mapped_column(default=1)


class Session(IDMixin, Base):
    __tablename__ = "sessions"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    permission_version: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTC)
    revoked_at: Mapped[datetime | None] = mapped_column(UTC)
    __table_args__ = (Index("ix_sessions_user_expiry", "user_id", "expires_at"),)


class LoginNonce(IDMixin, Base):
    __tablename__ = "login_nonces"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(UTC)
    consumed_at: Mapped[datetime | None] = mapped_column(UTC)


class Role(IDMixin, TimestampMixin, Base):
    __tablename__ = "roles"
    name: Mapped[str] = mapped_column(String(60), unique=True)
    permissions: Mapped[list] = mapped_column(JSON, default=list)


class Group(IDMixin, TimestampMixin, Base):
    __tablename__ = "groups"
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")


class GroupMembership(Base):
    __tablename__ = "group_memberships"
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)


class GrantMixin(IDMixin, TimestampMixin):
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"))
    scope_type: Mapped[str] = mapped_column(String(20))
    scope_id: Mapped[str] = mapped_column(String(36))
    delegatable_roles: Mapped[list] = mapped_column(JSON, default=list)
    delegated_actions: Mapped[list] = mapped_column(JSON, default=list)


class UserGrant(GrantMixin, Base):
    __tablename__ = "user_grants"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", "scope_type", "scope_id"),
        Index("ix_user_grants_scope", "scope_type", "scope_id", "user_id"),
    )


class GroupGrant(GrantMixin, Base):
    __tablename__ = "group_grants"
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id"))
    __table_args__ = (
        UniqueConstraint("group_id", "role_id", "scope_type", "scope_id"),
        Index("ix_group_grants_scope", "scope_type", "scope_id", "group_id"),
    )


class Host(IDMixin, TimestampMixin, Base):
    __tablename__ = "hosts"
    name: Mapped[str] = mapped_column(String(120))
    verified_identity: Mapped[str | None] = mapped_column(String(255), unique=True)
    profile: Mapped[dict] = mapped_column(JSON, default=dict)


class Instance(IDMixin, TimestampMixin, Base):
    __tablename__ = "instances"
    system_id: Mapped[str] = mapped_column(ForeignKey("systems.id"))
    host_id: Mapped[str | None] = mapped_column(ForeignKey("hosts.id"))
    name: Mapped[str] = mapped_column(String(120))
    engine: Mapped[str] = mapped_column(String(30), default="mysql")
    endpoint: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(default=3306)
    connection_mode: Mapped[str] = mapped_column(String(20), default="direct")
    expected_server_uuid: Mapped[str | None] = mapped_column(String(64), unique=True)
    credential_ref: Mapped[str | None] = mapped_column(String(36))
    credential_version: Mapped[int] = mapped_column(default=1)
    ssh_profile_ref: Mapped[str | None] = mapped_column(String(120))
    tls_profile_ref: Mapped[str | None] = mapped_column(String(120))
    connection: Mapped[dict] = mapped_column(JSON, default=dict)
    selected_schemas: Mapped[list] = mapped_column(JSON, default=list)
    config_revision: Mapped[int] = mapped_column(default=1)
    lifecycle_state: Mapped[str] = mapped_column(String(30), default="Ready")
    enabled: Mapped[bool] = mapped_column(default=False)
    is_lab: Mapped[bool] = mapped_column(default=False)
    model_policy: Mapped[str] = mapped_column(String(30), default="rules_only")
    policy_version: Mapped[int] = mapped_column(default=1)
    health: Mapped[str] = mapped_column(String(30), default="unknown")
    last_success_at: Mapped[datetime | None] = mapped_column(UTC)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTC)
    __table_args__ = (Index("ix_instances_system_enabled", "system_id", "enabled", "id"),)


class ConnectionDraft(IDMixin, TimestampMixin, Base):
    __tablename__ = "connection_drafts"
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    system_id: Mapped[str] = mapped_column(ForeignKey("systems.id"))
    instance_id: Mapped[str | None] = mapped_column(ForeignKey("instances.id"))
    revision: Mapped[int] = mapped_column(default=1)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    secret_ref: Mapped[str | None] = mapped_column(String(36))
    secret_version: Mapped[int] = mapped_column(default=1)
    expires_at: Mapped[datetime] = mapped_column(UTC)
    __table_args__ = (
        Index("ix_connection_drafts_actor_updated", "created_by", "updated_at", "id"),
    )


class ConnectionTest(IDMixin, TimestampMixin, Base):
    __tablename__ = "connection_tests"
    draft_id: Mapped[str | None] = mapped_column(ForeignKey("connection_drafts.id"))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    system_id: Mapped[str] = mapped_column(ForeignKey("systems.id"))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    config_revision: Mapped[int] = mapped_column(default=1)
    config_fingerprint: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    secret_ref: Mapped[str] = mapped_column(String(36))
    secret_version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    server_uuid: Mapped[str | None] = mapped_column(String(64))
    source_version: Mapped[str | None] = mapped_column(String(80))
    results: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(UTC)
    consumed_at: Mapped[datetime | None] = mapped_column(UTC)
    __table_args__ = (Index("ix_connection_tests_actor_expiry", "created_by", "expires_at"),)


class CollectorCapability(IDMixin, Base):
    __tablename__ = "collector_capabilities"
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    collector: Mapped[str] = mapped_column(String(60))
    availability: Mapped[str] = mapped_column(String(30), default="unknown")
    reason_code: Mapped[str | None] = mapped_column(String(80))
    checked_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("instance_id", "collector"),)


class CollectionSchedule(IDMixin, TimestampMixin, Base):
    __tablename__ = "collection_schedules"
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    collector: Mapped[str] = mapped_column(String(60))
    interval_seconds: Mapped[int] = mapped_column(default=30)
    next_due_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    enabled: Mapped[bool] = mapped_column(default=True)
    config_revision: Mapped[int] = mapped_column(default=1)
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    __table_args__ = (
        UniqueConstraint("instance_id", "collector"),
        Index("ix_schedules_due", "enabled", "next_due_at", "id"),
    )


class Task(IDMixin, TimestampMixin, Base):
    __tablename__ = "tasks"
    type: Mapped[str] = mapped_column(String(60))
    queue: Mapped[str] = mapped_column(String(30), default="collection")
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    instance_id: Mapped[str | None] = mapped_column(ForeignKey("instances.id"))
    input_ref: Mapped[str | None] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    due_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    deadline_at: Mapped[datetime | None] = mapped_column(UTC)
    attempt: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    parent_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    started_at: Mapped[datetime | None] = mapped_column(UTC)
    finished_at: Mapped[datetime | None] = mapped_column(UTC)
    lease_until: Mapped[datetime | None] = mapped_column(UTC)
    fence: Mapped[int] = mapped_column(BIG, default=0)
    leased_by: Mapped[str | None] = mapped_column(String(100))
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(191), unique=True)
    request_hash: Mapped[str | None] = mapped_column(String(64))
    result_ref: Mapped[str | None] = mapped_column(String(255))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(255))
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        Index("ix_tasks_queue_due", "queue", "status", "due_at", "id"),
        Index("ix_tasks_queue_lease", "queue", "status", "lease_until", "id"),
        Index("ix_tasks_instance_status", "instance_id", "status"),
    )


class InstanceLease(Base):
    __tablename__ = "instance_leases"
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    leased_by: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[datetime | None] = mapped_column(UTC)
    fence: Mapped[int] = mapped_column(BIG, default=0)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    queue: Mapped[str] = mapped_column(String(30), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(UTC)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class CollectionRun(IDMixin, Base):
    __tablename__ = "collection_runs"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    collector: Mapped[str] = mapped_column(String(60))
    attempt: Mapped[int] = mapped_column(default=1)
    config_revision: Mapped[int] = mapped_column(default=1)
    fence: Mapped[int] = mapped_column(BIG, default=0)
    started_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTC)
    duration_ms: Mapped[float | None]
    status: Mapped[str] = mapped_column(String(30), default="running")
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_details: Mapped[dict] = mapped_column(JSON, default=dict)
    row_count: Mapped[int] = mapped_column(default=0)
    truncated: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (
        UniqueConstraint(
            "task_id", "attempt", "collector", name="uq_collection_runs_task_attempt_collector"
        ),
        Index("ix_runs_instance_time", "instance_id", "collector", "started_at", "id"),
    )


class Evidence(IDMixin, Base):
    __tablename__ = "evidence"
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("collection_runs.id"))
    source: Mapped[str] = mapped_column(String(60))
    source_mode: Mapped[str] = mapped_column(String(20), default="live")
    schema_version: Mapped[str] = mapped_column(String(30), default="1")
    observed_at: Mapped[datetime] = mapped_column(UTC)
    ingested_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    server_uuid: Mapped[str | None] = mapped_column(String(64))
    collector_version: Mapped[str] = mapped_column(String(30), default="1")
    sensitivity: Mapped[str] = mapped_column(String(30), default="redacted")
    coverage: Mapped[dict] = mapped_column(JSON, default=dict)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    truncated: Mapped[bool] = mapped_column(default=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTC)
    __table_args__ = (
        Index("ix_evidence_instance_source_time", "instance_id", "source", "observed_at", "id"),
        Index("ix_evidence_run", "run_id", "id"),
        Index("ix_evidence_expiry", "expires_at", "id"),
    )


class MetricFrame(Base):
    __tablename__ = "metric_frames"
    run_id: Mapped[str] = mapped_column(ForeignKey("collection_runs.id"), primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    collector: Mapped[str] = mapped_column(String(60))
    server_uuid: Mapped[str] = mapped_column(String(64))
    collector_boot_id: Mapped[str] = mapped_column(String(36))
    source_epoch: Mapped[str | None] = mapped_column(String(100))
    observed_monotonic_ns: Mapped[int] = mapped_column(BIG)
    counter_epoch: Mapped[int] = mapped_column(BIG, default=0)
    observed_at: Mapped[datetime] = mapped_column(UTC)
    server_observed_at: Mapped[datetime | None] = mapped_column(UTC)
    window_start: Mapped[datetime | None] = mapped_column(UTC)
    window_end: Mapped[datetime | None] = mapped_column(UTC)
    uptime_seconds: Mapped[int] = mapped_column(BIG)
    raw_values: Mapped[dict] = mapped_column(JSON, default=dict)
    derived_values: Mapped[dict] = mapped_column(JSON, default=dict)
    rate_validity: Mapped[str] = mapped_column(String(40), default="first_sample")
    __table_args__ = (
        Index(
            "ix_metric_frames_instance_time", "instance_id", "collector", "observed_at", "run_id"
        ),
        Index("ix_metric_frames_retention", "observed_at", "run_id"),
    )


class DigestSnapshot(IDMixin, Base):
    __tablename__ = "digest_snapshots"
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("collection_runs.id"))
    counter_epoch: Mapped[int] = mapped_column(BIG, default=0)
    schema_name: Mapped[str] = mapped_column(String(64), default="")
    digest: Mapped[str] = mapped_column(String(64))
    digest_text: Mapped[str] = mapped_column(Text, default="")
    observed_at: Mapped[datetime] = mapped_column(UTC)
    count_star: Mapped[int] = mapped_column(BIG, default=0)
    sum_timer_wait: Mapped[int] = mapped_column(BIG, default=0)
    delta_count: Mapped[int | None] = mapped_column(BIG)
    delta_timer_wait: Mapped[int | None] = mapped_column(BIG)
    __table_args__ = (
        Index(
            "ix_digest_instance_digest_time", "instance_id", "schema_name", "digest", "observed_at"
        ),
        Index("ix_digest_retention", "observed_at", "id"),
    )


class LogCursor(IDMixin, TimestampMixin, Base):
    __tablename__ = "log_cursors"
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    source: Mapped[str] = mapped_column(String(60))
    source_epoch: Mapped[str] = mapped_column(String(100), default="0")
    version: Mapped[int] = mapped_column(default=1)
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    gap_status: Mapped[str | None] = mapped_column(String(60))
    __table_args__ = (UniqueConstraint("instance_id", "source"),)


class LogEvent(IDMixin, Base):
    __tablename__ = "log_events"
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence.id"))
    source: Mapped[str] = mapped_column(String(60))
    source_epoch: Mapped[str] = mapped_column(String(100))
    source_event_key: Mapped[str] = mapped_column(String(191))
    event_time: Mapped[datetime] = mapped_column(UTC)
    ingested_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    severity: Mapped[str] = mapped_column(String(30), default="unknown")
    code: Mapped[str | None] = mapped_column(String(60))
    message_redacted: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("instance_id", "source", "source_epoch", "source_event_key"),
        Index("ix_logs_instance_time", "instance_id", "event_time", "id"),
        Index("ix_logs_instance_severity_time", "instance_id", "severity", "event_time", "id"),
    )


class Rule(IDMixin, TimestampMixin, Base):
    __tablename__ = "rules"
    name: Mapped[str] = mapped_column(String(120))
    rule_version: Mapped[str] = mapped_column(String(100), unique=True)
    template: Mapped[str] = mapped_column(String(60))
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)


class RuleEvaluation(IDMixin, Base):
    __tablename__ = "rule_evaluations"
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    rule_id: Mapped[str] = mapped_column(ForeignKey("rules.id"))
    rule_version: Mapped[str] = mapped_column(String(100))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("collection_runs.id"))
    evaluation_kind: Mapped[str] = mapped_column(String(20), default="observation")
    evaluation_key: Mapped[str] = mapped_column(String(191), unique=True)
    evaluated_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    state: Mapped[str] = mapped_column(String(30))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (Index("ix_evaluations_instance_time", "instance_id", "evaluated_at"),)


class ActiveAlert(IDMixin, TimestampMixin, Base):
    __tablename__ = "active_alerts"
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    rule_version: Mapped[str] = mapped_column(String(100))
    dimension_fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30), default="unknown")
    current_occurrence_id: Mapped[str | None] = mapped_column(String(36))
    pending_since: Mapped[datetime | None] = mapped_column(UTC)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(UTC)
    __table_args__ = (UniqueConstraint("instance_id", "rule_version", "dimension_fingerprint"),)


class AlertOccurrence(IDMixin, Base):
    __tablename__ = "alert_occurrences"
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    active_alert_id: Mapped[str] = mapped_column(ForeignKey("active_alerts.id"))
    evaluation_id: Mapped[str] = mapped_column(ForeignKey("rule_evaluations.id"))
    status: Mapped[str] = mapped_column(String(30), default="firing")
    severity: Mapped[str] = mapped_column(String(30), default="warning")
    first_seen_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    recovered_at: Mapped[datetime | None] = mapped_column(UTC)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        Index("ix_occurrences_instance_state", "instance_id", "status", "first_seen_at", "id"),
    )


class AlertTransition(IDMixin, Base):
    __tablename__ = "alert_transitions"
    occurrence_id: Mapped[str] = mapped_column(ForeignKey("alert_occurrences.id"))
    evaluation_id: Mapped[str] = mapped_column(ForeignKey("rule_evaluations.id"))
    from_state: Mapped[str | None] = mapped_column(String(30))
    to_state: Mapped[str] = mapped_column(String(30))
    occurred_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Event(IDMixin, TimestampMixin, Base):
    __tablename__ = "events"
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default="open")
    severity: Mapped[str] = mapped_column(String(30), default="warning")
    source: Mapped[str] = mapped_column(String(30), default="rule")
    assigned_to: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(default=1)
    coverage_revision: Mapped[int] = mapped_column(default=1, server_default="1")
    closure_type: Mapped[str | None] = mapped_column(String(40))
    closure_reason: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    merged_into_event_id: Mapped[str | None] = mapped_column(ForeignKey("events.id"))
    manifest_id: Mapped[str | None] = mapped_column(String(36))
    __table_args__ = (
        Index("ix_events_instance_state", "instance_id", "status", "last_seen_at", "id"),
    )


class EventAlert(Base):
    __tablename__ = "event_alerts"
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), primary_key=True)
    alert_occurrence_id: Mapped[str] = mapped_column(
        ForeignKey("alert_occurrences.id"), primary_key=True
    )


class EventTransition(IDMixin, Base):
    __tablename__ = "event_transitions"
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"))
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    evaluation_id: Mapped[str | None] = mapped_column(ForeignKey("rule_evaluations.id"))
    action: Mapped[str] = mapped_column(String(60))
    from_state: Mapped[str | None] = mapped_column(String(30))
    to_state: Mapped[str | None] = mapped_column(String(30))
    occurred_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Investigation(IDMixin, TimestampMixin, Base):
    __tablename__ = "investigations"
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    event_id: Mapped[str | None] = mapped_column(ForeignKey("events.id"))
    question: Mapped[str] = mapped_column(Text)
    window_start: Mapped[datetime] = mapped_column(UTC)
    window_end: Mapped[datetime] = mapped_column(UTC)
    policy_version: Mapped[int] = mapped_column(default=1)
    permission_version: Mapped[int]
    status: Mapped[str] = mapped_column(String(30), default="queued")
    conclusion_type: Mapped[str | None] = mapped_column(String(30))
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    manifest_id: Mapped[str | None] = mapped_column(String(36))
    input_sha256: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (Index("ix_investigations_actor_time", "created_by", "created_at", "id"),)


class InvestigationResource(Base):
    __tablename__ = "investigation_resources"
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"), primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"), primary_key=True)


class InvestigationEvidence(Base):
    __tablename__ = "investigation_evidence"
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence.id"), primary_key=True)


class RemediationPlan(IDMixin, TimestampMixin, Base):
    __tablename__ = "remediation_plans"
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    source: Mapped[str] = mapped_column(String(30), default="manual")
    prerequisites: Mapped[list] = mapped_column(JSON, default=list)
    risks: Mapped[list] = mapped_column(JSON, default=list)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    rollback: Mapped[list] = mapped_column(JSON, default=list)
    __table_args__ = (
        Index("ix_plans_event_time", "event_id", "created_at", "id"),
        UniqueConstraint("event_id", "version"),
    )


class ExecutionRecord(IDMixin, Base):
    __tablename__ = "execution_records"
    plan_id: Mapped[str] = mapped_column(ForeignKey("remediation_plans.id"))
    executed_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    executed_at: Mapped[datetime] = mapped_column(UTC)
    source: Mapped[str] = mapped_column(String(30), default="manual_record")
    actual_result: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (Index("ix_execution_plan_time", "plan_id", "executed_at", "id"),)


class Verification(IDMixin, TimestampMixin, Base):
    __tablename__ = "verifications"
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"))
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("remediation_plans.id"))
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    event_version: Mapped[int]
    status: Mapped[str] = mapped_column(String(30), default="queued")
    before_start: Mapped[datetime] = mapped_column(UTC)
    before_end: Mapped[datetime] = mapped_column(UTC)
    after_start: Mapped[datetime] = mapped_column(UTC)
    after_end: Mapped[datetime] = mapped_column(UTC)
    template: Mapped[str] = mapped_column(String(60))
    template_version: Mapped[str] = mapped_column(String(30), default="1")
    criteria: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    outcome: Mapped[str | None] = mapped_column(String(30))
    manifest_id: Mapped[str | None] = mapped_column(String(36))
    __table_args__ = (
        Index("ix_verifications_event_time", "event_id", "created_at", "id"),
        Index("ix_verifications_plan_state", "plan_id", "status", "id"),
    )


class KnowledgeEntry(IDMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_entries"
    title: Mapped[str] = mapped_column(String(255))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(30), default="draft")
    approved_version: Mapped[int | None]
    __table_args__ = (Index("ix_knowledge_status_time", "status", "updated_at", "id"),)


class KnowledgeVersion(IDMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_versions"
    entry_id: Mapped[str] = mapped_column(ForeignKey("knowledge_entries.id"))
    version: Mapped[int]
    engine: Mapped[str] = mapped_column(String(30), default="mysql")
    engine_versions: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    symptoms: Mapped[list] = mapped_column(JSON, default=list)
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    source_event_id: Mapped[str | None] = mapped_column(ForeignKey("events.id"))
    verification_id: Mapped[str | None] = mapped_column(ForeignKey("verifications.id"))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    reviewer_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(UTC)
    review_note: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("entry_id", "version"),)


class KnowledgeResource(Base):
    __tablename__ = "knowledge_resources"
    version_id: Mapped[str] = mapped_column(ForeignKey("knowledge_versions.id"), primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"), primary_key=True)
    __table_args__ = (Index("ix_knowledge_resources_instance", "instance_id", "version_id"),)


class EvidenceManifest(IDMixin, TimestampMixin, Base):
    __tablename__ = "evidence_manifests"
    owner_type: Mapped[str] = mapped_column(String(30))
    owner_id: Mapped[str] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(default=1)
    instance_ids: Mapped[list] = mapped_column(JSON, default=list)
    window_start: Mapped[datetime] = mapped_column(UTC)
    window_end: Mapped[datetime] = mapped_column(UTC)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    retain_until: Mapped[datetime] = mapped_column(UTC)
    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", "version"),
        Index("ix_manifests_retention", "retain_until", "id"),
    )


class ManifestItem(IDMixin, Base):
    __tablename__ = "manifest_items"
    manifest_id: Mapped[str] = mapped_column(ForeignKey("evidence_manifests.id"))
    evidence_id: Mapped[str | None] = mapped_column(ForeignKey("evidence.id"))
    metric_run_id: Mapped[str | None] = mapped_column(ForeignKey("metric_frames.run_id"))
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    item_type: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    __table_args__ = (Index("ix_manifest_items_evidence", "manifest_id", "evidence_id"),)


class EvidencePin(IDMixin, Base):
    __tablename__ = "evidence_pins"
    manifest_id: Mapped[str] = mapped_column(ForeignKey("evidence_manifests.id"))
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence.id"))
    retain_until: Mapped[datetime] = mapped_column(UTC)
    __table_args__ = (
        UniqueConstraint("manifest_id", "evidence_id"),
        Index("ix_pins_evidence_retention", "evidence_id", "retain_until"),
        Index("ix_pins_retention", "retain_until", "id"),
    )


class MaintenanceWindow(IDMixin, TimestampMixin, Base):
    __tablename__ = "maintenance_windows"
    system_id: Mapped[str] = mapped_column(ForeignKey("systems.id"))
    instance_id: Mapped[str | None] = mapped_column(ForeignKey("instances.id"))
    starts_at: Mapped[datetime] = mapped_column(UTC)
    ends_at: Mapped[datetime] = mapped_column(UTC)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text)
    policy: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (Index("ix_maintenance_system_window", "system_id", "starts_at", "ends_at"),)


class ManualChange(IDMixin, TimestampMixin, Base):
    __tablename__ = "manual_changes"
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    event_id: Mapped[str | None] = mapped_column(ForeignKey("events.id"))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    event_at: Mapped[datetime] = mapped_column(UTC)
    source: Mapped[str] = mapped_column(String(30), default="manual")
    description: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (Index("ix_changes_instance_time", "instance_id", "event_at", "id"),)


class ModelCall(IDMixin, Base):
    __tablename__ = "model_calls"
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"))
    sequence: Mapped[int]
    scope_ref: Mapped[str] = mapped_column(String(36))
    input_sha256: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    started_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTC)
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cost: Mapped[float | None]
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("investigation_id", "sequence"),)


class ToolCall(IDMixin, Base):
    __tablename__ = "tool_calls"
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"))
    sequence: Mapped[int]
    tool: Mapped[str] = mapped_column(String(60))
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    result_ref: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30))
    occurred_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    __table_args__ = (UniqueConstraint("investigation_id", "sequence"),)


class AuditLog(IDMixin, Base):
    __tablename__ = "audit_logs"
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(100))
    target_type: Mapped[str] = mapped_column(String(60))
    target_id: Mapped[str | None] = mapped_column(String(100))
    result: Mapped[str] = mapped_column(String(30))
    request_id: Mapped[str | None] = mapped_column(String(100))
    occurred_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        Index("ix_audit_actor_time", "actor_id", "occurred_at", "id"),
        Index("ix_audit_target_time", "target_type", "target_id", "occurred_at", "id"),
    )


class PlatformSetting(Base):
    __tablename__ = "platform_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    version: Mapped[int] = mapped_column(default=1)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTC, default=utcnow, onupdate=utcnow)


# Apply the same storage contract to every core table.
for _table in Base.metadata.tables.values():
    _table.dialect_options["mysql"]["engine"] = "InnoDB"
    _table.dialect_options["mysql"]["charset"] = "utf8mb4"
    _table.dialect_options["mysql"]["collate"] = "utf8mb4_0900_ai_ci"


class MonitoringSource(IDMixin, TimestampMixin, Base):
    __tablename__ = "monitoring_sources"
    name: Mapped[str] = mapped_column(String(120))
    url: Mapped[str] = mapped_column(String(1000))
    system_id: Mapped[str] = mapped_column(ForeignKey("systems.id"))
    secret_ref: Mapped[str] = mapped_column(String(36))
    allowed_host_group_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="unknown")
    last_success_at: Mapped[datetime | None] = mapped_column(UTC)
    error_code: Mapped[str | None] = mapped_column(String(80))
    __table_args__ = (Index("ix_monitoring_source_system", "system_id"),)


class MonitoringMapping(IDMixin, TimestampMixin, Base):
    __tablename__ = "monitoring_mappings"
    source_id: Mapped[str] = mapped_column(ForeignKey("monitoring_sources.id"))
    hostid: Mapped[str] = mapped_column(String(80))
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"), unique=True)
    layer: Mapped[str] = mapped_column(String(20), default="db")
    selected: Mapped[bool] = mapped_column(default=True)
    items: Mapped[list] = mapped_column(JSON, default=list)
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    relationships: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(default=1)
    last_observed_at: Mapped[datetime | None] = mapped_column(UTC)
    __table_args__ = (UniqueConstraint("source_id", "hostid"),)


class InspectionPolicy(IDMixin, TimestampMixin, Base):
    __tablename__ = "inspection_policies"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), unique=True)
    version: Mapped[int] = mapped_column(default=1)
    enabled: Mapped[bool] = mapped_column(default=False)
    instance_ids: Mapped[list] = mapped_column(JSON, default=list)
    signal_interval_seconds: Mapped[int] = mapped_column(default=300)
    ai_interval_seconds: Mapped[int] = mapped_column(default=1800)
    urgent_enabled: Mapped[bool] = mapped_column(default=True)
    cooldown_seconds: Mapped[int] = mapped_column(default=300)
    max_daily_runs: Mapped[int] = mapped_column(default=24)
    mode: Mapped[str] = mapped_column(String(30), default="rules_only")
    external_confirmed: Mapped[bool] = mapped_column(default=False)
    next_signal_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    next_ai_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    last_dispatched_at: Mapped[datetime | None] = mapped_column(UTC)
    last_completed_at: Mapped[datetime | None] = mapped_column(UTC)
    status: Mapped[str] = mapped_column(String(40), default="idle")
    reason: Mapped[str | None] = mapped_column(String(160))
    __table_args__ = (Index("ix_inspection_policy_due", "enabled", "next_signal_at", "next_ai_at"),)


class InspectionDispatch(IDMixin, TimestampMixin, Base):
    __tablename__ = "inspection_dispatches"
    policy_id: Mapped[str] = mapped_column(ForeignKey("inspection_policies.id"))
    policy_version: Mapped[int]
    dispatch_key: Mapped[str] = mapped_column(String(64), unique=True)
    trigger: Mapped[str] = mapped_column(String(30))
    event_id: Mapped[str | None] = mapped_column(ForeignKey("events.id"))
    instance_ids: Mapped[list] = mapped_column(JSON, default=list)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"), unique=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    __table_args__ = (Index("ix_inspection_dispatch_day", "policy_id", "created_at"),)


class InspectionFinding(IDMixin, TimestampMixin, Base):
    __tablename__ = "inspection_findings"
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    category: Mapped[str] = mapped_column(String(50))
    dimension: Mapped[str] = mapped_column(String(80))
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), unique=True)
    kind: Mapped[str] = mapped_column(String(20), default="current")
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    calculation: Mapped[dict] = mapped_column(JSON, default=dict)
    signal_hash: Mapped[str | None] = mapped_column(String(64))
    dispatched_hash: Mapped[str | None] = mapped_column(String(64))
    last_dispatched_at: Mapped[datetime | None] = mapped_column(UTC)
    latest_investigation_id: Mapped[str | None] = mapped_column(ForeignKey("investigations.id"))
    __table_args__ = (Index("ix_inspection_findings_instance", "instance_id", "updated_at"),)
