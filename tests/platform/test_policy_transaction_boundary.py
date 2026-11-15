"""Current eligibility and durable progress across independent policies."""
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select

from platform_app import intelligence, models as m
from platform_app.db import utcnow
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_intelligence import policy_env

pytestmark = pytest.mark.usefixtures("legacy_collection_compatibility")


@pytest.mark.parametrize("change", ["disabled", "postponed", "permission_revoked"])
def test_policy_selection_is_not_an_authorization_or_due_snapshot(ai_env, change):
    e = ai_env
    policy = policy_env(e)
    count = 0

    @contextmanager
    def begin():
        nonlocal count
        count += 1
        ordinal = count
        with e["factory"].begin() as db:
            yield db
        if ordinal == 1:
            with e["factory"].begin() as db:
                current = db.get(m.InspectionPolicy, policy.id)
                if change == "disabled":
                    current.enabled = False
                elif change == "postponed":
                    current.next_signal_at = current.next_ai_at = utcnow() + timedelta(hours=1)
                else:
                    db.execute(delete(m.UserGrant).where(m.UserGrant.user_id == current.owner_id))

    assert intelligence.dispatch_due(SimpleNamespace(begin=begin)) == []
    e["db"].expire_all()
    assert e["db"].scalar(select(func.count()).select_from(m.InspectionDispatch)) == 0
    if change == "permission_revoked":
        assert e["db"].get(m.InspectionPolicy, policy.id).status == "permission_denied"


def test_later_policy_error_preserves_only_earlier_committed_work(ai_env, monkeypatch):
    e = ai_env
    first = policy_env(e)
    now = utcnow()
    first.next_signal_at = first.next_ai_at = now-timedelta(seconds=2)
    owner = m.User(username="second-policy-owner", password_hash="unused")
    e["db"].add(owner); e["db"].flush()
    second = m.InspectionPolicy(owner_id=owner.id, enabled=True, instance_ids=[e["a"].id],
        next_signal_at=now-timedelta(seconds=1), next_ai_at=now-timedelta(seconds=1))
    e["db"].add(second); e["db"].commit()
    original = intelligence.actor_for_user

    def actor(db, user_id):
        if user_id == owner.id:
            raise RuntimeError("synthetic second-policy failure")
        return original(db, user_id)

    monkeypatch.setattr(intelligence, "actor_for_user", actor)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="synthetic second-policy failure"):
            intelligence.dispatch_due(e["factory"])
        e["db"].expire_all()
        rows = list(e["db"].scalars(select(m.InspectionDispatch)))
        assert len(rows) == 1 and rows[0].policy_id == first.id
        assert e["db"].get(m.Task, rows[0].task_id).status == "queued"
        assert e["db"].get(m.InspectionPolicy, first.id).last_dispatched_at is not None
        assert e["db"].get(m.InspectionPolicy, second.id).next_ai_at == now-timedelta(seconds=1)


@pytest.mark.parametrize("named,force,expected", [(True, True, 1), (True, False, 0), (False, True, 0)])
def test_explicit_policy_and_global_force_keep_existing_eligibility(ai_env, named, force, expected):
    e = ai_env
    policy = policy_env(e)
    policy.next_signal_at = policy.next_ai_at = utcnow()+timedelta(hours=1)
    e["db"].commit()
    tasks = intelligence.dispatch_due(e["factory"], policy_id=policy.id if named else None, force=force)
    assert len(tasks) == expected
