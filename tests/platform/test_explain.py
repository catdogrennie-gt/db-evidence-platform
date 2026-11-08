"""Security boundaries for structured, read-only EXPLAIN; no live target writes."""

from contextlib import contextmanager
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock

from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, func

from platform_app import models as m
from platform_app.access import actor_for_user
from platform_app.common import PlatformError
from platform_app.db import Base, create_db_engine, create_session_factory, utcnow
from platform_app.identity import current_actor
from platform_app.secrets import SecretStore
from platform_app.scheduling import claim, enqueue
from platform_app.explain import enqueue_explain, handle_explain
from platform_app.routes.explain import router


@pytest.fixture
def explain_graph(tmp_path):
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    db = factory()
    dep = m.Department(name="explain")
    db.add(dep)
    db.flush()
    system = m.System(name="explain", department_id=dep.id)
    db.add(system)
    db.flush()
    a = m.Instance(
        name="A",
        system_id=system.id,
        endpoint="127.0.0.1",
        expected_server_uuid="explain-a",
        selected_schemas=["fixture"],
        enabled=True,
        lifecycle_state="Active",
        connection={"mode": "direct", "db_host": "127.0.0.1", "tls_mode": "disabled"},
    )
    b = m.Instance(
        name="B", system_id=system.id, endpoint="127.0.0.1", expected_server_uuid="explain-b"
    )
    user = m.User(username="explainer", password_hash="unused")
    role = m.Role(
        name="explain-test",
        permissions=["data.read", "collection.trigger", "evidence.read_sensitive"],
    )
    db.add_all([a, b, user, role])
    db.flush()
    db.add(m.UserGrant(user_id=user.id, role_id=role.id, scope_type="instance", scope_id=a.id))
    db.flush()
    source = enqueue(
        db, "collect_instance", actor_id=user.id, instance_id=a.id, payload={"config_revision": 1}
    )
    source.status = "succeeded"
    run = m.CollectionRun(
        task_id=source.id,
        instance_id=a.id,
        collector="schema_config",
        config_revision=1,
        status="succeeded",
    )
    db.add(run)
    db.flush()
    columns = m.Evidence(
        instance_id=a.id,
        run_id=run.id,
        source="mysql.columns",
        source_mode="live",
        server_uuid=a.expected_server_uuid,
        observed_at=utcnow(),
        expires_at=utcnow() + timedelta(days=1),
        coverage={"availability": "available"},
        payload_sha256="a" * 64,
        payload={
            "rows": [
                {
                    "TABLE_SCHEMA": "fixture",
                    "TABLE_NAME": "records",
                    "COLUMN_NAME": "id",
                    "DATA_TYPE": "int",
                },
                {
                    "TABLE_SCHEMA": "fixture",
                    "TABLE_NAME": "records",
                    "COLUMN_NAME": "name",
                    "DATA_TYPE": "varchar",
                },
            ]
        },
    )
    tables = m.Evidence(
        instance_id=a.id,
        run_id=run.id,
        source="mysql.tables",
        source_mode="live",
        server_uuid=a.expected_server_uuid,
        observed_at=utcnow(),
        coverage={"availability": "available"},
        payload_sha256="b" * 64,
        payload={
            "rows": [
                {"TABLE_SCHEMA": "fixture", "TABLE_NAME": "records", "TABLE_TYPE": "BASE TABLE"}
            ]
        },
    )
    db.add_all([columns, tables])
    db.flush()
    key = tmp_path / "master.key"
    key.write_bytes(Fernet.generate_key())
    config = SimpleNamespace(
        fernet_key_path=key,
        secrets_dir=tmp_path / "secrets",
        private_dir=tmp_path,
        tls_profiles=[],
        ssh_profiles=[],
        known_hosts_path=tmp_path / "known-hosts",
    )
    a.credential_ref = (
        SecretStore(config).put_secret({"username": "fixture", "password": "never-output"}).ref
    )
    db.commit()
    app = FastAPI()
    app.state.config = config
    app.state.session_factory = factory
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_actor] = lambda: actor_for_user(db, user.id)
    yield SimpleNamespace(
        db=db,
        factory=factory,
        a=a,
        b=b,
        user=user,
        role=role,
        columns=columns,
        tables=tables,
        config=config,
        client=TestClient(app),
    )
    db.close()
    engine.dispose()


def body(g, value="sensitive-marker"):
    return {
        "template": "single_table_lookup",
        "metadata_evidence_id": g.columns.id,
        "config_revision": 1,
        "schema": "fixture",
        "table": "records",
        "projection": ["id", "name"],
        "predicates": [{"column": "name", "value": value}],
        "limit": 10,
    }


def actor(g):
    return actor_for_user(g.db, g.user.id)


@pytest.mark.parametrize(
    "change",
    [
        {"sql": "SELECT SLEEP(60)"},
        {"template": "arbitrary"},
        {"projection": ["id` FROM mysql.user --"]},
        {"predicates": [{"column": "name", "value": {"sql": "SLEEP(9)"}}]},
    ],
)
def test_explain_rejects_sql_fragments_and_unbound_shapes_before_queue(explain_graph, change):
    g = explain_graph
    request = {**body(g), **change}
    response = g.client.post(f"/api/v1/instances/{g.a.id}/explain", json=request)
    assert response.status_code == 422
    assert (
        g.db.scalar(select(func.count()).select_from(m.Task).where(m.Task.type == "explain")) == 0
    )


def test_constants_are_encrypted_and_hmac_idempotency_has_no_plaintext(explain_graph):
    g = explain_graph
    request = body(g, "sensitive-marker' OR 1=1 --")
    first = enqueue_explain(actor(g), g.config, g.a.id, request, idempotency_key="same")
    g.db.commit()
    again = enqueue_explain(actor(g), g.config, g.a.id, request, idempotency_key="same")
    g.db.commit()
    assert first.id == again.id
    payload = json.dumps(first.payload)
    assert "sensitive-marker" not in payload and "value" not in payload
    for path in g.config.secrets_dir.iterdir():
        assert b"sensitive-marker" not in path.read_bytes()
    with pytest.raises(HTTPException):
        enqueue_explain(actor(g), g.config, g.a.id, body(g, "different"), idempotency_key="same")
    audit = "".join(json.dumps(row.details) for row in g.db.scalars(select(m.AuditLog)))
    assert "sensitive-marker" not in audit


@pytest.mark.parametrize(
    "condition", ["view", "stale", "replay", "revision", "foreign", "missing_sensitive"]
)
def test_explain_requires_current_owned_live_base_table_metadata(explain_graph, condition):
    g = explain_graph
    if condition == "view":
        g.tables.payload = {
            "rows": [{"TABLE_SCHEMA": "fixture", "TABLE_NAME": "records", "TABLE_TYPE": "VIEW"}]
        }
    elif condition == "stale":
        g.columns.observed_at = utcnow() - timedelta(hours=2)
    elif condition == "replay":
        g.columns.source_mode = "replay"
    elif condition == "revision":
        g.db.get(m.CollectionRun, g.columns.run_id).config_revision = 2
    elif condition == "foreign":
        g.columns.instance_id = g.b.id
    else:
        g.role.permissions = ["data.read", "collection.trigger"]
    g.db.commit()
    with pytest.raises(HTTPException):
        enqueue_explain(actor(g), g.config, g.a.id, body(g))


class Cursor:
    def __init__(self, mutate=None, view=False):
        self.calls = []
        self.mutate = mutate
        self.view = view

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, args=None):
        self.calls.append((sql, args))
        if sql.startswith("EXPLAIN") and self.mutate:
            self.mutate()

    def fetchone(self):
        sql = self.calls[-1][0]
        if "information_schema.tables" in sql:
            return {"TABLE_TYPE": "VIEW" if self.view else "BASE TABLE"}
        return {
            "EXPLAIN": json.dumps(
                {
                    "query_block": {
                        "select_id": 1,
                        "table": {
                            "table_name": "records",
                            "access_type": "ALL",
                            "rows_examined_per_scan": 20,
                            "filtered": 10,
                            "key": "PRIMARY",
                            "possible_keys": ["PRIMARY", "sensitive-marker"],
                            "attached_condition": "name='sensitive-marker'",
                            "message": "sensitive-marker",
                        },
                    }
                }
            )
        }

    def fetchall(self):
        return [
            {"COLUMN_NAME": "id", "DATA_TYPE": "int"},
            {"COLUMN_NAME": "name", "DATA_TYPE": "varchar"},
        ]


def worker(g, monkeypatch, mutate=None, view=False):
    import platform_app.explain as service

    queued = enqueue_explain(actor(g), g.config, g.a.id, body(g))
    g.db.commit()
    lease = claim(g.factory, "explain-fixture")
    # The metadata collection fixture is terminal so the newly queued task wins.
    while lease and lease.type != "explain":
        with g.factory.begin() as db:
            db.get(m.Task, lease.id).status = "succeeded"
            db.get(m.InstanceLease, g.a.id).lease_until = None
        lease = claim(g.factory, "explain-fixture")
    cursor = Cursor(mutate, view)
    connection = SimpleNamespace(cursor=lambda: cursor, rollback=Mock())

    @contextmanager
    def connect(*args, **kwargs):
        yield connection

    monkeypatch.setattr(service, "open_mysql", connect)
    monkeypatch.setattr(service, "_target", lambda config, value, expected: dict(value))
    return queued, lease, cursor, connection


def test_explain_binds_constants_never_runs_business_query_and_persists_restricted_projection(
    explain_graph, monkeypatch
):
    g = explain_graph
    queued, lease, cursor, connection = worker(g, monkeypatch)
    handle_explain(g.factory, lease, g.config)
    g.db.expire_all()
    finished = g.db.get(m.Task, queued.id)
    assert finished.status == "succeeded" and set(finished.result) == {"evidence_id"}
    evidence = g.db.get(m.Evidence, finished.result["evidence_id"])
    assert evidence.sensitivity == "restricted" and evidence.source == "mysql.explain_plan"
    assert "sensitive-marker" not in json.dumps(
        evidence.payload
    ) and "attached_condition" not in json.dumps(evidence.payload["plan"])
    explain = [(sql, args) for sql, args in cursor.calls if sql.startswith("EXPLAIN")]
    assert (
        len(explain) == 1
        and "%s" in explain[0][0]
        and "sensitive-marker" not in explain[0][0]
        and explain[0][1] == ["sensitive-marker"]
    )
    assert (
        any(sql == "START TRANSACTION READ ONLY" for sql, args in cursor.calls)
        and connection.rollback.called
    )
    statements = [sql for sql, args in cursor.calls]
    opened = next(
        i for i, sql in enumerate(statements) if sql.startswith("HANDLER ") and " OPEN AS " in sql
    )
    explained = next(i for i, sql in enumerate(statements) if sql.startswith("EXPLAIN"))
    closed = next(
        i
        for i, sql in enumerate(statements)
        if sql.startswith("HANDLER ") and sql.endswith(" CLOSE")
    )
    assert opened < explained < closed
    assert g.db.get(m.CollectionRun, evidence.run_id).duration_ms is not None
    assert not any(
        sql.startswith("SELECT ") and "information_schema." not in sql for sql, args in cursor.calls
    )


@pytest.mark.parametrize("change", ["permission", "configuration", "cancel", "view"])
def test_explain_rechecks_authority_and_fence_before_publishing(explain_graph, monkeypatch, change):
    g = explain_graph

    def mutate():
        with g.factory.begin() as db:
            if change == "permission":
                db.get(m.Role, g.role.id).permissions = ["data.read"]
            elif change == "configuration":
                db.get(m.Instance, g.a.id).config_revision = 2
            else:
                task = db.scalar(select(m.Task).where(m.Task.type == "explain"))
                task.cancel_requested = True

    queued, lease, cursor, connection = worker(
        g, monkeypatch, mutate if change != "view" else None, change == "view"
    )
    with pytest.raises(HTTPException):
        handle_explain(g.factory, lease, g.config)
    assert (
        g.db.scalar(
            select(func.count())
            .select_from(m.Evidence)
            .where(m.Evidence.source == "mysql.explain_plan")
        )
        == 0
    )
    if change == "view":
        assert not any(sql.startswith("EXPLAIN") for sql, args in cursor.calls)


def test_options_expose_only_authorized_current_base_tables(explain_graph):
    g = explain_graph
    data = g.client.get(f"/api/v1/instances/{g.a.id}/explain-options").json()
    assert data["metadata_evidence_id"] == g.columns.id and data["items"][0]["table"] == "records"
    assert g.client.get(f"/api/v1/instances/{g.b.id}/explain-options").status_code == 404
    g.tables.payload = {
        "rows": [{"TABLE_SCHEMA": "fixture", "TABLE_NAME": "records", "TABLE_TYPE": "VIEW"}]
    }
    g.db.commit()
    assert g.client.get(f"/api/v1/instances/{g.a.id}/explain-options").json()["items"] == []


def test_explain_task_details_recheck_sensitive_and_collection_scope(explain_graph):
    from platform_app.routes.tasks import router as tasks

    g = explain_graph
    app = FastAPI()
    app.include_router(tasks, prefix="/api/v1")
    app.dependency_overrides[current_actor] = lambda: actor(g)
    client = TestClient(app)
    queued = enqueue_explain(actor(g), g.config, g.a.id, body(g))
    g.db.commit()
    assert client.get(f"/api/v1/tasks/{queued.id}").status_code == 200
    g.role.permissions = ["data.read", "collection.trigger"]
    g.db.commit()
    assert client.get(f"/api/v1/tasks/{queued.id}").status_code == 403


def test_parameter_reference_cannot_be_rebound_to_another_task_or_actor(explain_graph, monkeypatch):
    g = explain_graph
    queued, lease, cursor, connection = worker(g, monkeypatch)
    secret = SecretStore(g.config).put_secret(
        {
            "values": ["sensitive-marker"],
            "actor_id": "other",
            "instance_id": g.a.id,
            "metadata_evidence_id": g.columns.id,
            "config_revision": 1,
            "request_hash": queued.request_hash,
        },
        purpose="explain_parameter",
    )
    from dataclasses import replace

    forged = replace(lease, payload={**lease.payload, "parameter_ref": secret.ref})
    with pytest.raises(PlatformError):
        handle_explain(g.factory, forged, g.config)
    assert cursor.calls == []
