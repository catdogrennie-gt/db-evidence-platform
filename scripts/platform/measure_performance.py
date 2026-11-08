"""Read-only, actual HTTP/collection measurements; never extrapolate capacity.

The private initialization files supply passwords only in memory. Reports must
be outside the source tree. Roles keep their real grants, including denials.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time

import httpx
from sqlalchemy import select, text

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.platform.verify_scope import Verification
from platform_app.config import load_config
from platform_app.db import create_db_engine, create_session_factory, utcnow
from platform_app import models as m
from platform_app.common import iso


def distribution(values):
    values = sorted(float(x) for x in values if x is not None and math.isfinite(float(x)))

    def quantile(p):
        return values[max(0, math.ceil(len(values) * p) - 1)] if values else None

    return {
        "samples": len(values),
        "p50": quantile(0.5),
        "p95": quantile(0.95),
        "maximum": max(values) if values else None,
    }


def windows_snapshot():
    if os.name != "nt":
        return {"os": platform.platform(), "logical_processors": os.cpu_count()}
    command = r"""
$machine=Get-CimInstance Win32_ComputerSystem
$cpu=Get-CimInstance Win32_Processor | Select-Object -First 1
$osInfo=Get-CimInstance Win32_OperatingSystem
$processes=@(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'platform_app\.(api|worker|ai_worker)' } | ForEach-Object {
 $p=Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
 if($p){ @{pid=$p.Id;cpu_seconds=$p.CPU;working_set_bytes=$p.WorkingSet64;started_at=$p.StartTime.ToUniversalTime().ToString('o')} }
})
@{os=$osInfo.Caption;os_version=$osInfo.Version;cpu=$cpu.Name;physical_cores=$cpu.NumberOfCores;logical_processors=$machine.NumberOfLogicalProcessors;physical_memory_bytes=$machine.TotalPhysicalMemory;free_memory_bytes=([long]$osInfo.FreePhysicalMemory*1024);cpu_load_percent=$cpu.LoadPercentage;platform_processes=$processes} | ConvertTo-Json -Depth 5 -Compress
"""
    try:
        raw = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            check=True,
            timeout=25,
        ).stdout
        return json.loads(raw.decode("utf-8-sig", errors="replace"))
    except Exception as exc:
        return {"status": "unavailable", "error_type": type(exc).__name__}


def collection_statistics(factory, instance_ids, end):
    start = end - timedelta(minutes=30)
    result = {
        "from": iso(start),
        "to": iso(end),
        "seconds": 1800,
        "source": "committed_control_mysql_runs_evidence_and_frames",
        "owners": [],
    }
    with factory() as db:
        schedules = list(
            db.scalars(
                select(m.CollectionSchedule).where(
                    m.CollectionSchedule.instance_id.in_(instance_ids)
                )
            )
        )
        runs = list(
            db.scalars(
                select(m.CollectionRun).where(
                    m.CollectionRun.instance_id.in_(instance_ids),
                    m.CollectionRun.started_at >= start - timedelta(minutes=10),
                    m.CollectionRun.started_at < end,
                )
            )
        )
        run_map = {r.id: r for r in runs}
        observations = defaultdict(list)
        ingests = defaultdict(list)
        for run_id, observed, ingested in db.execute(
            select(m.Evidence.run_id, m.Evidence.observed_at, m.Evidence.ingested_at).where(
                m.Evidence.instance_id.in_(instance_ids),
                m.Evidence.source_mode == "live",
                m.Evidence.observed_at >= start - timedelta(minutes=10),
                m.Evidence.observed_at < end,
            )
        ):
            if run_id in run_map:
                observations[run_id].append(observed)
                if start <= observed:
                    ingests[run_id].append((ingested - observed).total_seconds())
        tasks = list(
            db.scalars(
                select(m.Task).where(
                    m.Task.instance_id.in_(instance_ids),
                    m.Task.queue == "collection",
                    m.Task.started_at >= start,
                    m.Task.started_at < end,
                )
            )
        )
        result["collection_task_queue_wait_seconds"] = distribution(
            (t.started_at - t.due_at).total_seconds() for t in tasks if t.started_at and t.due_at
        )
        result["collection_task_duration_seconds"] = distribution(
            (t.finished_at - t.started_at).total_seconds()
            for t in tasks
            if t.started_at and t.finished_at
        )
        for schedule in schedules:
            period = schedule.interval_seconds
            all_runs = [
                r
                for r in runs
                if r.instance_id == schedule.instance_id and r.collector == schedule.collector
            ]
            window_runs = [r for r in all_runs if start <= r.started_at < end]
            successes = sorted(
                min(observations[r.id])
                for r in all_runs
                if r.status == "succeeded" and observations[r.id]
            )
            stamps = [t for t in successes if start <= t < end]
            expected = math.floor(1800 / period)
            occupied = {int((t - start).total_seconds() // period) for t in stamps}
            occupied = {x for x in occupied if 0 <= x < expected}
            # These are nominal cadence buckets, not invented failed task rows.
            ages, unknown_seconds, stale_seconds = [], 0, 0
            index = -1
            for second in range(1800):
                point = start + timedelta(seconds=second)
                while index + 1 < len(successes) and successes[index + 1] <= point:
                    index += 1
                if index < 0:
                    unknown_seconds += 1
                else:
                    age = (point - successes[index]).total_seconds()
                    ages.append(age)
                    stale_seconds += age > period * 1.5
            result["owners"].append(
                {
                    "instance_id": schedule.instance_id,
                    "collector": schedule.collector,
                    "configured_interval_seconds": period,
                    "enabled": schedule.enabled,
                    "attempt_status_counts": dict(Counter(r.status for r in window_runs)),
                    "successful_observations": len(stamps),
                    "nominal_cadence_buckets": expected,
                    "occupied_cadence_buckets": len(occupied),
                    "empty_cadence_buckets": expected - len(occupied),
                    "cadence_note": "Unobserved nominal slots include scheduler jitter, backlog, deliberate restarts and faults; no zero-loss assumption.",
                    "actual_observation_interval_seconds": distribution(
                        (b - a).total_seconds() for a, b in zip(stamps, stamps[1:])
                    ),
                    "source_age_sampled_every_second": distribution(ages),
                    "age_unknown_seconds": unknown_seconds,
                    "age_exceeds_1_5_period_seconds": stale_seconds,
                    "profile_duration_ms": distribution(
                        r.duration_ms for r in window_runs if r.duration_ms is not None
                    ),
                    "observation_to_ingestion_seconds": distribution(
                        v for r in window_runs for v in ingests[r.id]
                    ),
                    "first_observed_at": iso(stamps[0]) if stamps else None,
                    "last_observed_at": iso(stamps[-1]) if stamps else None,
                }
            )
        frames = list(
            db.scalars(
                select(m.MetricFrame).where(
                    m.MetricFrame.instance_id.in_(instance_ids),
                    m.MetricFrame.collector == "host",
                    m.MetricFrame.observed_at >= start,
                    m.MetricFrame.observed_at < end,
                )
            )
        )
        result["linux_host_observed_load"] = []
        for identifier in instance_ids:
            chosen = [f for f in frames if f.instance_id == identifier]
            values = [(f.derived_values or {}).get("values", {}) for f in chosen]
            result["linux_host_observed_load"].append(
                {
                    "instance_id": identifier,
                    "cpu_busy_ratio": distribution(v.get("host_cpu_busy_ratio") for v in values),
                    "load1": distribution(v.get("host_load1") for v in values),
                    "memory_available_bytes": distribution(
                        v.get("host_memory_available_bytes") for v in values
                    ),
                    "memory_total_bytes": next(
                        (
                            f.raw_values.get("memory_total_bytes")
                            for f in chosen
                            if f.raw_values.get("memory_total_bytes")
                        ),
                        None,
                    ),
                }
            )
        result["table_space_estimates"] = [
            dict(row._mapping)
            for row in db.execute(
                text(
                    "SELECT table_name,table_rows,data_length,index_length FROM information_schema.tables WHERE table_schema=DATABASE()"
                )
            )
        ]
        result["control_mysql_version"] = db.execute(text("SELECT VERSION()")).scalar()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cycles", type=int, default=20)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.is_relative_to(ROOT) or not 1 <= args.cycles <= 100:
        raise SystemExit("Use a private output outside source; cycles must be 1–100.")
    config = load_config(args.config)
    plan = json.loads(
        (config.private_dir / "initialization-plan.json").read_text(encoding="utf-8-sig")
    )
    ids = json.loads(
        (config.private_dir / "initialization-ids.json").read_text(encoding="utf-8-sig")
    )
    verification = Verification(config.public_origin, config.public_origin, plan, ids)
    engine = create_db_engine(config)
    factory = create_session_factory(engine, config)
    with httpx.Client(trust_env=False, timeout=5) as client:
        ready = client.get(config.public_origin.rstrip("/") + "/api/v1/health/ready")
        if ready.status_code != 200 or ready.json().get("ready") is not True:
            raise RuntimeError("Actual API runtime is not ready.")
    before = windows_snapshot()
    roles = ["admin", "dba", "developer", "viewer", "auditor"]
    samples = []
    try:
        for role in roles:
            if not verification.login(
                role, next(u["password"] for u in plan["users"] if u["username"] == role)
            ):
                raise RuntimeError("Benchmark login failed for role " + role)
        with factory() as db:
            assets = list(
                db.scalars(
                    select(m.Instance).where(
                        m.Instance.enabled.is_(True), m.Instance.lifecycle_state == "Active"
                    )
                )
            )
            asset_ids = sorted(i.id for i in assets)
        if len(asset_ids) != 3:
            raise RuntimeError("Expected exactly three real active assets for this run.")
        baseline_end = utcnow()
        params = {
            "from": iso(baseline_end - timedelta(hours=24)),
            "to": iso(baseline_end),
            "step": 60,
            "template": "connections",
        }
        history = {
            "from": iso(baseline_end - timedelta(minutes=30)),
            "to": iso(baseline_end),
            "limit": 50,
        }
        barrier = threading.Barrier(5)

        def user_loop(role):
            actor = verification.actors[role]
            permissions = set(actor["permissions"])
            scope = actor.get("scope", {}).get("instance_ids", [])
            selected = [x for x in asset_ids if x in scope] or asset_ids[:1]
            local = []
            barrier.wait()
            for cycle in range(args.cycles):
                identifier = selected[cycle % len(selected)]
                for endpoint, path, query, expected in [
                    # The catalog returns the authorized metadata set, including
                    # an empty set for an audit-only user; it has no read action.
                    ("asset_list", "/instances", {"limit": 50}, 200),
                    (
                        "event_list",
                        "/events",
                        {"limit": 50},
                        200 if "data.read" in permissions else 403,
                    ),
                    (
                        "evidence_history",
                        "/evidence",
                        history,
                        200 if "data.read" in permissions else 403,
                    ),
                    (
                        "metrics_24h",
                        f"/instances/{identifier}/metrics",
                        params,
                        200 if "data.read" in permissions else 403,
                    ),
                ]:
                    started = time.perf_counter()
                    row = {
                        "role": role,
                        "endpoint": endpoint,
                        "cycle": cycle,
                        "expected_status": expected,
                    }
                    try:
                        response, payload = verification.request(role, "GET", path, params=query)
                        row.update(
                            status=response.status_code,
                            bytes=len(response.content),
                            correct_status=response.status_code == expected,
                        )
                        if response.status_code == 200:
                            row["returned_total"] = payload.get("total")
                            if endpoint == "metrics_24h":
                                points = payload.get("points", [])
                                row.update(
                                    metric_points=len(points),
                                    valid_metric_points=sum(
                                        p.get("value") is not None for p in points
                                    ),
                                )
                    except Exception as exc:
                        row.update(status=None, error_type=type(exc).__name__, correct_status=False)
                    row["elapsed_ms"] = (time.perf_counter() - started) * 1000
                    local.append(row)
                time.sleep(0.1)
            return local

        started = datetime.now(timezone.utc)
        wall = time.perf_counter()
        with ThreadPoolExecutor(max_workers=5) as pool:
            for rows in pool.map(user_loop, roles):
                samples.extend(rows)
        duration = time.perf_counter() - wall
        finished = datetime.now(timezone.utc)
        after = windows_snapshot()
        collection = collection_statistics(factory, asset_ids, utcnow())
        summaries = []
        for endpoint in ["asset_list", "event_list", "evidence_history", "metrics_24h"]:
            rows = [r for r in samples if r["endpoint"] == endpoint]
            summaries.append(
                {
                    "endpoint": endpoint,
                    "successful_200_latency_ms": distribution(
                        r["elapsed_ms"] for r in rows if r["status"] == 200
                    ),
                    "expected_denial_latency_ms": distribution(
                        r["elapsed_ms"]
                        for r in rows
                        if r["status"] in (403, 404) and r["correct_status"]
                    ),
                    "status_counts": dict(Counter(str(r["status"]) for r in rows)),
                    "unexpected_outcomes": sum(not r["correct_status"] for r in rows),
                }
            )
        try:
            with httpx.Client(trust_env=False, timeout=3) as client:
                prom_version = (
                    client.get(config.prometheus_url.rstrip("/") + "/api/v1/status/buildinfo")
                    .json()
                    .get("data", {})
                    .get("version")
                )
        except Exception:
            prom_version = None
        pids_before = {p["pid"]: p for p in before.get("platform_processes", [])}
        process_usage = []
        for p in after.get("platform_processes", []):
            old = pids_before.get(p["pid"])
            if old and p["started_at"] == old["started_at"]:
                delta = max(0, p["cpu_seconds"] - old["cpu_seconds"])
                process_usage.append(
                    {
                        "pid": p["pid"],
                        "cpu_seconds_delta": delta,
                        "average_cpu_percent_one_core": delta / duration * 100,
                        "working_set_bytes_before": old["working_set_bytes"],
                        "working_set_bytes_after": p["working_set_bytes"],
                    }
                )
        report = {
            "schema_version": "1",
            "source": "actual_live_control_and_http",
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "load": {
                "concurrent_roles": roles,
                "max_concurrent_http_requests": 5,
                "real_active_mysql_instances": 3,
                "instance_ids": asset_ids,
                "cycles_per_role": args.cycles,
                "requests": len(samples),
                "duration_seconds": duration,
                "requests_per_second": len(samples) / duration,
                "cache_condition": "Existing running services and warmed caches; no cache reset; not a cold-start benchmark.",
                "history_window_seconds": 1800,
                "trend_window_seconds": 86400,
                "trend_step_seconds": 60,
                "scope_note": "Grants unchanged. Successful business queries cover A/B; C contributes actual continuous collection load and remains inaccessible to these data readers.",
            },
            "environment": {
                "before": before,
                "after": after,
                "versions": {
                    "python": platform.python_version(),
                    "fastapi": version("fastapi"),
                    "sqlalchemy": version("sqlalchemy"),
                    "pymysql": version("pymysql"),
                    "prometheus": prom_version,
                    "control_mysql": collection["control_mysql_version"],
                },
            },
            "http": {
                "summaries": summaries,
                "unexpected_outcomes": sum(not r["correct_status"] for r in samples),
                "samples": samples,
            },
            "collection_last_30_minutes": collection,
            "observed_platform_process_resources": process_usage,
            "limitations": [
                "Only three real instances, so ten-instance NFR capacity remains unverified.",
                "The 24-hour trend query has roughly one hour of real history; missing earlier points remain null. A representative populated 24-hour dataset is not available.",
                "Collection ran throughout; no collection-off target CPU/query-latency baseline exists, so attributable monitoring overhead is not measured.",
                "Collection window includes deliberate service restarts and controlled fault validation; it is not labeled a fault-free steady-state window.",
                "Expected authorization denials are reported separately and never improve successful-data-query latency percentiles.",
                "Host resource use includes concurrent VM/UI/fault-validation activity; no production capacity claim.",
            ],
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "requests": len(samples),
                    "unexpected_outcomes": report["http"]["unexpected_outcomes"],
                    "duration_seconds": duration,
                    "summaries": summaries,
                },
                ensure_ascii=False,
            )
        )
    finally:
        for role in list(verification.actors):
            try:
                verification.request(role, "POST", "/auth/logout")
            except Exception:
                pass
        verification.close()
        engine.dispose()


if __name__ == "__main__":
    main()
