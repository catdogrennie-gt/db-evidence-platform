"""Explicit initialization from a private plan; never resets existing users."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select

from .access import seed_roles
from .config import load_config
from .db import create_db_engine, create_session_factory
from .identity import hash_password
from .models import Department, PlatformSetting, Role, System, User, UserGrant


def initialize(db, plan):
    roles = seed_roles(db)
    for name, permissions in plan.get("additional_roles", {}).items():
        role = db.scalar(select(Role).where(Role.name == name))
        if role is None:
            role = Role(name=name, permissions=permissions)
            db.add(role)
            db.flush()
        roles[name] = role
    departments, systems, users = {}, {}, {}
    for item in plan.get("departments", []):
        row = db.scalar(select(Department).where(Department.name == item["name"]))
        if row is None:
            row = Department(name=item["name"])
            db.add(row)
            db.flush()
        departments[item["ref"]] = row
    for item in plan.get("systems", []):
        department = departments[item["department_ref"]]
        row = db.scalar(
            select(System).where(System.department_id == department.id, System.name == item["name"])
        )
        if row is None:
            row = System(
                department_id=department.id,
                name=item["name"],
                environment=item.get("environment", "lab"),
                model_policy=item.get("model_policy", "rules_only"),
            )
            db.add(row)
            db.flush()
        systems[item["ref"]] = row
    for item in plan.get("users", []):
        user = db.scalar(select(User).where(User.username == item["username"]))
        if user is not None:
            users[item["username"]] = user
            continue
        user = User(
            username=item["username"],
            display_name=item.get("display_name", item["username"]),
            password_hash=hash_password(item["password"]),
            active=True,
        )
        db.add(user)
        db.flush()
        for grant in item["grants"]:
            scope_type = grant["scope_type"]
            scope_id = (
                "*"
                if scope_type == "global"
                else (departments if scope_type == "department" else systems)[grant["scope_ref"]].id
            )
            db.add(
                UserGrant(
                    user_id=user.id,
                    role_id=roles[grant["role"]].id,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    delegatable_roles=grant.get("delegatable_roles", []),
                    delegated_actions=grant.get("delegated_actions", []),
                )
            )
        users[item["username"]] = user
    defaults = {
        "collection_profiles": {
            "items": [
                {
                    "id": "standard",
                    "name": "标准采集",
                    "version": 1,
                    "intervals": {
                        "identity_status": 30,
                        "runtime": 30,
                        "statement_digest": 60,
                        "schema_config": 300,
                        "replication": 60,
                        "error_log": 60,
                        "slow_log": 60,
                        "host": 30,
                    },
                },
                {
                    "id": "low_impact",
                    "name": "低频采集",
                    "version": 1,
                    "intervals": {
                        "identity_status": 60,
                        "runtime": 60,
                        "statement_digest": 120,
                        "schema_config": 600,
                        "replication": 120,
                        "error_log": 120,
                        "slow_log": 120,
                        "host": 60,
                    },
                },
            ]
        },
        "retention": {
            "evidence_days": 14,
            "metric_frames_days": 7,
            "objects_days": 90,
            "audit_days": 90,
            "prometheus_days": 14,
            "min_free_disk_bytes": 5 * 1024**3,
        },
    }
    for key, value in defaults.items():
        if db.get(PlatformSetting, key) is None:
            db.add(PlatformSetting(key=key, version=1, value=value))
    db.flush()
    return {
        "departments": {key: row.id for key, row in departments.items()},
        "systems": {key: row.id for key, row in systems.items()},
        "users": {key: row.id for key, row in users.items()},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--plan", required=True, help="OS-protected private initialization plan")
    parser.add_argument("--output", required=True, help="Private identifier map output path")
    args = parser.parse_args()
    config = load_config(args.config)
    plan_path = Path(args.plan).resolve()
    repo = Path(__file__).resolve().parents[1]
    output = Path(args.output).resolve()
    if plan_path.is_relative_to(repo) or output.is_relative_to(repo):
        raise SystemExit("Private initialization plan/output must stay outside repository")
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    factory = create_session_factory(create_db_engine(config), config)
    with factory.begin() as db:
        result = initialize(db, plan)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "Initialization complete; existing identities unchanged; identifier map written privately."
    )


if __name__ == "__main__":
    main()
