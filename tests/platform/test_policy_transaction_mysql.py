"""Actual dispatch transactions against an explicitly registered synthetic MySQL."""
from datetime import timedelta
from types import SimpleNamespace
import threading
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.exc import OperationalError

from platform_app import intelligence, models as m
from platform_app.db import Base, create_session_factory, utcnow
from tests.platform.test_background_mysql import mysql_background_engine


@pytest.mark.parametrize("blocked", [True, False], ids=["blocked-other-instance", "normal"])
def test_dispatch_does_not_retain_unrelated_policy_lock(mysql_background_engine, monkeypatch, blocked):
    engine = mysql_background_engine
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    token = uuid4().hex
    with factory.begin() as db:
        department = m.Department(name="policy-lock-" + token)
        users = [m.User(username=token + str(i), password_hash="unused") for i in range(2)]
        db.add_all([department, *users]); db.flush()
        system = m.System(department_id=department.id, name=token)
        db.add(system); db.flush()
        instances = [m.Instance(system_id=system.id, name=str(i), endpoint="localhost",
            expected_server_uuid=token + str(i), enabled=True, lifecycle_state="Active") for i in range(2)]
        db.add_all(instances); db.flush()
        now = utcnow()
        policies = [m.InspectionPolicy(owner_id=users[i].id, enabled=True,
            instance_ids=[instances[i].id], next_signal_at=now-timedelta(seconds=2-i),
            next_ai_at=now+timedelta(hours=1)) for i in range(2)]
        db.add_all(policies); db.flush()
        policy_ids, instance_ids = [p.id for p in policies], [i.id for i in instances]
        user_ids, system_id, department_id = [u.id for u in users], system.id, department.id

    monkeypatch.setattr(intelligence, "sync_reports", lambda db: None)
    monkeypatch.setattr(intelligence, "actor_for_user", lambda db, owner: SimpleNamespace(db=db))
    monkeypatch.setattr(intelligence, "require_investigation_access", lambda *a: None)
    monkeypatch.setattr(intelligence, "group_collection_allowed", lambda *a: True)
    monkeypatch.setattr(intelligence, "mapping_for", lambda *a: None)
    monkeypatch.setattr(intelligence, "scan_signals", lambda *a: [])
    monkeypatch.setattr(intelligence, "daily_dispatch_usage", lambda *a: (0, 0))
    monkeypatch.setattr(intelligence, "effective_policy_state", lambda *a, **k: ("scheduled", None))
    entered, errors = threading.Event(), []

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if threading.current_thread().name == token and "FROM instances" in statement and "FOR UPDATE" in statement:
            entered.set()

    event.listen(engine, "before_cursor_execute", before_execute)
    holder = engine.connect(); holder_transaction = holder.begin()
    if blocked:
        holder.execute(select(m.Instance.id).where(m.Instance.id == instance_ids[0]).with_for_update())

    def dispatch():
        try:
            assert intelligence.dispatch_due(factory) == []
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=dispatch, name=token)
    unrelated_available = False
    current_locked = False
    try:
        thread.start()
        assert entered.wait(3), "dispatcher never attempted actual instance lock"
        if not blocked:
            thread.join(4)
            assert not thread.is_alive()
        with engine.connect() as probe:
            probe.exec_driver_sql("SET SESSION innodb_lock_wait_timeout=1")
            if blocked:
                try:
                    probe.execute(select(m.InspectionPolicy.id).where(m.InspectionPolicy.id == policy_ids[0]).with_for_update())
                except OperationalError as exc:
                    assert exc.orig.args[0] == 1205
                    current_locked = True
                finally:
                    probe.rollback()
            try:
                probe.execute(select(m.InspectionPolicy.id).where(m.InspectionPolicy.id == policy_ids[1]).with_for_update())
                unrelated_available = True
            except OperationalError as exc:
                assert exc.orig.args[0] == 1205
            finally:
                probe.rollback()
    finally:
        holder_transaction.rollback(); holder.close()
        thread.join(8)
        event.remove(engine, "before_cursor_execute", before_execute)
        assert not thread.is_alive(), "dispatcher not drained before fixture cleanup"
        with factory.begin() as db:
            db.execute(delete(m.InspectionPolicy).where(m.InspectionPolicy.id.in_(policy_ids)))
            db.execute(delete(m.Instance).where(m.Instance.id.in_(instance_ids)))
            db.execute(delete(m.System).where(m.System.id == system_id))
            db.execute(delete(m.Department).where(m.Department.id == department_id))
            db.execute(delete(m.User).where(m.User.id.in_(user_ids)))
    assert not errors
    assert not blocked or current_locked, "current policy must remain serialized"
    assert unrelated_available, "blocked instance retained unrelated policy B's lock"
