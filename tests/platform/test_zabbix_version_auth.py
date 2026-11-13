"""The version method is anonymous; monitored evidence remains token-scoped."""
from contextlib import nullcontext
import json
import httpx
import pytest
from platform_app.zabbix import ZabbixClient
from tests.platform.test_zabbix_transport import transport


@pytest.mark.parametrize('pooled',[False,True])
def test_anonymous_version_probe_and_authorized_history_share_no_auth_state(transport,pooled):
    def endpoint(request):
        body=json.loads(request.content)
        if body['method']=='apiinfo.version':
            if 'authorization'in request.headers:
                return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],
                    'error':{'code':-32602,'message':'Invalid params.',
                             'data':'apiinfo.version requires an anonymous request'}})
            return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':'7.0.32'})
        if request.headers.get('authorization')!='Bearer source-private-token':
            return httpx.Response(200,json={'error':{'code':-32602,'message':'Not authorized'}})
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':[]})
    transport.handler=endpoint
    client=ZabbixClient('http://monitor.invalid','source-private-token')
    with client.batch()if pooled else nullcontext():
        assert client.call('apiinfo.version',[])=='7.0.32'
        assert client.call('host.get',{})==[]
        assert client.call('apiinfo.version',[])=='7.0.32'
        assert client.call('history.get',{'itemids':['1'],'history':3})==[]
    assert [r.headers.get('authorization')for r in transport.requests]==[
        None,'Bearer source-private-token',None,'Bearer source-private-token']
    assert client.calls==4
    assert len(transport.clients)==(1 if pooled else 4)
    assert all(c.is_closed for c in transport.clients)
