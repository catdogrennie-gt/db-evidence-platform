"""Additive index rollout supports old and new schema, not unknown revisions."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from platform_app.api import create_app
from platform_app.db import create_db_engine, create_session_factory


@pytest.mark.parametrize("revision,status", [
    ("0007_demo", 200), ("0008_evidence_live_window", 200), ("future_unknown", 503),
])
def test_additive_schema_rollout_readiness(revision, status):
    engine = create_db_engine("sqlite://")
    with engine.begin() as db:
        db.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(64))"))
        db.execute(text("INSERT INTO alembic_version VALUES (:version)"), {"version": revision})
    app = create_app(SimpleNamespace(public_origin="http://127.0.0.1"), create_session_factory(engine))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/health/ready")
        assert response.status_code == status
        assert response.json()["ready"] is (status == 200)
    engine.dispose()
