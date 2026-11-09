"""Provisioning uses only an isolated SQLite control database and private temp files."""
import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from platform_app import models as m
from platform_app.db import Base, create_db_engine, create_session_factory, utcnow
from platform_app.identity import verify_password

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/platform/prepare_demo.py'


@pytest.fixture
def env(tmp_path):
    spec = importlib.util.spec_from_file_location('prepare_demo', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_db_engine('sqlite://')
    Base.metadata.create_all(engine)
    with create_session_factory(engine)() as db:
        department = m.Department(name='test')
        db.add(department); db.flush()
        system = m.System(name='test', department_id=department.id)
        db.add(system); db.flush()
        instance = m.Instance(system_id=system.id, name='lab', endpoint='zabbix:group',
                              connection_mode='zabbix', enabled=True, lifecycle_state='Active')
        source = m.MonitoringSource(system_id=system.id, name='test', url='https://invalid',
                                    secret_ref='unused', status='ready')
        db.add_all([instance, source]); db.flush()
        mapping = m.MonitoringMapping(instance_id=instance.id, source_id=source.id,
                    hostid='group:test', layer='group', selected=True,
                    items=[dict(itemid='42', hostid='17', enhancement='mysql.lock_waits')])
        db.add(mapping); db.commit()
        config = tmp_path / 'platform.private.json'
        config.write_text(json.dumps({'public_origin':'https://test.invalid','untouched':{'a':1}}))
        password = tmp_path / 'password.private.txt'
        password.write_text('test-only-public-password\n')
        target = tmp_path / 'target.private.json'
        target.write_text('{}')
        args = SimpleNamespace(config=str(config), password_file=str(password),
                    target_config=str(target), instance_id=instance.id, source_id=source.id,
                    lock_itemid='42', lock_hostid='17', accept_isolated_lab=True)
        yield module, db, args, instance, mapping
    engine.dispose()


def test_provisions_exact_scoped_identities_and_preserves_config(env):
    module, db, args, instance, _ = env
    result = module.prepare(db, args)
    db.commit()
    users = {u.username:u for u in db.scalars(select(m.User))}
    assert set(users) == {'demo','demo-runner'}
    assert verify_password(users['demo'].password_hash, 'test-only-public-password')
    runner = json.loads(Path(args.password_file).with_name('service-account.private.json').read_text())
    assert verify_password(users['demo-runner'].password_hash, runner['password'])
    assert len(runner['password']) >= 32
    assert result['runner_user_id'] == users['demo-runner'].id
    for name, permissions in [('demo', {'data.read','event.read','knowledge.read','demo.operate'}),
                              ('demo-runner', {'data.read','event.read','investigation.create','model.external_use'})]:
        grant = db.scalar(select(m.UserGrant).where(m.UserGrant.user_id == users[name].id))
        assert (grant.scope_type, grant.scope_id) == ('instance', instance.id)
        assert set(db.get(m.Role, grant.role_id).permissions) == permissions
        assert not grant.delegatable_roles and not grant.delegated_actions
    assert instance.is_lab
    data = json.loads(Path(args.config).read_text())
    assert data['untouched'] == {'a':1} and data['public_origin'] == 'https://test.invalid'
    assert data['demo']['lock_hostid'] == '17'
    assert data['demo']['max_daily_ai_runs'] == 4
    assert data['demo']['enabled'] is True


def test_reconciles_only_owned_users_revokes_sessions_and_is_idempotent(env):
    module, db, args, _, _ = env
    module.prepare(db, args); db.commit()
    demo = db.scalar(select(m.User).where(m.User.username == 'demo'))
    other = m.User(username='operator', password_hash='unused')
    group = m.Group(name='broad')
    role = m.Role(name='broad', permissions=['*'])
    db.add_all([other,group,role]); db.flush()
    db.add_all([m.GroupMembership(user_id=demo.id,group_id=group.id),
                m.UserGrant(user_id=demo.id,role_id=role.id,scope_type='global',scope_id='*'),
                m.UserGrant(user_id=other.id,role_id=role.id,scope_type='global',scope_id='*'),
                m.Session(user_id=demo.id,token_hash='a'*64,csrf_hash='b'*64,
                          permission_version=demo.permission_version,expires_at=utcnow()+timedelta(hours=1))])
    db.commit(); version = demo.permission_version
    module.prepare(db,args); db.commit()
    assert demo.permission_version == version+1
    assert db.scalar(select(m.Session)).revoked_at is not None
    assert len(list(db.scalars(select(m.UserGrant).where(m.UserGrant.user_id==demo.id)))) == 1
    assert db.scalar(select(m.GroupMembership)) is None
    assert db.scalar(select(m.UserGrant).where(m.UserGrant.user_id==other.id)).scope_type == 'global'
    module.prepare(db,args); db.commit()
    assert demo.permission_version == version+1


@pytest.mark.parametrize('bad', ['consent','source','host','item','enhancement','paused','unselected','username','role','group_role','foreign_role'])
def test_refuses_unsafe_targets_and_identity_collisions_before_changes(env,bad):
    module,db,args,instance,mapping=env
    if bad=='consent': args.accept_isolated_lab=False
    elif bad=='source': args.source_id='unknown'
    elif bad=='host': args.lock_hostid='wrong'
    elif bad=='item': args.lock_itemid='wrong'
    elif bad=='enhancement': mapping.items=[dict(itemid='42',hostid='17',enhancement='other')]
    elif bad=='paused': instance.enabled=False
    elif bad=='unselected': mapping.selected=False
    elif bad=='username': db.add(m.User(username='demo',display_name='Unrelated human',password_hash='unused'))
    elif bad=='role': db.add(m.Role(name='public_demo', permissions=['*']))
    else:
        role=m.Role(name='public_demo',permissions=['data.read']); db.add(role); db.flush()
        if bad=='group_role':
            group=m.Group(name='unrelated'); db.add(group); db.flush()
            db.add(m.GroupGrant(group_id=group.id,role_id=role.id,scope_type='instance',scope_id=instance.id))
        else:
            other=m.User(username='other',password_hash='unused'); db.add(other); db.flush()
            db.add(m.UserGrant(user_id=other.id,role_id=role.id,scope_type='instance',scope_id=instance.id))
    db.commit()
    before=Path(args.config).read_bytes()
    with pytest.raises(ValueError): module.prepare(db,args)
    assert Path(args.config).read_bytes()==before
    assert not instance.is_lab
    assert db.scalar(select(m.User).where(m.User.username=='demo-runner')) is None


@pytest.mark.parametrize('field',['config','password_file','target_config'])
def test_rejects_repository_paths(env,field):
    module,db,args,_,_=env
    setattr(args,field,str(SCRIPT))
    with pytest.raises(ValueError): module.prepare(db,args)


def test_cli_failure_is_sanitized(env,capsys):
    module,_,args,_,_=env
    Path(args.config).write_text('PRIVATE-SECRET-INVALID-JSON')
    code=module.main(['--config',args.config,'--password-file',args.password_file,
              '--target-config',args.target_config,'--instance-id',args.instance_id,
              '--source-id',args.source_id,'--lock-itemid','42','--lock-hostid','17','--accept-isolated-lab'])
    assert code==1
    output=capsys.readouterr()
    assert 'PRIVATE-SECRET' not in output.err and args.config not in output.err
