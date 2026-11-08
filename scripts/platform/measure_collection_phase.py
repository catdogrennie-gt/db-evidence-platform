"""Observe actual collection after a controlled reload; no target I/O or writes."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
import json
from pathlib import Path
import sys
import time

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from platform_app import models as m
from platform_app.config import load_config
from platform_app.db import create_db_engine, create_session_factory, utcnow
from platform_app.common import iso
from scripts.platform.measure_performance import distribution


def observe(factory, ids, start, end):
    with factory() as db:
        runs = list(
            db.scalars(
                select(m.CollectionRun).where(
                    m.CollectionRun.instance_id.in_(ids),
                    m.CollectionRun.started_at >= start - timedelta(minutes=10),
                    m.CollectionRun.started_at < end,
                )
            )
        )
        tasks = list(
            db.scalars(
                select(m.Task).where(
                    m.Task.instance_id.in_(ids),
                    m.Task.type == "collect_instance",
                    m.Task.started_at >= start,
                    m.Task.started_at < end,
                )
            )
        )
        frames = list(
            db.scalars(
                select(m.MetricFrame).where(
                    m.MetricFrame.instance_id.in_(ids),
                    m.MetricFrame.observed_at >= start - timedelta(minutes=10),
                    m.MetricFrame.observed_at < end,
                )
            )
        )
        schedules = list(
            db.scalars(
                select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id.in_(ids))
            )
        )
        evidence = list(
            db.execute(
                select(m.Evidence.run_id, m.Evidence.observed_at, m.Evidence.ingested_at).where(
                    m.Evidence.instance_id.in_(ids),
                    m.Evidence.source_mode == "live",
                    m.Evidence.observed_at >= start - timedelta(minutes=10),
                    m.Evidence.observed_at < end,
                )
            )
        )
        by_run = defaultdict(list)
        for runid, stamp, ingested in evidence:
            by_run[runid].append((stamp, ingested))
        owners = []
        for s in schedules:
            selected = [
                r for r in runs if r.instance_id == s.instance_id and r.collector == s.collector
            ]
            stamps = sorted(
                min(x[0] for x in by_run[r.id])
                for r in selected
                if r.status == "succeeded" and by_run[r.id]
            )
            current = [t for t in stamps if start <= t < end]
            selected_frames = [
                f
                for f in frames
                if f.instance_id == s.instance_id
                and f.collector == s.collector
                and start <= f.observed_at < end
            ]
            ages = []
            unknown = stale = 0
            index = -1
            for second in range(int((end - start).total_seconds())):
                point = start + timedelta(seconds=second)
                while index + 1 < len(stamps) and stamps[index + 1] <= point:
                    index += 1
                if index < 0:
                    unknown += 1
                else:
                    age = (point - stamps[index]).total_seconds()
                    ages.append(age)
                    stale += age > s.interval_seconds * 1.5
            inside = [r for r in selected if start <= r.started_at < end]
            owners.append(
                {
                    "instance_id": s.instance_id,
                    "collector": s.collector,
                    "period_seconds": s.interval_seconds,
                    "observations": len(current),
                    "status_counts": dict(Counter(r.status for r in inside)),
                    "observation_times": [iso(t) for t in current],
                    "interval_seconds": distribution(
                        (b - a).total_seconds() for a, b in zip(current, current[1:])
                    ),
                    "age_seconds": distribution(ages),
                    "age_unknown_seconds": unknown,
                    "age_over_1_5_period_seconds": stale,
                    "profile_duration_ms": distribution(
                        r.duration_ms for r in inside if r.duration_ms is not None
                    ),
                    "rate_validity_counts": dict(Counter(f.rate_validity for f in selected_frames)),
                    "rate_reasons": dict(
                        Counter(
                            reason
                            for f in selected_frames
                            for reason in (f.derived_values or {}).get("rate_reasons", [])
                        )
                    ),
                }
            )
        return {
            "from": iso(start),
            "to": iso(end),
            "owners": owners,
            "tasks": {
                "count": len(tasks),
                "bundle_size_counts": dict(
                    Counter(len(t.payload.get("collectors", [])) for t in tasks)
                ),
                "queue_wait_seconds": distribution(
                    (t.started_at - t.due_at).total_seconds()
                    for t in tasks
                    if t.started_at and t.due_at
                ),
                "duration_seconds": distribution(
                    (t.finished_at - t.started_at).total_seconds() for t in tasks if t.finished_at
                ),
            },
            "task_records": [
                {
                    "id": t.id,
                    "instance_id": t.instance_id,
                    "collectors": t.payload.get("collectors", []),
                    "started_at": iso(t.started_at),
                    "finished_at": iso(t.finished_at),
                    "status": t.status,
                    "attempt": t.attempt,
                }
                for t in tasks
            ],
            "run_records": [
                {
                    "task_id": r.task_id,
                    "collector": r.collector,
                    "status": r.status,
                    "started_at": iso(r.started_at),
                    "finished_at": iso(r.finished_at),
                    "duration_ms": r.duration_ms,
                    "error_code": r.error_code,
                }
                for r in runs
                if start <= r.started_at < end
            ],
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--duration-seconds", type=int, default=600)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.is_relative_to(ROOT) or not 300 <= args.duration_seconds <= 1800:
        raise SystemExit("Use private output and a300–1800s window.")
    config = load_config(args.config)
    engine = create_db_engine(config)
    factory = create_session_factory(engine, config)
    with factory() as db:
        ids = sorted(
            db.scalars(
                select(m.Instance.id).where(
                    m.Instance.enabled.is_(True), m.Instance.lifecycle_state == "Active"
                )
            )
        )
    if len(ids) != 3:
        raise RuntimeError("Expected exactly three real active assets.")
    start = utcnow()
    before = observe(factory, ids, start - timedelta(minutes=10), start)
    print(
        json.dumps(
            {
                "phase": "started",
                "from": iso(start),
                "duration_seconds": args.duration_seconds,
                "actual_instances": len(ids),
            }
        ),
        flush=True,
    )
    deadline = time.monotonic() + args.duration_seconds
    progress = []
    next_progress = 0
    try:
        while time.monotonic() < deadline:
            now = utcnow()
            if time.monotonic() >= next_progress:
                with factory() as db:
                    statuses = Counter(
                        db.scalars(
                            select(m.Task.status).where(
                                m.Task.instance_id.in_(ids),
                                m.Task.type == "collect_instance",
                                m.Task.started_at >= start,
                            )
                        )
                    )
                    heartbeats = list(
                        db.scalars(
                            select(m.WorkerHeartbeat).where(
                                m.WorkerHeartbeat.queue == "collection",
                                m.WorkerHeartbeat.last_seen_at >= start,
                            )
                        )
                    )
                sample = {
                    "at": iso(now),
                    "task_status_counts": dict(statuses),
                    "worker_ids": [h.worker_id for h in heartbeats],
                }
                progress.append(sample)
                print(json.dumps(sample), flush=True)
                next_progress = time.monotonic() + 60
            time.sleep(min(15, max(0.01, deadline - time.monotonic())))
        end = utcnow()
        report = {
            "source": "actual_live_collection_after_stable_phase_reload",
            "started_at": iso(start),
            "finished_at": iso(end),
            "instance_ids": ids,
            "before_10_minutes": before,
            "after_full_window": observe(factory, ids, start, end),
            "after_excluding_first_two_transition_minutes": observe(
                factory, ids, start + timedelta(minutes=2), end
            ),
            "progress": progress,
            "limitations": [
                "Initial phase realignment and worker restart produce real gaps/invalid windows; full results are preserved separately from the explicit two-minute transition exclusion.",
                "Three actual instances only; no ten-instance capacity claim. No worker or database restart/DDL was scheduled within this observation window.",
                "Measured profile durations include transport setup for the first owner, so they are not attributed exclusively to SQL execution.",
                "Age is derived from actual successful source observations; failed runs and invalid rates remain separately visible.",
            ],
        }
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "phase": "finished",
                    "output": output.name,
                    "tasks": report["after_full_window"]["tasks"],
                }
            ),
            flush=True,
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
