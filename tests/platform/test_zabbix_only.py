from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import Mock
import pytest
from sqlalchemy import select
from platform_app import config, worker, models as m
from platform_app.common import PlatformError
from platform_app.db import utcnow
from platform_app.scheduling import enqueue, claim
from tests.platform.test_leases import lease_env


def test_new_deployments_default_zabbix_only():
    assert config.PlatformConfig.model_fields["collection_mode"].default == "zabbix_only"


@pytest.mark.parametrize(
    "kind,external",
    [
        ("connection_test", False),
        ("collect_instance", False),
        ("activate_instance", False),
        ("collect_instance", True),
        ("activate_instance", True),
    ],
)
@pytest.mark.parametrize("queued_state", ["queued", "retry_wait"])
def test_prequeued_legacy_tasks_never_enter_transport(
    lease_env, monkeypatch, kind, external, queued_state
):
    factory, (a, _) = lease_env
    with factory.begin() as db:
        if external:
            instance = db.get(m.Instance, a)
            instance.connection_mode = "zabbix"
            source = m.MonitoringSource(
                name="old",
                system_id=instance.system_id,
                url="http://unused.invalid",
                secret_ref="unused",
            )
            db.add(source)
            db.flush()
            db.add(
                m.MonitoringMapping(
                    source_id=source.id, instance_id=a, hostid="101", layer="db", selected=True
                )
            )
        task = enqueue(db, kind, instance_id=a, payload={"config_revision": 1})
        task.status = queued_state
    lease = claim(factory, "boundary", "collection")
    direct = Mock()
    monitoring = Mock()
    monkeypatch.setattr("platform_app.collection_service._target", direct)
    monkeypatch.setattr("platform_app.monitoring.client_for", monitoring)
    result = worker.execute(factory, lease, SimpleNamespace(collection_mode="zabbix_only"))
    assert result == {"status": "failed", "error_code": "collection_mode_retired"}
    direct.assert_not_called()
    monitoring.assert_not_called()


def test_scheduler_does_not_enqueue_direct_sources(lease_env):
    factory, (a, _) = lease_env
    factory.configure(info={"platform_config": SimpleNamespace(collection_mode="zabbix_only")})
    with factory.begin() as db:
        db.add(
            m.CollectionSchedule(
                instance_id=a, collector="runtime", next_due_at=utcnow() - timedelta(seconds=3)
            )
        )
    assert worker.schedule_due(factory) == []


def test_retired_routes_return_410_before_processing():
    from platform_app.routes import inventory, admin, monitoring

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(config=SimpleNamespace(collection_mode="zabbix_only"))
        )
    )
    actor = SimpleNamespace(db=SimpleNamespace(info={"platform_config": request.app.state.config}))
    for call in [
        lambda: inventory.create_draft(request, {}, actor),
        lambda: inventory.test_connection(request, {}, actor),
        lambda: inventory.create_instance(request, {}, actor),
        lambda: admin.create_collection_profile({}, actor),
        lambda: monitoring.selection("old", SimpleNamespace(hostids=[]), request, actor),
    ]:
        with pytest.raises(PlatformError) as error:
            call()
        assert error.value.status_code == 410


from tests.platform.test_analysis_groups import group_env
from tests.platform.test_monitoring import monitoring_env
from tests.platform.test_ai_scope import ai_env


def test_group_collection_uses_zabbix_transport_and_keeps_history_scoped(group_env):
    from platform_app.analysis_groups import save_group
    from platform_app.routes.inventory import get_instance

    e = group_env
    e["config"].collection_mode = "zabbix_only"
    e["factory"].configure(info={"platform_config": e["config"]})
    group = save_group(e["actor"], e["config"], e["body"])
    e["db"].commit()
    instance = e["db"].get(m.Instance, group["instance_id"])
    enqueue(
        e["db"],
        "collect_instance",
        instance_id=instance.id,
        payload={"config_revision": instance.config_revision, "collectors": ["monitoring"]},
    )
    e["db"].commit()
    lease = claim(e["factory"], "group-boundary", "collection")
    result = worker.execute(e["factory"], lease, e["config"])
    assert result.get("status") != "failed"
    assert any(method == "history.get" for method, _ in e["fake"].calls)
    e["db"].expire_all()
    assert e["db"].get(m.Task, lease.id).status == "succeeded"
    # Archives retain identity/history reads and their existing authorization.
    instance = e["db"].get(m.Instance, instance.id)
    instance.enabled = False
    instance.lifecycle_state = "Archived"
    e["db"].commit()
    assert get_instance(instance.id, e["actor"])["id"] == instance.id
    e["grant"].scope_type = "instance"
    e["grant"].scope_id = e["b"].id
    e["db"].commit()
    with pytest.raises(PlatformError):
        get_instance(instance.id, e["actor"])


def test_home_and_policy_scope_only_include_groups(group_env):
    from platform_app.analysis_groups import save_group
    from platform_app.intelligence import overview, save_policy

    e = group_env
    e["config"].collection_mode = "zabbix_only"
    group = save_group(e["actor"], e["config"], e["body"])
    e["a"].enabled = True
    e["a"].lifecycle_state = "Active"
    e["db"].commit()
    assert overview(e["actor"])["stats"]["objects"] == 1
    with pytest.raises(PlatformError) as error:
        save_policy(e["actor"], {"enabled": True, "instance_ids": [e["a"].id]})
    assert error.value.status_code == 410
    policy = save_policy(e["actor"], {"enabled": True, "instance_ids": [group["instance_id"]]})
    assert policy.instance_ids == [group["instance_id"]]


def test_group_scheduler_skips_nonmonitoring_profiles(group_env):
    from platform_app.analysis_groups import save_group

    e = group_env
    e["config"].collection_mode = "zabbix_only"
    e["factory"].configure(info={"platform_config": e["config"]})
    group = save_group(e["actor"], e["config"], e["body"])
    e["db"].commit()
    instance = e["db"].get(m.Instance, group["instance_id"])
    for schedule in e["db"].scalars(
        select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id == instance.id)
    ):
        schedule.next_due_at = utcnow() - timedelta(seconds=5)
    e["db"].add(
        m.CollectionSchedule(
            instance_id=instance.id,
            collector="runtime",
            config_revision=instance.config_revision,
            next_due_at=utcnow() - timedelta(seconds=5),
        )
    )
    e["db"].commit()
    identifiers = worker.schedule_due(e["factory"])
    assert len(identifiers) == 1
    e["db"].expire_all()
    assert e["db"].get(m.Task, identifiers[0]).payload["collectors"] == ["monitoring"]


def test_retired_schedules_cannot_starve_group_dispatch(group_env):
    from platform_app.analysis_groups import save_group

    e = group_env
    e["config"].collection_mode = "zabbix_only"
    e["factory"].configure(info={"platform_config": e["config"]})
    group = save_group(e["actor"], e["config"], e["body"])
    now = utcnow()
    for schedule in e["db"].scalars(
        select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id == group["instance_id"])
    ):
        schedule.next_due_at = now - timedelta(seconds=5)
    for index in range(100):
        instance = m.Instance(
            name="retired-" + str(index),
            system_id=e["system"].id,
            endpoint="unused.invalid",
            enabled=True,
            lifecycle_state="Active",
        )
        e["db"].add(instance)
        e["db"].flush()
        e["db"].add(
            m.CollectionSchedule(
                instance_id=instance.id,
                collector="runtime",
                config_revision=instance.config_revision,
                next_due_at=now - timedelta(days=1),
            )
        )
    e["db"].commit()
    identifiers = worker.schedule_due(e["factory"])
    assert len(identifiers) == 1
    with e["factory"]() as db:
        assert db.get(m.Task, identifiers[0]).instance_id == group["instance_id"]
