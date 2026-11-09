"""Explicitly provision the fixed public demo; never connects to the fault target.

No credentials are supplied by default. All three input files must already exist
outside any source checkout. Run while API/workers are stopped; restart them only
after this command succeeds. Database and filesystem are separate resources: on
an interrupted/failed run keep services stopped and rerun the command to reconcile.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sqlalchemy import delete, select
from platform_app import models as m
from platform_app.db import utcnow
from platform_app.identity import hash_password, verify_password

IDENTITIES = (
    ('demo', '公共演示访客', 'public_demo',
     ('data.read', 'event.read', 'knowledge.read', 'demo.operate')),
    ('demo-runner', '演示分析服务', 'demo_runner',
     ('data.read', 'event.read', 'investigation.create', 'model.external_use')),
)


def private_path(value, *, must_exist=True):
    """Resolve symlinks and reject every enclosing git checkout, even ignored files."""
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('private_absolute_path_required')
    path = path.resolve()
    if any((parent / '.git').exists() for parent in (path, *path.parents)):
        raise ValueError('repository_private_path_forbidden')
    if path == REPO or REPO in path.parents:
        raise ValueError('repository_private_path_forbidden')
    if must_exist and not path.is_file():
        raise ValueError('private_file_required')
    return path


def atomic_json(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.demo-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare(db, args):
    """Validate all boundaries first, then reconcile only the two owned identities.

    Caller owns the database transaction. Output contains IDs only. Existing
    roles are reusable only when their permissions are a subset of our contract
    and they have no bindings outside their single owned user.
    """
    config_path = private_path(args.config)
    password_path = private_path(args.password_file)
    target_path = private_path(args.target_config)
    service_path = private_path(password_path.with_name('service-account.private.json'), must_exist=False)
    if len({config_path, password_path, target_path, service_path}) != 4:
        raise ValueError('private_files_must_be_distinct')
    if args.accept_isolated_lab is not True:
        raise ValueError('isolated_lab_confirmation_required')
    raw = json.loads(config_path.read_text(encoding='utf-8-sig'))
    if not isinstance(raw, dict) or not isinstance(raw.get('demo', {}), dict):
        raise ValueError('invalid_private_configuration')
    password = password_path.read_text(encoding='utf-8-sig').rstrip('\r\n')
    if not 12 <= len(password) <= 1024:
        raise ValueError('invalid_demo_password')
    service = json.loads(service_path.read_text(encoding='utf-8-sig')) if service_path.exists() else {
        'username': 'demo-runner', 'password': secrets.token_urlsafe(48)}
    if (not isinstance(service, dict) or service.get('username') != 'demo-runner'
            or not isinstance(service.get('password'), str) or not 32 <= len(service['password']) <= 1024):
        raise ValueError('invalid_private_service_account')
    instance = db.scalar(select(m.Instance).where(m.Instance.id == args.instance_id).with_for_update())
    source = db.get(m.MonitoringSource, args.source_id)
    mapping = db.scalar(select(m.MonitoringMapping).where(
        m.MonitoringMapping.instance_id == args.instance_id).with_for_update())
    if not (instance and instance.enabled and instance.lifecycle_state in ('Ready', 'Active')
            and instance.connection_mode == 'zabbix' and source and source.status == 'ready'
            and source.system_id == instance.system_id and mapping and mapping.selected
            and mapping.layer == 'group' and mapping.hostid.startswith('group:')
            and mapping.source_id == source.id):
        raise ValueError('ready_selected_zabbix_group_required')
    selected = [i for i in mapping.items if isinstance(i, dict)
                and str(i.get('itemid', '')) == args.lock_itemid
                and str(i.get('hostid', '')) == args.lock_hostid
                and i.get('enhancement') == 'mysql.lock_waits']
    if len(selected) != 1:
        raise ValueError('exact_lock_snapshot_required')

    plans = []
    for username, display_name, role_name, permissions in IDENTITIES:
        user = db.scalar(select(m.User).where(m.User.username == username).with_for_update())
        role = db.scalar(select(m.Role).where(m.Role.name == role_name).with_for_update())
        if user and user.display_name != display_name:
            raise ValueError('unowned_demo_username_collision')
        if role:
            bindings = list(db.scalars(select(m.UserGrant).where(m.UserGrant.role_id == role.id)))
            if (not set(role.permissions).issubset(permissions)
                    or any(g.user_id != (user.id if user else None) for g in bindings)
                    or db.scalar(select(m.GroupGrant.id).where(m.GroupGrant.role_id == role.id).limit(1))):
                raise ValueError('unowned_demo_role_collision')
        plans.append((user, role, username, display_name, permissions))

    ids = {}
    for user, role, username, display_name, permissions in plans:
        chosen_password = password if username == 'demo' else service['password']
        new_user = user is None
        if role is None:
            role = m.Role(name='public_demo' if username == 'demo' else 'demo_runner',
                          permissions=list(permissions))
            db.add(role); db.flush()
        role_changed = set(role.permissions) != set(permissions)
        role.permissions = list(permissions)
        if user is None:
            user = m.User(username=username, display_name=display_name,
                          password_hash=hash_password(chosen_password), active=True)
            db.add(user); db.flush()
        grants = list(db.scalars(select(m.UserGrant).where(m.UserGrant.user_id == user.id)))
        membership = db.scalar(select(m.GroupMembership).where(m.GroupMembership.user_id == user.id).limit(1))
        exact = len(grants) == 1 and grants[0].role_id == role.id and grants[0].scope_type == 'instance' and grants[0].scope_id == instance.id and not grants[0].delegatable_roles and not grants[0].delegated_actions
        password_changed = not verify_password(user.password_hash, chosen_password)
        changed = role_changed or not exact or membership is not None or not user.active or password_changed
        if password_changed:
            user.password_hash = hash_password(chosen_password)
        if changed:
            db.execute(delete(m.UserGrant).where(m.UserGrant.user_id == user.id))
            db.execute(delete(m.GroupMembership).where(m.GroupMembership.user_id == user.id))
            db.add(m.UserGrant(user_id=user.id, role_id=role.id, scope_type='instance', scope_id=instance.id,
                               delegatable_roles=[], delegated_actions=[]))
            if not new_user:
                user.permission_version += 1
                for session in db.scalars(select(m.Session).where(m.Session.user_id == user.id, m.Session.revoked_at.is_(None))):
                    session.revoked_at = utcnow()
        user.active = True
        ids[username] = user.id
    instance.is_lab = True
    if db.get(m.DemoControl, 1) is None:
        db.add(m.DemoControl(id=1))
    db.flush()
    raw['demo'] = {**raw.get('demo', {}), 'enabled': True, 'instance_id': instance.id,
                   'source_id': source.id, 'lock_itemid': args.lock_itemid, 'lock_hostid': args.lock_hostid,
                   'runner_user_id': ids['demo-runner'], 'target_config_path': str(target_path),
                   'max_duration_seconds': 180, 'cooldown_seconds': 600, 'max_daily_runs': 8,
                   'max_daily_ai_runs': 4, 'mode': 'live_redacted', 'external_confirmed': True}
    atomic_json(service_path, service)
    atomic_json(config_path, raw)
    return {'demo_user_id': ids['demo'], 'runner_user_id': ids['demo-runner']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('config','instance-id','source-id','lock-itemid','lock-hostid','password-file','target-config'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--accept-isolated-lab', action='store_true', required=True)
    args = parser.parse_args(argv)
    try:
        from platform_app.config import load_config
        from platform_app.db import create_db_engine, create_session_factory
        # Validate private paths before reading a credential-bearing configuration.
        private_path(args.password_file)
        private_path(args.target_config)
        config = load_config(private_path(args.config))
        engine = create_db_engine(config)
        try:
            with create_session_factory(engine).begin() as db:
                prepare(db, args)
        finally:
            engine.dispose()
    except Exception:
        print('Demo provisioning failed; keep services stopped, check private inputs and ownership, then rerun.', file=sys.stderr)
        return 1
    print('Demo provisioned. Restart API and worker to load private configuration.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
