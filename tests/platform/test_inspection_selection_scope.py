"""An automatic inspection freezes its selected identities, never a new global history filter."""
from copy import deepcopy
from datetime import timedelta
import hashlib

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from platform_app import models as m
from platform_app.analysis_groups import save_group
from platform_app.common import iso
from platform_app.db import utcnow
from platform_app.evidence import create_manifest, digest
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_monitoring import monitoring_env
from tests.platform.test_analysis_groups import group_env


@pytest.fixture
def selection_env(group_env):
    e = group_env
    e["fake"].items[0]["tags"].append({"tag": "semantics", "value": "gauge"})
    e["fake"].items[2]["key_"] = "dbops.mysql.lock_snapshot"
    first = save_group(e["actor"], e["config"], e["body"])
    mapping = e["db"].get(m.MonitoringMapping, first["id"])
    e["original_items"] = deepcopy(mapping.items)
    second = save_group(e["actor"], e["config"], dict(
        e["body"], config_revision=first["config_revision"],
        items=[item for item in e["body"]["items"] if item["itemid"] != "12"],
    ), first["id"])
    e["group"], e["mapping"] = second, mapping
    e["instance"] = e["db"].get(m.Instance, second["instance_id"])
    e["end"] = utcnow()
    e["start"] = e["end"] - timedelta(minutes=10)
    task = m.Task(type="collection", instance_id=e["instance"].id, status="succeeded")
    e["db"].add(task); e["db"].flush()
    run = m.CollectionRun(task_id=task.id, instance_id=e["instance"].id,
                          collector="monitoring", config_revision=1, status="succeeded")
    e["db"].add(run); e["db"].flush()
    e["run"] = run
    return e


def raw_observation(e, itemid, *, source_id=None, meta_changes=None, coverage_changes=None):
    meta = deepcopy(next(item for item in e["original_items"] if item["itemid"] == itemid))
    source_id = source_id or e["source"].id
    identity = ":".join((source_id, meta["hostid"], meta["itemid"]))
    source = "zabbix.item." + meta["value_kind"] + ":" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    coverage = {"availability": "available", "source_id": source_id,
                "hostid": meta["hostid"], "itemid": meta["itemid"],
                "mapping_version": 1, "config_revision": 1, "transport": "zabbix"}
    coverage.update(coverage_changes or {})
    meta.update(meta_changes or {})
    payload = {"item": meta, "rows": [{"time": iso(e["start"] + timedelta(minutes=1)), "value": 17}]}
    row = m.Evidence(instance_id=e["instance"].id, run_id=e["run"].id, source=source,
        source_mode="live", observed_at=e["start"] + timedelta(minutes=1),
        coverage=coverage, payload=payload, payload_sha256=digest(payload))
    e["db"].add(row); e["db"].flush()
    return row


def numeric_window(e, itemid="11"):
    meta = deepcopy(next(item for item in e["original_items"] if item["itemid"] == itemid))
    return {"instance_id": e["instance"].id, "source": "zabbix_history",
        "template": "zabbix_item_numeric", "owner_collector": "monitoring",
        "item": meta, "unit": meta["units"],
        "provenance": {"source_id": e["source"].id, "hostid": meta["hostid"],
                       "itemid": itemid, "mapping_version": e["mapping"].version,
                       "config_revision": e["instance"].config_revision},
        "points": [{"time": iso(e["start"] + timedelta(minutes=1)), "value": 17, "validity": "valid"}],
        "coverage": {"complete": True}}


def test_selected_raw_history_keeps_compatible_old_revision_and_excludes_removed_or_forged_identity(selection_env):
    from platform_app.selection_scope import capture_selection_scope, evidence_selection_predicate
    e = selection_env
    kept = raw_observation(e, "11")
    removed = raw_observation(e, "12")
    foreign = raw_observation(e, "11", source_id="a-different-source")
    conflict = raw_observation(e, "11", meta_changes={"hostid": "102"})
    wrong_unit = raw_observation(e, "11", meta_changes={"units": "B"})
    wrong_kind = raw_observation(e, "11", meta_changes={"value_kind": "text"})
    wrong_semantics = raw_observation(e, "11", meta_changes={"value_semantics": "counter"})
    before = deepcopy(kept.payload), deepcopy(kept.coverage), kept.observed_at
    scope = capture_selection_scope(e["db"], [e["instance"].id])
    actual = set(e["db"].scalars(select(m.Evidence.id).where(
        m.Evidence.instance_id == e["instance"].id, evidence_selection_predicate(scope))))
    assert actual == {kept.id}
    assert not actual.intersection({removed.id, foreign.id, conflict.id,
                                    wrong_unit.id, wrong_kind.id, wrong_semantics.id})
    assert (kept.payload, kept.coverage, kept.observed_at) == before
    assert kept.coverage["config_revision"] < e["instance"].config_revision


def test_metric_windows_need_exact_selected_identity_and_current_provenance(selection_env):
    from platform_app.selection_scope import capture_selection_scope, filter_metric_windows
    e = selection_env
    scope = capture_selection_scope(e["db"], [e["instance"].id])
    good = numeric_window(e)
    removed = numeric_window(e, "12")
    stale = deepcopy(good); stale["provenance"]["mapping_version"] -= 1
    forged = deepcopy(good); forged["provenance"]["source_id"] = "a-different-source"
    conflict = deepcopy(good); conflict["item"]["itemid"] = "999"
    wrong_config = deepcopy(good); wrong_config["provenance"]["config_revision"] -= 1
    wrong_unit = deepcopy(good); wrong_unit["item"]["units"] = "B"
    wrong_kind = deepcopy(good); wrong_kind["item"]["value_kind"] = "text"
    wrong_semantics = deepcopy(good); wrong_semantics["item"]["value_semantics"] = "counter"
    legacy = {"instance_id": e["a"].id, "source": "prometheus_resampled", "template": "connections",
              "points": [{"time": iso(e["start"]), "value": 5}]}
    items = [good, removed, stale, forged, conflict, wrong_config,
             wrong_unit, wrong_kind, wrong_semantics, legacy]
    before = deepcopy(items)
    assert filter_metric_windows(items, scope) == [good, legacy]
    assert items == before


def test_capture_is_immutable_and_a_later_configuration_change_is_rejected(selection_env):
    from platform_app.selection_scope import capture_selection_scope, validate_selection_scope
    e = selection_env
    scope = capture_selection_scope(e["db"], [e["instance"].id])
    assert scope["contract"] == "selected-monitoring-evidence-v1"
    selected = scope["instances"][e["instance"].id]
    assert selected["source_id"] == e["source"].id
    assert selected["mapping_id"] == e["mapping"].id
    assert {item["itemid"] for item in selected["items"]} == {"11", "13"}
    frozen = deepcopy(scope)
    assert validate_selection_scope(e["db"], scope, [e["instance"].id]) is None
    e["mapping"].version += 1
    e["instance"].config_revision += 1
    e["db"].flush()
    with pytest.raises(HTTPException) as rejected:
        validate_selection_scope(e["db"], scope, [e["instance"].id], lock=True)
    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == "selection_changed"
    assert scope == frozen


def fake_materializer(monkeypatch, items):
    monkeypatch.setattr("platform_app.metrics.materialize_source_windows", lambda *_args, **_kw: {
        "items": deepcopy(items),
        "coverage": {"materialized": True, "complete": True, "reason_codes": []},
    })


def frozen_rows(e, manifest):
    return list(e["db"].scalars(select(m.ManifestItem).where(m.ManifestItem.manifest_id == manifest.id)))


def test_scoped_manifest_filters_both_raw_and_windows_and_excludes_unattributed_group_frame(selection_env, monkeypatch):
    from platform_app.selection_scope import capture_selection_scope
    e = selection_env
    kept, removed = raw_observation(e, "11"), raw_observation(e, "12")
    lock = raw_observation(e, "13")
    lock.source = "mysql.lock_waits"
    lock.payload = dict(lock.payload, rows=[{"waiting_trx_id": 42, "blocking_trx_id": 41}])
    lock.payload_sha256 = digest(lock.payload)
    e["db"].add(m.MetricFrame(run_id=e["run"].id, instance_id=e["instance"].id,
        collector="monitoring", server_uuid="synthetic-server", collector_boot_id="synthetic-boot",
        observed_monotonic_ns=1, observed_at=kept.observed_at, uptime_seconds=100,
        raw_values={"connections": 17}, derived_values={"values": {"connections": 17}}))
    e["db"].flush()
    good, old_window = numeric_window(e), numeric_window(e, "12")
    fake_materializer(monkeypatch, [good, old_window])
    scope = capture_selection_scope(e["db"], [e["instance"].id])
    before = {row.id: (deepcopy(row.payload), deepcopy(row.coverage), row.payload_sha256)
              for row in (kept, removed, lock)}
    manifest = create_manifest(e["db"], "investigation", "scoped-owner", [e["instance"].id],
        e["start"], e["end"], selection_scope=scope)
    items = frozen_rows(e, manifest)
    assert {item.evidence_id for item in items if item.evidence_id} == {kept.id, lock.id}
    assert [item.payload["item"]["itemid"] for item in items if item.item_type == "time_series_window"] == ["11"]
    assert not any(item.metric_run_id for item in items)
    for row in (kept, removed, lock):
        assert (row.payload, row.coverage, row.payload_sha256) == before[row.id]


def test_manual_history_and_an_existing_frozen_report_do_not_follow_current_selection(selection_env, monkeypatch):
    from platform_app.investigations import _bundle, create_investigation
    e = selection_env
    kept, removed = raw_observation(e, "11"), raw_observation(e, "12")
    fake_materializer(monkeypatch, [numeric_window(e), numeric_window(e, "12")])
    inv, _ = create_investigation(e["actor"], [e["instance"].id], e["start"], e["end"], "Historical evidence")
    manifest = create_manifest(e["db"], "investigation", inv.id, [e["instance"].id], e["start"], e["end"])
    inv.manifest_id = manifest.id
    inv.status = "completed"
    inv.report = {"facts": [{"statement": "A frozen observation", "evidence_ids": [removed.id]}]}
    e["db"].flush()
    assert {item.evidence_id for item in frozen_rows(e, manifest) if item.evidence_id} == {kept.id, removed.id}
    before_bundle = deepcopy(_bundle(e["db"], inv, manifest)[0])
    before_report = deepcopy(inv.report)
    before_manifest = manifest.manifest_sha256
    before_items = [(item.id, item.payload_sha256, deepcopy(item.payload)) for item in frozen_rows(e, manifest)]
    save_group(e["actor"], e["config"], dict(e["body"],
        config_revision=e["instance"].config_revision,
        relations=[],
        items=[{"hostid": "102", "itemid": "13"}]), e["group"]["id"])
    assert _bundle(e["db"], inv, manifest)[0] == before_bundle
    assert inv.report == before_report and manifest.manifest_sha256 == before_manifest
    assert [(item.id, item.payload_sha256, item.payload) for item in frozen_rows(e, manifest)] == before_items
    another = create_manifest(e["db"], "investigation", "manual-history", [e["instance"].id], e["start"], e["end"])
    assert {item.evidence_id for item in frozen_rows(e, another) if item.evidence_id} == {kept.id, removed.id}


def test_required_evidence_cannot_reintroduce_a_removed_item(selection_env, monkeypatch):
    from platform_app.selection_scope import capture_selection_scope
    e = selection_env
    removed = raw_observation(e, "12")
    fake_materializer(monkeypatch, [])
    scope = capture_selection_scope(e["db"], [e["instance"].id])
    with pytest.raises(HTTPException) as rejected:
        create_manifest(e["db"], "investigation", "required-outside-selection", [e["instance"].id],
            e["start"], e["end"], selection_scope=scope, required_evidence_ids=[removed.id])
    assert rejected.value.status_code == 409
    assert e["db"].scalar(select(m.EvidenceManifest.id).where(
        m.EvidenceManifest.owner_id == "required-outside-selection")) is None


def test_validation_observes_configuration_change_even_with_a_cached_mapping(selection_env):
    from platform_app.selection_scope import capture_selection_scope, validate_selection_scope
    e = selection_env
    scope = capture_selection_scope(e["db"], [e["instance"].id])
    version = e["mapping"].version
    e["db"].commit()
    with e["factory"].begin() as other:
        other.execute(update(m.MonitoringMapping).where(m.MonitoringMapping.id == e["mapping"].id)
                      .values(version=version + 1))
    assert e["mapping"].version == version  # expire_on_commit=False deliberately leaves this object cached.
    with pytest.raises(HTTPException) as rejected:
        validate_selection_scope(e["db"], scope, [e["instance"].id])
    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == "selection_changed"
