from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select, func
from platform_app.db import Base, create_db_engine, create_session_factory, utcnow
from platform_app import models as m
from platform_app.access import ROLE_PERMISSIONS, actor_for_user
from platform_app.events import evaluate, clock_tick, merge_events, event_command, get_event


@pytest.fixture
def db():
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    with create_session_factory(engine)() as value:
        yield value
    engine.dispose()


def graph(db):
    dep = m.Department(name="test")
    db.add(dep)
    db.flush()
    system = m.System(name="test", department_id=dep.id)
    db.add(system)
    db.flush()
    a = m.Instance(
        name="a",
        system_id=system.id,
        endpoint="127.0.0.1",
        expected_server_uuid="uuid-a",
        enabled=True,
        lifecycle_state="Active",
    )
    b = m.Instance(
        name="b",
        system_id=system.id,
        endpoint="127.0.0.1",
        expected_server_uuid="uuid-b",
        enabled=True,
        lifecycle_state="Active",
    )
    user = m.User(username="test-dba", password_hash="unused")
    role = m.Role(name="dba", permissions=sorted(ROLE_PERMISSIONS["dba"]))
    db.add_all([a, b, user, role])
    db.flush()
    grant = m.UserGrant(user_id=user.id, role_id=role.id, scope_type="instance", scope_id=a.id)
    db.add(grant)
    db.commit()
    return actor_for_user(db, user.id), a, b, grant


def observe(
    db,
    instance,
    stamp,
    source="mysql.lock_waits",
    rows=None,
    availability="available",
    collector="runtime",
):
    task = m.Task(type="collect", queue="collection", instance_id=instance.id)
    db.add(task)
    db.flush()
    run = m.CollectionRun(
        task_id=task.id,
        instance_id=instance.id,
        collector=collector,
        status="succeeded",
        started_at=stamp,
        finished_at=stamp,
    )
    db.add(run)
    db.flush()
    e = m.Evidence(
        instance_id=instance.id,
        run_id=run.id,
        source=source,
        observed_at=stamp,
        server_uuid=instance.expected_server_uuid,
        payload={
            "rows": rows if rows is not None else [{"waiting_trx_id": "w", "blocking_trx_id": "b"}]
        },
        coverage={
            "availability": availability,
            "collector": collector,
            "interval_seconds": 30,
            "complete": True,
        },
        payload_sha256="a" * 64,
    )
    db.add(e)
    db.flush()
    return run, e


def manual_event(db, instance):
    event = m.Event(instance_id=instance.id, title="test", source="human_assertion")
    db.add(event)
    db.flush()
    return event


def test_duration_only_advances_with_new_real_observations_and_recurrence_keeps_episode(db):
    actor, a, b, _ = graph(db)
    start = utcnow() - timedelta(minutes=5)
    for seconds in (0, 30):
        run, _ = observe(db, a, start + timedelta(seconds=seconds))
        evaluate(db, a.id, run.id, now=start + timedelta(seconds=seconds))
        evaluate(db, a.id, run.id, now=start + timedelta(seconds=seconds + 20))
    assert db.scalar(select(func.count()).select_from(m.AlertOccurrence)) == 0
    run, _ = observe(db, a, start + timedelta(seconds=60))
    evaluate(db, a.id, run.id, now=start + timedelta(seconds=60))
    episode = db.scalar(select(m.AlertOccurrence))
    assert episode.status == "firing"
    assert db.scalar(select(func.count()).select_from(m.Event)) == 1
    run, _ = observe(db, a, start + timedelta(seconds=90), rows=[])
    evaluate(db, a.id, run.id, now=start + timedelta(seconds=90))
    assert episode.status == "recovered"
    assert db.scalar(select(m.Event)).status == "pending_verification"
    for seconds in (120, 150, 180):
        run, _ = observe(db, a, start + timedelta(seconds=seconds))
        evaluate(db, a.id, run.id, now=start + timedelta(seconds=seconds))
    assert db.scalar(select(func.count()).select_from(m.AlertOccurrence)) == 2
    assert db.scalar(select(func.count()).select_from(m.EvidenceManifest)) >= 2


def test_clock_tick_marks_firing_unknown_without_new_target_data(db):
    actor, a, b, _ = graph(db)
    start = utcnow() - timedelta(minutes=5)
    for seconds in (0, 30, 60):
        run, _ = observe(db, a, start + timedelta(seconds=seconds))
        evaluate(db, a.id, run.id, now=start + timedelta(seconds=seconds))
    episode = db.scalar(select(m.AlertOccurrence))
    clock_tick(db, now=start + timedelta(seconds=110))
    assert episode.status == "unknown"
    assert episode.recovered_at is None


def test_identity_status_staleness_cannot_borrow_repeated_status_items_from_runtime(db):
    actor, a, _, _ = graph(db)
    start = utcnow() - timedelta(minutes=5)
    run, _ = observe(
        db, a, start, source="mysql.global_status", rows=[], collector="identity_status"
    )
    evaluate(db, a.id, run.id, now=start)
    observe(
        db,
        a,
        start + timedelta(seconds=100),
        source="mysql.global_status",
        rows=[],
        collector="runtime",
    )
    clock_tick(db, now=start + timedelta(seconds=100))
    active = db.scalar(
        select(m.ActiveAlert).where(
            m.ActiveAlert.instance_id == a.id, m.ActiveAlert.rule_version == "collector_stale:v1"
        )
    )
    assert active.state == "firing"


def test_latest_core_failure_is_unknown_even_when_previous_success_is_fresh(db):
    actor, a, _, _ = graph(db)
    start = utcnow() - timedelta(minutes=1)
    run, _ = observe(
        db, a, start, source="mysql.global_status", rows=[], collector="identity_status"
    )
    evaluate(db, a.id, run.id, now=start)
    run, _ = observe(
        db,
        a,
        start + timedelta(seconds=10),
        source="mysql.global_status",
        rows=[],
        collector="identity_status",
        availability="unreachable",
    )
    evaluate(db, a.id, run.id, now=start + timedelta(seconds=10))
    active = db.scalar(
        select(m.ActiveAlert).where(
            m.ActiveAlert.instance_id == a.id, m.ActiveAlert.rule_version == "collector_stale:v1"
        )
    )
    assert active.state == "unknown"


def test_core_connect_failure_with_only_identity_evidence_is_unknown(db):
    actor, a, _, _ = graph(db)
    start = utcnow() - timedelta(minutes=1)
    run, _ = observe(
        db, a, start, source="mysql.global_status", rows=[], collector="identity_status"
    )
    evaluate(db, a.id, run.id, now=start)
    run, _ = observe(
        db,
        a,
        start + timedelta(seconds=10),
        source="mysql.identity",
        rows=[],
        collector="identity_status",
        availability="unreachable",
    )
    run.status = "failed"
    db.flush()
    evaluate(db, a.id, run.id, now=start + timedelta(seconds=10))
    active = db.scalar(
        select(m.ActiveAlert).where(
            m.ActiveAlert.instance_id == a.id, m.ActiveAlert.rule_version == "collector_stale:v1"
        )
    )
    assert active.state == "unknown"


def test_cross_instance_merge_rejected_and_same_instance_invalidates_coverage(db):
    actor, a, b, grant = graph(db)
    grant.scope_type = "global"
    grant.scope_id = "*"
    db.flush()
    target = manual_event(db, a)
    other = manual_event(db, b)
    source = manual_event(db, a)
    from platform_app.common import PlatformError

    with pytest.raises(PlatformError) as out:
        merge_events(
            actor,
            {
                "target_event_id": target.id,
                "source_event_ids": [other.id],
                "expected_versions": {target.id: 1, other.id: 1},
                "reason": "test",
            },
        )
    assert out.value.status_code == 409
    merge_events(
        actor,
        {
            "target_event_id": target.id,
            "source_event_ids": [source.id],
            "expected_versions": {target.id: 1, source.id: 1},
            "reason": "test",
        },
    )
    assert target.coverage_revision == 2 and source.status == "merged"
    assert source.merged_into_event_id == target.id


def test_self_merge_is_rejected_without_corrupting_event(db):
    from platform_app.common import PlatformError

    actor, a, _, _ = graph(db)
    event = manual_event(db, a)
    with pytest.raises(PlatformError):
        merge_events(
            actor,
            {
                "target_event_id": event.id,
                "source_event_ids": [event.id],
                "expected_versions": {event.id: 1},
                "reason": "self merge",
            },
        )
    assert event.status == "open" and event.merged_into_event_id is None


def test_merge_retains_explicit_source_manifest_links(db):
    from platform_app.events import _snapshot

    actor, a, _, _ = graph(db)
    target = manual_event(db, a)
    source = manual_event(db, a)
    _snapshot(db, source)
    original_manifest = source.manifest_id
    merge_events(
        actor,
        {
            "target_event_id": target.id,
            "source_event_ids": [source.id],
            "expected_versions": {target.id: 1, source.id: 1},
            "reason": "preserve original proof",
        },
    )
    references = list(
        db.scalars(
            select(m.ManifestItem).where(
                m.ManifestItem.manifest_id == target.manifest_id,
                m.ManifestItem.item_type == "manifest_reference",
            )
        )
    )
    assert original_manifest in {item.payload["manifest_id"] for item in references}


def test_developer_can_comment_but_cannot_assign_or_close(db):
    actor, a, _, grant = graph(db)
    role = m.Role(name="dev", permissions=sorted(ROLE_PERMISSIONS["developer"]))
    db.add(role)
    db.flush()
    grant.role_id = role.id
    db.flush()
    event = manual_event(db, a)
    event_command(
        actor, event.id, "comment", {"expected_version": 1, "body": "operator observation"}
    )
    from fastapi import HTTPException

    for command in ("assign", "close"):
        with pytest.raises(HTTPException):
            event_command(
                actor,
                event.id,
                command,
                {
                    "expected_version": event.version,
                    "closure_type": "manual_exception",
                    "reason": "test",
                },
            )
    assert event.status == "open"


def test_one_recovered_episode_does_not_close_multi_alert_event(db):
    actor, a, _, _ = graph(db)
    start = utcnow() - timedelta(minutes=5)
    for seconds in (0, 30, 60):
        run, _ = observe(db, a, start + timedelta(seconds=seconds))
        evaluate(db, a.id, run.id, now=start + timedelta(seconds=seconds))
    event = db.scalar(select(m.Event))
    occ = db.scalar(select(m.AlertOccurrence))
    occ.status = "unknown"
    from platform_app.common import PlatformError

    with pytest.raises(PlatformError):
        event_command(
            actor,
            event.id,
            "close",
            {
                "expected_version": event.version,
                "closure_type": "verified_recovery",
                "reason": "test",
            },
        )
    assert event.status != "closed"


def test_repeated_fresh_recovery_proofs_keep_episode_closeable_without_rewriting_first_recovery(db):
    from platform_app.events import episodes_recovered

    actor, a, _, _ = graph(db)
    start = utcnow() - timedelta(minutes=5)
    for seconds in (0, 30, 60):
        run, _ = observe(db, a, start + timedelta(seconds=seconds))
        evaluate(db, a.id, run.id, now=start + timedelta(seconds=seconds))
    episode = db.scalar(select(m.AlertOccurrence))
    event = db.scalar(select(m.Event))
    for seconds in (90, 120, 150):
        run, _ = observe(db, a, start + timedelta(seconds=seconds), rows=[])
        evaluate(db, a.id, run.id, now=start + timedelta(seconds=seconds))
    assert episode.recovered_at == start + timedelta(seconds=90)
    assert episodes_recovered(db, event, start + timedelta(seconds=151))


def test_all_episodes_need_fresh_recovery_and_matching_current_verification(db):
    from platform_app.common import PlatformError

    actor, a, _, _ = graph(db)
    event = manual_event(db, a)
    now = utcnow()
    episodes = []
    rule_versions = {}
    for index in range(2):
        rule = m.Rule(name=f"rule{index}", rule_version=f"rule-{index}", template="locks_present")
        db.add(rule)
        db.flush()
        evaluation = m.RuleEvaluation(
            instance_id=a.id,
            rule_id=rule.id,
            rule_version=rule.rule_version,
            evaluation_key=f"key-{index}",
            state="normal",
        )
        db.add(evaluation)
        db.flush()
        active = m.ActiveAlert(
            instance_id=a.id, rule_version=rule.rule_version, dimension_fingerprint=str(index)
        )
        db.add(active)
        db.flush()
        occurrence = m.AlertOccurrence(
            instance_id=a.id,
            active_alert_id=active.id,
            evaluation_id=evaluation.id,
            status="recovered",
            recovered_at=now,
            evidence={
                "latest": {
                    "recovery_observation": True,
                    "source_observed_at": now.isoformat() + "Z",
                    "freshness_seconds": 45,
                }
            },
        )
        db.add(occurrence)
        db.flush()
        db.add(m.EventAlert(event_id=event.id, alert_occurrence_id=occurrence.id))
        episodes.append(occurrence)
        rule_versions[occurrence.id] = rule.rule_version
    db.flush()

    def verified(selected):
        row = m.Verification(
            event_id=event.id,
            instance_id=a.id,
            created_by=actor.user_id,
            event_version=event.version,
            status="completed",
            before_start=now - timedelta(minutes=4),
            before_end=now - timedelta(minutes=3),
            after_start=now - timedelta(minutes=2),
            after_end=now,
            template="locks_absent",
            criteria={
                "coverage_revision": event.coverage_revision,
                "episode_ids": [e.id for e in selected],
                "episode_rule_versions": {e.id: rule_versions[e.id] for e in selected},
            },
            result={"frozen_input_valid": True},
            outcome="improved",
        )
        db.add(row)
        db.flush()
        return row

    verified([episodes[0]])
    with pytest.raises(PlatformError):
        event_command(
            actor,
            event.id,
            "close",
            {
                "expected_version": event.version,
                "closure_type": "verified_recovery",
                "reason": "one issue only",
            },
        )
    verified([episodes[1]])
    episodes[1].status = "unknown"
    with pytest.raises(PlatformError):
        event_command(
            actor,
            event.id,
            "close",
            {
                "expected_version": event.version,
                "closure_type": "verified_recovery",
                "reason": "unknown issue",
            },
        )
    episodes[1].status = "recovered"
    result = event_command(
        actor,
        event.id,
        "close",
        {
            "expected_version": event.version,
            "closure_type": "verified_recovery",
            "reason": "all selected conditions verified",
        },
    )
    assert result["status"] == "closed" and result["closure_type"] == "verified_recovery"


def test_canonical_http_paths_and_eligible_assignees(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from platform_app.routes.events import router
    from platform_app.identity import current_actor

    actor, a, _, _ = graph(db)
    event = manual_event(db, a)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_actor] = lambda: actor
    with TestClient(app) as client:
        detail = client.get(f"/api/v1/events/{event.id}")
        assert detail.status_code == 200
        assert detail.json()["eligible_assignees"] == [
            {"id": actor.user_id, "display_name": "test-dba"}
        ]
        denied = client.post(
            f"/api/v1/events/{event.id}/comments",
            json={"expected_version": 999, "body": "stale form"},
        )
        assert denied.status_code == 409
        note = client.post(
            f"/api/v1/events/{event.id}/comments",
            json={"expected_version": 1, "body": "reviewable note"},
        )
        assert note.status_code == 200
        plan = client.post(
            f"/api/v1/events/{event.id}/remediation-plans",
            json={
                "expected_version": note.json()["version"],
                "steps": ["Human-only operation"],
                "risks": ["review context"],
                "rollback": ["re-evaluate"],
            },
        )
        assert plan.status_code == 201
