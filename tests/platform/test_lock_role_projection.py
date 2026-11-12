"""Request-side lock attributes require the independently verified snapshot contract."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from platform_app.common import iso
from platform_app.model_context import (
    bounded_tool_result, compact_final_context, compact_initial, context_refs,
)
from platform_app.tools import project_evidence
from tests.platform.test_ai_scope import ai_env


CONTRACT = "db-evidence-lock-snapshot-v2"
LOCK_ROW = {
    "waiting_trx_id": "18446744073709551615",
    "blocking_trx_id": 9007199254740993,
    "waiting_thread_id": 4561,
    "blocking_thread_id": 4510,
    "LOCK_MODE": "X,REC_NOT_GAP",
    "LOCK_TYPE": "RECORD",
    "LOCK_STATUS": "WAITING",
    "OBJECT_SCHEMA": "private_schema",
    "OBJECT_NAME": "private_table",
    "sql_text": "SELECT PRIVATE_LITERAL",
}


@pytest.fixture
def trusted_snapshot(ai_env):
    key = ai_env["tmp_path"] / "lock-role-test-key"
    key.write_bytes(b"local-lock-projection-test-only-key")
    ai_env["db"].info["platform_config"] = SimpleNamespace(fernet_key_path=key)
    row = ai_env["evidence"]
    row.source = "mysql.lock_waits"
    row.source_mode = "live"
    row.truncated = False
    row.payload = {
        "item": {
            "hostid": "host-a", "itemid": "lock-item",
            "name": "A custom title does not establish lock roles",
            "key": "custom.lock.key", "enhancement": "mysql.lock_waits",
            "tags": [{"tag": "sl.snapshot_contract", "value": CONTRACT}],
        },
        "rows": [deepcopy(LOCK_ROW)],
    }
    row.coverage = {
        "availability": "available", "complete": False,
        "snapshot_contract": CONTRACT, "snapshot_complete": True,
        "snapshot_truncated": False, "snapshot_total_row_count": 1,
    }
    return row


def evidence_state(row):
    return deepcopy({name: getattr(row, name) for name in (
        "id", "instance_id", "source", "source_mode", "observed_at", "payload",
        "payload_sha256", "coverage", "truncated",
    )})


def assert_requested(projected):
    value = projected["data"]["rows"][0]
    assert value["requested_lock_mode"] == "X,REC_NOT_GAP"
    assert value["requested_lock_type"] == "RECORD"
    assert value["requested_lock_status"] == "WAITING"
    assert not {"LOCK_MODE", "LOCK_TYPE", "LOCK_STATUS"} & value.keys()
    assert not any(key.startswith("blocking_lock_") for key in value)
    assert value["waiting_trx_id"] == "18446744073709551615"
    assert value["blocking_trx_id"] == "9007199254740993"
    assert value["waiting_thread_id"] == 4561
    assert value["blocking_thread_id"] == 4510
    return value


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def test_verified_snapshot_binds_request_role_without_changing_evidence_or_aliases(trusted_snapshot):
    row = trusted_snapshot
    # The same identifiers must keep their existing secret-keyed aliases.
    row.source = "mysql.transactions"
    legacy = project_evidence(row)
    row.source = "mysql.lock_waits"
    before = evidence_state(row)
    projected = project_evidence(row)
    value = assert_requested(projected)
    assert projected["evidence_id"] == row.id
    assert projected["instance_id"] == row.instance_id
    assert projected["observed_at"] == iso(row.observed_at)
    for field in ("schema_ref", "table_ref"):
        assert value[field] == legacy["data"]["rows"][0][field]
        assert value[field].startswith("id_")
    assert all(secret not in json.dumps(projected) for secret in (
        "private_schema", "private_table", "PRIVATE_LITERAL",
    ))
    assert evidence_state(row) == before


def test_complete_current_snapshot_keeps_request_role_and_history_gap(trusted_snapshot):
    row = trusted_snapshot
    row.truncated = True
    row.coverage = dict(row.coverage, gaps=["history_truncated"], truncated=True)
    projected = project_evidence(row)
    assert_requested(projected)
    assert projected["coverage"]["complete"] is False
    assert projected["coverage"]["truncated"] is True
    assert projected["coverage"]["gaps"] == ["history_truncated"]


def test_unverified_custom_source_keeps_legacy_enums_without_guessing_roles(trusted_snapshot):
    row = trusted_snapshot
    row.payload["item"].pop("enhancement")
    row.payload["item"]["tags"] = []
    row.coverage = {"availability": "available", "complete": False}
    value = project_evidence(row)["data"]["rows"][0]
    assert value["LOCK_MODE"] == "X,REC_NOT_GAP"
    assert value["LOCK_TYPE"] == "RECORD"
    assert not any(key.startswith(("requested_lock_", "blocking_lock_")) for key in value)


@pytest.mark.parametrize("invalid", [
    "tag_only", "coverage_only", "wrong_enhancement", "wrong_source",
    "wrong_server_contract", "snapshot_incomplete", "snapshot_truncated",
    "conflicting_tags", "wrong_count", "boolean_count", "fractional_count",
    "non_object_row", "more_than_50_rows",
])
def test_incomplete_or_conflicting_proof_never_assigns_request_role(trusted_snapshot, invalid):
    row = trusted_snapshot
    if invalid == "tag_only":
        row.coverage = {"availability": "available", "complete": False}
    elif invalid == "coverage_only":
        row.payload["item"]["tags"] = []
    elif invalid == "wrong_enhancement":
        row.payload["item"]["enhancement"] = "mysql.transactions"
    elif invalid == "wrong_source":
        row.source = "mysql.transactions"
    elif invalid == "wrong_server_contract":
        row.coverage["snapshot_contract"] = "unverified-snapshot"
    elif invalid == "snapshot_incomplete":
        row.coverage["snapshot_complete"] = False
    elif invalid == "snapshot_truncated":
        row.coverage["snapshot_truncated"] = True
    elif invalid == "conflicting_tags":
        # Valid last must not conceal an earlier conflicting tag.
        row.payload["item"]["tags"].insert(0, {
            "tag": "sl.snapshot_contract", "value": "unverified-snapshot",
        })
    elif invalid in ("wrong_count", "boolean_count", "fractional_count"):
        row.coverage["snapshot_total_row_count"] = {
            "wrong_count": 2, "boolean_count": True, "fractional_count": 1.0,
        }[invalid]
    elif invalid == "non_object_row":
        row.payload["rows"].append(None)
        row.coverage["snapshot_total_row_count"] = 2
    elif invalid == "more_than_50_rows":
        row.payload["rows"] = [deepcopy(LOCK_ROW) for _ in range(51)]
        row.coverage["snapshot_total_row_count"] = 51
    before = evidence_state(row)
    projected = project_evidence(row)
    assert projected["data"]["rows"]
    for value in projected["data"]["rows"]:
        assert not any(key.startswith(("requested_lock_", "blocking_lock_")) for key in value)
    assert evidence_state(row) == before


def test_role_rename_does_not_resolve_conflicting_enums_or_accept_spoofed_blocker_mode(trusted_snapshot):
    row = trusted_snapshot
    row.payload["rows"][0].update(lock_mode="S", blocking_lock_mode="S")
    value = project_evidence(row)["data"]["rows"][0]
    assert "LOCK_MODE" not in value and "requested_lock_mode" not in value
    assert "blocking_lock_mode" not in value
    assert value["requested_lock_type"] == "RECORD"
    assert value["requested_lock_status"] == "WAITING"


@pytest.mark.parametrize("pipeline", ["initial", "final", "tool"])
def test_role_ids_and_time_survive_bounded_context_projections(trusted_snapshot, ai_env, pipeline):
    row = trusted_snapshot
    projected = project_evidence(row)
    before = deepcopy(projected)
    bundle = {
        "from": iso(ai_env["start"]), "to": iso(ai_env["end"]),
        "coverage": {"complete": False}, "evidence": [projected],
        "inspection_signals": [{"evidence_ids": [row.id]}],
    }
    if pipeline == "tool":
        packed = bounded_tool_result({"items": [projected]}, max_bytes=1600)
        assert size(packed) <= 1600
        actual = packed["items"][0]
    else:
        if pipeline == "final":
            # Independent optional dimensions create real packing pressure;
            # the protected lock fact must survive within the same hard bound.
            for index in range(6):
                bundle["evidence"].append({
                    "evidence_id": "optional-metric-" + str(index),
                    "instance_id": row.instance_id,
                    "source": "zabbix.item.numeric:optional-" + str(index),
                    "observed_at": iso(ai_env["start"] + timedelta(seconds=15)),
                    "coverage": {"complete": False, "availability": "available"},
                    "data": {"row_count": 4, "item": {
                        "hostid": "host-a", "itemid": "metric-" + str(index),
                        "name": "Independent measured dimension " + str(index),
                        "units": "ms", "value_semantics": "gauge",
                    }, "rows": [{"time": iso(ai_env["start"] + timedelta(seconds=5 * n)),
                                 "value": n} for n in range(4)]},
                })
        packed = compact_initial(bundle)
        if pipeline == "final":
            assert size(packed) + 512 > 3200
            packed = compact_final_context(
                packed, max_bytes=3200, measure=lambda value: size(value) + 512,
                protected_evidence_ids=[row.id],
            )
            assert size(packed) + 512 <= 3200
            assert len(context_refs(packed)) < len(bundle["evidence"])
        assert row.id in context_refs(packed)
        actual = next(item for item in packed["evidence"] if item["evidence_id"] == row.id)
        assert packed["from"] == bundle["from"] and packed["to"] == bundle["to"]
    assert_requested(actual)
    assert actual["evidence_id"] == row.id
    assert actual["observed_at"] == iso(row.observed_at)
    assert projected == before
