"""Saved onboarding state must obey current scope and consumption state."""

from datetime import timedelta
import pytest
from fastapi import HTTPException
from tests.platform.test_events import db, graph
from platform_app import models as m
from platform_app.db import utcnow
from platform_app.routes.inventory import list_drafts, get_draft


def test_saved_draft_list_does_not_expose_previously_granted_system(db):
    actor, a, _, grant = graph(db)
    role = db.get(m.Role, grant.role_id)
    role.permissions = list(role.permissions) + ["asset.onboard"]
    system = m.System(
        name="former-scope", department_id=db.get(m.System, a.system_id).department_id
    )
    db.add(system)
    db.flush()
    draft = m.ConnectionDraft(
        created_by=actor.user_id,
        system_id=system.id,
        config={"name": "old-scope-name"},
        expires_at=utcnow() + timedelta(days=1),
    )
    db.add(draft)
    grant.scope_type = "system"
    grant.scope_id = a.system_id
    db.flush()
    assert list_drafts(actor=actor)["total"] == 0
    with pytest.raises(HTTPException):
        get_draft(draft.id, actor)


def test_consumed_legacy_draft_is_not_resumable(db):
    actor, a, _, grant = graph(db)
    role = db.get(m.Role, grant.role_id)
    role.permissions = list(role.permissions) + ["asset.onboard"]
    grant.scope_type = "system"
    grant.scope_id = a.system_id
    a.credential_ref = "consumed-secret-reference"
    draft = m.ConnectionDraft(
        created_by=actor.user_id,
        system_id=a.system_id,
        secret_ref=a.credential_ref,
        config={"name": "already-registered"},
        expires_at=utcnow() + timedelta(days=1),
    )
    db.add(draft)
    db.flush()
    assert list_drafts(actor=actor)["total"] == 0
    with pytest.raises(HTTPException):
        get_draft(draft.id, actor)
