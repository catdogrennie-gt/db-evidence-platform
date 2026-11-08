"""Fenced collection persistence; network fixtures never count as live proof."""

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from sqlalchemy import select, func
import pytest
from platform_app import models as m
from platform_app.db import Base, create_db_engine, create_session_factory, utcnow
from platform_app.common import PlatformError
from platform_app.connectors import ConnectorError
from platform_app.scheduling import enqueue, claim
from platform_app.collectors.logs import result


@pytest.fixture
def pipeline(monkeypatch):
    from platform_app import collection_service as service

    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory.begin() as db:
        dep = m.Department(name="pipeline fixture")
        db.add(dep)
        db.flush()
        system = m.System(name="pipeline fixture", department_id=dep.id)
        db.add(system)
        db.flush()
        asset = m.Instance(
            name="synthetic fixture",
            system_id=system.id,
            endpoint="127.0.0.1",
            expected_server_uuid="expected",
            credential_ref="private-ref",
            enabled=True,
            lifecycle_state="Active",
            connection={
                "mode": "direct",
                "db_host": "127.0.0.1",
                "tls_mode": "disabled",
                "host_profile_ref": "host",
            },
        )
        db.add(asset)
        db.flush()
        identifier = asset.id
        db.add(m.InstanceLease(instance_id=identifier))
        for name in ("identity_status", "runtime", "host"):
            db.add(m.CollectionSchedule(instance_id=identifier, collector=name))
    monkeypatch.setattr(
        service,
        "SecretStore",
        lambda config: SimpleNamespace(
            resolve=lambda ref: {"username": "test", "password": "never persisted"}
        ),
    )
    monkeypatch.setattr(service, "validate_target", lambda config, value: dict(value))
    monkeypatch.setattr(service, "evaluate", lambda *args, **kwargs: None)
    config = SimpleNamespace(
        host_profiles=[{"id": "host", "ssh_profile_ref": "ssh"}],
        log_profiles=[],
        tls_profiles=[],
        known_hosts_path="private",
        ssh_profiles=[],
    )
    yield service, factory, identifier, config
    engine.dispose()


def leased(factory, identifier, collectors, activation=False):
    with factory.begin() as db:
        actor_id = None
        if activation:
            row = db.get(m.Instance, identifier)
            row.lifecycle_state = "Activating"
            row.enabled = False
            user = m.User(username="activation", password_hash="unused")
            role = m.Role(name="activation", permissions=["asset.manage"])
            db.add_all([user, role])
            db.flush()
            actor_id = user.id
            db.add(
                m.UserGrant(
                    user_id=user.id, role_id=role.id, scope_type="instance", scope_id=identifier
                )
            )
        task = enqueue(
            db,
            "activate_instance" if activation else "collect_instance",
            instance_id=identifier,
            actor_id=actor_id,
            payload={"collectors": collectors, "config_revision": 1},
        )
    return claim(factory, "fixture-worker")


@contextmanager
def working(*args, **kwargs):
    yield object()


def core_batch(identifier="expected"):
    return {
        "observed_at": utcnow().isoformat() + "Z",
        "server_uuid": identifier,
        "evidence_items": [
            result("mysql.identity", {"rows": [{"server_uuid": identifier}]}),
            result("mysql.global_status", {"rows": [{"Variable_name": "Uptime", "Value": "100"}]}),
        ],
    }


def test_successful_collection_preserves_scheduler_reserved_phase(pipeline, monkeypatch):
    from platform_app import worker

    s, factory, identifier, config = pipeline
    anchor = utcnow() - timedelta(seconds=121)
    current = anchor + timedelta(seconds=121)
    monkeypatch.setattr(worker, "db_now", lambda db: current)
    with factory.begin() as db:
        db.get(m.Instance, identifier).created_at = anchor
        for schedule in db.scalars(select(m.CollectionSchedule)):
            schedule.enabled = schedule.collector == "identity_status"
            schedule.next_due_at = anchor + timedelta(seconds=120)
    task_ids = worker.schedule_due(factory)
    assert len(task_ids) == 1
    with factory() as db:
        reserved = db.scalar(
            select(m.CollectionSchedule).where(m.CollectionSchedule.collector == "identity_status")
        ).next_due_at
    assert reserved == anchor + timedelta(seconds=150)
    lease = claim(factory, "phase-worker")
    monkeypatch.setattr(s, "open_mysql", working)
    monkeypatch.setattr(s, "collect_mysql", lambda *a, **k: core_batch())
    s.handle_collection(factory, lease, config)
    with factory() as db:
        assert (
            db.scalar(
                select(m.CollectionSchedule).where(
                    m.CollectionSchedule.collector == "identity_status"
                )
            ).next_due_at
            == reserved
        )


@pytest.mark.parametrize("activation", [False, True])
def test_expired_due_after_success_realigns_once_on_same_asset_anchor(
    pipeline, monkeypatch, activation
):
    s, factory, identifier, config = pipeline
    anchor = utcnow() - timedelta(seconds=121)
    current = anchor + timedelta(seconds=121)
    with factory.begin() as db:
        db.get(m.Instance, identifier).created_at = anchor
        db.scalar(
            select(m.CollectionSchedule).where(m.CollectionSchedule.collector == "identity_status")
        ).next_due_at = anchor + timedelta(seconds=90)
    lease = leased(factory, identifier, ["identity_status"], activation=activation)
    monkeypatch.setattr(s, "db_now", lambda db: current)
    monkeypatch.setattr(s, "open_mysql", working)
    monkeypatch.setattr(s, "collect_mysql", lambda *a, **k: core_batch())
    s.handle_collection(factory, lease, config)
    with factory() as db:
        schedule = db.scalar(
            select(m.CollectionSchedule).where(m.CollectionSchedule.collector == "identity_status")
        )
        assert schedule.next_due_at == anchor + timedelta(seconds=180)
        assert schedule.consecutive_failures == 0


def test_failed_collection_keeps_explicit_backoff_even_when_phase_was_reserved(
    pipeline, monkeypatch
):
    s, factory, identifier, config = pipeline
    current = utcnow()
    with factory.begin() as db:
        db.scalar(
            select(m.CollectionSchedule).where(m.CollectionSchedule.collector == "identity_status")
        ).next_due_at = current + timedelta(seconds=20)
    lease = leased(factory, identifier, ["identity_status"])
    monkeypatch.setattr(s, "db_now", lambda db: current)

    @contextmanager
    def broken(*a, **k):
        raise ConnectorError("unreachable")
        yield

    monkeypatch.setattr(s, "open_mysql", broken)
    s.handle_collection(factory, lease, config)
    with factory() as db:
        schedule = db.scalar(
            select(m.CollectionSchedule).where(m.CollectionSchedule.collector == "identity_status")
        )
        assert schedule.consecutive_failures == 1 and schedule.next_due_at == current + timedelta(
            seconds=60
        )


def test_uuid_mismatch_quarantines_without_recording_foreign_evidence(pipeline, monkeypatch):
    s, factory, identifier, config = pipeline
    lease = leased(factory, identifier, ["identity_status"])
    monkeypatch.setattr(s, "open_mysql", working)
    monkeypatch.setattr(s, "collect_mysql", lambda *a, **k: core_batch("foreign"))
    s.handle_collection(factory, lease, config)
    with factory() as db:
        assert db.get(m.Instance, identifier).lifecycle_state == "Quarantined"
        assert db.scalar(select(func.count()).select_from(m.Evidence)) == 0
        assert db.get(m.Task, lease.id).status == "failed"
        assert all(not row.enabled for row in db.scalars(select(m.CollectionSchedule)))


def test_host_continues_when_database_transport_fails(pipeline, monkeypatch):
    s, factory, identifier, config = pipeline
    lease = leased(factory, identifier, ["identity_status", "host"])

    @contextmanager
    def broken(*a, **k):
        raise ConnectorError("unreachable")
        yield

    monkeypatch.setattr(s, "open_mysql", broken)
    monkeypatch.setattr(s, "open_ssh", working)
    monkeypatch.setattr(s, "resolve_ssh_profile", lambda *a: {})
    monkeypatch.setattr(
        s, "collect_host", lambda *a, **k: result("linux.host", {"values": {"load1": 1}})
    )
    s.handle_collection(factory, lease, config)
    with factory() as db:
        rows = list(db.scalars(select(m.CollectionRun).order_by(m.CollectionRun.collector)))
        assert {r.collector: r.status for r in rows} == {
            "host": "succeeded",
            "identity_status": "failed",
        }
        assert (
            db.scalar(select(m.Evidence).where(m.Evidence.source == "linux.host")).coverage[
                "availability"
            ]
            == "available"
        )
        assert db.get(m.Instance, identifier).last_success_at is None


def test_schema_transport_failure_preserves_column_source_gap(pipeline, monkeypatch):
    s, factory, identifier, config = pipeline
    lease = leased(factory, identifier, ["schema_config"])

    @contextmanager
    def broken(*a, **k):
        raise ConnectorError("unreachable")
        yield

    monkeypatch.setattr(s, "open_mysql", broken)
    s.handle_collection(factory, lease, config)
    with factory() as db:
        sources = {row.source: row.coverage for row in db.scalars(select(m.Evidence))}
        assert set(sources) == {
            "mysql.identity",
            "mysql.tables",
            "mysql.columns",
            "mysql.indexes",
            "mysql.configuration",
        }
        assert all(
            value["availability"] == "unreachable" and value["error_code"] == "unreachable"
            for value in sources.values()
        )


def test_configuration_changed_during_network_read_rejects_result(pipeline, monkeypatch):
    s, factory, identifier, config = pipeline
    lease = leased(factory, identifier, ["identity_status"])
    monkeypatch.setattr(s, "open_mysql", working)

    def mutate(*a, **k):
        with factory.begin() as db:
            db.get(m.Instance, identifier).config_revision = 2
        return core_batch()

    monkeypatch.setattr(s, "collect_mysql", mutate)
    with pytest.raises(PlatformError):
        s.handle_collection(factory, lease, config)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(m.Evidence)) == 0


def test_failed_core_activation_returns_to_ready_and_never_enables_schedules(pipeline, monkeypatch):
    s, factory, identifier, config = pipeline
    lease = leased(factory, identifier, ["identity_status"], True)

    @contextmanager
    def broken(*a, **k):
        raise ConnectorError("authentication_failed")
        yield

    monkeypatch.setattr(s, "open_mysql", broken)
    s.handle_collection(factory, lease, config)
    with factory() as db:
        asset = db.get(m.Instance, identifier)
        assert asset.lifecycle_state == "Ready" and asset.enabled is False
        assert all(not row.enabled for row in db.scalars(select(m.CollectionSchedule)))


def test_automated_actor_cannot_activate_inactive_asset(pipeline, monkeypatch):
    s, factory, identifier, config = pipeline
    with factory.begin() as db:
        asset = db.get(m.Instance, identifier)
        asset.lifecycle_state = "Activating"
        asset.enabled = False
        enqueue(db, "activate_instance", instance_id=identifier, payload={"config_revision": 1})
    lease = claim(factory, "automated")
    with pytest.raises(PlatformError) as out:
        s.handle_collection(factory, lease, config)
    assert out.value.detail["code"] == "activation_actor_required"


def test_optional_failure_still_activates_but_marks_degraded(pipeline, monkeypatch):
    s, factory, identifier, config = pipeline
    lease = leased(factory, identifier, ["identity_status", "host"], True)
    monkeypatch.setattr(s, "open_mysql", working)
    monkeypatch.setattr(s, "collect_mysql", lambda *a, **k: core_batch())
    monkeypatch.setattr(s, "resolve_ssh_profile", lambda *a: {})

    @contextmanager
    def broken(*a, **k):
        raise ConnectorError("ssh_authentication_failed")
        yield

    monkeypatch.setattr(s, "open_ssh", broken)
    s.handle_collection(factory, lease, config)
    with factory() as db:
        asset = db.get(m.Instance, identifier)
        assert asset.enabled and asset.lifecycle_state == "Active" and asset.health == "degraded"
        assert all(row.enabled for row in db.scalars(select(m.CollectionSchedule)))
        assert db.get(m.Task, lease.id).result["partial"] is True


def test_optional_owner_cannot_advance_mysql_core_freshness(pipeline, monkeypatch):
    s, factory, identifier, config = pipeline
    old = utcnow() - timedelta(minutes=1)
    with factory.begin() as db:
        asset = db.get(m.Instance, identifier)
        asset.last_success_at = old
        asset.last_attempt_at = old
    lease = leased(factory, identifier, ["runtime"])
    monkeypatch.setattr(s, "open_mysql", working)
    monkeypatch.setattr(s, "collect_mysql", lambda *a, **k: core_batch())
    s.handle_collection(factory, lease, config)
    with factory() as db:
        asset = db.get(m.Instance, identifier)
        assert asset.last_success_at == asset.last_attempt_at == old


def test_group_reuses_database_transport_and_optional_owner_cannot_refresh_core(
    pipeline, monkeypatch
):
    s, factory, identifier, config = pipeline
    lease = leased(factory, identifier, ["identity_status", "runtime"])
    calls = []

    @contextmanager
    def counted(*a, **k):
        calls.append(True)
        yield object()

    monkeypatch.setattr(s, "open_mysql", counted)

    def sample(connection, context, name):
        batch = core_batch()
        if name == "runtime":
            batch["evidence_items"].append(result("mysql.lock_waits", {"rows": []}))
        return batch

    monkeypatch.setattr(s, "collect_mysql", sample)
    s.handle_collection(factory, lease, config)
    with factory() as db:
        core = db.scalar(
            select(m.CollectionRun).where(m.CollectionRun.collector == "identity_status")
        )
        assert db.get(m.Instance, identifier).last_success_at == core.finished_at
        assert db.get(m.Task, lease.id).status == "succeeded"
    assert len(calls) == 1


def test_cancel_requested_during_read_prevents_evidence_commit(pipeline, monkeypatch):
    s, factory, identifier, config = pipeline
    lease = leased(factory, identifier, ["identity_status"])
    monkeypatch.setattr(s, "open_mysql", working)

    def cancel(*a, **k):
        with factory.begin() as db:
            db.get(m.Task, lease.id).cancel_requested = True
        return core_batch()

    monkeypatch.setattr(s, "collect_mysql", cancel)
    with pytest.raises(PlatformError) as out:
        s.handle_collection(factory, lease, config)
    assert out.value.detail["code"] == "task_cancelled"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(m.Evidence)) == 0


def test_file_failure_preserved_beside_bounded_table_fallback(pipeline, monkeypatch):
    s, factory, identifier, config = pipeline
    with factory.begin() as db:
        asset = db.get(m.Instance, identifier)
        asset.connection = dict(asset.connection, log_profile_ref="log")
    config.log_profiles = [
        {
            "id": "log",
            "ssh_profile_ref": "ssh",
            "slow_log_path": "/allowed/slow",
            "allowed_paths": ["/allowed/slow"],
        }
    ]
    lease = leased(factory, identifier, ["slow_log"])

    @contextmanager
    def denied(*a, **k):
        raise ConnectorError("ssh_authentication_failed")
        yield

    monkeypatch.setattr(s, "resolve_ssh_profile", lambda *a: {})
    monkeypatch.setattr(s, "open_ssh", denied)
    monkeypatch.setattr(s, "open_mysql", working)
    monkeypatch.setattr(
        s,
        "collect_mysql",
        lambda *a, **k: {
            "server_uuid": "expected",
            "observed_at": utcnow().isoformat() + "Z",
            "evidence_items": [
                result(
                    "mysql.slow_log.table",
                    {"events": []},
                    coverage={"complete": False, "late_arrival_possible": True},
                )
            ],
        },
    )
    s.handle_collection(factory, lease, config)
    with factory() as db:
        rows = {r.source: r for r in db.scalars(select(m.Evidence))}
        assert rows["mysql.slow_log.file"].coverage["availability"] == "unavailable"
        assert rows["mysql.slow_log.table"].coverage["availability"] == "available"
        assert rows["mysql.slow_log.table"].coverage["late_arrival_possible"] is True
        assert db.scalar(select(m.CollectionRun)).status == "partial"


def test_persisted_string_epoch_does_not_invalidate_next_rate_or_increment_epoch():
    from platform_app.collection_service import _derive_epoch

    stamp = utcnow()
    base = {
        "instance_id": "a",
        "owner_collector": "identity_status",
        "server_uuid": "expected",
        "collector_boot_id": "boot",
        "counter_epoch": 3,
        "source_epoch": "3",
        "uptime_seconds": 100,
        "observed_monotonic_ns": 1_000_000_000,
        "observed_at": stamp.isoformat() + "Z",
        "raw_values": {"Questions": 10},
    }
    frame = dict(
        base,
        observed_at=(stamp + timedelta(seconds=30)).isoformat() + "Z",
        observed_monotonic_ns=31_000_000_000,
        uptime_seconds=130,
        source_epoch=3,
        raw_values={"Questions": 70},
    )
    batch = {"metric_frame": frame}
    _derive_epoch(batch, {"previous_frame": base, "interval_seconds": 30}, "identity_status")
    assert frame["counter_epoch"] == 3 and frame["source_epoch"] == "3"
    assert frame["derived_values"]["qps"] == 2 and frame["window_validity"] == "valid"
    reset = dict(frame, uptime_seconds=2, raw_values={"Questions": 1})
    _derive_epoch(
        {"metric_frame": reset}, {"previous_frame": base, "interval_seconds": 30}, "identity_status"
    )
    assert reset["counter_epoch"] == 4 and reset["derived_values"]["qps"] is None


def connection_lease(factory, identifier):
    from platform_app.inventory import connection_fingerprint

    with factory.begin() as db:
        asset = db.get(m.Instance, identifier)
        user = m.User(username="preflight", password_hash="unused")
        role = m.Role(name="preflight", permissions=["asset.onboard", "asset.manage"])
        db.add_all([user, role])
        db.flush()
        db.add(m.UserGrant(user_id=user.id, role_id=role.id, scope_type="global", scope_id="*"))
        safe = {
            "engine": "mysql",
            "connection": {"mode": "direct", "db_host": "127.0.0.1", "tls_mode": "disabled"},
            "requested_collectors": ["identity_status"],
            "selected_schemas": [],
        }
        fingerprint = connection_fingerprint(asset.system_id, safe, "private-ref")
        test = m.ConnectionTest(
            created_by=user.id,
            system_id=asset.system_id,
            config_fingerprint=fingerprint,
            config=safe,
            secret_ref="private-ref",
            expires_at=utcnow() + timedelta(minutes=20),
        )
        db.add(test)
        db.flush()
        task = enqueue(
            db,
            "connection_test",
            actor_id=user.id,
            payload={"test_id": test.id, "fingerprint": fingerprint},
        )
        test.task_id = task.id
        test_id = test.id
        user_id = user.id
    return claim(factory, "preflight-worker"), test_id, user_id


def test_preflight_failure_persists_safe_failure_and_task_in_same_transaction(
    pipeline, monkeypatch
):
    s, factory, identifier, config = pipeline
    lease, test_id, _ = connection_lease(factory, identifier)

    @contextmanager
    def broken(*a, **k):
        raise ConnectorError("authentication_failed")
        yield

    monkeypatch.setattr(s, "open_mysql", broken)
    s.handle_connection_test(factory, lease, config)
    with factory() as db:
        test = db.get(m.ConnectionTest, test_id)
        task = db.get(m.Task, lease.id)
        assert (
            test.status == task.status == "failed"
            and test.results["error_code"] == "authentication_failed"
        )
        assert "never persisted" not in str(test.results) and test.server_uuid is None


def test_preflight_revoked_authority_during_network_cannot_publish_result(pipeline, monkeypatch):
    s, factory, identifier, config = pipeline
    lease, test_id, user_id = connection_lease(factory, identifier)
    monkeypatch.setattr(s, "open_mysql", working)
    monkeypatch.setattr(s, "_probe_capabilities", lambda *a: [])

    def revoke(*a, **k):
        with factory.begin() as db:
            db.get(m.User, user_id).active = False
        return {"server_uuid": "expected", "version": "8.4.5"}

    monkeypatch.setattr(s, "probe_mysql", revoke)
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        s.handle_connection_test(factory, lease, config)
    with factory() as db:
        assert db.get(m.ConnectionTest, test_id).status == "running"
        assert db.get(m.Task, lease.id).status == "running"
