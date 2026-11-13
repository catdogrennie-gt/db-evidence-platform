"""Bounded read-only JSON-RPC. Upstream errors and tokens never cross this boundary."""
from contextlib import contextmanager, nullcontext
import json
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
from .common import PlatformError

READ_METHODS = frozenset({'apiinfo.version', 'host.get', 'item.get', 'history.get'})


def normalize_url(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError()
        if parsed.port is not None and not 1 <= parsed.port <= 65535: raise ValueError()
        path = parsed.path.rstrip('/')
        if not path.endswith('/api_jsonrpc.php'): path += '/api_jsonrpc.php'
        return urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))
    except (ValueError, TypeError):
        raise PlatformError(422, 'invalid_monitoring_url', '需要不含凭据、查询参数的 HTTP(S) Zabbix 地址。') from None


class ZabbixClient:
    def __init__(self, url, token, *, deadline_seconds=30):
        self.url, self._token = normalize_url(url), token
        self.deadline = time.monotonic() + deadline_seconds
        self.calls = 0
        self._client = None

    @contextmanager
    def batch(self):
        """Reuse a pool only inside this caller-owned, single-source operation.

        No global pool or credential cache is retained. Budgets belong to this
        ZabbixClient and are not reset by entering another batch.
        """
        if self._client is not None:
            yield self
            return
        with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as client:
            self._client = client
            try:
                yield self
            finally:
                self._client = None

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise PlatformError(503, 'monitoring_budget_exceeded', '监控查询超过时间或调用预算。')
        return remaining

    def call(self, method, params):
        if method not in READ_METHODS:
            raise PlatformError(422, 'monitoring_readonly', '仅允许已注册只读监控接口。')
        self._remaining()
        self.calls += 1
        if self.calls > 48:
            raise PlatformError(503, 'monitoring_budget_exceeded', '监控查询超过时间或调用预算。')
        # Version discovery is anonymous in Zabbix. Keep evidence requests
        # authenticated without retaining either header set on a pooled client.
        headers = {'Content-Type': 'application/json-rpc'}
        if method != 'apiinfo.version':
            headers['Authorization'] = 'Bearer ' + self._token
        try:
            scope = nullcontext(self._client) if self._client is not None else httpx.Client(timeout=8, follow_redirects=False, trust_env=False)
            with scope as client:
                with client.stream('POST', self.url, timeout=min(8, self._remaining()), headers=headers, json={'jsonrpc': '2.0', 'id': self.calls, 'method': method, 'params': params}) as response:
                    if response.status_code != 200: raise ValueError()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        self._remaining()
                        size += len(chunk)
                        if size > 2_000_000: raise ValueError()
                        chunks.append(chunk)
                    data = json.loads(b''.join(chunks))
            if 'error' in data or 'result' not in data: raise ValueError()
            return data['result']
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise PlatformError(503, 'monitoring_upstream_unavailable', 'Zabbix 只读查询失败，请检查地址、授权或上游状态。') from None
