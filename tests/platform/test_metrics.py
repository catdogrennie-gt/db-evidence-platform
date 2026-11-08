from datetime import timedelta
import pytest
from tests.platform.test_ai_scope import ai_env


def add_frame(env, owner, *, age=5, status="succeeded", values=None, interval=30):
    from platform_app.db import utcnow
    from platform_app.models import Task, CollectionRun, MetricFrame, CollectionSchedule, Evidence

    db = env["db"]
    instance = env["a"]
    instance.enabled = True
    instance.lifecycle_state = "Active"
    task = Task(type="collection", instance_id=instance.id)
    db.add(task)
    db.flush()
    stamp = utcnow() - timedelta(seconds=age)
    run = CollectionRun(
        task_id=task.id,
        instance_id=instance.id,
        collector=owner,
        status=status,
        started_at=stamp,
        finished_at=stamp,
    )
    db.add(run)
    db.flush()
    frame = MetricFrame(
        run_id=run.id,
        instance_id=instance.id,
        collector=owner,
        server_uuid="a",
        collector_boot_id="test",
        observed_monotonic_ns=123456789,
        observed_at=stamp,
        uptime_seconds=1000,
        derived_values={
            "values": values or {},
            "metric_validity": {k: {"validity": "valid", "reasons": []} for k in (values or {})},
            "window_validity": "valid",
        },
        rate_validity="valid",
    )
    db.add_all(
        [
            frame,
            CollectionSchedule(instance_id=instance.id, collector=owner, interval_seconds=interval),
        ]
    )
    db.add(
        Evidence(
            instance_id=instance.id,
            run_id=run.id,
            source=owner,
            source_mode="live",
            observed_at=stamp,
            payload={},
            payload_sha256="0" * 64,
            coverage={"availability": "available"},
        )
    )
    db.commit()
    return frame, run


def test_bridge_uses_owner_freshness_and_preserves_other_sources(ai_env):
    from platform_app.metrics_bridge import render_metrics

    add_frame(ai_env, "identity_status", values={"connections": 7, "qps": 12})
    add_frame(ai_env, "host", age=50, values={"host_load1": 1.5})
    output = render_metrics(ai_env["factory"])
    assert "tdx_db_connections{" in output and " 7.0\n" in output
    assert "tdx_db_questions_per_second{" in output
    assert "tdx_host_load1{" not in output
    assert f'tdx_collector_up{{instance_id="{ai_env["a"].id}",collector="host"}} 0' in output


def test_latest_owner_failure_suppresses_old_success_value(ai_env):
    from platform_app.metrics_bridge import render_metrics
    from platform_app.models import Task, CollectionRun
    from platform_app.db import utcnow

    add_frame(ai_env, "identity_status", values={"connections": 7})
    db = ai_env["db"]
    task = Task(type="collection", instance_id=ai_env["a"].id)
    db.add(task)
    db.flush()
    db.add(
        CollectionRun(
            task_id=task.id,
            instance_id=ai_env["a"].id,
            collector="identity_status",
            status="failed",
            started_at=utcnow(),
        )
    )
    db.commit()
    assert "tdx_db_connections{" not in render_metrics(ai_env["factory"])


@pytest.mark.parametrize(
    "prior_status,age,expected,reason",
    [
        ("succeeded", 5, True, "valid"),
        ("succeeded", 100, False, "stale"),
        ("failed", 5, False, "latest_attempt_failed"),
    ],
)
def test_running_attempt_preserves_only_fresh_preceding_success(
    ai_env, prior_status, age, expected, reason
):
    from platform_app.metrics_bridge import source_health
    from platform_app.models import Task, CollectionRun
    from platform_app.db import utcnow

    add_frame(ai_env, "identity_status", values={"connections": 7}, age=age)
    db = ai_env["db"]
    if prior_status == "failed":
        failed_task = Task(type="collection", instance_id=ai_env["a"].id)
        db.add(failed_task)
        db.flush()
        db.add(
            CollectionRun(
                task_id=failed_task.id,
                instance_id=ai_env["a"].id,
                collector="identity_status",
                status="failed",
                started_at=utcnow() - timedelta(seconds=1),
            )
        )
    task = Task(type="collection", instance_id=ai_env["a"].id)
    db.add(task)
    db.flush()
    db.add(
        CollectionRun(
            task_id=task.id,
            instance_id=ai_env["a"].id,
            collector="identity_status",
            status="running",
            started_at=utcnow(),
        )
    )
    db.commit()
    health = source_health(db, ai_env["a"], "identity_status")
    assert health["up"] is expected and health["reason_code"] == reason
    assert health["attempt_in_progress"] is True


def test_replication_not_applicable_is_neither_failure_nor_coverage_gap(ai_env):
    from sqlalchemy import select
    from platform_app.models import Evidence
    from platform_app.metrics_bridge import source_health
    from platform_app.routes.observability import instance_overview

    frame, run = add_frame(ai_env, "replication", values={})
    db = ai_env["db"]
    evidence = db.scalar(select(Evidence).where(Evidence.run_id == run.id))
    evidence.source = "mysql.replication"
    evidence.coverage = {"availability": "not_applicable"}
    db.add(
        Evidence(
            instance_id=ai_env["a"].id,
            run_id=run.id,
            source="mysql.identity",
            source_mode="live",
            observed_at=evidence.observed_at + timedelta(seconds=1),
            payload={},
            payload_sha256="0" * 64,
            coverage={"availability": "available"},
        )
    )
    db.commit()
    health = source_health(db, ai_env["a"], "replication")
    assert health["up"] is False and health["reason_code"] == "not_applicable"
    overview = instance_overview(ai_env["a"].id, ai_env["actor"])
    assert overview["coverage"]["gaps"] == [] and overview["coverage"]["complete"] is True
    assert overview["coverage"]["not_applicable_sources"] == ["replication"]


def test_configured_interval_and_disabled_asset_are_respected(ai_env):
    from platform_app.metrics_bridge import render_metrics

    add_frame(ai_env, "host", age=70, values={"host_load1": 2.0}, interval=60)
    assert "tdx_host_load1{" in render_metrics(ai_env["factory"])
    ai_env["a"].enabled = False
    ai_env["db"].commit()
    assert "tdx_host_load1{" not in render_metrics(ai_env["factory"])


def test_bridge_control_db_failure_never_returns_old_cache(ai_env):
    from platform_app.metrics_bridge import render_metrics
    from sqlalchemy.exc import OperationalError

    add_frame(ai_env, "identity_status", values={"connections": 7})
    assert "tdx_db_connections{" in render_metrics(ai_env["factory"])

    def broken():
        raise OperationalError("test", {}, Exception("unavailable"))

    with pytest.raises(OperationalError):
        render_metrics(broken)


def test_prometheus_lookback_cannot_extend_old_measurements():
    from platform_app.metrics import mask_series, METRIC_TEMPLATES

    start = 1000.0
    series = {
        "tdx_db_questions_per_second": [[1000, "9"], [1030, "9"]],
        "tdx_collector_up": [[1000, "1"], [1030, "1"]],
        "tdx_collector_last_success_timestamp_seconds": [[1000, "990"], [1030, "990"]],
        "tdx_collector_sample_age_seconds": [[1000, "10"], [1030, "10"]],
        "tdx_collector_freshness_limit_seconds": [[1000, "20"], [1030, "20"]],
        "tdx_collector_rate_valid": [[1000, "1"], [1030, "1"]],
    }
    result = mask_series(series, start, 1060, 30, METRIC_TEMPLATES["questions_per_second"])
    assert [p["value"] for p in result["points"]] == [9.0, None]
    assert result["points"][1]["validity"] == "stale"


def test_metric_range_uses_prometheus_millisecond_grid_without_widening_window(ai_env, monkeypatch):
    from types import SimpleNamespace
    import math
    from platform_app import metrics

    start = ai_env["start"].replace(microsecond=123456)
    end = start + timedelta(seconds=30)
    requested = []

    def prom(config, selector, lower, upper, step):
        requested.append((lower, upper))
        stamp = round(lower, 3)  # Prometheus query_range evaluation precision.
        values = {
            "tdx_db_connections": 7,
            "tdx_collector_up": 1,
            "tdx_collector_last_success_timestamp_seconds": stamp - 5,
            "tdx_collector_sample_age_seconds": 5,
            "tdx_collector_freshness_limit_seconds": 45,
        }
        return [
            {"metric": {"__name__": name}, "values": [[stamp, str(value)]]}
            for name, value in values.items()
        ]

    monkeypatch.setattr(metrics, "prometheus_range", prom)
    result = metrics.query_template(
        ai_env["actor"], ai_env["a"].id, "connections", start, end, config=SimpleNamespace()
    )
    assert result["points"][0]["value"] == 7
    lower, upper = requested[0]
    assert lower == math.ceil(metrics.timestamp(start) * 1000) / 1000
    assert lower >= metrics.timestamp(start) and upper < metrics.timestamp(end)


def test_frozen_prometheus_window_strips_labels_masks_gaps_and_caps_total_points(monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace
    from platform_app import metrics
    from uuid import uuid4

    identifier = str(uuid4())
    seen = []

    def prom(config, selector, start, end, step, **kwargs):
        seen.append((selector, start, end, step))
        times = [start, start + step]
        values = {
            "tdx_db_connections": [7, 8],
            "tdx_collector_up": [1, 0],
            "tdx_collector_last_success_timestamp_seconds": [start - 1, start + step - 1],
            "tdx_collector_sample_age_seconds": [1, 1],
            "tdx_collector_freshness_limit_seconds": [45, 45],
        }
        return [
            {
                "metric": {
                    "__name__": name,
                    "instance_id": identifier,
                    "collector": "identity_status",
                    "private_host": "never-persist",
                },
                "values": list(zip(times, map(str, items))),
            }
            for name, items in values.items()
        ]

    monkeypatch.setattr(metrics, "prometheus_range", prom)
    result = metrics.materialize_metric_window(
        SimpleNamespace(),
        [identifier],
        datetime(2026, 1, 1),
        datetime(2026, 1, 1, 0, 1),
        max_points=128,
    )
    selected = next(i for i in result["items"] if i["template"] == "connections")
    assert [p["value"] for p in selected["points"]][:2] == [7, None]
    assert "never-persist" not in str(result) and "private_host" not in str(result)
    assert sum(len(i["points"]) for i in result["items"]) <= 128
    assert result["coverage"]["complete"] is False
    assert "source_unavailable" in result["coverage"]["reason_codes"]
    assert identifier in seen[0][0] and "tdx_db_connections" in seen[0][0]


def test_frozen_prometheus_failure_is_explicit_and_scope_injection_never_queries(monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace
    from platform_app import metrics
    from platform_app.common import PlatformError
    from fastapi import HTTPException
    from uuid import uuid4

    calls = []

    def unavailable(*args, **kwargs):
        calls.append(1)
        raise PlatformError(503, "prometheus_unavailable", "unavailable")

    monkeypatch.setattr(metrics, "prometheus_range", unavailable)
    start, end = datetime(2026, 1, 1), datetime(2026, 1, 1, 0, 1)
    with pytest.raises(HTTPException):
        metrics.materialize_metric_window(SimpleNamespace(), ['uuid"} or secret{}'], start, end)
    assert calls == []
    result = metrics.materialize_metric_window(SimpleNamespace(), [str(uuid4())], start, end)
    assert result["items"] == [] and result["coverage"]["materialized"] is False
    assert result["coverage"]["reason_codes"] == ["prometheus_unavailable"]


def test_five_asset_materialization_fits_whole_manifest_budgets(monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace
    from uuid import uuid4
    import json
    from platform_app import metrics

    ids = [str(uuid4()) for _ in range(5)]
    calls = []

    def empty_prom(config, selector, start, end, step, **kwargs):
        calls.append((selector, step))
        return []

    monkeypatch.setattr(metrics, "prometheus_range", empty_prom)
    result = metrics.materialize_metric_window(
        SimpleNamespace(), ids, datetime(2026, 1, 1), datetime(2026, 1, 2)
    )
    assert len(calls) == 1 and all(i in calls[0][0] for i in ids)
    assert result["coverage"]["materialized"] is True
    assert result["coverage"]["point_count"] <= 10000
    assert len(json.dumps(result).encode()) <= 2 * 1024 * 1024


def test_metric_query_is_authorized_template_only(ai_env):
    from platform_app.metrics import query_template
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        query_template(
            ai_env["actor"], ai_env["b"].id, "connections", ai_env["start"], ai_env["end"]
        )
    with pytest.raises(HTTPException):
        query_template(
            ai_env["actor"], ai_env["a"].id, "arbitrary_promql{}", ai_env["start"], ai_env["end"]
        )


def test_replay_only_run_is_never_exported_as_live(ai_env):
    from platform_app.metrics_bridge import render_metrics
    from platform_app.models import Evidence
    from sqlalchemy import select

    frame, _ = add_frame(ai_env, "identity_status", values={"connections": 7})
    evidence = ai_env["db"].scalar(select(Evidence).where(Evidence.run_id == frame.run_id))
    evidence.source_mode = "replay"
    ai_env["db"].commit()
    assert "tdx_db_connections{" not in render_metrics(ai_env["factory"])


def test_frame_identity_mismatch_never_reuses_previous_business_value(ai_env):
    from platform_app.metrics_bridge import render_metrics

    frame, _ = add_frame(ai_env, "identity_status", values={"connections": 7})
    frame.server_uuid = "different-server"
    ai_env["db"].commit()
    assert "tdx_db_connections{" not in render_metrics(ai_env["factory"])


def test_http_bridge_returns_503_after_a_successful_scrape(ai_env):
    from types import SimpleNamespace
    import httpx
    from sqlalchemy.exc import OperationalError
    from platform_app.metrics_bridge import start_bridge

    add_frame(ai_env, "identity_status", values={"connections": 7})
    unavailable = [False]

    def sessions():
        if unavailable[0]:
            raise OperationalError("test", {}, Exception("unavailable"))
        return ai_env["factory"]()

    server = start_bridge(
        sessions, SimpleNamespace(metrics_bridge_host="127.0.0.1", metrics_bridge_port=0)
    )
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/metrics"
        assert httpx.get(url, trust_env=False).status_code == 200
        unavailable[0] = True
        response = httpx.get(url, trust_env=False)
        assert response.status_code == 503 and "tdx_db_connections" not in response.text
    finally:
        server.shutdown()
        server.server_close()
