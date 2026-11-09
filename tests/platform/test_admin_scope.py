"""Administrative scope and delegation tests without external services."""

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("legacy_collection_compatibility")
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from platform_app import models as m
from platform_app.access import ROLE_PERMISSIONS, actor_for_user
from platform_app.db import Base, create_db_engine, create_session_factory
from platform_app.identity import authenticate, create_session, current_actor
from platform_app.routes.admin import router


@pytest.fixture
def admin_graph(tmp_path):
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = create_session_factory(engine)()
    key = tmp_path / "key"
    key.write_bytes(Fernet.generate_key())
    config = SimpleNamespace(
        collection_mode="legacy",
        private_dir=tmp_path,
        fernet_key_path=key,
        secrets_dir=tmp_path / "secrets",
        allowed_networks=["127.0.0.0/8"],
        ssh_profiles=[
            {"id": "pinned", "name": "trusted", "host": "private-host", "password": "must-not-show"}
        ],
        tls_profiles=[],
        host_profiles=[{"id": "host-a", "name": "Host A", "path": "private"}],
        log_profiles=[{"id": "logs-a", "name": "Logs A", "path": "private"}],
        allowed_db_ports=[3306],
        model={},
        prometheus_url="http://127.0.0.1:1",
    )
    roles = {
        name: m.Role(name=name, permissions=sorted(actions))
        for name, actions in ROLE_PERMISSIONS.items()
    }
    a, b = m.Department(name="A"), m.Department(name="B")
    db.add_all([a, b, *roles.values()])
    db.flush()
    sa, sb = m.System(name="SA", department_id=a.id), m.System(name="SB", department_id=b.id)
    db.add_all([sa, sb])
    db.flush()
    ia, ib = (
        m.Instance(
            name="IA", system_id=sa.id, endpoint="127.0.0.1", expected_server_uuid="admin-test-a"
        ),
        m.Instance(
            name="IB", system_id=sb.id, endpoint="127.0.0.1", expected_server_uuid="admin-test-b"
        ),
    )
    names = ["global", "manager", "own", "outside", "mixed", "ungrouped", "foreign-global"]
    users = {
        name: m.User(username=name, display_name=name, password_hash="unused", active=True)
        for name in names
    }
    db.add_all([ia, ib, *users.values()])
    db.flush()

    def grant(user, role, kind, scope, delegatable=None):
        row = m.UserGrant(
            user_id=users[user].id,
            role_id=roles[role].id,
            scope_type=kind,
            scope_id=scope,
            delegatable_roles=delegatable or [],
        )
        db.add(row)
        return row

    grant("global", "platform_admin", "global", "*", ["viewer", "developer", "dba"])
    grant("manager", "platform_admin", "department", a.id, ["viewer", "developer", "dba"])
    grant("own", "viewer", "department", a.id)
    grant("outside", "viewer", "department", b.id)
    grant("mixed", "viewer", "department", a.id)
    grant("mixed", "viewer", "department", b.id)
    grant("foreign-global", "auditor", "global", "*")
    group = m.Group(name="own-group")
    mixed_group = m.Group(name="mixed-group")
    db.add_all([group, mixed_group])
    db.flush()
    db.add_all(
        [
            m.GroupGrant(
                group_id=group.id,
                role_id=roles["viewer"].id,
                scope_type="department",
                scope_id=a.id,
            ),
            m.GroupGrant(
                group_id=mixed_group.id,
                role_id=roles["viewer"].id,
                scope_type="department",
                scope_id=a.id,
            ),
            m.GroupGrant(
                group_id=mixed_group.id,
                role_id=roles["viewer"].id,
                scope_type="department",
                scope_id=b.id,
            ),
            m.GroupMembership(group_id=group.id, user_id=users["own"].id),
            m.PlatformSetting(key="retention", value={"evidence_days": 14, "prometheus_days": 14}),
            m.PlatformSetting(
                key="collection_profiles",
                value={
                    "items": [
                        {"id": "standard", "name": "标准", "intervals": {"identity_status": 30}}
                    ]
                },
            ),
        ]
    )
    db.commit()
    actors = {name: actor_for_user(db, user.id) for name, user in users.items()}
    app = FastAPI()
    app.state.config = config
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_actor] = lambda: actors["manager"]
    client = TestClient(app)
    yield SimpleNamespace(
        db=db,
        app=app,
        client=client,
        config=config,
        actors=actors,
        users=users,
        roles=roles,
        a=a,
        b=b,
        sa=sa,
        sb=sb,
        ia=ia,
        ib=ib,
        group=group,
        mixed_group=mixed_group,
    )
    db.close()
    engine.dispose()


def as_actor(g, name):
    g.app.dependency_overrides[current_actor] = lambda: g.actors[name]


def test_org_directory_and_counts_scope_before_pagination(admin_graph):
    g = admin_graph
    departments = g.client.get("/api/v1/departments").json()
    assert departments["total"] == 1 and departments["items"][0]["id"] == g.a.id
    systems = g.client.get("/api/v1/systems").json()
    assert systems["total"] == 1 and systems["items"][0]["id"] == g.sa.id
    assert systems["items"][0]["instance_count"] == 1
    hidden = g.client.get(f"/api/v1/systems?department_id={g.b.id}").json()
    assert hidden["total"] == 0 and hidden["items"] == []


def test_department_manager_cannot_create_parent_or_reparent_system(admin_graph):
    g = admin_graph
    assert g.client.post("/api/v1/departments", json={"name": "outside-new"}).status_code == 403
    assert (
        g.client.post("/api/v1/systems", json={"name": "bad", "department_id": g.b.id}).status_code
        == 404
    )
    assert (
        g.client.patch(f"/api/v1/systems/{g.sa.id}", json={"department_id": g.b.id}).status_code
        == 409
    )
    assert g.sa.department_id == g.a.id


def test_identity_scope_requires_all_direct_and_group_grants_contained(admin_graph):
    g = admin_graph
    response = g.client.get("/api/v1/users").json()
    names = {item["username"] for item in response["items"]}
    assert names == {"manager", "own"}
    assert response["total"] == 2
    assert (
        g.client.patch(f"/api/v1/users/{g.users['mixed'].id}", json={"active": False}).status_code
        == 404
    )
    assert (
        g.client.patch(
            f"/api/v1/users/{g.users['foreign-global'].id}", json={"active": False}
        ).status_code
        == 404
    )
    assert (
        g.client.patch(
            f"/api/v1/users/{g.users['ungrouped'].id}", json={"active": False}
        ).status_code
        == 404
    )
    g.db.add(m.GroupMembership(group_id=g.mixed_group.id, user_id=g.users["own"].id))
    g.db.commit()
    assert (
        g.client.patch(f"/api/v1/users/{g.users['own'].id}", json={"active": False}).status_code
        == 404
    )


def test_access_options_are_explicit_allowlisted_and_structurally_scoped(admin_graph):
    g = admin_graph
    data = g.client.get("/api/v1/settings/access-options").json()
    assert {r["name"] for r in data["delegable_roles"]} == {"viewer", "developer", "dba"}
    scopes = {r["scope_id"] for r in data["delegable_scopes"]}
    assert {g.a.id, g.sa.id, g.ia.id} <= scopes
    assert not {g.b.id, g.sb.id, g.ib.id, "*"} & scopes


def test_user_creation_delegation_cannot_escape_scope_or_role_allowlist(admin_graph):
    g = admin_graph
    base = {
        "username": "new-user",
        "password": "isolated-test-password",
        "display_name": "New user",
        "grants": [
            {"role_id": g.roles["viewer"].id, "scope_type": "department", "scope_id": g.b.id}
        ],
    }
    assert g.client.post("/api/v1/users", json=base).status_code == 403
    base["grants"][0]["scope_id"] = g.a.id
    base["grants"][0]["role_id"] = g.roles["platform_admin"].id
    assert g.client.post("/api/v1/users", json=base).status_code == 403
    base["grants"][0]["role_id"] = g.roles["viewer"].id
    created = g.client.post("/api/v1/users", json=base)
    assert created.status_code == 201, created.json()
    assert "password" not in str(created.json())
    assert "isolated-test-password" not in str(created.json())
    assert g.db.get(m.User, created.json()["id"]).password_hash.startswith("$argon2id$")


def test_revocation_and_password_update_revoke_sessions_immediately(admin_graph):
    g = admin_graph
    user = g.users["own"]
    token, _, session = create_session(g.db, user)
    response = g.client.patch(f"/api/v1/users/{user.id}", json={"active": False})
    assert response.status_code == 200
    assert response.json()["permission_version"] == 2
    with pytest.raises(HTTPException) as denied:
        authenticate(g.db, token)
    assert denied.value.status_code == 401
    assert g.db.get(m.Session, session.id).revoked_at is not None


def test_cannot_disable_last_global_administrator(admin_graph):
    g = admin_graph
    as_actor(g, "global")
    response = g.client.patch(f"/api/v1/users/{g.users['global'].id}", json={"active": False})
    assert response.status_code == 409
    assert g.users["global"].active is True


def test_group_scope_and_membership_do_not_bypass_delegation(admin_graph):
    g = admin_graph
    listed = g.client.get("/api/v1/groups").json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == g.group.id
    assert (
        g.client.put(
            f"/api/v1/groups/{g.mixed_group.id}/members", json={"user_ids": []}
        ).status_code
        == 404
    )
    assert (
        g.client.put(
            f"/api/v1/groups/{g.group.id}/members", json={"user_ids": [g.users["outside"].id]}
        ).status_code
        == 404
    )
    response = g.client.put(f"/api/v1/groups/{g.group.id}/members", json={"user_ids": []})
    assert response.status_code == 200 and response.json()["member_count"] == 0
    assert g.users["own"].permission_version == 2


def test_group_with_unmanageable_member_does_not_leak_member_ids_or_count(admin_graph):
    g = admin_graph
    g.db.add(m.GroupMembership(group_id=g.group.id, user_id=g.users["outside"].id))
    g.db.commit()
    result = g.client.get("/api/v1/groups").json()
    assert result["total"] == 0 and result["items"] == []
    assert (
        g.client.put(f"/api/v1/groups/{g.group.id}/members", json={"user_ids": []}).status_code
        == 404
    )


def test_strings_cannot_silently_enable_or_disable_account(admin_graph):
    g = admin_graph
    response = g.client.patch(f"/api/v1/users/{g.users['own'].id}", json={"active": "false"})
    assert response.status_code == 422 and g.users["own"].active is True


def test_connection_options_only_return_safe_profile_references(admin_graph):
    g = admin_graph
    data = g.client.get("/api/v1/settings/connection-options").json()
    assert data["ssh_profiles"] == [{"id": "pinned", "name": "trusted"}]
    assert data["host_profiles"] == [{"id": "host-a", "name": "Host A"}]
    assert data["log_profiles"] == [{"id": "logs-a", "name": "Logs A"}]
    assert (
        "private-host" not in str(data)
        and "password" not in str(data)
        and "'path'" not in str(data)
    )


def test_shared_template_mutations_need_global_authority_and_fixed_rules(admin_graph):
    g = admin_graph
    assert g.client.get("/api/v1/collection-profiles").json()["total"] == 1
    assert g.client.post("/api/v1/collection-profiles", json={"name": "bad"}).status_code == 403
    as_actor(g, "global")
    assert (
        g.client.post(
            "/api/v1/rules",
            json={"name": "bad", "type": "locks_present", "expression": "arbitrary-code"},
        ).status_code
        == 422
    )
    assert (
        g.client.post(
            "/api/v1/rules", json={"name": "fixed", "type": "locks_present", "threshold": 1}
        ).status_code
        == 201
    )


def test_retention_tightening_is_proposal_and_never_deletes_evidence(admin_graph):
    g = admin_graph
    as_actor(g, "global")
    response = g.client.patch(
        "/api/v1/settings/retention",
        json={"evidence_days": 3, "metrics_days": 3, "reason": "review tighter policy"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "pending_review"
    assert response.json()["deletion_performed"] is False
    assert g.db.get(m.PlatformSetting, "retention").value["evidence_days"] == 14
    assert g.db.get(m.PlatformSetting, "retention_pending").value["values"]["evidence_days"] == 3


def test_model_policy_scope_parent_and_no_implicit_business_read(admin_graph):
    g = admin_graph
    body = {
        "scope_type": "system",
        "scope_id": g.sb.id,
        "mode": "live_redacted",
        "reason": "test scope",
        "allowed_sources": ["runtime"],
    }
    assert g.client.patch("/api/v1/settings/model-policies", json=body).status_code == 404
    body.update(scope_id=g.sa.id)
    assert g.client.patch("/api/v1/settings/model-policies", json=body).status_code == 200
    assert g.sa.policy_version == 2 and g.sa.model_policy == "live_redacted"
    assert "data.read" not in g.actors["manager"].permissions


def test_model_profile_only_configured_default_and_never_returns_secret(admin_graph):
    g = admin_graph
    as_actor(g, "global")
    assert g.client.get("/api/v1/model-profiles").json()["total"] == 0
    assert (
        g.client.post(
            "/api/v1/model-profiles", json={"model": "arbitrary", "api_key": "must-not-save"}
        ).status_code
        == 409
    )
    g.config.model = {
        "provider": "trusted",
        "model": "reviewed-model",
        "base_url": "https://trusted.invalid/v1",
    }
    assert (
        g.client.post(
            "/api/v1/model-profiles",
            json={
                "provider": "trusted",
                "model": "reviewed-model",
                "base_url": "http://untrusted.invalid/v1",
                "api_key": "test-model-input",
            },
        ).status_code
        == 422
    )
    response = g.client.post(
        "/api/v1/model-profiles",
        json={"provider": "trusted", "model": "reviewed-model", "api_key": "test-model-input"},
    )
    assert response.status_code == 201 and response.json()["has_secret"] is True
    assert (
        "test-model-input" not in str(response.json()) and "credential_ref" not in response.json()
    )


def test_audit_scope_filter_happens_before_total_and_never_echoes_body(admin_graph):
    g = admin_graph
    g.db.add_all(
        [
            m.AuditLog(
                actor_id=g.users["global"].id,
                action="asset.test",
                target_type="instance",
                target_id=g.ia.id,
                result="success",
                details={"password": "must-not-show"},
            ),
            m.AuditLog(
                actor_id=g.users["global"].id,
                action="asset.test",
                target_type="instance",
                target_id=g.ib.id,
                result="success",
                details={"password": "must-not-show"},
            ),
        ]
    )
    g.db.commit()
    result = g.client.get("/api/v1/audit-logs").json()
    assert result["total"] == 1 and result["items"][0]["resource_id"] == g.ia.id
    assert "must-not-show" not in str(result) and g.ib.id not in str(result)


def test_author_does_not_bypass_current_audit_scope_on_past_operation(admin_graph):
    g = admin_graph
    g.db.add(
        m.AuditLog(
            actor_id=g.users["manager"].id,
            action="old.own.operation",
            target_type="instance",
            target_id=g.ib.id,
            result="success",
        )
    )
    g.db.commit()
    result = g.client.get("/api/v1/audit-logs").json()
    assert result["total"] == 0 and result["items"] == []


@pytest.mark.parametrize("inherited", [False, True])
@pytest.mark.parametrize(
    "change",
    [{"password": "replacement-test-password"}, {"active": False}, {"display_name": "changed"}],
)
def test_scoped_identity_manager_cannot_take_over_stronger_role_account(
    admin_graph, inherited, change
):
    g = admin_graph
    limited = m.Role(
        name="limited-identity-manager", permissions=["user.manage", "grant.manage", "group.manage"]
    )
    sensitive = m.Role(
        name="external-sensitive", permissions=["model.external_use", "evidence.read_sensitive"]
    )
    operator = m.User(username="limited-manager", password_hash="unused")
    target = m.User(
        username="sensitive-target",
        display_name="Original",
        password_hash="original-hash",
        active=True,
    )
    g.db.add_all([limited, sensitive, operator, target])
    g.db.flush()
    g.db.add(
        m.UserGrant(
            user_id=operator.id,
            role_id=limited.id,
            scope_type="system",
            scope_id=g.sa.id,
            delegatable_roles=["viewer"],
        )
    )
    if inherited:
        group = m.Group(name="sensitive-team")
        g.db.add(group)
        g.db.flush()
        g.db.add_all(
            [
                m.GroupGrant(
                    group_id=group.id, role_id=sensitive.id, scope_type="system", scope_id=g.sa.id
                ),
                m.GroupMembership(group_id=group.id, user_id=target.id),
            ]
        )
    else:
        g.db.add(
            m.UserGrant(
                user_id=target.id, role_id=sensitive.id, scope_type="system", scope_id=g.sa.id
            )
        )
    g.db.commit()
    operator_actor = actor_for_user(g.db, operator.id)
    g.app.dependency_overrides[current_actor] = lambda: operator_actor
    response = g.client.patch(f"/api/v1/users/{target.id}", json=change)
    assert response.status_code in (403, 404), response.json()
    assert target.password_hash == "original-hash" and target.active is True
    assert target.display_name == "Original" and target.permission_version == 1


def test_scoped_identity_manager_cannot_reset_its_non_delegable_management_identity(admin_graph):
    g = admin_graph
    response = g.client.patch(
        f"/api/v1/users/{g.users['manager'].id}", json={"password": "replacement-test-password"}
    )
    assert response.status_code in (403, 404), response.json()
    assert g.users["manager"].password_hash == "unused"


def test_global_manager_still_needs_explicit_role_allowlist_for_identity_reset(admin_graph):
    g = admin_graph
    as_actor(g, "global")
    target = g.users["foreign-global"]
    assert (
        g.client.patch(
            f"/api/v1/users/{target.id}", json={"password": "replacement-test-password"}
        ).status_code
        == 403
    )
    grant = g.db.scalar(select(m.UserGrant).where(m.UserGrant.user_id == g.users["global"].id))
    grant.delegatable_roles = [*grant.delegatable_roles, "auditor"]
    g.db.commit()
    allowed = g.client.patch(
        f"/api/v1/users/{target.id}", json={"password": "replacement-test-password"}
    )
    assert allowed.status_code == 200, allowed.json()
    assert target.password_hash.startswith("$argon2id$")


@pytest.mark.parametrize("operation", ["members", "grants"])
def test_group_membership_and_grant_replacement_preserve_existing_role_ceiling(
    admin_graph, operation
):
    g = admin_graph
    sensitive = m.Role(name="non-delegable-group-role", permissions=["model.external_use"])
    g.db.add(sensitive)
    g.db.flush()
    old = g.db.scalar(select(m.GroupGrant).where(m.GroupGrant.group_id == g.group.id))
    old.role_id = sensitive.id
    g.db.commit()
    body = (
        {"user_ids": []}
        if operation == "members"
        else {
            "grants": [
                {"role_id": g.roles["viewer"].id, "scope_type": "department", "scope_id": g.a.id}
            ]
        }
    )
    response = g.client.put(f"/api/v1/groups/{g.group.id}/{operation}", json=body)
    assert response.status_code == 403, response.json()
    assert old.role_id == sensitive.id
    assert (
        g.db.scalar(
            select(m.GroupMembership).where(m.GroupMembership.group_id == g.group.id)
        ).user_id
        == g.users["own"].id
    )


def test_model_policy_roundtrip_preserves_explicit_empty_and_unmodified_sources(admin_graph):
    g = admin_graph
    base = {
        "scope_type": "system",
        "scope_id": g.sa.id,
        "mode": "live_redacted",
        "reason": "review projection",
    }
    default = g.client.get("/api/v1/settings/model-policies").json()["items"]
    initial = next(row for row in default if row["scope_id"] == g.sa.id)
    assert initial["source_policy_configured"] is False and "runtime" in initial["allowed_sources"]
    assert (
        g.client.patch(
            "/api/v1/settings/model-policies", json={**base, "allowed_sources": ["runtime"]}
        ).status_code
        == 200
    )
    unchanged = g.client.patch("/api/v1/settings/model-policies", json=base)
    assert unchanged.json()["allowed_sources"] == ["runtime"]
    assert (
        g.client.patch(
            "/api/v1/settings/model-policies", json={**base, "allowed_sources": []}
        ).status_code
        == 200
    )
    items = g.client.get("/api/v1/settings/model-policies").json()["items"]
    explicit = next(row for row in items if row["scope_id"] == g.sa.id)
    assert explicit["source_policy_configured"] is True and explicit["allowed_sources"] == []


def sql_rule(g, **parameters):
    return {
        "name": "Scoped SQL latency",
        "template": "sql_latency",
        "severity": "medium",
        "enabled": True,
        "parameters": {
            "instance_ids": [g.ia.id],
            "schema": "measured_schema",
            "digest": "a" * 64,
            "min_executions": 10,
            "threshold": 0.001,
            "min_duration_seconds": 60,
            "freshness_multiplier": 1.5,
            "collector": "statement_digest",
            **parameters,
        },
    }


def test_scoped_manager_creates_exact_sql_rule_without_dropping_parameters(admin_graph):
    g = admin_graph
    body = sql_rule(g)
    response = g.client.post("/api/v1/rules", json=body)
    assert response.status_code == 201, response.json()
    result = response.json()
    assert result["parameters"] == {**body["parameters"], "severity": "medium"}
    assert result["scope"] == "instances" and result["instance_ids"] == [g.ia.id]
    stored = g.db.get(m.Rule, result["id"])
    assert stored.parameters["threshold"] == 0.001
    assert stored.parameters["min_executions"] == 10 and stored.parameters["digest"] == "a" * 64


@pytest.mark.parametrize(
    "parameters,status",
    [
        ({"instance_ids": []}, 422),
        ({"instance_ids": "all"}, 422),
        ({"instance_ids": ["unknown-instance"]}, 404),
        ({"instance_ids": ["1", "2", "3", "4", "5", "6"]}, 422),
        ({"digest": "SELECT 1"}, 422),
        ({"digest": "a" * 63}, 422),
        ({"schema": ""}, 422),
        ({"schema": "a" * 65}, 422),
        ({"schema": "bad\u0000name"}, 422),
        ({"min_executions": 9}, 422),
        ({"min_executions": 10001}, 422),
        ({"min_executions": True}, 422),
        ({"min_executions": 10.5}, 422),
        ({"collector": "runtime"}, 422),
        ({"expression": "untrusted"}, 422),
        ({"freshness_multiplier": 0}, 422),
        ({"threshold": 10**500}, 422),
    ],
)
def test_rule_fixed_parameters_reject_invalid_or_unscoped_input(admin_graph, parameters, status):
    g = admin_graph
    as_actor(g, "global")
    response = g.client.post("/api/v1/rules", json=sql_rule(g, **parameters))
    assert response.status_code == status, response.json()


def test_rule_scope_requires_whole_current_management_scope_and_no_unknown_fields(admin_graph):
    g = admin_graph
    assert (
        g.client.post(
            "/api/v1/rules", json=sql_rule(g, instance_ids=[g.ia.id, g.ib.id])
        ).status_code
        == 404
    )
    assert (
        g.client.post(
            "/api/v1/rules", json={"name": "global", "template": "sql_latency", "threshold": 0.001}
        ).status_code
        == 403
    )
    assert g.client.post("/api/v1/rules", json={**sql_rule(g), "scope": "all"}).status_code == 422
    assert (
        g.client.post("/api/v1/rules", json={**sql_rule(g), "severity": "arbitrary"}).status_code
        == 422
    )
    assert (
        g.client.post(
            "/api/v1/rules", json={**sql_rule(g), "sql": "SELECT * FROM private_table"}
        ).status_code
        == 422
    )
    assert (
        g.client.post("/api/v1/rules", json={**sql_rule(g), "digest": "b" * 64}).status_code == 422
    )
    assert (
        g.client.post(
            "/api/v1/rules", json=sql_rule(g, instance_ids=[g.ia.id, g.ia.id])
        ).status_code
        == 422
    )
    assert (
        g.client.post(
            "/api/v1/rules",
            json={
                "name": "unsupported SQL selector",
                "template": "locks_present",
                "parameters": {"instance_ids": [g.ia.id], "digest": "a" * 64},
            },
        ).status_code
        == 422
    )


def test_rule_lists_and_candidates_filter_whole_rule_scope_before_counts(admin_graph):
    g = admin_graph
    as_actor(g, "global")
    local = g.client.post("/api/v1/rules", json=sql_rule(g)).json()
    foreign = g.client.post(
        "/api/v1/rules", json={**sql_rule(g, instance_ids=[g.ib.id]), "name": "hidden-foreign"}
    ).json()
    mixed = g.client.post(
        "/api/v1/rules",
        json={**sql_rule(g, instance_ids=[g.ia.id, g.ib.id]), "name": "hidden-mixed"},
    ).json()
    global_rule = g.client.post(
        "/api/v1/rules",
        json={"name": "global-shared", "template": "locks_present", "scope": "all", "threshold": 0},
    ).json()
    as_actor(g, "manager")
    result = g.client.get("/api/v1/rules?limit=1").json()
    assert result["total"] == 1 and result["next_cursor"] is None
    assert result["items"][0]["id"] == local["id"]
    assert {row["id"] for row in result["instance_options"]} == {g.ia.id}
    assert result["can_manage_all"] is False
    assert g.ib.id not in str(result) and all(
        item["id"] not in str(result) for item in [foreign, mixed, global_rule]
    )


def test_rule_top_level_compatibility_maps_to_fixed_canonical_parameters(admin_graph):
    g = admin_graph
    body = sql_rule(g)
    params = body.pop("parameters")
    body.update(params)
    body["duration_seconds"] = body.pop("min_duration_seconds")
    response = g.client.post("/api/v1/rules", json=body)
    assert response.status_code == 201, response.json()
    assert response.json()["parameters"]["min_duration_seconds"] == 60
    assert response.json()["parameters"]["min_executions"] == 10


def test_rule_enable_lifecycle_is_scoped_versioned_audited_and_cannot_change_parameters(
    admin_graph,
):
    g = admin_graph
    created = g.client.post("/api/v1/rules", json=sql_rule(g))
    assert created.status_code == 201, created.json()
    rule = created.json()
    path = f"/api/v1/rules/{rule['id']}"
    assert (
        g.client.patch(
            path,
            json={
                "version": "stale",
                "expected_enabled": True,
                "enabled": False,
                "reason": "complete",
            },
        ).status_code
        == 409
    )
    assert (
        g.client.patch(
            path,
            json={
                "version": rule["version"],
                "expected_enabled": True,
                "enabled": False,
                "reason": " ",
            },
        ).status_code
        == 422
    )
    assert (
        g.client.patch(
            path,
            json={
                "version": rule["version"],
                "expected_enabled": True,
                "enabled": "false",
                "reason": "complete",
            },
        ).status_code
        == 422
    )
    assert (
        g.client.patch(
            path,
            json={
                "version": rule["version"],
                "expected_enabled": True,
                "enabled": False,
                "reason": "complete",
                "parameters": {},
            },
        ).status_code
        == 422
    )
    result = g.client.patch(
        path,
        json={
            "version": rule["version"],
            "expected_enabled": True,
            "enabled": False,
            "reason": "controlled acceptance completed",
        },
    )
    assert result.status_code == 200, result.json()
    assert result.json()["enabled"] is False and result.json()["version"] == rule["version"]
    assert result.json()["parameters"] == rule["parameters"]
    conflict = g.client.patch(
        path,
        json={
            "version": rule["version"],
            "expected_enabled": True,
            "enabled": True,
            "reason": "stale concurrent writer",
        },
    )
    assert conflict.status_code == 409 and conflict.json()["detail"]["details"]["enabled"] is False
    audit = g.db.scalar(
        select(m.AuditLog).where(
            m.AuditLog.target_id == rule["id"], m.AuditLog.action == "rule.set_enabled"
        )
    )
    assert audit and audit.details["reason"] == "controlled acceptance completed"
    grant = g.db.scalar(select(m.UserGrant).where(m.UserGrant.user_id == g.users["manager"].id))
    grant.scope_type = "department"
    grant.scope_id = g.b.id
    g.db.commit()
    assert (
        g.client.patch(
            path,
            json={
                "version": rule["version"],
                "expected_enabled": True,
                "enabled": True,
                "reason": "outside",
            },
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "case,expected",
    [
        ("matched", "firing"),
        ("other_instance", None),
        ("other_digest", "unknown"),
        ("other_schema", "unknown"),
        ("below_minimum", "unknown"),
    ],
)
def test_api_created_fixed_sql_rule_evaluator_honors_every_selector(admin_graph, case, expected):
    from platform_app.db import utcnow
    from platform_app.events import evaluate
    from tests.platform.test_events import observe

    g = admin_graph
    created = g.client.post("/api/v1/rules", json=sql_rule(g, min_duration_seconds=0))
    assert created.status_code == 201, created.json()
    rule = created.json()
    instance = g.ib if case == "other_instance" else g.ia
    now = utcnow()
    run, evidence = observe(
        g.db, instance, now, source="mysql.statement_digest", collector="statement_digest"
    )
    evidence.payload = {
        "window": {
            "rows": [
                {
                    "DIGEST": "b" * 64 if case == "other_digest" else "a" * 64,
                    "SCHEMA_NAME": "another_schema"
                    if case == "other_schema"
                    else "measured_schema",
                    "window_validity": "valid",
                    "count_delta": 9 if case == "below_minimum" else 10,
                    "total_time_seconds_delta": 0.04,
                }
            ]
        }
    }
    g.db.flush()
    evaluate(g.db, instance.id, run.id, now=now)
    result = g.db.scalar(select(m.RuleEvaluation).where(m.RuleEvaluation.rule_id == rule["id"]))
    assert (result.state if result else None) == expected


@pytest.mark.parametrize(
    "severity,expected",
    [
        ("critical", "critical"),
        ("high", "high"),
        ("medium", "medium"),
        ("low", "low"),
        ("invalid-history-value", "warning"),
        (None, "warning"),
        ({}, "warning"),
    ],
)
def test_new_rule_evaluation_copies_safe_severity_to_episode_and_event(
    admin_graph, severity, expected
):
    from platform_app.db import utcnow
    from platform_app.events import evaluate
    from tests.platform.test_events import observe

    g = admin_graph
    now = utcnow()
    rule = m.Rule(
        name="Severity propagation",
        rule_version="severity-isolated",
        template="locks_present",
        parameters={
            "collector": "runtime",
            "instance_ids": [g.ia.id],
            "threshold": 0,
            "min_duration_seconds": 0,
            "severity": severity,
        },
    )
    g.db.add(rule)
    g.db.flush()
    run, _ = observe(g.db, g.ia, now)
    evaluate(g.db, g.ia.id, run.id, now=now)
    episode = g.db.scalar(
        select(m.AlertOccurrence)
        .join(m.RuleEvaluation, m.RuleEvaluation.id == m.AlertOccurrence.evaluation_id)
        .where(m.RuleEvaluation.rule_id == rule.id)
    )
    assert episode.severity == expected
    event = g.db.scalar(
        select(m.Event)
        .join(m.EventAlert, m.EventAlert.event_id == m.Event.id)
        .where(m.EventAlert.alert_occurrence_id == episode.id)
    )
    assert event.severity == expected


def test_merging_episodes_keeps_highest_severity_across_later_lower_severity_merges(admin_graph):
    from platform_app.db import utcnow
    from platform_app.events import evaluate, merge_events
    from tests.platform.test_events import observe

    g = admin_graph
    now = utcnow()
    rules = []
    g.db.add(
        m.UserGrant(
            user_id=g.users["manager"].id,
            role_id=g.roles["dba"].id,
            scope_type="instance",
            scope_id=g.ia.id,
        )
    )
    g.db.flush()
    for index, severity in enumerate(["low", "critical", "medium"]):
        row = m.Rule(
            name=f"Rule {index}",
            rule_version=f"merge-severity-{index}",
            template="locks_present",
            parameters={
                "collector": "runtime",
                "instance_ids": [g.ia.id],
                "threshold": 0,
                "min_duration_seconds": 0,
                "severity": severity,
            },
        )
        rules.append(row)
        g.db.add(row)
    g.db.flush()
    run, _ = observe(g.db, g.ia, now)
    evaluate(g.db, g.ia.id, run.id, now=now)
    events = []
    for rule in rules:
        events.append(
            g.db.scalar(
                select(m.Event)
                .join(m.EventAlert, m.EventAlert.event_id == m.Event.id)
                .join(m.AlertOccurrence, m.AlertOccurrence.id == m.EventAlert.alert_occurrence_id)
                .join(m.RuleEvaluation, m.RuleEvaluation.id == m.AlertOccurrence.evaluation_id)
                .where(m.RuleEvaluation.rule_id == rule.id)
            )
        )
    target = events[0]
    for source in events[1:]:
        result = merge_events(
            g.actors["manager"],
            {
                "target_event_id": target.id,
                "source_event_ids": [source.id],
                "expected_versions": {target.id: target.version, source.id: source.version},
                "reason": "all episodes need review",
            },
        )
        assert result["severity"] == "critical"
