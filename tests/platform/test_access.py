"""Authorization regressions use real SQLAlchemy sessions, no permission mocks."""

import os
from uuid import uuid4
import pytest
from fastapi import HTTPException


@pytest.fixture
def db():
    from platform_app.db import Base, create_db_engine, create_session_factory
    from platform_app import models  # noqa: F401

    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    with create_session_factory(engine)() as session:
        yield session
    engine.dispose()


def fixture_graph(db):
    from platform_app.models import Department, System, Instance, User, Role, UserGrant
    from platform_app.access import ROLE_PERMISSIONS, actor_for_user

    dep = Department(name="A")
    other = Department(name="B")
    db.add_all([dep, other])
    db.flush()
    sys = System(name="A", department_id=dep.id)
    sys2 = System(name="B", department_id=other.id)
    db.add_all([sys, sys2])
    db.flush()
    a = Instance(name="A", system_id=sys.id, endpoint="127.0.0.1", expected_server_uuid="uuid-a")
    b = Instance(name="B", system_id=sys2.id, endpoint="127.0.0.1", expected_server_uuid="uuid-b")
    user = User(username="dba", password_hash="unused")
    role = Role(name="dba", permissions=sorted(ROLE_PERMISSIONS["dba"]))
    db.add_all([a, b, user, role])
    db.flush()
    grant = UserGrant(user_id=user.id, role_id=role.id, scope_type="department", scope_id=dep.id)
    db.add(grant)
    db.commit()
    return actor_for_user(db, user.id), a, b, grant


def test_scope_and_action_denial_are_distinct(db):
    from platform_app.access import authorize

    actor, a, b, _ = fixture_graph(db)
    assert authorize(actor, "data.read").instance_ids == frozenset([a.id])
    with pytest.raises(HTTPException) as out:
        authorize(actor, "data.read", [b.id])
    assert out.value.status_code == 404
    with pytest.raises(HTTPException) as out:
        authorize(actor, "grant.manage", [a.id])
    assert out.value.status_code == 403


def test_grant_deletion_is_effective_for_existing_actor(db):
    from platform_app.access import authorize

    actor, a, _, grant = fixture_graph(db)
    db.delete(grant)
    db.commit()
    with pytest.raises(HTTPException):
        authorize(actor, "data.read", [a.id])


def test_admin_has_no_implicit_business_data(db):
    from platform_app.models import Role
    from platform_app.access import ROLE_PERMISSIONS, authorize

    actor, a, _, grant = fixture_graph(db)
    role = Role(name="platform_admin", permissions=sorted(ROLE_PERMISSIONS["platform_admin"]))
    db.add(role)
    db.flush()
    grant.role_id = role.id
    db.commit()
    assert authorize(actor, "asset.onboard", [a.id]).instance_ids == frozenset([a.id])
    with pytest.raises(HTTPException) as out:
        authorize(actor, "data.read", [a.id])
    assert out.value.status_code == 403


def test_group_membership_revocation_is_immediate(db):
    from platform_app.models import Group, GroupGrant, GroupMembership
    from platform_app.access import authorize

    actor, a, _, grant = fixture_graph(db)
    group = Group(name="Ops")
    db.add(group)
    db.flush()
    member = GroupMembership(group_id=group.id, user_id=actor.user_id)
    db.add_all(
        [
            member,
            GroupGrant(
                group_id=group.id,
                role_id=grant.role_id,
                scope_type=grant.scope_type,
                scope_id=grant.scope_id,
            ),
        ]
    )
    db.delete(grant)
    db.commit()
    assert a.id in authorize(actor, "data.read").instance_ids
    db.delete(member)
    db.commit()
    with pytest.raises(HTTPException):
        authorize(actor, "data.read", [a.id])


def test_session_disable_revoke_and_permission_version(db):
    from platform_app.identity import create_session, authenticate, hash_password, verify_password
    from platform_app.models import User

    user = User(username="session-user", password_hash=hash_password("long enough test password"))
    db.add(user)
    db.commit()
    assert user.password_hash.startswith("$argon2id$")
    assert verify_password(user.password_hash, "long enough test password")
    token, csrf, stored = create_session(db, user)
    assert stored.token_hash != token and stored.csrf_hash != csrf
    assert authenticate(db, token).user_id == user.id
    user.permission_version += 1
    db.commit()
    with pytest.raises(HTTPException) as out:
        authenticate(db, token)
    assert out.value.status_code == 401
    token, _, stored = create_session(db, user)
    user.active = False
    db.commit()
    with pytest.raises(HTTPException):
        authenticate(db, token)
    user.active = True
    db.commit()
    from platform_app.identity import revoke_session

    revoke_session(db, stored.id)
    with pytest.raises(HTTPException):
        authenticate(db, token)


def test_unauthenticated_is_401(db):
    from platform_app.identity import authenticate

    with pytest.raises(HTTPException) as out:
        authenticate(db, None)
    assert out.value.status_code == 401


def test_delegation_uses_structural_scope_and_allowlist(db):
    from platform_app.models import Role, User, UserGrant, System
    from platform_app.access import (
        ROLE_PERMISSIONS,
        actor_for_user,
        authorize_system,
        require_delegation,
    )

    _, a, b, data_grant = fixture_graph(db)
    admin = User(username="control", password_hash="unused")
    role = Role(name="platform_admin", permissions=sorted(ROLE_PERMISSIONS["platform_admin"]))
    db.add_all([admin, role])
    db.flush()
    grant = UserGrant(
        user_id=admin.id,
        role_id=role.id,
        scope_type="department",
        scope_id=data_grant.scope_id,
        delegatable_roles=["dba", "viewer"],
    )
    db.add(grant)
    db.commit()
    actor = actor_for_user(db, admin.id)
    empty = System(name="empty", department_id=data_grant.scope_id)
    db.add(empty)
    db.commit()
    assert authorize_system(actor, empty.id).id == empty.id
    require_delegation(actor, data_grant.role_id, "system", empty.id)
    with pytest.raises(HTTPException):
        require_delegation(actor, data_grant.role_id, "instance", b.id)
    with pytest.raises(HTTPException):
        require_delegation(actor, role.id, "instance", a.id)


def test_login_csrf_origin_nonce_replay_and_logout(db, tmp_path):
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    from platform_app.config import PlatformConfig
    from platform_app.db import create_session_factory
    from platform_app.identity import current_actor, hash_password
    from platform_app.models import User
    from platform_app.routes.auth import router

    user = User(username="http-user", password_hash=hash_password("long enough http password"))
    db.add(user)
    db.commit()
    app = FastAPI()
    app.state.session_factory = create_session_factory(db.bind)
    app.state.config = PlatformConfig(
        control_db={"database": "test", "user": "test", "password": "unused"},
        private_dir=tmp_path,
        fernet_key_path=tmp_path / "key",
        secrets_dir=tmp_path / "secrets",
        known_hosts_path=tmp_path / "hosts",
        public_origin="http://127.0.0.1:18080",
    )
    app.include_router(router, prefix="/api/v1")

    @app.post("/api/v1/protected")
    def protected(actor=Depends(current_actor)):
        return {"user_id": actor.user_id}

    client = TestClient(app, base_url="http://127.0.0.1:18080")
    credentials = {"username": "http-user", "password": "long enough http password"}
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.post("/api/v1/auth/login", json=credentials).status_code == 403
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    headers = {"Origin": "http://127.0.0.1:18080", "X-CSRF-Token": csrf}
    anonymous = client.cookies.get("dba_login_nonce")
    assert (
        client.post(
            "/api/v1/auth/login",
            json=credentials,
            headers={**headers, "Origin": "https://outside.invalid"},
        ).status_code
        == 403
    )
    response = client.post("/api/v1/auth/login", json=credentials, headers=headers)
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert "Secure" not in response.headers["set-cookie"]
    auth = response.json()
    assert auth["user"]["username"] == "http-user"
    assert auth["scope"]["instance_count"] == 0
    assert client.post("/api/v1/protected").status_code == 403
    valid = {"Origin": "http://127.0.0.1:18080", "X-CSRF-Token": auth["csrf_token"]}
    assert client.post("/api/v1/protected", headers=valid).status_code == 200
    me = client.get("/api/v1/auth/me").json()
    assert me["csrf_token"] == auth["csrf_token"]
    # A stolen old anonymous nonce cannot be used twice.
    replay = TestClient(app, base_url="http://127.0.0.1:18080")
    replay.cookies.set("dba_login_nonce", anonymous, path="/api/v1")
    assert replay.post("/api/v1/auth/login", json=credentials, headers=headers).status_code == 403
    session_cookie = client.cookies.get("dba_session")
    assert client.post("/api/v1/auth/logout", headers=valid).status_code == 204
    replay.cookies.set("dba_session", session_cookie, path="/api/v1")
    assert replay.get("/api/v1/auth/me").status_code == 401


@pytest.mark.skipif(
    os.environ.get("DBA_PLATFORM_MYSQL_TEST") != "1",
    reason="requires real private MySQL control database",
)
def test_real_mysql_revocation_visible_to_already_open_actor_transaction():
    """A long-lived worker context must not retain MySQL repeatable-read grants."""
    from sqlalchemy import delete
    from platform_app.config import load_config
    from platform_app.db import create_db_engine, create_session_factory
    from platform_app.models import Department, System, User, Role, UserGrant, Instance
    from platform_app.access import actor_for_user, authorize

    engine = create_db_engine(load_config())
    factory = create_session_factory(engine)
    label = "auth-integration-" + str(uuid4())
    objects = []
    with factory() as first:
        try:
            department = Department(name=label)
            role = Role(name=label, permissions=["data.read"])
            user = User(username=label, password_hash="unused-test-account", active=True)
            first.add_all([department, role, user])
            first.flush()
            system = System(name=label, department_id=department.id)
            first.add(system)
            first.flush()
            instance = Instance(
                name=label, system_id=system.id, endpoint="127.0.0.1", expected_server_uuid=label
            )
            first.add(instance)
            first.flush()
            grant = UserGrant(
                user_id=user.id, role_id=role.id, scope_type="instance", scope_id=instance.id
            )
            first.add(grant)
            first.commit()
            objects = [
                (UserGrant, grant.id),
                (Instance, instance.id),
                (System, system.id),
                (Department, department.id),
                (User, user.id),
                (Role, role.id),
            ]
            actor = actor_for_user(first, user.id)
            assert instance.id in authorize(actor, "data.read").instance_ids
            with factory() as second:
                second.execute(delete(UserGrant).where(UserGrant.id == grant.id))
                second.commit()
            with pytest.raises(HTTPException) as denied:
                authorize(actor, "data.read", [instance.id])
            assert denied.value.status_code == 403
        finally:
            first.rollback()
            with factory() as cleanup:
                for model, object_id in objects:
                    cleanup.execute(delete(model).where(model.id == object_id))
                cleanup.commit()
    engine.dispose()
