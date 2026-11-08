from datetime import timedelta, timezone
from types import SimpleNamespace
import json

from cryptography.fernet import Fernet
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from tests.platform.test_ai_scope import ai_env
from platform_app import models as m
from platform_app.common import iso
from platform_app.db import utcnow


@pytest.fixture
def monitoring_env(ai_env, monkeypatch):
    from platform_app import monitoring
    env = ai_env
    key = env['tmp_path'] / 'master.key'
    key.write_bytes(Fernet.generate_key())
    config = SimpleNamespace(fernet_key_path=key, secrets_dir=env['tmp_path'] / 'secrets')
    env['db'].info['platform_config'] = config
    role = env['db'].scalar(select(m.Role).where(m.Role.name == 'ai-test'))
    role.permissions = [*role.permissions, 'asset.onboard', 'event.read']
    env['grant'].scope_type, env['grant'].scope_id = 'system', env['system'].id
    env['db'].commit()
    class Fake:
        hosts = [{'hostid': '101', 'name': 'mysql-one', 'host': 'mysql-one', 'tags': [{'tag': 'sl.layer', 'value': 'db'}]}]
        items = [
            {'itemid': '1', 'hostid': '101', 'key_': 'sl.mysql.qps', 'value_type': '0', 'units': 'qps', 'status': '0', 'state': '0', 'delay': '30s', 'tags': []},
            {'itemid': '2', 'hostid': '101', 'key_': 'sl.mysql.lock_snapshot', 'value_type': '4', 'units': '', 'status': '0', 'state': '0', 'delay': '30s', 'tags': []},
        ]
        values = [{'clock': str(int(utcnow().replace(tzinfo=timezone.utc).timestamp()) - 60), 'value': '12', 'ns': '0'}]
        def call(self, method, params):
            if method == 'apiinfo.version': return '7.0.0'
            if method == 'host.get': return self.hosts
            if method == 'item.get': return self.items
            if method == 'history.get':
                if params['history'] == 4:
                    return [{'clock': self.values[0]['clock'], 'value': json.dumps({'rows': [{'waiting_trx_id': 1, 'blocking_trx_id': 2}]})}]
                return self.values
            raise AssertionError(method)
    env.update(config=config, fake=Fake())
    monkeypatch.setattr(monitoring, 'ZabbixClient', lambda *args, **kwargs: env['fake'])
    monkeypatch.setattr(monitoring, 'client_for', lambda *args, **kwargs: env['fake'])
    env['source'] = monitoring.save_source(env['actor'], config, dict(name='local', url='http://monitor.invalid/api_jsonrpc.php', system_id=env['system'].id, api_token='PRIVATE-TOKEN'))
    env['db'].commit()
    return env


def test_source_is_encrypted_redacted_and_scoped(monitoring_env):
    from platform_app import monitoring
    e = monitoring_env
    assert 'PRIVATE-TOKEN' not in json.dumps(monitoring.source_response(e['source']))
    assert monitoring.source_response(e['source'])['has_token']
    e['grant'].scope_id = e['b'].id
    e['grant'].scope_type = 'instance'
    e['db'].commit()
    with pytest.raises(HTTPException): monitoring.get_source(e['actor'], e['source'].id)


def test_selection_is_uuid_free_idempotent_and_deselection_preserves(monitoring_env):
    from platform_app import monitoring
    e = monitoring_env
    monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])
    first = monitoring.objects(e['actor'])['items'][0]
    instance = e['db'].get(m.Instance, first['id'])
    assert instance.connection_mode == 'zabbix' and instance.expected_server_uuid is None
    monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])
    assert monitoring.objects(e['actor'])['total'] == 1
    monitoring.select_hosts(e['actor'], e['source'], [], e['config'])
    assert not monitoring.objects(e['actor'])['items'][0]['enabled']
    assert e['db'].get(m.Instance, instance.id) is not None


def test_selected_source_url_cannot_change_and_multi_db_rejected(monitoring_env):
    from platform_app import monitoring
    e = monitoring_env
    monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])
    with pytest.raises(HTTPException):
        monitoring.save_source(e['actor'], e['config'], dict(url='http://other.invalid/api_jsonrpc.php'), e['source'].id)
    e['fake'].items = [dict(e['fake'].items[0], itemid='5', key_='mysql.threads_running[a]'), dict(e['fake'].items[0], itemid='6', key_='mysql.threads_running[b]')]
    with pytest.raises(HTTPException): monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])


def test_history_has_source_times_gaps_and_truthful_capabilities(monitoring_env):
    from platform_app import monitoring
    e = monitoring_env
    monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])
    obj = monitoring.objects(e['actor'])['items'][0]
    upper = utcnow()
    result = monitoring.object_metrics(e['actor'], obj['id'], upper - timedelta(minutes=5), upper, e['config'])
    chart = next(c for c in result['charts'] if c['id'] == 'questions_per_second')
    assert chart['unit'] == 'qps' and any(p['value'] == 12 for p in chart['points'])
    assert any(p['value'] is None for p in chart['points'])
    assert next(c for c in result['capabilities'] if c['name'] == 'error_log')['status'] == 'missing'
    assert all(c['source'] == 'zabbix_history' for c in result['charts'])


def test_zabbix_client_disallows_writes_and_credential_urls():
    from platform_app.zabbix import ZabbixClient, normalize_url
    with pytest.raises(HTTPException): normalize_url('http://token@server/api_jsonrpc.php')
    with pytest.raises(HTTPException): ZabbixClient('http://server/api_jsonrpc.php', 'x').call('host.delete', {})


def test_api_token_rotation_preserves_omitted_group_allowlist(monitoring_env):
    from platform_app.routes.monitoring import SourceInput, update
    e = monitoring_env
    e['source'].allowed_host_group_ids = ['22']; e['db'].commit()
    body = SourceInput(name='local', url=e['source'].url, system_id=e['system'].id, api_token='ROTATED-PRIVATE')
    update(e['source'].id, body, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=e['config']))), e['actor'])
    assert e['source'].allowed_host_group_ids == ['22']


def test_renaming_offline_source_does_not_fabricate_success(monitoring_env):
    from platform_app.monitoring import save_source
    e = monitoring_env
    old_time = utcnow()-timedelta(hours=2)
    e['source'].status='failed'; e['source'].error_code='network_error'; e['source'].last_success_at=old_time
    save_source(e['actor'],e['config'],{'name':'renamed'},e['source'].id)
    assert e['source'].status=='failed' and e['source'].error_code=='network_error'
    assert e['source'].last_success_at==old_time
