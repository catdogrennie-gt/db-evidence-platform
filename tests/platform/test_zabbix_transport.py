"""Collection-scoped pooling must retain the read-only transport boundaries."""
from contextlib import contextmanager
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace

import httpx
import pytest

from platform_app import zabbix
from platform_app.common import PlatformError
from tests.platform.test_analysis_groups import group_env
from tests.platform.test_monitoring import monitoring_env
from tests.platform.test_ai_scope import ai_env


@pytest.fixture
def transport(monkeypatch):
    original = httpx.Client
    state = SimpleNamespace(clients=[], requests=[], handler=None)

    def handle(request):
        state.requests.append(request)
        if state.handler:
            return state.handler(request)
        body = json.loads(request.content)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': []})

    def create(**kwargs):
        client = original(transport=httpx.MockTransport(handle), **kwargs)
        state.clients.append(client)
        return client

    monkeypatch.setattr(zabbix.httpx, 'Client', create)
    return state


def test_explicit_batch_reuses_one_client_then_closes_and_discards_it(transport):
    client = zabbix.ZabbixClient('http://monitor.invalid', 'one')
    with client.batch():
        client.call('host.get', {})
        client.call('item.get', {})
        assert len(transport.clients) == 1 and not transport.clients[0].is_closed
    assert transport.clients[0].is_closed
    with client.batch():
        client.call('history.get', {})
    assert len(transport.clients) == 2 and all(c.is_closed for c in transport.clients)
    assert client.calls == 3


def test_batch_closes_on_caller_exception_and_does_not_leak_source_tokens(transport):
    one = zabbix.ZabbixClient('http://monitor.invalid', 'first-source')
    with pytest.raises(RuntimeError):
        with one.batch():
            one.call('host.get', {})
            raise RuntimeError('caller abort')
    two = zabbix.ZabbixClient('http://monitor.invalid', 'second-source')
    with two.batch():
        two.call('item.get', {})
    assert [r.headers['authorization'] for r in transport.requests] == [
        'Bearer first-source', 'Bearer second-source']
    assert len(transport.clients) == 2 and all(c.is_closed for c in transport.clients)


def test_unscoped_calls_still_close_after_each_request(transport):
    client = zabbix.ZabbixClient('http://monitor.invalid', 'one')
    client.call('host.get', {})
    client.call('item.get', {})
    assert len(transport.clients) == 2 and all(c.is_closed for c in transport.clients)


def test_each_pooled_request_uses_remaining_deadline_not_initial_timeout(transport, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(zabbix, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    client = zabbix.ZabbixClient('http://monitor.invalid', 'one', deadline_seconds=10)
    with client.batch():
        client.call('host.get', {})
        clock[0] = 107.0
        client.call('item.get', {})
        clock[0] = 111.0
        with pytest.raises(PlatformError) as error:
            client.call('history.get', {})
    assert error.value.detail['code'] == 'monitoring_budget_exceeded'
    assert [r.extensions['timeout']['read'] for r in transport.requests] == [8, 3]


def test_batch_does_not_reset_48_call_budget_or_allow_write_methods(transport):
    client = zabbix.ZabbixClient('http://monitor.invalid', 'one')
    with client.batch():
        with pytest.raises(PlatformError) as error:
            client.call('host.delete', {})
        assert error.value.detail['code'] == 'monitoring_readonly'
        for _ in range(48):
            client.call('history.get', {})
    with client.batch():
        with pytest.raises(PlatformError) as error:
            client.call('history.get', {})
    assert error.value.detail['code'] == 'monitoring_budget_exceeded'
    assert len(transport.requests) == 48


def test_stream_chunks_cannot_extend_collection_deadline(transport, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(zabbix, 'time', SimpleNamespace(monotonic=lambda: clock[0]))

    class SlowBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"result":'
            clock[0] = 111.0
            yield b'[]}'

    transport.handler = lambda request: httpx.Response(200, stream=SlowBody())
    client = zabbix.ZabbixClient('http://monitor.invalid', 'one', deadline_seconds=10)
    with pytest.raises(PlatformError) as error:
        with client.batch():
            client.call('history.get', {})
    assert error.value.detail['code'] == 'monitoring_budget_exceeded'
    assert transport.clients[0].is_closed


def test_oversized_stream_is_rejected_and_batch_closes(transport):
    class Oversized(httpx.SyncByteStream):
        def __iter__(self):
            yield b'x' * 1_000_000
            yield b'x' * 1_000_001

    transport.handler = lambda request: httpx.Response(200, stream=Oversized())
    client = zabbix.ZabbixClient('http://monitor.invalid', 'private-token')
    with pytest.raises(PlatformError) as error:
        with client.batch():
            client.call('history.get', {})
    assert error.value.detail['code'] == 'monitoring_upstream_unavailable'
    assert 'private-token' not in str(error.value.detail)
    assert transport.clients[0].is_closed


def test_real_http_keepalive_is_scoped_to_one_batch():
    peers = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            peers.append(self.client_address)
            data = json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': []}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = zabbix.ZabbixClient(f'http://127.0.0.1:{server.server_port}', 'test-token')
        with client.batch():
            for method in ('host.get', 'item.get', 'history.get'):
                assert client.call(method, {}) == []
        client.call('history.get', {})
        assert len(peers) == 4 and len(set(peers[:3])) == 1
        assert peers[-1] != peers[0]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('metadata_fails', [False, True])
def test_collection_opens_one_batch_for_metadata_and_all_history(group_env, monkeypatch, metadata_fails):
    from platform_app.analysis_groups import save_group
    from platform_app.monitoring import handle_collection
    from platform_app.scheduling import claim, enqueue

    e = group_env
    group = save_group(e['actor'], e['config'], e['body'])
    original = e['fake'].call
    state = {'active': False, 'opened': 0, 'closed': 0}

    @contextmanager
    def batch():
        assert not state['active']
        state['active'] = True
        state['opened'] += 1
        try:
            yield e['fake']
        finally:
            state['active'] = False
            state['closed'] += 1

    def call(method, params):
        assert state['active'], 'metadata/history read escaped collection batch'
        if metadata_fails:
            raise PlatformError(503, 'monitoring_upstream_unavailable', 'test upstream failure')
        return original(method, params)

    monkeypatch.setattr(e['fake'], 'batch', batch, raising=False)
    monkeypatch.setattr(e['fake'], 'call', call)
    enqueue(e['db'], 'collect_instance', instance_id=group['instance_id'],
            payload={'config_revision': group['config_revision'], 'collectors': ['monitoring']})
    e['db'].commit()
    lease = claim(e['factory'], 'batch-test', 'collection')
    if metadata_fails:
        with pytest.raises(PlatformError):
            handle_collection(e['factory'], lease, e['config'])
    else:
        handle_collection(e['factory'], lease, e['config'])
    assert state == {'active': False, 'opened': 1, 'closed': 1}
