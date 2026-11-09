import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest


def config_file(tmp_path, **overrides):
    value = {
        'ssh': {'host': '192.0.2.2', 'username': 'lab', 'known_hosts': 'private'},
        'profile': {'mode': 'ssh', 'db_host': '172.18.0.2', 'db_port': 3306, 'tls_mode': 'disabled', 'ssh_profile_ref': 'fixed-demo', 'expected_server_uuid': '11111111-1111-1111-1111-111111111111'},
        'scenario': {'username': 'demo_scenario', 'password': 'PRIVATE TEST VALUE'},
        'monitor': {'username': 'monitor', 'password': 'PRIVATE TEST VALUE'},
        'expected_marker': {'marker': 'signallens-lab-v1', 'container': 'signallens-lab-target', 'owner': 'signallens-lab', 'role': 'target', 'database': 'lab_orders'},
        'baseline_sha256': 'a'*64,
        **overrides,
    }
    path=tmp_path/'target.json'; path.write_text(json.dumps(value))
    return SimpleNamespace(demo={'enabled': True, 'target_config_path': str(path), 'max_duration_seconds': 180})


def test_target_file_is_private_absolute_and_has_pinned_identity(tmp_path):
    from platform_app.demo_fault import target_config, DemoFaultError
    with pytest.raises(DemoFaultError): target_config(SimpleNamespace(demo={'target_config_path': 'relative.json'}))
    cfg=config_file(tmp_path)
    assert target_config(cfg)['profile']['mode']=='ssh'
    cfg=config_file(tmp_path, profile={'mode':'direct','db_host':'8.8.8.8'})
    with pytest.raises(DemoFaultError): target_config(cfg)


def test_container_guard_accepts_only_fixed_target_and_pinned_network_address(tmp_path):
    from platform_app.demo_fault import validate_container, target_config, DemoFaultError
    cfg=target_config(config_file(tmp_path))
    actual={'Name':'/signallens-lab-target','State':{'Running':True},'Config':{'Labels':{'sl.owner':'signallens-lab','sl.role':'target','com.docker.compose.project':'signallens-lab'}},'HostConfig':{'NetworkMode':'signallens-lab_lab'},'NetworkSettings':{'Networks':{'signallens-lab_lab':{'IPAddress':'172.18.0.2'},'signallens-internal':{'IPAddress':'172.30.88.20'}},'Ports':{'3306/tcp':None}}}
    validate_container(actual,cfg)
    actual['NetworkSettings']['Networks']['signallens-lab_lab']['IPAddress']='172.18.0.3'
    with pytest.raises(DemoFaultError): validate_container(actual,cfg)
    actual['NetworkSettings']['Networks']['signallens-lab_lab']['IPAddress']='172.18.0.2'
    actual['NetworkSettings']['Ports']['3306/tcp']=[{'HostPort':'3306'}]
    with pytest.raises(DemoFaultError): validate_container(actual,cfg)


def test_stop_rolls_back_both_connections_even_if_first_rollback_fails(tmp_path, monkeypatch):
    from platform_app import demo_fault
    events=[]
    class Conn:
        def __init__(self,name): self.name=name
        def rollback(self):
            events.append(self.name+':rollback')
            if self.name=='holder': raise OSError('private secret must not surface')
        def close(self): events.append(self.name+':close')
    fault=demo_fault.FixedLabFault(config_file(tmp_path))
    fault._holder=Conn('holder'); fault._waiter=Conn('waiter')
    monkeypatch.setattr(demo_fault,'verify_idle',lambda config:{'sessions_closed':True,'locks_cleared':True,'dataset_unchanged':True})
    result=fault.stop()
    assert all(x in events for x in ['holder:rollback','holder:close','waiter:rollback','waiter:close'])
    assert result['sessions_closed']
    assert 'secret' not in str(result)


def test_fixed_sql_never_commits_or_accepts_user_sql():
    from platform_app.demo_fault import FAULT_SQL
    assert FAULT_SQL=='UPDATE lab_orders.orders SET amount_cents=amount_cents+1 WHERE id=1'


def test_expired_deadline_does_not_open_target_connections(tmp_path,monkeypatch):
    from platform_app import demo_fault
    from platform_app.demo_fault import FixedLabFault, DemoFaultError
    calls=[]
    def forbidden(*args,**kwargs):
        calls.append(True)
        raise AssertionError('Expired start attempted target connection')
    monkeypatch.setattr(demo_fault,'_container_guard',forbidden)
    monkeypatch.setattr(demo_fault,'verify_idle',forbidden)
    monkeypatch.setattr(demo_fault,'_connect',forbidden)
    fault=FixedLabFault(config_file(tmp_path), expires_in_seconds=0)
    with pytest.raises(DemoFaultError): fault.start()
    assert calls==[]


def test_slow_setup_recomputes_idle_bound_with_transport_and_row_wait_reserve():
    from platform_app.demo_fault import holder_idle_seconds,DemoFaultError
    # 30s consumed setting up connections/identity must not be added to TTL.
    assert holder_idle_seconds(180-30)==130
    # Even all bounded remaining transport and row wait (16s) stays below deadline.
    assert holder_idle_seconds(150)+16<150
    with pytest.raises(DemoFaultError): holder_idle_seconds(22)


def test_absolute_expiry_overrides_stale_relative_budget(tmp_path,monkeypatch):
    from datetime import timedelta
    from platform_app import demo_fault
    calls=[]
    def forbidden(*args,**kwargs):
        calls.append(True)
        raise AssertionError('Expired start attempted transport')
    monkeypatch.setattr(demo_fault,'_container_guard',forbidden)
    fault=demo_fault.FixedLabFault(config_file(tmp_path),expires_in_seconds=180,expires_at=demo_fault.utcnow()-timedelta(seconds=1))
    with pytest.raises(demo_fault.DemoFaultError): fault.start()
    assert calls==[]
