import pytest
from tests.platform.test_ai_scope import ai_env


def test_evidence_history_filters_before_total_and_unknown_id_is_404(ai_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from platform_app.routes.observability import router
    from platform_app.identity import current_actor
    from platform_app.models import Evidence, Task, CollectionRun
    from platform_app.common import iso

    db = ai_env["db"]
    task = Task(type="collection", instance_id=ai_env["b"].id)
    db.add(task)
    db.flush()
    run = CollectionRun(task_id=task.id, instance_id=ai_env["b"].id, collector="runtime")
    db.add(run)
    db.flush()
    hidden = Evidence(
        instance_id=ai_env["b"].id,
        run_id=run.id,
        source="mysql.lock_waits",
        observed_at=ai_env["start"],
        payload={"secret": "hidden"},
        payload_sha256="0" * 64,
    )
    db.add(hidden)
    db.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_actor] = lambda: ai_env["actor"]
    client = TestClient(app)
    response = client.get(
        "/api/v1/evidence", params={"from": iso(ai_env["start"]), "to": iso(ai_env["end"])}
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert hidden.id not in response.text
    assert client.get("/api/v1/evidence/" + hidden.id).status_code == 404


def test_admin_control_overview_does_not_read_diagnostic_data(ai_env):
    from platform_app.access import ROLE_PERMISSIONS
    from platform_app.models import Role
    from platform_app.routes.observability import overview_response

    role = ai_env["db"].get(Role, ai_env["grant"].role_id)
    role.permissions = sorted(ROLE_PERMISSIONS["platform_admin"])
    ai_env["db"].commit()
    result = overview_response(ai_env["actor"])
    assert result["metadata_only"] is True
    assert result["counts"]["healthy"] is None and result["events"] == []
    assert all(row["last_success_at"] is None for row in result["instances"])
