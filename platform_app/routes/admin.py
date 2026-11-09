"""Scoped control-plane APIs. Management never grants implicit evidence access."""

from __future__ import annotations

import base64
import json
import math
import re
from pathlib import Path
import shutil
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import delete, func, select, text as sql_text

from .. import models as m
from ..access import (
    ActorContext,
    ROLE_PERMISSIONS,
    _action_grants,
    allowed_scope_ids,
    authorize,
    authorize_system,
    bump_permission_version,
    current_permissions,
    require_delegation,
    scope_contains,
)
from ..audit import append_audit
from ..common import PlatformError, fields, iso
from ..db import utcnow
from ..identity import current_actor, hash_password
from ..inventory import COLLECTORS
from ..secrets import SecretStore

router = APIRouter(tags=["administration"])
ENVIRONMENTS = {"production", "staging", "test", "lab"}
RULE_TEMPLATES = [
    {"id": "locks_present", "type": "locks_present", "name": "锁等待持续存在"},
    {"id": "sql_latency", "type": "sql_latency", "name": "SQL 摘要延迟"},
    {"id": "collector_stale", "type": "collector_stale", "name": "采集资料过期"},
    {"id": "collector_unreachable", "type": "collector_unreachable", "name": "采集来源不可达"},
]


def _global(actor, action):
    return any(
        g.scope_type == "global" and g.scope_id == "*" for g in _action_grants(actor, action)
    )


def _require_global(actor, action):
    if not _global(actor, action):
        raise PlatformError(
            403, "global_management_required", "该操作修改平台公共配置，需要明确的全局管理范围。"
        )


def _contains(actor, action, scope_type, scope_id):
    return any(
        scope_contains(actor.db, g.scope_type, g.scope_id, scope_type, scope_id)
        for g in _action_grants(actor, action)
    )


def _name(body, key="name", maximum=120):
    value = body.get(key)
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise PlatformError(422, "invalid_field", f"{key} 需要 1–{maximum} 个字符。")
    return value.strip()


def _boolean(body, key, default=None):
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise PlatformError(422, "invalid_boolean", f"{key} 必须为布尔值。")
    return value


def _page(items, limit=50, cursor=None):
    if not 1 <= limit <= 100:
        raise PlatformError(422, "invalid_limit", "每批记录数必须为 1–100。")
    key = lambda row: (
        str(row.get("name") or row.get("username") or row.get("created_at") or row.get("id", "")),
        str(row.get("id", "")),
    )
    items = sorted(items, key=key)
    total = len(items)
    if cursor:
        try:
            after = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if (
                not isinstance(after, list)
                or len(after) != 2
                or not all(isinstance(v, str) for v in after)
            ):
                raise ValueError()
        except (ValueError, TypeError):
            raise PlatformError(422, "invalid_cursor", "分页游标无效。") from None
        items = [row for row in items if key(row) > tuple(after)]
    result = items[:limit]
    next_cursor = (
        base64.urlsafe_b64encode(json.dumps(key(result[-1])).encode()).decode().rstrip("=")
        if len(items) > limit
        else None
    )
    return {"items": result, "total": total, "next_cursor": next_cursor}


def _setting(db, key, default=None):
    row = db.get(m.PlatformSetting, key)
    return row, dict(row.value or {}) if row else dict(default or {})


def _save_setting(db, key, value):
    row = db.get(m.PlatformSetting, key)
    if row is None:
        row = m.PlatformSetting(key=key, version=1, value=value)
        db.add(row)
    else:
        row.value = value
        row.version += 1
        row.updated_at = utcnow()
    return row


def _visible_org(actor):
    departments, systems, instances = set(), set(), set()
    for action in current_permissions(actor):
        for grant in _action_grants(actor, action):
            if grant.scope_type == "global" and grant.scope_id == "*":
                departments.update(actor.db.scalars(select(m.Department.id)))
                systems.update(actor.db.scalars(select(m.System.id)))
                instances.update(actor.db.scalars(select(m.Instance.id)))
            elif grant.scope_type == "department":
                departments.add(grant.scope_id)
                systems.update(
                    actor.db.scalars(
                        select(m.System.id).where(m.System.department_id == grant.scope_id)
                    )
                )
            elif grant.scope_type == "system":
                systems.add(grant.scope_id)
            elif grant.scope_type == "instance":
                instances.add(grant.scope_id)
    for row in actor.db.scalars(select(m.Instance).where(m.Instance.id.in_(instances))):
        systems.add(row.system_id)
    for row in actor.db.scalars(select(m.System).where(m.System.id.in_(systems))):
        departments.add(row.department_id)
    return departments, systems


def _org_metadata(db):
    return _setting(db, "organization_metadata")[1]


def _save_org_meta(db, row, body):
    data = _org_metadata(db)
    data[row.id] = {key: str(body[key])[:1000] for key in ("code", "description") if key in body}
    _save_setting(db, "organization_metadata", data)


@router.get("/departments")
def departments(
    limit: int = 50, cursor: str | None = None, actor: ActorContext = Depends(current_actor)
):
    visible, _ = _visible_org(actor)
    meta = _org_metadata(actor.db)
    result = [
        {
            **fields(row, ["id", "name", "active", "created_at", "updated_at"]),
            **meta.get(row.id, {}),
        }
        for row in actor.db.scalars(select(m.Department).where(m.Department.id.in_(visible)))
    ]
    return _page(result, limit, cursor)


@router.post("/departments", status_code=201)
def create_department(body: dict = Body(...), actor: ActorContext = Depends(current_actor)):
    _require_global(actor, "organization.manage")
    name = _name(body)
    if actor.db.scalar(select(m.Department.id).where(m.Department.name == name)):
        raise PlatformError(409, "department_exists", "部门名称已存在。")
    row = m.Department(name=name, active=_boolean(body, "active", True))
    actor.db.add(row)
    actor.db.flush()
    _save_org_meta(actor.db, row, body)
    append_audit(actor.db, actor.user_id, "organization.create", "department", row.id)
    actor.db.commit()
    return {**fields(row, ["id", "name", "active"]), **_org_metadata(actor.db).get(row.id, {})}


@router.patch("/departments/{identifier}")
def update_department(
    identifier: str, body: dict = Body(...), actor: ActorContext = Depends(current_actor)
):
    if not _contains(actor, "organization.manage", "department", identifier):
        raise PlatformError(404, "not_found", "部门不可访问。")
    row = actor.db.get(m.Department, identifier)
    if not row:
        raise PlatformError(404, "not_found", "部门不可访问。")
    if "name" in body:
        row.name = _name(body)
    if "active" in body:
        row.active = _boolean(body, "active")
    _save_org_meta(actor.db, row, {**_org_metadata(actor.db).get(row.id, {}), **body})
    append_audit(actor.db, actor.user_id, "organization.update", "department", row.id)
    actor.db.commit()
    return fields(row, ["id", "name", "active", "updated_at"])


@router.get("/systems")
def systems(
    department_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    actor: ActorContext = Depends(current_actor),
):
    _, visible = _visible_org(actor)
    meta = _org_metadata(actor.db)
    statement = select(m.System).where(m.System.id.in_(visible))
    if department_id:
        statement = statement.where(m.System.department_id == department_id)
    permissions = current_permissions(actor)
    allowed = set()
    for action in ("data.read", "asset.manage", "asset.onboard"):
        if action in permissions:
            allowed.update(authorize(actor, action).instance_ids)
    result = []
    for row in actor.db.scalars(statement):
        dep = actor.db.get(m.Department, row.department_id)
        result.append(
            {
                **fields(
                    row,
                    [
                        "id",
                        "name",
                        "department_id",
                        "environment",
                        "active",
                        "model_policy",
                        "policy_version",
                        "created_at",
                        "updated_at",
                    ],
                ),
                **meta.get(row.id, {}),
                "department_name": dep.name if dep else None,
                "instance_count": actor.db.scalar(
                    select(func.count())
                    .select_from(m.Instance)
                    .where(m.Instance.system_id == row.id, m.Instance.id.in_(allowed))
                ),
            }
        )
    return _page(result, limit, cursor)


@router.post("/systems", status_code=201)
def create_system(body: dict = Body(...), actor: ActorContext = Depends(current_actor)):
    department_id = body.get("department_id")
    if not _contains(actor, "organization.manage", "department", department_id):
        raise PlatformError(404, "not_found", "部门不可访问。")
    if actor.db.get(m.Department, department_id) is None:
        raise PlatformError(404, "not_found", "部门不可访问。")
    name = _name(body)
    environment = body.get("environment", "test")
    if environment not in ENVIRONMENTS:
        raise PlatformError(422, "invalid_environment", "环境枚举无效。")
    if actor.db.scalar(
        select(m.System.id).where(m.System.department_id == department_id, m.System.name == name)
    ):
        raise PlatformError(409, "system_exists", "同部门内系统名称已存在。")
    row = m.System(
        name=name,
        department_id=department_id,
        environment=environment,
        active=_boolean(body, "active", True),
        model_policy="rules_only",
    )
    actor.db.add(row)
    actor.db.flush()
    _save_org_meta(actor.db, row, body)
    append_audit(actor.db, actor.user_id, "organization.create", "system", row.id)
    actor.db.commit()
    return fields(row, ["id", "name", "department_id", "environment", "active", "model_policy"])


@router.patch("/systems/{identifier}")
def update_system(
    identifier: str, body: dict = Body(...), actor: ActorContext = Depends(current_actor)
):
    row = authorize_system(actor, identifier, "organization.manage")
    if "department_id" in body and body["department_id"] != row.department_id:
        raise PlatformError(409, "system_parent_immutable", "P0 不支持跨部门迁移系统。")
    if "name" in body:
        row.name = _name(body)
    if "active" in body:
        row.active = _boolean(body, "active")
    if "environment" in body:
        if body["environment"] not in ENVIRONMENTS:
            raise PlatformError(422, "invalid_environment", "环境枚举无效。")
        row.environment = body["environment"]
    _save_org_meta(actor.db, row, {**_org_metadata(actor.db).get(row.id, {}), **body})
    append_audit(actor.db, actor.user_id, "organization.update", "system", row.id)
    actor.db.commit()
    return fields(row, ["id", "name", "department_id", "environment", "active"])


def _user_grants(db, user_id):
    direct = list(db.scalars(select(m.UserGrant).where(m.UserGrant.user_id == user_id)))
    inherited = list(
        db.scalars(
            select(m.GroupGrant)
            .join(m.GroupMembership, m.GroupMembership.group_id == m.GroupGrant.group_id)
            .where(m.GroupMembership.user_id == user_id)
        )
    )
    return direct, inherited


def _manageable_user(actor, user, action="user.manage"):
    if not user:
        return False
    if _global(actor, action):
        return True
    direct, inherited = _user_grants(actor.db, user.id)
    all_grants = direct + inherited
    return bool(all_grants) and all(
        _contains(actor, action, g.scope_type, g.scope_id) for g in all_grants
    )


def _require_identity_grant_authority(actor, grant):
    """Account control can exercise every role the account currently holds."""
    if grant.scope_type != "global":
        return require_delegation(actor, grant.role_id, grant.scope_type, grant.scope_id)
    role = actor.db.get(m.Role, grant.role_id)
    if grant.scope_id == "*" and role:
        for authority in _action_grants(actor, "grant.manage"):
            if (
                authority.scope_type != "global"
                or authority.scope_id != "*"
                or role.name not in authority.delegatable_roles
            ):
                continue
            ceiling = ROLE_PERMISSIONS.get(role.name, frozenset(authority.delegated_actions))
            if set(role.permissions).issubset(ceiling):
                return
    raise PlatformError(
        403, "identity_delegation_required", "管理账号需要拥有其全部现有角色的明确委派权。"
    )


def _require_identity_authority(actor, user):
    direct, inherited = _user_grants(actor.db, user.id)
    for grant in direct + inherited:
        _require_identity_grant_authority(actor, grant)


def _manageable_group(actor, group, action="group.manage"):
    if not group:
        return False
    if _global(actor, action):
        return True
    grants = list(actor.db.scalars(select(m.GroupGrant).where(m.GroupGrant.group_id == group.id)))
    if not grants or not all(_contains(actor, action, g.scope_type, g.scope_id) for g in grants):
        return False
    members = actor.db.scalars(
        select(m.User)
        .join(m.GroupMembership, m.GroupMembership.user_id == m.User.id)
        .where(m.GroupMembership.group_id == group.id)
    )
    return all(_manageable_user(actor, user, action) for user in members)


def _grants_payload(actor, values):
    if not isinstance(values, list) or len(values) > 50:
        raise PlatformError(422, "invalid_grants", "授权清单无效。")
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, dict) or set(value) - {
            "role_id",
            "scope_type",
            "scope_id",
            "id",
            "created_at",
            "updated_at",
            "role_name",
        }:
            raise PlatformError(
                422, "invalid_grant", "只允许明确角色与范围，不允许转授管理权元数据。"
            )
        role_id, scope_type, scope_id = (
            value.get("role_id"),
            value.get("scope_type"),
            value.get("scope_id"),
        )
        require_delegation(actor, role_id, scope_type, scope_id)
        key = (role_id, scope_type, scope_id)
        if key in seen:
            raise PlatformError(422, "duplicate_grant", "同一角色与范围重复。")
        seen.add(key)
        result.append({"role_id": role_id, "scope_type": scope_type, "scope_id": scope_id})
    return result


def _user_response(db, user):
    direct, inherited = _user_grants(db, user.id)
    all_grants = direct + inherited
    roles = {role.id: role for role in db.scalars(select(m.Role))}
    sessions = db.scalar(
        select(func.count())
        .select_from(m.Session)
        .where(
            m.Session.user_id == user.id,
            m.Session.revoked_at.is_(None),
            m.Session.expires_at > utcnow(),
        )
    )
    return {
        **fields(
            user,
            [
                "id",
                "username",
                "display_name",
                "active",
                "permission_version",
                "created_at",
                "updated_at",
            ],
        ),
        "roles": sorted({roles[g.role_id].name for g in all_grants if g.role_id in roles}),
        "grants": [
            {
                **fields(g, ["id", "role_id", "scope_type", "scope_id"]),
                "role_name": roles[g.role_id].name,
            }
            for g in direct
        ],
        "inherited_grants": [fields(g, ["role_id", "scope_type", "scope_id"]) for g in inherited],
        "active_sessions": sessions,
    }


@router.get("/users")
def users(
    limit: int = 50,
    cursor: str | None = None,
    q: str | None = None,
    actor: ActorContext = Depends(current_actor),
):
    _action_grants(actor, "user.manage")
    result = [
        _user_response(actor.db, row)
        for row in actor.db.scalars(select(m.User))
        if _manageable_user(actor, row)
    ]
    if q:
        result = [
            r
            for r in result
            if q.casefold() in (r["username"] + " " + r["display_name"]).casefold()
        ]
    return _page(result, limit, cursor)


@router.post("/users", status_code=201)
def create_user(body: dict = Body(...), actor: ActorContext = Depends(current_actor)):
    _action_grants(actor, "user.manage")
    username = _name(body, "username", 100)
    if actor.db.scalar(select(m.User.id).where(m.User.username == username)):
        raise PlatformError(409, "username_exists", "用户名已存在。")
    grants = (
        _grants_payload(actor, body.get("grants", [])) if body.get("grants") is not None else []
    )
    if not _global(actor, "user.manage") and (
        not grants
        or not all(_contains(actor, "user.manage", g["scope_type"], g["scope_id"]) for g in grants)
    ):
        raise PlatformError(
            403, "identity_scope_required", "范围管理员建立账号时必须同时赋予其可管理的明确范围。"
        )
    try:
        password = hash_password(body.get("password", ""))
    except (ValueError, TypeError):
        raise PlatformError(422, "invalid_password", "密码需要 12–1024 个字符。") from None
    row = m.User(
        username=username,
        display_name=str(body.get("display_name", username))[:120],
        password_hash=password,
        active=_boolean(body, "active", True),
    )
    actor.db.add(row)
    actor.db.flush()
    for grant in grants:
        actor.db.add(m.UserGrant(user_id=row.id, **grant))
    actor.db.flush()
    append_audit(
        actor.db, actor.user_id, "user.create", "user", row.id, details={"grant_count": len(grants)}
    )
    actor.db.commit()
    return _user_response(actor.db, row)


def _last_admin_guard(actor, user, body, new_grants):
    role = actor.db.scalar(select(m.Role).where(m.Role.name == "platform_admin"))
    if not role:
        return
    admins = set(
        actor.db.scalars(
            select(m.UserGrant.user_id)
            .join(m.User, m.User.id == m.UserGrant.user_id)
            .where(
                m.User.active.is_(True),
                m.UserGrant.role_id == role.id,
                m.UserGrant.scope_type == "global",
                m.UserGrant.scope_id == "*",
            )
        )
    )
    inherited_admins = set(
        actor.db.scalars(
            select(m.GroupMembership.user_id)
            .join(m.GroupGrant, m.GroupGrant.group_id == m.GroupMembership.group_id)
            .join(m.User, m.User.id == m.GroupMembership.user_id)
            .where(
                m.User.active.is_(True),
                m.GroupGrant.role_id == role.id,
                m.GroupGrant.scope_type == "global",
                m.GroupGrant.scope_id == "*",
            )
        )
    )
    admins.update(inherited_admins)
    if user.id not in admins or len(admins) > 1:
        return
    disables = body.get("active") is False
    removes = (
        new_grants is not None
        and user.id not in inherited_admins
        and not any(g["role_id"] == role.id and g["scope_type"] == "global" for g in new_grants)
    )
    if disables or removes:
        raise PlatformError(409, "last_administrator", "不能停用或撤销最后一个可用的全局管理员。")


@router.patch("/users/{identifier}")
def update_user(
    identifier: str, body: dict = Body(...), actor: ActorContext = Depends(current_actor)
):
    row = actor.db.get(m.User, identifier)
    if not _manageable_user(actor, row):
        raise PlatformError(404, "not_found", "账号不可访问。")
    grants = _grants_payload(actor, body["grants"]) if "grants" in body else None
    _last_admin_guard(actor, row, body, grants)
    # Resetting credentials, enabling an identity, or editing its metadata must
    # never provide an indirect path around role delegation (including groups).
    _require_identity_authority(actor, row)
    if (
        grants is not None
        and not _global(actor, "user.manage")
        and not all(_contains(actor, "user.manage", g["scope_type"], g["scope_id"]) for g in grants)
    ):
        raise PlatformError(403, "identity_scope_required", "新授权超出账号管理范围。")
    if "username" in body and body["username"] != row.username:
        raise PlatformError(409, "username_immutable", "用户名不可改写，请修改显示名。")
    if "display_name" in body:
        row.display_name = _name(body, "display_name")
    if "active" in body:
        row.active = _boolean(body, "active")
    if body.get("password"):
        try:
            row.password_hash = hash_password(body["password"])
        except (ValueError, TypeError):
            raise PlatformError(422, "invalid_password", "密码需要 12–1024 个字符。") from None
    if grants is not None:
        # Replacing grants also requires delegation authority over every removed grant.
        for old in actor.db.scalars(select(m.UserGrant).where(m.UserGrant.user_id == row.id)):
            _require_identity_grant_authority(actor, old)
        actor.db.execute(delete(m.UserGrant).where(m.UserGrant.user_id == row.id))
        for grant in grants:
            actor.db.add(m.UserGrant(user_id=row.id, **grant))
    bump_permission_version(actor.db, [row.id])
    for session in actor.db.scalars(
        select(m.Session).where(m.Session.user_id == row.id, m.Session.revoked_at.is_(None))
    ):
        session.revoked_at = utcnow()
    append_audit(
        actor.db,
        actor.user_id,
        "user.update",
        "user",
        row.id,
        details={"grants_replaced": grants is not None},
    )
    actor.db.commit()
    return _user_response(actor.db, row)


def _group_response(db, group):
    return {
        **fields(group, ["id", "name", "description", "created_at", "updated_at"]),
        "user_ids": list(
            db.scalars(
                select(m.GroupMembership.user_id).where(m.GroupMembership.group_id == group.id)
            )
        ),
        "member_count": db.scalar(
            select(func.count())
            .select_from(m.GroupMembership)
            .where(m.GroupMembership.group_id == group.id)
        ),
        "grants": [
            fields(g, ["role_id", "scope_type", "scope_id"])
            for g in db.scalars(select(m.GroupGrant).where(m.GroupGrant.group_id == group.id))
        ],
    }


@router.get("/groups")
def groups(
    limit: int = 50, cursor: str | None = None, actor: ActorContext = Depends(current_actor)
):
    _action_grants(actor, "group.manage")
    return _page(
        [
            _group_response(actor.db, g)
            for g in actor.db.scalars(select(m.Group))
            if _manageable_group(actor, g)
        ],
        limit,
        cursor,
    )


@router.post("/groups", status_code=201)
def create_group(body: dict = Body(...), actor: ActorContext = Depends(current_actor)):
    _action_grants(actor, "group.manage")
    name = _name(body)
    if actor.db.scalar(select(m.Group.id).where(m.Group.name == name)):
        raise PlatformError(409, "group_exists", "组名已存在。")
    grants = _grants_payload(actor, body.get("grants", []))
    if not _global(actor, "group.manage") and not grants:
        raise PlatformError(403, "group_scope_required", "范围管理员必须为新组指定明确授权范围。")
    if any(not _contains(actor, "group.manage", g["scope_type"], g["scope_id"]) for g in grants):
        raise PlatformError(403, "group_scope_required", "组授权超出管理范围。")
    row = m.Group(name=name, description=str(body.get("description", ""))[:2000])
    actor.db.add(row)
    actor.db.flush()
    for grant in grants:
        actor.db.add(m.GroupGrant(group_id=row.id, **grant))
    append_audit(actor.db, actor.user_id, "group.create", "group", row.id)
    actor.db.commit()
    return _group_response(actor.db, row)


@router.put("/groups/{identifier}/members")
def group_members(
    identifier: str, body: dict = Body(...), actor: ActorContext = Depends(current_actor)
):
    group = actor.db.get(m.Group, identifier)
    if not _manageable_group(actor, group):
        raise PlatformError(404, "not_found", "组不可访问。")
    ids = body.get("user_ids", [])
    if not isinstance(ids, list) or len(ids) > 500 or len(ids) != len(set(ids)):
        raise PlatformError(422, "invalid_members", "成员清单无效。")
    existing = set(
        actor.db.scalars(
            select(m.GroupMembership.user_id).where(m.GroupMembership.group_id == identifier)
        )
    )
    for user_id in set(ids) | existing:
        if not _manageable_user(actor, actor.db.get(m.User, user_id), "group.manage"):
            raise PlatformError(404, "not_found", "成员不可访问。")
    # Joining a group delegates all of its grants; group.manage alone cannot bypass grant.manage.
    for grant in actor.db.scalars(select(m.GroupGrant).where(m.GroupGrant.group_id == identifier)):
        require_delegation(actor, grant.role_id, grant.scope_type, grant.scope_id)
    actor.db.execute(delete(m.GroupMembership).where(m.GroupMembership.group_id == identifier))
    for user_id in ids:
        actor.db.add(m.GroupMembership(group_id=identifier, user_id=user_id))
    bump_permission_version(actor.db, set(ids) | existing)
    append_audit(
        actor.db,
        actor.user_id,
        "group.members.replace",
        "group",
        identifier,
        details={"member_count": len(ids)},
    )
    actor.db.commit()
    return _group_response(actor.db, group)


@router.put("/groups/{identifier}/grants")
def group_grants(
    identifier: str, body: dict = Body(...), actor: ActorContext = Depends(current_actor)
):
    group = actor.db.get(m.Group, identifier)
    if not _manageable_group(actor, group):
        raise PlatformError(404, "not_found", "组不可访问。")
    grants = _grants_payload(actor, body.get("grants", []))
    for old in actor.db.scalars(select(m.GroupGrant).where(m.GroupGrant.group_id == identifier)):
        require_delegation(actor, old.role_id, old.scope_type, old.scope_id)
    if any(not _contains(actor, "group.manage", g["scope_type"], g["scope_id"]) for g in grants):
        raise PlatformError(403, "group_scope_required", "组授权超出管理范围。")
    members = list(
        actor.db.scalars(
            select(m.GroupMembership.user_id).where(m.GroupMembership.group_id == identifier)
        )
    )
    actor.db.execute(delete(m.GroupGrant).where(m.GroupGrant.group_id == identifier))
    for grant in grants:
        actor.db.add(m.GroupGrant(group_id=identifier, **grant))
    bump_permission_version(actor.db, members)
    append_audit(
        actor.db,
        actor.user_id,
        "group.grants.replace",
        "group",
        identifier,
        details={"grant_count": len(grants)},
    )
    actor.db.commit()
    return _group_response(actor.db, group)


@router.get("/settings/access-options")
def access_options(actor: ActorContext = Depends(current_actor)):
    authority = _action_grants(actor, "grant.manage")
    roles = []
    scopes = []
    valid_roles = set()
    for role in actor.db.scalars(select(m.Role)):
        if any(role.name in grant.delegatable_roles for grant in authority):
            # require_delegation rechecks role action ceiling for each actual choice below.
            roles.append(fields(role, ["id", "name", "permissions"]))
    for kind, model in [
        ("department", m.Department),
        ("system", m.System),
        ("instance", m.Instance),
    ]:
        for row in actor.db.scalars(select(model)):
            valid = False
            for role in roles:
                try:
                    require_delegation(actor, role["id"], kind, row.id)
                    valid = True
                    valid_roles.add(role["id"])
                except HTTPException:
                    pass
            if valid:
                scopes.append({"scope_type": kind, "scope_id": row.id, "name": row.name})
    return {
        "delegable_roles": [role for role in roles if role["id"] in valid_roles],
        "delegable_scopes": scopes,
    }


@router.get("/settings/connection-options")
def connection_options(request: Request, actor: ActorContext = Depends(current_actor)):
    permissions = current_permissions(actor)
    if not {"asset.onboard", "asset.manage", "collection_profile.manage"} & permissions:
        raise PlatformError(403, "not_permitted", "没有连接管理权限。")
    config = request.app.state.config

    def options(name):
        value = getattr(config, name, []) or []
        if isinstance(value, dict):
            value = [{"id": key, **item} for key, item in value.items()]
        return [
            {"id": row["id"], "name": row.get("name", row["id"])}
            for row in value
            if isinstance(row, dict) and row.get("id")
        ]

    return {
        "ssh_profiles": options("ssh_profiles"),
        "tls_profiles": options("tls_profiles"),
        "host_profiles": options("host_profiles"),
        "log_profiles": options("log_profiles"),
        "allowed_networks": list(config.allowed_networks),
        "allowed_db_ports": getattr(config, "allowed_db_ports", [3306, 3307, 3308, 3310]),
    }


@router.get("/collection-profiles")
def collection_profiles(
    limit: int = 50, cursor: str | None = None, actor: ActorContext = Depends(current_actor)
):
    if not {"asset.onboard", "asset.manage", "collection_profile.manage"} & current_permissions(
        actor
    ):
        raise PlatformError(403, "not_permitted", "没有采集模板访问权限。")
    row, value = _setting(actor.db, "collection_profiles")
    result = _page(value.get("items", []), limit, cursor)
    result["version"] = row.version if row else None
    return result


@router.post("/collection-profiles", status_code=201)
def create_collection_profile(body: dict = Body(...), actor: ActorContext = Depends(current_actor)):
    _require_global(actor, "collection_profile.manage")
    name = _name(body)
    collectors = body.get("collectors") or ["identity_status", "runtime"]
    if not isinstance(collectors, list) or any(c not in COLLECTORS for c in collectors):
        raise PlatformError(422, "invalid_collectors", "固定采集器枚举无效。")
    try:
        interval = int(body.get("interval_seconds", 30))
        timeout = int(body.get("timeout_seconds", 10))
    except (ValueError, TypeError):
        raise PlatformError(422, "invalid_interval", "周期与超时无效。") from None
    if not 15 <= interval <= 3600 or not 1 <= timeout <= 30:
        raise PlatformError(422, "invalid_interval", "周期或超时超过允许范围。")
    _, value = _setting(actor.db, "collection_profiles")
    items = value.get("items", [])
    if any(p.get("name") == name for p in items):
        raise PlatformError(409, "profile_exists", "模板名称已存在。")
    profile = {
        "id": str(uuid4()),
        "name": name,
        "version": 1,
        "intervals": {c: interval for c in set(collectors) | {"identity_status"}},
        "timeout_seconds": timeout,
        "enabled": _boolean(body, "enabled", True),
        "updated_at": iso(utcnow()),
    }
    _save_setting(actor.db, "collection_profiles", {"items": [*items, profile]})
    append_audit(
        actor.db, actor.user_id, "collection_profile.create", "collection_profile", profile["id"]
    )
    actor.db.commit()
    return profile


@router.get("/rules")
def rules(limit: int = 50, cursor: str | None = None, actor: ActorContext = Depends(current_actor)):
    allowed = authorize(actor, "rule.manage").instance_ids
    global_manager = _global(actor, "rule.manage")
    instances = list(
        actor.db.scalars(
            select(m.Instance)
            .where(m.Instance.id.in_(allowed))
            .order_by(m.Instance.name, m.Instance.id)
        )
    )
    known = {row.id: row.name for row in instances}
    items = []
    for row in actor.db.scalars(select(m.Rule)):
        ids = _rule_instance_ids(row)
        if ids is None:
            if not global_manager:
                continue
        elif not ids or not set(ids).issubset(known):
            continue
        items.append(_rule_response(row, known))
    result = _page(items, limit, cursor)
    result.update(
        templates=RULE_TEMPLATES,
        can_manage_all=global_manager,
        instance_options=[{"id": row.id, "name": row.name} for row in instances[:500]],
        instance_options_truncated=len(instances) > 500,
    )
    return result


def _rule_instance_ids(row):
    params = row.parameters or {}
    if "instance_ids" not in params:
        return None  # Existing fixed global rules retain their scope.
    ids = params["instance_ids"]
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 5
        or any(not isinstance(value, str) or not value for value in ids)
    ):
        return []
    return ids if len(set(ids)) == len(ids) else []


def _rule_response(row, known=None):
    ids = _rule_instance_ids(row)
    return {
        **fields(
            row,
            [
                "id",
                "name",
                "rule_version",
                "template",
                "parameters",
                "enabled",
                "created_at",
                "updated_at",
            ],
        ),
        "type": row.template,
        "version": row.rule_version,
        "severity": (row.parameters or {}).get("severity"),
        "scope": "all" if ids is None else "instances",
        "instance_ids": ids,
        "instances": [
            {"id": identifier, "name": known.get(identifier, identifier)}
            for identifier in ids or []
        ]
        if known is not None
        else [],
    }


def _rule_parameters(actor, body, template):
    common = {
        "threshold",
        "min_duration_seconds",
        "freshness_multiplier",
        "instance_ids",
        "severity",
        "collector",
    }
    allowed = common | (
        {"schema", "digest", "min_executions"}
        if template == "sql_latency"
        else {"stale_intervals"}
        if template in ("collector_stale", "collector_unreachable")
        else set()
    )
    aliases = {"duration_seconds": "min_duration_seconds"}
    top = {"name", "type", "template", "enabled", "scope", "parameters"} | allowed | set(aliases)
    nested = body.get("parameters", {})
    if not isinstance(nested, dict) or set(nested) - allowed or set(body) - top:
        raise PlatformError(422, "invalid_rule_parameters", "规则只接受所选固定模板的参数。")
    params = dict(nested)
    for key, value in body.items():
        canonical = aliases.get(key, key)
        if canonical not in allowed:
            continue
        if canonical in params and params[canonical] != value:
            raise PlatformError(
                422, "conflicting_rule_parameters", "同一规则参数不能同时提交不同值。"
            )
        params[canonical] = value
    scope = body.get("scope")
    if (
        scope not in (None, "all", "instances")
        or scope == "all"
        and "instance_ids" in params
        or scope == "instances"
        and "instance_ids" not in params
    ):
        raise PlatformError(422, "invalid_rule_scope", "请选择全部实例或明确的实例集合。")
    if "instance_ids" in params:
        ids = params["instance_ids"]
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 5
            or any(not isinstance(value, str) or not value for value in ids)
            or len(set(ids)) != len(ids)
        ):
            raise PlatformError(422, "invalid_rule_scope", "实例范围必须是1–5个不重复的现有实例。")
        authorize(actor, "rule.manage", ids)
        params["instance_ids"] = sorted(ids)
    else:
        _require_global(actor, "rule.manage")
    severity = params.get("severity", "medium")
    if severity not in ("critical", "high", "medium", "low"):
        raise PlatformError(422, "invalid_rule_severity", "严重度必须是固定枚举。")
    params["severity"] = severity

    def number(key, default, minimum, maximum, integer=False):
        value = params.get(key, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not minimum <= value <= maximum
            or not math.isfinite(value)
            or integer
            and not isinstance(value, int)
        ):
            raise PlatformError(422, "invalid_rule_parameters", "规则数值参数超过边界。")
        params[key] = value

    number("threshold", 0.5 if template == "sql_latency" else 0, 0, 1e9)
    number("min_duration_seconds", 60, 0, 3600, True)
    number("freshness_multiplier", 1.5, 1, 10)
    fixed_collector = (
        "runtime"
        if template == "locks_present"
        else "statement_digest"
        if template == "sql_latency"
        else "identity_status"
    )
    collector = params.get("collector", fixed_collector)
    if (
        collector not in COLLECTORS
        or template in ("locks_present", "sql_latency")
        and collector != fixed_collector
    ):
        raise PlatformError(422, "invalid_rule_collector", "采集器必须与固定模板匹配。")
    params["collector"] = collector
    if template == "sql_latency":
        number("min_executions", 10, 10, 10000, True)
        if "schema" in params and (
            not isinstance(params["schema"], str)
            or not 1 <= len(params["schema"]) <= 64
            or "\0" in params["schema"]
        ):
            raise PlatformError(422, "invalid_rule_schema", "Schema 名称须为1–64字符且不含空字符。")
        if "digest" in params:
            if not isinstance(params["digest"], str) or not re.fullmatch(
                r"[0-9a-fA-F]{64}", params["digest"]
            ):
                raise PlatformError(
                    422, "invalid_rule_digest", "SQL 摘要必须是64位十六进制标识，不接受 SQL 文本。"
                )
            params["digest"] = params["digest"].lower()
    elif template in ("collector_stale", "collector_unreachable"):
        number("stale_intervals", max(1, params["threshold"] or 3), 1, 100)
    return params


@router.post("/rules", status_code=201)
def create_rule(body: dict = Body(...), actor: ActorContext = Depends(current_actor)):
    _action_grants(actor, "rule.manage")
    template = body.get("template") or body.get("type")
    if "type" in body and "template" in body and body["type"] != body["template"]:
        raise PlatformError(422, "conflicting_rule_template", "规则模板只能有一个确定值。")
    if template not in {r["id"] for r in RULE_TEMPLATES}:
        raise PlatformError(422, "unsupported_rule_template", "只允许已实现的固定规则模板。")
    params = _rule_parameters(actor, body, template)
    row = m.Rule(
        name=_name(body),
        rule_version=str(uuid4()),
        template=template,
        parameters=params,
        enabled=_boolean(body, "enabled", True),
    )
    actor.db.add(row)
    actor.db.flush()
    append_audit(actor.db, actor.user_id, "rule.create", "rule", row.id)
    actor.db.commit()
    return _rule_response(row)


@router.patch("/rules/{identifier}")
def set_rule_enabled(
    identifier: str, body: dict = Body(...), actor: ActorContext = Depends(current_actor)
):
    _action_grants(actor, "rule.manage")
    if set(body) != {"enabled", "expected_enabled", "version", "reason"}:
        raise PlatformError(
            422,
            "invalid_rule_update",
            "仅允许提交启用状态、原启用状态、当前规则版本与原因；参数变更请新建规则。",
        )
    enabled = _boolean(body, "enabled")
    expected_enabled = _boolean(body, "expected_enabled")
    reason = _name(body, "reason", 2000)
    row = actor.db.scalar(
        select(m.Rule)
        .where(m.Rule.id == identifier)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise PlatformError(404, "not_found", "规则不存在。")
    ids = _rule_instance_ids(row)
    if ids is None:
        _require_global(actor, "rule.manage")
    elif not ids:
        raise PlatformError(404, "not_found", "规则不存在。")
    else:
        authorize(actor, "rule.manage", ids)
    if body["version"] != row.rule_version:
        raise PlatformError(409, "version_conflict", "规则定义版本已变化，请重新读取。")
    if expected_enabled != row.enabled:
        raise PlatformError(
            409,
            "state_conflict",
            "规则启用状态已变化，请重新核对。",
            {"enabled": row.enabled, "version": row.rule_version},
        )
    prior = row.enabled
    row.enabled = enabled
    append_audit(
        actor.db,
        actor.user_id,
        "rule.set_enabled",
        "rule",
        row.id,
        details={
            "version": row.rule_version,
            "previous_enabled": prior,
            "enabled": enabled,
            "reason": reason,
        },
    )
    actor.db.commit()
    return _rule_response(row)


@router.get("/settings/model-policies")
def model_policies(actor: ActorContext = Depends(current_actor)):
    _action_grants(actor, "model_policy.manage")
    items = []
    for kind, model in [("system", m.System), ("instance", m.Instance)]:
        ids = allowed_scope_ids(actor, "model_policy.manage", kind)
        for row in actor.db.scalars(select(model).where(model.id.in_(ids))):
            configured, value = _setting(actor.db, f"model_policy:{kind}:{row.id}")
            items.append(
                {
                    "scope_type": kind,
                    "scope_id": row.id,
                    "name": row.name,
                    "mode": row.model_policy,
                    "version": row.policy_version,
                    "source_policy_configured": configured is not None,
                    "allowed_sources": value.get("allowed_sources", sorted(set(COLLECTORS) | {'monitoring'})),
                }
            )
    return {"items": items, "default": "rules_only", "raw_external_use": False}


@router.patch("/settings/model-policies")
def update_model_policy(body: dict = Body(...), actor: ActorContext = Depends(current_actor)):
    kind = body.get("scope_type")
    identifier = body.get("scope_id")
    mode = body.get("mode")
    if kind not in ("system", "instance") or mode not in (
        "disabled",
        "rules_only",
        "live_redacted",
    ):
        raise PlatformError(422, "invalid_policy", "策略范围或模式无效。")
    if not _contains(actor, "model_policy.manage", kind, identifier):
        raise PlatformError(404, "not_found", "策略范围不可访问。")
    row = actor.db.get(m.System if kind == "system" else m.Instance, identifier)
    if not row:
        raise PlatformError(404, "not_found", "策略范围不可访问。")
    if kind == "instance":
        parent = actor.db.get(m.System, row.system_id)
        if mode == "live_redacted" and parent.model_policy != "live_redacted":
            raise PlatformError(409, "parent_policy_restricts", "实例策略不能扩大上级系统许可。")
    reason = _name(body, "reason", 2000)
    supplied = body.get("version")
    if supplied is not None and supplied != row.policy_version:
        raise PlatformError(409, "policy_changed", "策略版本已变化。")
    # Allowed source metadata is stored for stricter worker checks; never implicitly grants external_use.
    _, existing = _setting(actor.db, f"model_policy:{kind}:{identifier}")
    model_sources = set(COLLECTORS) | {'monitoring'}
    allowed = body.get("allowed_sources", existing.get("allowed_sources", sorted(model_sources)))
    if not isinstance(allowed, list) or any(source not in model_sources for source in allowed):
        raise PlatformError(422, "invalid_sources", "资料类别无效。")
    row.model_policy = mode
    row.policy_version += 1
    _save_setting(
        actor.db,
        f"model_policy:{kind}:{identifier}",
        {"mode": mode, "allowed_sources": allowed, "reason": reason},
    )
    append_audit(
        actor.db,
        actor.user_id,
        "model_policy.update",
        kind,
        identifier,
        details={"mode": mode, "policy_version": row.policy_version},
    )
    actor.db.commit()
    return {
        "scope_type": kind,
        "scope_id": identifier,
        "mode": mode,
        "version": row.policy_version,
        "allowed_sources": allowed,
    }


@router.get("/model-profiles")
def model_profiles(request: Request, actor: ActorContext = Depends(current_actor)):
    if not {"model_policy.manage", "model.external_use"} & current_permissions(actor):
        raise PlatformError(403, "not_permitted", "没有模型配置访问权限。")
    config = request.app.state.config
    row, value = _setting(actor.db, "model_profile:default")
    private = config.model or {}
    if not private.get("model"):
        return {"items": [], "total": 0, "next_cursor": None}
    try:
        key_file_configured = (
            bool(private.get("key_path"))
            and Path(private["key_path"]).is_file()
            and Path(private["key_path"]).stat().st_size > 0
        )
    except (OSError, TypeError, ValueError):
        key_file_configured = False
    item = {
        "id": "default",
        "name": value.get("name", "默认模型配置"),
        "provider": value.get("provider", private.get("provider")),
        "model": value.get("model", private.get("model")),
        "has_secret": bool(
            value.get("credential_ref") or private.get("credential_ref") or key_file_configured
        ),
        "version": row.version if row else 0,
        "updated_at": iso(row.updated_at) if row else None,
        "pricing_verified": bool(private.get("pricing")),
        "provider_locked": True,
        "max_calls": value.get("max_calls", 4),
        "max_tokens": value.get("max_tokens", 2000),
    }
    return {"items": [item], "total": 1, "next_cursor": None}


@router.post("/model-profiles", status_code=201)
def update_model_profile(
    request: Request, body: dict = Body(...), actor: ActorContext = Depends(current_actor)
):
    _require_global(actor, "model_policy.manage")
    private = request.app.state.config.model or {}
    if not private.get("model"):
        raise PlatformError(
            409, "model_provider_not_configured", "请先通过私密运行配置登记受信模型端点。"
        )
    if body.get("base_url") and body["base_url"] != private.get("base_url"):
        raise PlatformError(
            422, "provider_endpoint_locked", "API 不允许提交新的任意模型端点；请使用受信私密配置。"
        )
    for key in ("provider", "model"):
        if body.get(key) and body[key] != private.get(key):
            raise PlatformError(422, "provider_identity_locked", "模型身份需与已核实私密配置一致。")
    _, value = _setting(actor.db, "model_profile:default")
    value.update(
        {
            "provider": private.get("provider"),
            "model": private["model"],
            "name": str(body.get("name", "默认模型配置"))[:120],
        }
    )
    for key, maximum in [("max_calls", 4), ("max_tokens", 2000), ("timeout_seconds", 180)]:
        if key in body:
            try:
                number = int(body[key])
            except (TypeError, ValueError):
                raise PlatformError(422, "invalid_model_limit", "模型预算无效。") from None
            if not 1 <= number <= maximum:
                raise PlatformError(422, "invalid_model_limit", "模型预算超过平台上限。")
            value[key] = number
    if body.get("api_key"):
        if not isinstance(body["api_key"], str) or len(body["api_key"]) > 4096:
            raise PlatformError(422, "invalid_api_key", "模型凭据无效。")
        ref = SecretStore(request.app.state.config).put_secret(
            {"api_key": body["api_key"]}, purpose="model"
        )
        value["credential_ref"] = ref.ref
    setting = _save_setting(actor.db, "model_profile:default", value)
    append_audit(
        actor.db,
        actor.user_id,
        "model_profile.update",
        "model_profile",
        "default",
        details={"version": setting.version, "credential_changed": bool(body.get("api_key"))},
    )
    actor.db.commit()
    return model_profiles(request, actor)["items"][0]


@router.get("/settings/retention")
def retention(actor: ActorContext = Depends(current_actor)):
    _action_grants(actor, "settings.manage")
    row, value = _setting(actor.db, "retention")
    return {
        **value,
        "version": row.version if row else None,
        "metrics_days": value.get("prometheus_days"),
        "pending_proposal": _setting(actor.db, "retention_pending")[1] or None,
        "deletion_performed": False,
    }


@router.patch("/settings/retention")
def update_retention(body: dict = Body(...), actor: ActorContext = Depends(current_actor)):
    _require_global(actor, "settings.manage")
    row, current = _setting(actor.db, "retention")
    values = {}
    mapping = {"metrics_days": "prometheus_days"}
    for key in (
        "evidence_days",
        "metrics_days",
        "metric_frames_days",
        "objects_days",
        "audit_days",
    ):
        if key in body:
            try:
                number = int(body[key])
            except (ValueError, TypeError):
                raise PlatformError(422, "invalid_retention", "保留天数无效。") from None
            if not 1 <= number <= 3650:
                raise PlatformError(422, "invalid_retention", "保留天数超过范围。")
            values[mapping.get(key, key)] = number
    if not values:
        raise PlatformError(422, "retention_required", "需要至少一项保留期限。")
    reason = _name(body, "reason", 2000)
    tightens = any(key in current and value < current[key] for key, value in values.items())
    pins = actor.db.scalar(
        select(func.count()).select_from(m.EvidencePin).where(m.EvidencePin.retain_until > utcnow())
    )
    if tightens:
        proposal = {
            "id": str(uuid4()),
            "base_version": row.version if row else None,
            "values": values,
            "reason": reason,
            "protected_pin_count": pins,
            "state": "pending_review",
            "created_at": iso(utcnow()),
            "deletion_performed": False,
        }
        _save_setting(actor.db, "retention_pending", proposal)
        append_audit(
            actor.db,
            actor.user_id,
            "retention.propose",
            "settings",
            "retention",
            details={"tightening": True, "protected_pin_count": pins},
        )
        actor.db.commit()
        return {
            "status": "pending_review",
            "proposal": proposal,
            "active_values": current,
            "deletion_performed": False,
        }
    updated = _save_setting(actor.db, "retention", {**current, **values})
    append_audit(
        actor.db,
        actor.user_id,
        "retention.update",
        "settings",
        "retention",
        details={"tightening": False},
    )
    actor.db.commit()
    return {**updated.value, "version": updated.version, "deletion_performed": False}


def _audit_visible(actor, row):
    if _global(actor, "audit.read"):
        return True
    if row.actor_id == actor.user_id and row.target_type in ("session", "authentication"):
        return True
    kinds = {"department": m.Department, "system": m.System, "instance": m.Instance}
    if row.target_type in kinds:
        return _contains(actor, "audit.read", row.target_type, row.target_id)
    if row.target_type == "user":
        return _manageable_user(actor, actor.db.get(m.User, row.target_id), "audit.read")
    if row.target_type == "group":
        return _manageable_group(actor, actor.db.get(m.Group, row.target_id), "audit.read")
    target_model = {
        "connection_draft": m.ConnectionDraft,
        "connection_test": m.ConnectionTest,
        "event": m.Event,
        "task": m.Task,
        "evidence": m.Evidence,
    }.get(row.target_type)
    target = actor.db.get(target_model, row.target_id) if target_model else None
    if target and getattr(target, "instance_id", None):
        return _contains(actor, "audit.read", "instance", target.instance_id)
    if target and getattr(target, "system_id", None):
        return _contains(actor, "audit.read", "system", target.system_id)
    return False


@router.get("/audit-logs")
def audit_logs(
    limit: int = 50,
    cursor: str | None = None,
    q: str | None = None,
    actor: ActorContext = Depends(current_actor),
):
    _action_grants(actor, "audit.read")
    items = []
    for row in actor.db.scalars(select(m.AuditLog)):
        if not _audit_visible(actor, row):
            continue
        if q and q.casefold() not in row.action.casefold():
            continue
        # Audit bodies are intentionally not returned. These reviewed summaries cannot echo secrets.
        user = actor.db.get(m.User, row.actor_id) if row.actor_id else None
        items.append(
            {
                "id": row.id,
                "created_at": iso(row.occurred_at),
                "actor_id": row.actor_id,
                "actor_name": user.display_name or user.username if user else "system",
                "action": row.action,
                "resource_type": row.target_type,
                "resource_id": row.target_id,
                "outcome": row.result,
                "request_id": row.request_id,
                "summary": row.action,
            }
        )
    return _page(items, limit, cursor)


@router.get("/platform-status")
def platform_status(request: Request, actor: ActorContext = Depends(current_actor)):
    _action_grants(actor, "platform.read")
    config = request.app.state.config
    control = {"status": "unknown", "migration": None}
    try:
        actor.db.execute(sql_text("SELECT 1"))
        control["status"] = "available"
        control["migration"] = actor.db.execute(
            sql_text("SELECT version_num FROM alembic_version")
        ).scalar()
    except Exception:
        actor.db.rollback()
        control["status"] = "degraded"
        control["reason_code"] = "control_database_or_migration_unavailable"
    try:
        with build_opener(ProxyHandler({})).open(
            config.prometheus_url.rstrip("/") + "/-/ready", timeout=1.5
        ) as response:
            prometheus = {
                "status": "available" if response.status == 200 else "unreachable",
                "checked_at": iso(utcnow()),
            }
    except Exception:
        prometheus = {
            "status": "unreachable",
            "checked_at": iso(utcnow()),
            "reason_code": "prometheus_readiness_failed",
        }
    workers = []
    for w in actor.db.scalars(select(m.WorkerHeartbeat)):
        worker_status = (
            "available" if (utcnow() - w.last_seen_at).total_seconds() < 120 else "stale"
        )
        maintenance_summary = (w.details or {}).get("maintenance", {})
        if worker_status == "available" and any(
            isinstance(v, dict) and v.get("last_error_type") for v in maintenance_summary.values()
        ):
            worker_status = "warning"
        workers.append(
            {**fields(w, ["worker_id", "queue", "last_seen_at"]), "status": worker_status}
        )
    statement = select(m.Task)
    if not _global(actor, "platform.read"):
        ids = authorize(actor, "platform.read").instance_ids
        statement = statement.where(m.Task.instance_id.in_(ids))
    tasks = list(actor.db.scalars(statement))
    queued = [t for t in tasks if t.status in ("queued", "retry_wait")]
    _, retention_values = _setting(actor.db, "retention")
    threshold = retention_values.get("min_free_disk_bytes", 5 * 1024**3)
    threshold = (
        threshold
        if isinstance(threshold, int) and not isinstance(threshold, bool) and threshold >= 0
        else 5 * 1024**3
    )
    warnings = []
    try:
        usage = shutil.disk_usage(config.private_dir)
        storage = {
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "measured_at": iso(utcnow()),
        }
        storage.update(
            min_free_disk_bytes=threshold,
            status="warning" if usage.free < threshold else "available",
        )
        if usage.free < threshold:
            warnings.append({"code": "disk_space_below_threshold", "severity": "warning"})
    except OSError:
        storage = {"status": "unknown", "reason_code": "capacity_unavailable"}
        warnings.append({"code": "disk_capacity_unknown", "severity": "warning"})
    maintenance = {}
    from ..common import parse_time

    for name, interval in [("clock", 15), ("retention", 60), ("exports", 60)]:
        _, value = _setting(actor.db, "maintenance:" + name)
        # Settings may contain future fields; expose only this safe contract.
        safe = {
            key: value.get(key)
            for key in (
                "status",
                "last_attempt_at",
                "last_success_at",
                "last_error_type",
                "next_due_at",
                "next_retry_at",
                "consecutive_failures",
                "interval_seconds",
                "last_result",
                "backlog",
            )
        }
        maintenance[name] = safe
        if not value:
            warnings.append(
                {"code": "maintenance_not_observed", "job": name, "severity": "warning"}
            )
        elif value.get("last_error_type") or value.get("status") == "failed":
            warnings.append({"code": "maintenance_failed", "job": name, "severity": "warning"})
        else:
            boundary = (
                value.get("last_attempt_at")
                if value.get("status") == "running"
                else value.get("last_success_at")
            )
            try:
                stale = not boundary or (utcnow() - parse_time(boundary)).total_seconds() > max(
                    120, interval * 3
                )
            except Exception:
                stale = True
            if stale:
                warnings.append({"code": "maintenance_stale", "job": name, "severity": "warning"})
    warnings.append({"code": "object_and_audit_retention_cleanup_pending", "severity": "warning"})
    return {
        "control_database": control,
        "prometheus": prometheus,
        "workers": workers,
        "queue": {
            "waiting": len(queued),
            "running": sum(t.status == "running" for t in tasks),
            "oldest_waiting_at": iso(min((t.created_at for t in queued), default=None)),
        },
        "storage": storage,
        "maintenance": maintenance,
        "warnings": warnings,
        "status": "warning" if warnings else "available",
        "retention_coverage": {
            "business_objects": "dependency_aware_cleanup_pending",
            "audit": "cleanup_pending",
            "protected_evidence": "bounded_reference_aware_cleanup",
        },
        "backup": {
            "status": "not_configured",
            "reason_code": "no_verified_control_database_backup_record",
        },
        "updated_at": iso(utcnow()),
    }
