"""Content hashes follow stored JSON; historical rows are never resealed.

The opt-in MySQL tests use only fixed, same-connection TEMPORARY shadow tables.
The config environment variable contains a private path, never credentials.
"""
from copy import deepcopy
from datetime import timedelta
import os
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import JSON, MetaData, UniqueConstraint, create_engine, event, inspect, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateTable

from platform_app import evidence as ev
from platform_app import models as m
from platform_app.common import iso
from platform_app.db import utcnow
from tests.platform.test_ai_scope import ai_env


FLOATS = (21.299155509783194, 21.397854606161694)


@pytest.fixture
def store(ai_env):
    db = ai_env["db"]
    task = m.Task(type="collection", instance_id=ai_env["b"].id)
    db.add(task)
    db.flush()
    run = m.CollectionRun(task_id=task.id, instance_id=ai_env["b"].id,
                          collector="numeric_integrity_test")
    db.add(run)
    db.commit()
    assert not (db.new or db.dirty or db.deleted)
    return {"db": db, "instance": ai_env["b"], "run": run,
            "start": ai_env["start"], "end": ai_env["end"]}


def payload(at):
    return {"item": {"hostid": "101", "itemid": "11", "units": "rps"},
            "rows": [{"time": iso(at), "value": value, "raw_value": str(value)}
                     for value in FLOATS]}


def raw_row(store, *, valid=True):
    body = payload(store["start"] + timedelta(seconds=30))
    return m.Evidence(id=str(uuid4()), instance_id=store["instance"].id,
                      run_id=store["run"].id, source="zabbix.item.numeric:test",
                      source_mode="live", observed_at=store["start"] + timedelta(seconds=30),
                      coverage={"availability": "available", "complete": True},
                      payload=body, payload_sha256=ev.digest(body) if valid else "0" * 64,
                      truncated=False)


def seal(db, row):
    helper = getattr(ev, "_persist_new_payload", None)
    assert callable(helper), "new rows need a persistence-aware payload sealing helper"
    return helper(db, row)


def manifest(store, **kwargs):
    return ev.create_manifest(store["db"], "integrity_test", str(uuid4()),
                              [store["instance"].id], store["start"], store["end"], **kwargs)


def coverage(db, frozen):
    return db.scalar(select(m.ManifestItem).where(m.ManifestItem.manifest_id == frozen.id,
                                                 m.ManifestItem.item_type == "coverage"))


def test_new_row_sealing_returns_same_row_and_does_not_commit(store):
    db = store["db"]
    row = raw_row(store)
    original = deepcopy(row.payload)
    assert seal(db, row) is row
    db.refresh(row, ["payload", "payload_sha256"])
    assert row.payload_sha256 == ev.digest(row.payload)
    assert row.payload == original
    identifier = row.id
    db.rollback()
    assert db.get(m.Evidence, identifier) is None


def test_bad_only_legacy_raw_is_omitted_as_integrity_gap_not_truncation(store):
    db = store["db"]
    bad = raw_row(store, valid=False)
    db.add(bad)
    db.commit()
    original = deepcopy(bad.payload), bad.payload_sha256
    frozen = manifest(store)
    own = coverage(db, frozen)
    assert "evidence_payload_checksum_unverifiable" in own.payload["gaps"]
    assert own.payload["coverage_complete"] is False
    assert own.payload["truncated"] is False
    assert "manifest_limit_reached" not in own.payload["gaps"]
    assert not db.scalar(select(m.ManifestItem.id).where(
        m.ManifestItem.manifest_id == frozen.id, m.ManifestItem.evidence_id == bad.id))
    db.refresh(bad)
    assert (bad.payload, bad.payload_sha256) == original


def test_invalid_required_raw_fails_closed_without_repair(store):
    db = store["db"]
    bad = raw_row(store, valid=False)
    db.add(bad)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        manifest(store, required_evidence_ids=[bad.id])
    assert exc.value.status_code == 409
    db.rollback()
    db.refresh(bad)
    assert bad.payload_sha256 == "0" * 64


@pytest.mark.parametrize("state", ["pending", "persistent", "detached"])
def test_only_transient_rows_can_be_sealed_without_rewriting_existing_hash(store, state):
    db = store["db"]
    row = raw_row(store)
    if state == "pending":
        db.add(row)
    else:
        db.add(row)
        db.commit()
        if state == "detached":
            db.expunge(row)
    before = deepcopy(row.payload), row.payload_sha256
    with pytest.raises(ValueError):
        seal(db, row)
    assert (row.payload, row.payload_sha256) == before


@pytest.mark.parametrize("kind", ["evidence", "unrecognized_type"])
def test_payload_helper_rejects_pointer_or_unknown_item_before_add_or_autoflush(store, kind):
    db = store["db"]
    row = m.ManifestItem(manifest_id=str(uuid4()), instance_id=store["instance"].id,
                         item_type=kind, payload={"source": "only metadata"},
                         payload_sha256="0" * 64)
    assert inspect(row).transient
    assert not (db.new or db.dirty or db.deleted)
    with pytest.raises(ValueError):
        seal(db, row)
    assert inspect(row).transient
    assert not (db.new or db.dirty or db.deleted)


def test_payload_helper_rejects_other_model_before_add(store):
    db = store["db"]
    row = m.PlatformSetting(key="must-not-be-added", value={"value": 1})
    with pytest.raises(ValueError):
        seal(db, row)
    assert inspect(row).transient
    assert db.get(m.PlatformSetting, row.key) is None


@pytest.mark.parametrize("kind", ["metric_window", "time_series_window", "prometheus_window", "coverage"])
def test_self_contained_manifest_item_types_can_be_sealed(store, kind):
    db = store["db"]
    frozen = manifest(store)
    row = m.ManifestItem(manifest_id=frozen.id, instance_id=store["instance"].id,
                         item_type=kind, payload={"value": FLOATS[0],
                         "raw_value": str(FLOATS[0])}, payload_sha256="0" * 64)
    assert seal(db, row) is row
    db.expire(row, ["payload", "payload_sha256"])
    assert row.payload_sha256 == ev.digest(row.payload)


def test_readback_hash_update_never_rewrites_payload_and_rolls_back(store):
    db = store["db"]
    db.execute(text("""CREATE TRIGGER evidence_payload_readback AFTER INSERT ON evidence
        BEGIN UPDATE evidence
        SET payload=json_set(NEW.payload, '$.rows[0].value', 21.29915550978319)
        WHERE id=NEW.id; END"""))
    db.commit()
    updates = []
    bind = db.get_bind()

    def capture(connection, cursor, statement, parameters, context, many):
        if re.match(r"UPDATE\s+evidence\s+SET", statement, re.I):
            updates.append(statement)

    event.listen(bind, "before_cursor_execute", capture)
    try:
        row = raw_row(store)
        identifier = row.id
        seal(db, row)
        assert row.payload_sha256 == ev.digest(row.payload)
        assert row.payload["rows"][0]["raw_value"] == str(FLOATS[0])
        assert updates, "actual readback change requires a new content hash"
        assert all("payload_sha256=" in sql.replace(" ", "") for sql in updates)
        assert all(not re.search(r"(?:SET|,)\s*payload\s*=", sql, re.I) for sql in updates)
        db.rollback()
        assert db.get(m.Evidence, identifier) is None
    finally:
        event.remove(bind, "before_cursor_execute", capture)


def test_valid_raw_pointer_keeps_original_raw_hash_not_its_small_metadata_hash(store):
    db = store["db"]
    row = raw_row(store)
    db.add(row)
    db.commit()
    frozen = manifest(store)
    pointer = db.scalar(select(m.ManifestItem).where(
        m.ManifestItem.manifest_id == frozen.id, m.ManifestItem.item_type == "evidence"))
    assert pointer.evidence_id == row.id
    assert pointer.payload_sha256 == row.payload_sha256 == ev.digest(row.payload)
    assert pointer.payload_sha256 != ev.digest(pointer.payload)


def test_integrity_gap_and_real_quota_truncation_keep_both_causes(store):
    db = store["db"]
    rows = [raw_row(store) for _ in range(1002)]
    rows[-1].payload_sha256 = "0" * 64
    rows[-1].observed_at = store["end"] - timedelta(seconds=1)
    db.add_all(rows)
    db.commit()
    frozen = manifest(store)
    own = coverage(db, frozen)
    assert "manifest_limit_reached" in own.payload["gaps"]
    assert "evidence_payload_checksum_unverifiable" in own.payload["gaps"]
    assert own.payload["truncated"] is True
    assert own.payload["coverage_complete"] is False


def test_manifest_coverage_uses_sealed_member_hash_and_has_no_self_hash(store, monkeypatch):
    db = store["db"]
    # An actual isolated SQLite trigger models a JSON readback representation
    # change. The raw payload is not patched in Python and the helper must read it.
    db.execute(text("""CREATE TRIGGER own_payload_readback AFTER INSERT ON manifest_items
        WHEN NEW.item_type = 'time_series_window'
        BEGIN UPDATE manifest_items
        SET payload=json_set(NEW.payload, '$.points[0].value', 21.29915550978319)
        WHERE id=NEW.id; END"""))
    db.commit()
    window = {"instance_id": store["instance"].id, "source": "zabbix_history",
              "template": "zabbix_item_numeric", "points": [{"time": iso(store["start"]),
              "value": FLOATS[0]}], "provenance": {"hostid": "101", "itemid": "11"}}
    from platform_app import metrics
    monkeypatch.setattr(metrics, "materialize_source_windows", lambda *a: {
        "items": [deepcopy(window)], "coverage": {"materialized": True,
        "complete": True, "reason_codes": []}})
    db.info["platform_config"] = object()  # Only the isolated materializer above.
    frozen = manifest(store)
    db.expire_all()
    member = db.scalar(select(m.ManifestItem).where(
        m.ManifestItem.manifest_id == frozen.id,
        m.ManifestItem.item_type == "time_series_window"))
    own = coverage(db, frozen)
    assert member.payload["points"][0]["value"] != FLOATS[0]
    assert member.payload_sha256 == ev.digest(member.payload)
    assert own.payload["prometheus_windows"][0]["sha256"] == member.payload_sha256
    assert own.payload_sha256 == ev.digest(own.payload) == frozen.manifest_sha256
    assert "manifest_sha256" not in own.payload
    assert "payload_sha256" not in own.payload


@pytest.mark.parametrize("required", [False, True])
def test_manifest_checks_actual_database_payload_not_cached_preinsert_json(store, required):
    db = store["db"]
    assert db.expire_on_commit is False
    db.execute(text("""CREATE TRIGGER legacy_payload_readback AFTER INSERT ON evidence
        BEGIN UPDATE evidence
        SET payload=json_set(NEW.payload, '$.rows[0].value', 21.29915550978319)
        WHERE id=NEW.id; END"""))
    db.commit()
    row = raw_row(store)
    cached_payload = deepcopy(row.payload)
    inserted_hash = row.payload_sha256
    db.add(row)
    db.commit()
    assert row.payload == cached_payload  # Identity map retains pre-database JSON.
    actual_payload, actual_hash = db.execute(select(m.Evidence.payload, m.Evidence.payload_sha256)
                                             .where(m.Evidence.id == row.id)).one()
    assert actual_payload != cached_payload
    assert actual_hash == inserted_hash == ev.digest(cached_payload)
    assert ev.digest(actual_payload) != actual_hash
    if required:
        with pytest.raises(HTTPException) as exc:
            manifest(store, required_evidence_ids=[row.id])
        assert exc.value.status_code == 409
    else:
        frozen = manifest(store)
        assert "evidence_payload_checksum_unverifiable" in coverage(db, frozen).payload["gaps"]
        assert not db.scalar(select(m.ManifestItem.id).where(
            m.ManifestItem.manifest_id == frozen.id, m.ManifestItem.evidence_id == row.id))
    # Reading the actual stored value must not repair the historical hash.
    assert db.scalar(select(m.Evidence.payload_sha256).where(m.Evidence.id == row.id)) == inserted_hash


def test_manifest_reads_committed_metric_frame_not_preinsert_identity_map(store):
    db = store["db"]
    db.execute(text("""CREATE TRIGGER committed_frame_readback AFTER INSERT ON metric_frames
        BEGIN UPDATE metric_frames
        SET derived_values=json_set(NEW.derived_values, '$.rate', 21.29915550978319)
        WHERE run_id=NEW.run_id; END"""))
    db.commit()
    frame = m.MetricFrame(run_id=store["run"].id, instance_id=store["instance"].id,
                          collector="numeric_integrity_test", server_uuid="test-only",
                          collector_boot_id=str(uuid4()), source_epoch="test-only",
                          observed_monotonic_ns=100, counter_epoch=0,
                          observed_at=store["start"] + timedelta(seconds=40),
                          uptime_seconds=100, raw_values={}, derived_values={"rate": FLOATS[0]},
                          rate_validity="valid")
    db.add(frame)
    db.commit()
    assert frame.derived_values == {"rate": FLOATS[0]}
    actual_values = db.scalar(select(m.MetricFrame.derived_values).where(
        m.MetricFrame.run_id == frame.run_id))
    assert actual_values != frame.derived_values
    frozen = manifest(store)
    member = db.scalar(select(m.ManifestItem).where(
        m.ManifestItem.manifest_id == frozen.id, m.ManifestItem.item_type == "metric_window"))
    assert member.payload["derived_values"] == actual_values
    assert member.payload_sha256 == ev.digest(member.payload)


# Fixed safety contract: no additional table may become writable implicitly.
SHADOW_TABLES = ("collection_runs", "evidence", "evidence_manifests",
                 "manifest_items", "evidence_pins", "metric_frames")


@pytest.fixture
def mysql_shadow_store(record_property):
    config_path = os.environ.get("DBE_TEST_MYSQL_CONFIG")
    if not config_path:
        pytest.skip("opt-in requires DBE_TEST_MYSQL_CONFIG private path")
    from platform_app.config import load_config

    config = load_config(Path(config_path))
    c = config.control_db
    url = URL.create("mysql+pymysql", username=c.user,
                     password=c.password.get_secret_value(), host=c.host, port=c.port,
                     database=c.database, query={"charset": "utf8mb4"})
    engine = create_engine(url, poolclass=NullPool, hide_parameters=True,
                           isolation_level="READ COMMITTED")
    gate = {"connection_id": None, "ready": False, "shadows": set(), "dml": []}
    shadow_metadata = MetaData()
    approved_ddl = {}
    for name in SHADOW_TABLES:
        original = m.Base.metadata.tables[name]
        shadow = original.to_metadata(shadow_metadata)
        shadow._prefixes = ["TEMPORARY"]
        ddl = str(CreateTable(shadow, include_foreign_key_constraints=[]).compile(
            dialect=engine.dialect)).strip()
        assert ddl.startswith("CREATE TEMPORARY TABLE") and "FOREIGN KEY" not in ddl
        assert list(shadow.columns.keys()) == list(original.columns.keys())
        assert all(isinstance(shadow.c[col.name].type, JSON)
                   for col in original.columns if isinstance(col.type, JSON))
        assert len([c for c in shadow.constraints if isinstance(c, UniqueConstraint)]) == len(
            [c for c in original.constraints if isinstance(c, UniqueConstraint)])
        approved_ddl[ddl] = name
    assert set(approved_ddl.values()) == set(SHADOW_TABLES)
    # PK/unique constraints are retained. Secondary performance indexes need
    # no separate DDL for these bounded test rows and are intentionally omitted.

    @event.listens_for(engine, "before_cursor_execute")
    def prevent_permanent_writes(connection, cursor, statement, parameters, context, many):
        actual_id = connection.connection.driver_connection.thread_id()
        if gate["connection_id"] is not None:
            assert actual_id == gate["connection_id"], "MySQL connection drift rejected"
        sql = statement.strip()
        drop = re.fullmatch(r"DROP TEMPORARY TABLE IF EXISTS `([a-z_]+)`", sql, re.I)
        if sql in approved_ddl:
            assert not gate["ready"] and approved_ddl[sql] not in gate["shadows"]
            return
        if drop:
            assert drop[1] in SHADOW_TABLES and drop[1] in gate["shadows"]
            return
        dml = re.match(r"^(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+`?([a-z_]+)`?(?=[\s(])", sql, re.I)
        if dml:
            assert gate["ready"] and gate["shadows"] == set(SHADOW_TABLES), "all six temporary tables must be verified before DML"
            assert dml[1] in SHADOW_TABLES, "non-shadow DML rejected before execution"
            gate["dml"].append((sql.split()[0].upper(), dml[1]))
            return
        assert re.match(r"^(SELECT|SHOW|SET)\b", sql, re.I), "DDL/DML outside the fixed temporary contract rejected"
        assert not re.search(r"\b(INTO\s+OUTFILE|INTO\s+DUMPFILE|FOR\s+SHARE)\b", sql, re.I)

    connection = None
    db = None
    try:
        connection = engine.connect()
        gate["connection_id"] = connection.execute(text("SELECT CONNECTION_ID()")).scalar_one()
        connection.execute(text("SET time_zone = '+00:00'"))
        for table in SHADOW_TABLES:
            ddl, = [sql for sql, name in approved_ddl.items() if name == table]
            connection.exec_driver_sql(ddl)
            gate["shadows"].add(table)
            ddl = connection.exec_driver_sql(f"SHOW CREATE TABLE `{table}`").one()[1]
            assert "CREATE TEMPORARY TABLE" in ddl.upper(), "shadow must be confirmed temporary"
        gate["ready"] = True
        record_property("mysql_connection_id", gate["connection_id"])
        record_property("mysql_verified_temporary_tables", ",".join(sorted(gate["shadows"])))
        db = Session(bind=connection, autoflush=False, expire_on_commit=False, info={})
        assert db.get_bind() is connection
        assert not (db.new or db.dirty or db.deleted)
        now = utcnow()
        instance = SimpleNamespace(id=str(uuid4()))
        run = m.CollectionRun(id=str(uuid4()), task_id=str(uuid4()),
                              instance_id=instance.id, collector="numeric_integrity_test")
        db.add(run)
        db.flush()
        assert not (db.new or db.dirty or db.deleted)
        yield {"db": db, "instance": instance, "run": run,
               "start": now - timedelta(minutes=2), "end": now,
               "connection": connection, "gate": gate,
               "validate_statement": prevent_permanent_writes}
        assert connection.execute(text("SELECT CONNECTION_ID()")).scalar_one() == gate["connection_id"]
    finally:
        if db is not None:
            db.rollback()
            db.close()
        if connection is not None:
            physical_connection = connection.connection.driver_connection
            try:
                connection.rollback()
                for table in reversed(SHADOW_TABLES):
                    if table in gate["shadows"]:
                        connection.exec_driver_sql(f"DROP TEMPORARY TABLE IF EXISTS `{table}`")
                        gate["shadows"].remove(table)
            finally:
                connection.close()  # NullPool closes the pinned physical session.
                assert not physical_connection.open
                record_property("mysql_shadow_dml_count", len(gate["dml"]))
                record_property("mysql_shadow_dml_tables", ",".join(sorted({table for _, table in gate["dml"]})))
                record_property("mysql_physical_closed", not physical_connection.open)
        engine.dispose()


@pytest.mark.parametrize("value", FLOATS)
def test_mysql_append_hashes_actual_persisted_numeric_json(mysql_shadow_store, value):
    store = mysql_shadow_store
    db = store["db"]
    at = store["start"] + timedelta(seconds=30)
    body = {"rows": [{"time": iso(at), "value": value, "raw_value": str(value)}]}
    identifier, = ev.append_batch(db, store["instance"], store["run"], {
        "observed_at": iso(at), "evidence_items": [{"source": "zabbix.item.numeric:test",
        "source_mode": "live", "observed_at": iso(at), "availability": "available",
        "payload": body}], "capability_results": []})
    db.expire_all()
    stored = db.get(m.Evidence, identifier)
    assert stored.payload_sha256 == ev.digest(stored.payload), "new raw hash must match actual MySQL JSON readback"
    assert stored.payload["rows"][0]["raw_value"] == str(value)
    assert stored.coverage["payload_hash_basis"] == "persisted_json_v1"
    assert db.get_bind() is store["connection"]
    assert all(table in SHADOW_TABLES for _, table in store["gate"]["dml"])


def test_mysql_guard_rejects_nonshadow_or_incomplete_setup_without_executing_sql(mysql_shadow_store):
    store = mysql_shadow_store
    validate = store["validate_statement"]
    connection = store["connection"]
    with pytest.raises(AssertionError, match="non-shadow"):
        validate(connection, None, "UPDATE platform_settings SET version=2", (), None, False)
    store["gate"]["ready"] = False
    try:
        with pytest.raises(AssertionError, match="all six"):
            validate(connection, None, "INSERT INTO evidence (id) VALUES ('test')", (), None, False)
    finally:
        store["gate"]["ready"] = True


def test_mysql_manifest_seals_actual_members_coverage_and_original_raw_pointer(mysql_shadow_store, monkeypatch):
    store = mysql_shadow_store
    db = store["db"]
    raw = raw_row(store)
    # This raw row is intentionally exact and hash-valid even on the old build;
    # the regression below isolates new manifest-owned JSON roundtrips.
    raw.payload = {"rows": [{"time": iso(raw.observed_at), "value": 1, "raw_value": "1"}]}
    raw.payload_sha256 = ev.digest(raw.payload)
    db.add(raw)
    db.flush()
    frame = m.MetricFrame(run_id=store["run"].id, instance_id=store["instance"].id,
                          collector="numeric_integrity_test", server_uuid="test-only",
                          collector_boot_id=str(uuid4()), source_epoch="test-only",
                          observed_monotonic_ns=100, counter_epoch=0,
                          observed_at=store["start"] + timedelta(seconds=40),
                          uptime_seconds=100, raw_values={}, derived_values={"rate": FLOATS[0]},
                          rate_validity="valid")
    db.add(frame)
    db.flush()
    windows = [{"instance_id": store["instance"].id, "source": "zabbix_history",
                "template": "synthetic_" + str(index),
                "points": [{"time": iso(store["start"]), "value": value,
                            "raw_value": str(value)}],
                "provenance": {"hostid": "101", "itemid": str(index)}}
               for index, value in enumerate(FLOATS)]
    from platform_app import metrics
    monkeypatch.setattr(metrics, "materialize_source_windows", lambda *a: {
        "items": deepcopy(windows), "coverage": {"materialized": True,
        "complete": True, "reason_codes": [], "synthetic_precision_probe": FLOATS[1]}})
    db.info["platform_config"] = object()  # No real provider/materializer is called.
    frozen = manifest(store)
    db.expire_all()
    members = list(db.scalars(select(m.ManifestItem).where(m.ManifestItem.manifest_id == frozen.id)))
    own = next(item for item in members if item.item_type == "coverage")
    own_windows = {item.payload["template"]: item for item in members
                   if item.item_type == "time_series_window"}
    own_frame, = [item for item in members if item.item_type == "metric_window"]
    assert own_frame.payload["derived_values"] == db.scalar(select(m.MetricFrame.derived_values)
        .where(m.MetricFrame.run_id == frame.run_id))
    pointer, = [item for item in members if item.item_type == "evidence"]
    assert len(own_windows) == 2
    for item in members:
        if item.item_type != "evidence":
            assert item.payload_sha256 == ev.digest(item.payload), "own member hash must use persisted JSON"
    for ref in own.payload["prometheus_windows"]:
        assert ref["sha256"] == own_windows[ref["template"]].payload_sha256
    assert own.payload["metric_frames"][0]["payload_sha256"] == own_frame.payload_sha256
    assert own.payload_sha256 == ev.digest(own.payload) == frozen.manifest_sha256
    assert "manifest_sha256" not in own.payload
    assert pointer.evidence_id == raw.id
    assert pointer.payload_sha256 == raw.payload_sha256 == ev.digest(raw.payload)
    assert pointer.payload_sha256 != ev.digest(pointer.payload)
    assert all(table in SHADOW_TABLES for _, table in store["gate"]["dml"])
