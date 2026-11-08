from datetime import timedelta
import pytest
from sqlalchemy import select
from tests.platform.test_events import db, graph, observe, manual_event
from platform_app import models as m
from platform_app.db import utcnow
from platform_app.remediation import (
    compare_locks,
    compare_sql,
    create_verification,
    handle_verification,
)


def test_lock_comparison_needs_three_samples_and_reports_condition_only():
    before = [{"rows": [{"waiting_trx_id": "w", "blocking_trx_id": "b"}]}] * 3
    after = [{"rows": []}] * 3
    result = compare_locks(before, after, {"load_context": {"kind": "unknown"}})
    assert result["outcome"] == "improved"
    assert result["business_recovery_proven"] is False
    assert compare_locks(before[:2], after, {})["outcome"] == "inconclusive"
    assert compare_locks(before, before, {})["outcome"] == "not_improved"


def test_sql_comparison_rejects_reset_no_load_and_incomparable_rate():
    before = {
        "schema": "s",
        "digest": "d",
        "counter_epoch": "e",
        "executions": 20,
        "total_seconds": 40,
        "window_seconds": 60,
        "observations": 3,
    }
    after = dict(before, total_seconds=10)
    assert compare_sql(before, after, {})["outcome"] == "improved"
    for change in (
        {"executions": 0},
        {"executions": 5},
        {"executions": 100},
        {"counter_epoch": "new"},
        {"digest": "other"},
        {"observations": 2},
    ):
        assert compare_sql(before, dict(after, **change), {})["outcome"] == "inconclusive"


def test_verification_uses_frozen_manifest_and_ignores_later_backfill(db):
    actor, a, _, _ = graph(db)
    event = manual_event(db, a)
    start = utcnow() - timedelta(minutes=10)
    for sec in (0, 30, 60):
        observe(db, a, start + timedelta(seconds=sec))
    for sec in (120, 150):
        observe(db, a, start + timedelta(seconds=sec), rows=[])
    task = create_verification(
        actor,
        event.id,
        {
            "expected_version": 1,
            "template": "locks_absent",
            "before": {
                "from": start.isoformat() + "Z",
                "to": (start + timedelta(seconds=61)).isoformat() + "Z",
            },
            "after": {
                "from": (start + timedelta(seconds=120)).isoformat() + "Z",
                "to": (start + timedelta(seconds=181)).isoformat() + "Z",
            },
            "criteria": {"load_context": {"kind": "unknown"}},
        },
    )
    # Existing-window backfill after submit must not repair a frozen insufficient manifest.
    observe(db, a, start + timedelta(seconds=180), rows=[])
    handle_verification(db, task)
    value = db.get(m.Verification, task.payload["verification_id"])
    assert value.outcome == "inconclusive"
    assert value.result["after_observations"] == 2
    assert value.manifest_id


def test_merge_invalidates_older_verification_coverage(db):
    actor, a, _, _ = graph(db)
    event = manual_event(db, a)
    event.coverage_revision = 2
    value = m.Verification(
        event_id=event.id,
        instance_id=a.id,
        created_by=actor.user_id,
        event_version=1,
        before_start=utcnow() - timedelta(minutes=2),
        before_end=utcnow() - timedelta(minutes=1),
        after_start=utcnow() - timedelta(seconds=50),
        after_end=utcnow(),
        template="locks_absent",
        criteria={"coverage_revision": 1, "episode_ids": []},
        status="queued",
    )
    db.add(value)
    db.flush()
    task = m.Task(
        type="verification",
        queue="collection",
        actor_id=actor.user_id,
        instance_id=a.id,
        payload={"verification_id": value.id},
    )
    db.add(task)
    db.flush()
    handle_verification(db, task)
    assert value.outcome == "inconclusive"
    assert "event_coverage_changed" in value.result["reasons"]
