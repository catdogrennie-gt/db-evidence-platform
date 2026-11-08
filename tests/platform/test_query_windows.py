"""Selected SQL windows never borrow the latest pair or bridge a reset/gap."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from platform_app import models as m
from platform_app.common import PlatformError
from platform_app.db import utcnow
from platform_app.routes.observability import snapshot_view
from tests.platform.test_access import db, fixture_graph


def samples(db, instance, counts=(10, 20, 40), *, times=None, second_schema=False):
    base = utcnow() - timedelta(hours=1)
    db.add(
        m.CollectionSchedule(
            instance_id=instance.id, collector="statement_digest", interval_seconds=60
        )
    )
    values = []
    for index, count in enumerate(counts):
        seconds = times[index] if times else index * 60
        stamp = base + timedelta(seconds=seconds)
        task = m.Task(type="collect_instance", instance_id=instance.id, status="succeeded")
        db.add(task)
        db.flush()
        run = m.CollectionRun(
            task_id=task.id,
            instance_id=instance.id,
            collector="statement_digest",
            status="succeeded",
            started_at=stamp,
            finished_at=stamp + timedelta(milliseconds=20),
        )
        db.add(run)
        db.flush()
        rows = [
            {
                "SCHEMA_NAME": "s",
                "DIGEST": "d",
                "DIGEST_TEXT": "SAFE normalized shape",
                "COUNT_STAR": count,
                "SUM_TIMER_WAIT": count * 1_000_000_000,
                "SUM_ROWS_EXAMINED": count * 10,
            }
        ]
        if second_schema:
            n = (100, 130, 180)[index]
            rows.append(
                {
                    "SCHEMA_NAME": "s2",
                    "DIGEST": "d",
                    "DIGEST_TEXT": "other schema safe shape",
                    "COUNT_STAR": n,
                    "SUM_TIMER_WAIT": n * 1_000_000_000,
                    "SUM_ROWS_EXAMINED": n * 10,
                }
            )
        frame = m.MetricFrame(
            run_id=run.id,
            instance_id=instance.id,
            collector="statement_digest",
            server_uuid=instance.expected_server_uuid,
            collector_boot_id="boot",
            source_epoch="source-epoch",
            counter_epoch=1,
            observed_monotonic_ns=(seconds + 100) * 1_000_000_000,
            observed_at=stamp,
            uptime_seconds=1000 + seconds,
            raw_values={"rows": rows},
            rate_validity="valid",
        )
        evidence = m.Evidence(
            instance_id=instance.id,
            run_id=run.id,
            source="mysql.statement_digest",
            source_mode="live",
            observed_at=stamp + timedelta(milliseconds=10),
            server_uuid=instance.expected_server_uuid,
            coverage={
                "availability": "available",
                "collector": "statement_digest",
                "interval_seconds": 60,
            },
            payload={
                "rows": rows,
                "window": {
                    "rows": [
                        {
                            "SCHEMA_NAME": "s",
                            "DIGEST": "d",
                            "count_delta": 999,
                            "total_time_seconds_delta": 999,
                        }
                    ]
                },
            },
            payload_sha256="0" * 64,
        )
        db.add_all([frame, evidence])
        db.flush()
        values.append((frame, evidence))
    db.commit()
    return base, values


def query(actor, instance, start, end):
    return snapshot_view(
        instance.id,
        SimpleNamespace(url=SimpleNamespace(path=f"/api/v1/instances/{instance.id}/queries")),
        actor=actor,
        from_time=start.isoformat() + "Z",
        to_time=end.isoformat() + "Z",
        limit=50,
        cursor=None,
        q="",
    )


def test_three_samples_use_full_window_not_latest_pair_or_payload_window(db):
    actor, a, *_ = fixture_graph(db)
    base, rows = samples(db, a)
    result = query(actor, a, base, base + timedelta(seconds=180))
    item = result["items"][0]
    assert item["window_count"] == 30
    assert item["total_time_ms"] == pytest.approx(30)
    assert item["avg_time_ms"] == pytest.approx(1)
    assert item["window_rows_examined"] == 300 and item["rows_examined"] == 400
    assert item["window_validity"] == "valid" and item["window_reasons"] == []
    assert result["observations"] == 3
    assert result["comparison_evidence_refs"][0]["id"] == rows[0][1].id
    assert result["comparison_evidence_refs"][-1]["id"] == rows[-1][1].id
    assert result["comparison_from"] != result["comparison_evidence_refs"][0]["frame_observed_at"]
    assert result["requested_from"] == base.isoformat() + "Z"


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("reset", "digest_reset"),
        ("missing", "digest_missing"),
        ("boot", "collector_restarted"),
        ("identity", "identity_changed"),
        ("epoch", "counter_epoch_changed"),
        ("source_epoch", "source_epoch_changed"),
        ("unavailable", "source_unavailable"),
        ("truncated", "source_truncated"),
        ("missing_evidence", "source_evidence_missing"),
        ("clock", "clock_jump"),
        ("uptime", "uptime_epoch_changed"),
    ],
)
def test_intermediate_invalid_sample_never_recovers_by_final_larger_counter(db, kind, reason):
    actor, a, *_ = fixture_graph(db)
    base, rows = samples(db, a)
    frame, evidence = rows[1]
    if kind == "reset":
        frame.raw_values = {
            "rows": [
                {**frame.raw_values["rows"][0], "COUNT_STAR": 2, "SUM_TIMER_WAIT": 2_000_000_000}
            ]
        }
    if kind == "missing":
        frame.raw_values = {"rows": []}
    if kind == "boot":
        frame.collector_boot_id = "restarted"
    if kind == "identity":
        frame.server_uuid = "other"
    if kind == "epoch":
        frame.counter_epoch = 2
    if kind == "source_epoch":
        frame.source_epoch = "different"
    if kind == "unavailable":
        evidence.coverage = {**evidence.coverage, "availability": "permission_denied"}
    if kind == "truncated":
        evidence.truncated = True
    if kind == "missing_evidence":
        db.delete(evidence)
    if kind == "clock":
        frame.observed_monotonic_ns += 10_000_000_000
    if kind == "uptime":
        frame.uptime_seconds += 10
    db.commit()
    result = query(actor, a, base, base + timedelta(seconds=180))
    item = result["items"][0]
    assert (
        item["window_count"] is None
        and item["total_time_ms"] is None
        and item["avg_time_ms"] is None
    )
    assert item["window_validity"] == "unknown"
    assert reason in item["window_reasons"]
    assert result["coverage"]["complete"] is False


def test_one_frame_is_unknown_not_zero(db):
    actor, a, *_ = fixture_graph(db)
    base, _ = samples(db, a, counts=(10,))
    result = query(actor, a, base, base + timedelta(seconds=60))
    assert result["items"][0]["window_count"] is None
    assert "insufficient_observations" in result["items"][0]["window_reasons"]


def test_source_without_intermediate_frame_is_an_explicit_gap(db):
    actor, a, *_ = fixture_graph(db)
    base, rows = samples(db, a)
    db.delete(rows[1][0])
    db.commit()
    result = query(actor, a, base, base + timedelta(seconds=180))
    assert result["items"][0]["window_count"] is None
    assert "source_frame_missing" in result["coverage"]["gaps"]


def test_latest_source_without_a_frame_is_not_ignored_as_an_edge(db):
    actor, a, *_ = fixture_graph(db)
    base, rows = samples(db, a)
    db.delete(rows[-1][0])
    db.commit()
    result = query(actor, a, base, base + timedelta(seconds=180))
    assert result["items"][0]["window_count"] is None
    assert "source_frame_missing" in result["coverage"]["gaps"]


def test_historical_source_interval_is_not_relaxed_by_current_schedule(db):
    actor, a, *_ = fixture_graph(db)
    base, _ = samples(db, a, times=(0, 60, 240))
    db.scalar(
        select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id == a.id)
    ).interval_seconds = 300
    db.commit()
    result = query(actor, a, base, base + timedelta(seconds=300))
    assert result["items"][0]["window_count"] is None
    assert "invalid_interval" in result["items"][0]["window_reasons"]


def test_source_timestamp_just_outside_requested_edge_is_not_silently_included(db):
    actor, a, *_ = fixture_graph(db)
    base, _ = samples(db, a)
    result = query(actor, a, base, base + timedelta(seconds=120, microseconds=1))
    assert result["items"][0]["window_count"] is None
    assert "source_observation_outside_window" in result["coverage"]["gaps"]


@pytest.mark.parametrize("source_seconds", [(100, 110, 90), (0, 150, 120), (20, 80, 140)])
def test_source_clock_reversal_or_large_frame_offset_is_unknown(db, source_seconds):
    actor, a, *_ = fixture_graph(db)
    base, rows = samples(db, a)
    for (_, source), seconds in zip(rows, source_seconds):
        source.observed_at = base + timedelta(seconds=seconds)
    db.commit()
    result = query(actor, a, base, base + timedelta(seconds=180))
    assert result["items"][0]["window_count"] is None
    assert any(
        reason in result["coverage"]["gaps"]
        for reason in ("source_clock_jump", "source_frame_clock_skew")
    )


def test_real_millisecond_source_offsets_are_accepted(db):
    actor, a, *_ = fixture_graph(db)
    base, rows = samples(db, a)
    for (frame, source), milliseconds in zip(rows, (10, 21, 6)):
        source.observed_at = frame.observed_at + timedelta(milliseconds=milliseconds)
    db.commit()
    result = query(actor, a, base, base + timedelta(seconds=180))
    assert result["items"][0]["window_count"] == 30
    assert result["items"][0]["window_validity"] == "valid"


def test_integer_counter_reset_is_detected_without_float_rounding(db):
    actor, a, *_ = fixture_graph(db)
    base, _ = samples(db, a, counts=(2**53 + 5, 2**53 + 4, 2**53 + 8))
    result = query(actor, a, base, base + timedelta(seconds=180))
    assert result["items"][0]["window_count"] is None
    assert "digest_reset" in result["items"][0]["window_reasons"]


def test_same_digest_is_isolated_by_schema(db):
    actor, a, *_ = fixture_graph(db)
    base, _ = samples(db, a, second_schema=True)
    result = query(actor, a, base, base + timedelta(seconds=180))
    assert {r["schema_name"]: r["window_count"] for r in result["items"]} == {"s": 30, "s2": 80}


def test_missing_or_gapped_history_is_unknown(db):
    actor, a, *_ = fixture_graph(db)
    base, _ = samples(db, a, times=(300, 360, 540))
    result = query(actor, a, base, base + timedelta(seconds=600))
    assert result["items"][0]["window_count"] is None
    assert "history_unavailable" in result["coverage"]["gaps"]
    assert "invalid_interval" in result["items"][0]["window_reasons"]


def test_no_raw_digest_rows_are_added_when_latest_evidence_is_redacted(db):
    actor, a, _, grant = fixture_graph(db)
    base, rows = samples(db, a)
    role = db.get(m.Role, grant.role_id)
    role.permissions = [p for p in role.permissions if p != "evidence.read_sensitive"]
    rows[-1][1].sensitivity = "restricted"
    db.commit()
    result = query(actor, a, base, base + timedelta(seconds=180))
    assert result["items"] == []
    assert "SAFE normalized shape" not in str(result) and "'DIGEST'" not in str(result)


def test_frame_limit_requires_narrowing_without_publishing_partial_sum(db, monkeypatch):
    from platform_app import query_windows

    actor, a, *_ = fixture_graph(db)
    base, _ = samples(db, a)
    monkeypatch.setattr(query_windows, "MAX_FRAMES", 2)
    with pytest.raises(PlatformError) as failure:
        query(actor, a, base, base + timedelta(seconds=180))
    assert failure.value.status_code == 503 and failure.value.detail["code"] == "query_window_limit"


def test_unauthorized_instance_is_rejected_before_historical_metrics(db):
    actor, a, b, _ = fixture_graph(db)
    base, _ = samples(db, b)
    with pytest.raises(HTTPException) as failure:
        query(actor, b, base, base + timedelta(seconds=180))
    assert failure.value.status_code == 404
