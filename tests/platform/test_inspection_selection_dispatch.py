"""Server dispatch and each frozen tool read honor the captured group selection."""
from copy import deepcopy
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from platform_app import models as m
from platform_app.db import utcnow
from platform_app.evidence import create_manifest, digest
from platform_app.intelligence import check_inspection_task, dispatch_due, save_policy
from platform_app.investigations import check_running
from platform_app.selection_scope import capture_selection_scope
from platform_app.tools import ToolBroker
from tests.platform.test_inspection_selection_scope import (
    ai_env, monitoring_env, group_env, selection_env, raw_observation,
    numeric_window, fake_materializer,
)


@pytest.fixture
def dispatched(selection_env, monkeypatch):
    e = selection_env
    e["source"].status = "ready"
    e["mapping"].last_observed_at = utcnow()
    policy = save_policy(e["actor"], {
        "enabled": True, "instance_ids": [e["instance"].id], "mode": "rules_only",
        "urgent_enabled": False, "signal_interval_seconds": 300,
        "ai_interval_seconds": 1800, "cooldown_seconds": 300, "max_daily_runs": 3,
    })
    e["db"].commit()
    monkeypatch.setattr("platform_app.intelligence.scan_signals", lambda *_: [])
    expected = capture_selection_scope(e["db"], [e["instance"].id])
    [task_id] = dispatch_due(e["factory"], policy_id=policy.id, force=True)
    e["db"].expire_all()
    task = e["db"].get(m.Task, task_id)
    e.update(inspection_task=task, inspection_policy=policy,
             inspection_inv=e["db"].get(m.Investigation, task.input_ref), expected_scope=expected)
    return e


def change_mapping_in_another_session(e):
    e["db"].commit()
    with e["factory"].begin() as other:
        other.execute(update(m.MonitoringMapping).where(m.MonitoringMapping.id == e["mapping"].id)
                      .values(version=e["mapping"].version + 1))


def assert_selection_changed(operation):
    with pytest.raises(HTTPException) as rejected:
        operation()
    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == "selection_changed"


def test_dispatch_captures_server_selection_in_the_actual_queued_task(dispatched):
    e = dispatched
    marker = e["inspection_task"].payload["options"]["inspection"]
    assert marker["evidence_scope"] == e["expected_scope"]
    assert marker["policy_id"] == e["inspection_policy"].id
    assert {item["itemid"] for item in marker["evidence_scope"]["instances"][e["instance"].id]["items"]} == {"11", "13"}
    assert "PRIVATE-TOKEN" not in json.dumps(marker["evidence_scope"])
    before = deepcopy(marker["evidence_scope"])
    change_mapping_in_another_session(e)
    assert marker["evidence_scope"] == before


def test_queued_inspection_rejects_a_selection_revision_changed_after_dispatch(dispatched):
    e = dispatched
    assert check_inspection_task(e["db"], e["inspection_task"]) is None
    change_mapping_in_another_session(e)
    assert_selection_changed(lambda: check_inspection_task(e["db"], e["inspection_task"]))


def test_group_inspection_without_captured_scope_fails_closed_while_manual_tasks_remain_manual(dispatched):
    e = dispatched
    task = e["inspection_task"]
    payload = deepcopy(task.payload)
    payload["options"]["inspection"].pop("evidence_scope", None)
    task.payload = payload
    e["db"].flush()
    assert_selection_changed(lambda: check_inspection_task(e["db"], task))
    manual = m.Task(payload={"instance_ids": [e["instance"].id], "options": {}})
    assert check_inspection_task(e["db"], manual) is None


def test_each_tool_call_rechecks_selection_before_returning_frozen_payload(dispatched):
    e = dispatched
    row = raw_observation(e, "11")
    task, inv = e["inspection_task"], e["inspection_inv"]
    task.status = "running"
    e["db"].commit()
    tool = ToolBroker(e["actor"], inv, [e["instance"].id], inv.window_start, inv.window_end,
        allowed_evidence_ids=[row.id],
        check_running=lambda: check_running(e["actor"], inv, task.id, "rules_only"))
    arguments = {"instance_id": e["instance"].id, "evidence_ids": [row.id]}
    assert [item["evidence_id"] for item in tool.call("get_evidence", arguments)["items"]] == [row.id]
    change_mapping_in_another_session(e)
    assert_selection_changed(lambda: tool.call("get_evidence", arguments))
    attempts = list(e["db"].scalars(select(m.ToolCall).where(m.ToolCall.investigation_id == inv.id)
                                  .order_by(m.ToolCall.sequence)))
    assert [attempt.status for attempt in attempts] == ["succeeded", "denied"]


def test_manifest_digest_commits_the_captured_selection_provenance(dispatched, monkeypatch):
    e = dispatched
    fake_materializer(monkeypatch, [numeric_window(e)])
    inv = e["inspection_inv"]
    scope = e["inspection_task"].payload["options"]["inspection"]["evidence_scope"]
    manifest = create_manifest(e["db"], "investigation", inv.id, [e["instance"].id],
                               inv.window_start, inv.window_end, selection_scope=scope)
    coverage = e["db"].scalar(select(m.ManifestItem).where(
        m.ManifestItem.manifest_id == manifest.id, m.ManifestItem.item_type == "coverage"))
    assert coverage.payload["selection_scope"] == scope
    assert digest(coverage.payload) == manifest.manifest_sha256 == coverage.payload_sha256
    altered = deepcopy(coverage.payload)
    altered["selection_scope"]["instances"][e["instance"].id]["items"][0]["units"] = "different-unit"
    assert digest(altered) != manifest.manifest_sha256
