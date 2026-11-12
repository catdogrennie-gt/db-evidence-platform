"""Action registry and explicit direct/group scope authorization.

Every check reads current grants. An ActorContext is an identity reference, never
a permanently trusted snapshot of authorization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from .db import utcnow
from .models import (
    Department,
    GroupGrant,
    GroupMembership,
    Instance,
    Role,
    Session,
    System,
    User,
    UserGrant,
)


CONTROL_ACTIONS = frozenset(
    {
        "organization.manage",
        "user.manage",
        "group.manage",
        "grant.manage",
        "asset.onboard",
        "asset.manage",
        "settings.manage",
        "rule.manage",
        "collection_profile.manage",
        "model_policy.manage",
        "audit.read",
        "platform.read",
        "configuration.read",
    }
)
ACTION_REGISTRY = CONTROL_ACTIONS | frozenset(
    {
        "data.read",
        "evidence.read_sensitive",
        "event.read",
        "event.comment",
        "event.create",
        "event.acknowledge",
        "event.assign",
        "event.close",
        "event.merge",
        "investigation.create",
        "model.external_use",
        "export.create",
        "knowledge.read",
        "knowledge.create",
        "knowledge.review",
        "remediation.create",
        "verification.create",
        "collection.trigger",
        "task.manage",
        "lab.operate",
        "demo.operate",
        "remediation.review",
        "remediation.record_execution",
        "maintenance.manage",
        "change.create",
    }
)
ROLE_PERMISSIONS = {
    "platform_admin": CONTROL_ACTIONS,
    "dba": frozenset(
        {
            "data.read",
            "event.read",
            "event.comment",
            "event.create",
            "event.acknowledge",
            "event.assign",
            "event.close",
            "event.merge",
            "investigation.create",
            "knowledge.read",
            "knowledge.create",
            "remediation.create",
            "verification.create",
            "collection.trigger",
            "remediation.review",
            "remediation.record_execution",
            "maintenance.manage",
            "task.manage",
            "export.create",
        }
    ),
    "developer": frozenset(
        {"data.read", "event.read", "event.comment", "knowledge.read", "investigation.create"}
    ),
    "viewer": frozenset({"data.read", "event.read", "knowledge.read"}),
    # A product tour may inspect safe configuration without any management action.
    # Keep the showcase bridge identity on its separate minimal role.
    "demo_viewer": frozenset({"data.read", "event.read", "knowledge.read", "configuration.read"}),
    "auditor": frozenset({"audit.read"}),
    "lab_operator": frozenset({"lab.operate"}),
}


@dataclass(frozen=True)
class ActorContext:
    user_id: str
    session_id: str | None
    permission_version: int
    permissions: frozenset[str]
    db: DBSession = field(repr=False, compare=False)


@dataclass(frozen=True)
class AuthorizedScope:
    instance_ids: frozenset[str]
    action: str = "data.read"
    user_id: str = ""


def _current_user(actor: ActorContext) -> User:
    db = actor.db
    user = db.scalar(
        select(User).where(User.id == actor.user_id).execution_options(populate_existing=True)
    )
    if user is None or not user.active:
        raise HTTPException(401, "Authentication required")
    if actor.session_id:
        session = db.scalar(
            select(Session)
            .where(Session.id == actor.session_id)
            .execution_options(populate_existing=True)
        )
        if (
            session is None
            or session.user_id != user.id
            or session.revoked_at is not None
            or session.expires_at <= utcnow()
            or session.permission_version != user.permission_version
        ):
            raise HTTPException(401, "Session expired or revoked")
    return user


def _grants(db: DBSession, user_id: str):
    direct = db.execute(
        select(UserGrant, Role)
        .join(Role, UserGrant.role_id == Role.id)
        .where(UserGrant.user_id == user_id)
        .execution_options(populate_existing=True)
    ).all()
    groups = db.execute(
        select(GroupGrant, Role)
        .join(Role, GroupGrant.role_id == Role.id)
        .join(GroupMembership, GroupMembership.group_id == GroupGrant.group_id)
        .where(GroupMembership.user_id == user_id)
        .execution_options(populate_existing=True)
    ).all()
    return [*direct, *groups]


def actor_for_user(db: DBSession, user_id: str, session_id: str | None = None) -> ActorContext:
    user = db.get(User, user_id, populate_existing=True)
    if user is None or not user.active:
        raise HTTPException(401, "Authentication required")
    permissions = frozenset(
        p for _, role in _grants(db, user_id) for p in role.permissions if p in ACTION_REGISTRY
    )
    actor = ActorContext(user.id, session_id, user.permission_version, permissions, db)
    _current_user(actor)
    return actor


def current_permissions(actor: ActorContext) -> frozenset[str]:
    _current_user(actor)
    return frozenset(
        p
        for _, role in _grants(actor.db, actor.user_id)
        for p in role.permissions
        if p in ACTION_REGISTRY
    )


def current_roles(actor: ActorContext) -> list[str]:
    _current_user(actor)
    return sorted({role.name for _, role in _grants(actor.db, actor.user_id)})


def _action_grants(actor: ActorContext, action: str):
    _current_user(actor)
    if action not in ACTION_REGISTRY:
        raise HTTPException(403, "Action is not permitted")
    grants = [
        grant for grant, role in _grants(actor.db, actor.user_id) if action in role.permissions
    ]
    if not grants:
        raise HTTPException(403, "Action is not permitted")
    return grants


def _instance_ids_for_grant(db: DBSession, grant) -> set[str]:
    query = select(Instance.id).join(System, Instance.system_id == System.id)
    if grant.scope_type == "department":
        query = query.where(System.department_id == grant.scope_id)
    elif grant.scope_type == "system":
        query = query.where(System.id == grant.scope_id)
    elif grant.scope_type == "instance":
        query = query.where(Instance.id == grant.scope_id)
    elif not (grant.scope_type == "global" and grant.scope_id == "*"):
        return set()
    return set(db.scalars(query))


def authorize(
    actor: ActorContext, action: str, instance_ids: Iterable[str] | None = None
) -> AuthorizedScope:
    allowed = set()
    for grant in _action_grants(actor, action):
        allowed.update(_instance_ids_for_grant(actor.db, grant))
    requested = set(instance_ids) if instance_ids is not None else allowed
    if not requested.issubset(allowed):
        raise HTTPException(404, "Resource not found")
    if action == "lab.operate" and requested:
        lab_ids = set(
            actor.db.scalars(
                select(Instance.id).where(Instance.id.in_(requested), Instance.is_lab.is_(True))
            )
        )
        if requested != lab_ids:
            raise HTTPException(404, "Resource not found")
    return AuthorizedScope(frozenset(requested), action, actor.user_id)


def scoped_instance(actor: ActorContext, instance_id: str, action: str = "data.read") -> Instance:
    authorize(actor, action, [instance_id])
    instance = actor.db.get(Instance, instance_id, populate_existing=True)
    if instance is None:
        raise HTTPException(404, "Resource not found")
    return instance


def allowed_scope_ids(actor: ActorContext, action: str, scope_type: str) -> frozenset[str]:
    """Resolve structural scopes, including systems that have no assets yet."""
    grants = _action_grants(actor, action)
    if scope_type == "instance":
        return authorize(actor, action).instance_ids
    if scope_type not in ("system", "department"):
        raise ValueError("Unsupported scope type")
    allowed = set()
    for grant in grants:
        if scope_type == "department":
            if grant.scope_type == "global" and grant.scope_id == "*":
                allowed.update(actor.db.scalars(select(Department.id)))
            elif grant.scope_type == "department" and actor.db.get(Department, grant.scope_id):
                allowed.add(grant.scope_id)
        else:
            query = select(System.id)
            if grant.scope_type == "department":
                query = query.where(System.department_id == grant.scope_id)
            elif grant.scope_type == "system":
                query = query.where(System.id == grant.scope_id)
            elif not (grant.scope_type == "global" and grant.scope_id == "*"):
                continue
            allowed.update(actor.db.scalars(query))
    return frozenset(allowed)


def authorize_system(actor: ActorContext, system_id: str, action: str = "asset.onboard") -> System:
    if system_id not in allowed_scope_ids(actor, action, "system"):
        raise HTTPException(404, "Resource not found")
    return actor.db.get(System, system_id, populate_existing=True)


def scope_contains(
    db: DBSession, outer_type: str, outer_id: str, inner_type: str, inner_id: str
) -> bool:
    if outer_type == "global" and outer_id == "*":
        return True
    if outer_type == inner_type and outer_id == inner_id:
        return True
    if inner_type == "instance":
        instance = db.get(Instance, inner_id)
        return bool(
            instance and scope_contains(db, outer_type, outer_id, "system", instance.system_id)
        )
    if inner_type == "system" and outer_type == "department":
        system = db.get(System, inner_id)
        return bool(system and system.department_id == outer_id)
    return False


def require_delegation(actor: ActorContext, role_id: str, scope_type: str, scope_id: str):
    """A manager may delegate explicit allowlisted roles inside delegated scope.

    Owning data.read is unrelated to managing grants. Structural containment is
    used rather than current asset sets, so empty scopes cannot widen authority.
    """
    role = actor.db.get(Role, role_id)
    model = {"department": Department, "system": System, "instance": Instance}.get(scope_type)
    if model is None or actor.db.get(model, scope_id) is None:
        raise HTTPException(404, "Resource not found")
    if role is None:
        raise HTTPException(404, "Resource not found")
    for grant in _action_grants(actor, "grant.manage"):
        if role.name not in grant.delegatable_roles:
            continue
        if not scope_contains(actor.db, grant.scope_type, grant.scope_id, scope_type, scope_id):
            continue
        # Built-in role allowlist is stable; custom roles need an explicit action allowlist.
        ceiling = ROLE_PERMISSIONS.get(role.name, frozenset(grant.delegated_actions))
        if set(role.permissions).issubset(ceiling):
            return
    raise HTTPException(403, "Grant is outside delegated authority")


def bump_permission_version(db: DBSession, user_ids: Iterable[str]):
    for user in db.scalars(select(User).where(User.id.in_(set(user_ids))).with_for_update()):
        user.permission_version += 1


def seed_roles(db: DBSession) -> dict[str, Role]:
    roles = {r.name: r for r in db.scalars(select(Role))}
    for name, permissions in ROLE_PERMISSIONS.items():
        if name not in roles:
            roles[name] = Role(name=name, permissions=sorted(permissions))
            db.add(roles[name])
    db.flush()
    return roles
