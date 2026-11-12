"""Public product inspection must not grant management or leak another scope."""
from datetime import timedelta
import importlib
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from platform_app import models as m
from platform_app.access import actor_for_user
from platform_app.db import Base, create_db_engine, create_session_factory, utcnow
from platform_app.identity import create_session


@pytest.fixture
def readonly_env(tmp_path):
    engine = create_db_engine('sqlite://')
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    db = factory()
    dep = m.Department(name='Demo department')
    guest = m.User(username='guest', password_hash='unused')
    owner = m.User(username='private-owner@example.invalid', password_hash='unused')
    role = m.Role(name='test-readonly', permissions=['data.read', 'event.read', 'knowledge.read', 'configuration.read'])
    db.add_all([dep, guest, owner, role]); db.flush()
    demo = m.System(name='Demo business', department_id=dep.id)
    private = m.System(name='PRIVATE BUSINESS', department_id=dep.id)
    db.add_all([demo, private]); db.flush()
    a = m.Instance(name='Orders', system_id=demo.id, endpoint='private-endpoint', enabled=True)
    b = m.Instance(name='PRIVATE OBJECT', system_id=private.id, endpoint='private-endpoint')
    db.add_all([a, b]); db.flush()
    grant = m.UserGrant(user_id=guest.id, role_id=role.id, scope_type='system', scope_id=demo.id)
    source = m.MonitoringSource(name='Demo Zabbix', system_id=demo.id, url='https://private-host.invalid', secret_ref='DO-NOT-EXPOSE-SECRET-REF')
    hidden = m.MonitoringSource(name='PRIVATE SOURCE', system_id=private.id, url='https://private.invalid', secret_ref='PRIVATE-CREDENTIAL')
    db.add_all([grant, source, hidden]); db.flush()
    item = dict(hostid='101', itemid='201', host_name='Orders DB', name='Request latency', key='app.latency[password=DO-NOT-EXPOSE]', units='ms', value_type=0, value_kind='numeric', tags=[{'tag':'owner','value':'personal@example.invalid'}], description='PRIVATE DESCRIPTION')
    mapping = m.MonitoringMapping(source_id=source.id, instance_id=a.id, layer='group', hostid='group:demo', items=[item])
    policy = m.InspectionPolicy(owner_id=owner.id, enabled=True, instance_ids=[a.id], mode='rules_only')
    task = m.Task(type='investigation', actor_id=owner.id, instance_id=a.id, payload={'password':'PRIVATE PAYLOAD'}, result={'key':'PRIVATE RESULT'}, error_message='PRIVATE ERROR')
    hidden_task = m.Task(type='collection', actor_id=owner.id, instance_id=b.id)
    mixed = m.Task(type='investigation', actor_id=owner.id, instance_id=a.id, payload={'instance_ids':[a.id,b.id]})
    db.add_all([mapping, policy, task, hidden_task, mixed]); db.flush()
    db.add(m.Rule(name='Latency rule', rule_version='demo-1', template='sql_latency', parameters={'instance_ids':[a.id], 'threshold':2, 'password':'PRIVATE RULE'}))
    db.add(m.Rule(name='PRIVATE RULE NAME', rule_version='private-1', template='sql_latency', parameters={'instance_ids':[b.id]}))
    db.add(m.PlatformSetting(key='model_profile:default', value={'name':'Default', 'credential_ref':'PRIVATE MODEL REF', 'base_url':'https://private.invalid'}))
    db.commit()
    config = SimpleNamespace(model={'provider':'DeepSeek','model':'example-model','key_path':str(tmp_path/'PRIVATE.key'),'max_calls':4,'max_tokens':3000}, public_origin='http://127.0.0.1:18080')
    app = FastAPI()
    app.state.session_factory = factory
    app.state.config = config
    for name in ['monitoring', 'intelligence', 'tasks', 'admin']:
        app.include_router(importlib.import_module('platform_app.routes.'+name).router, prefix='/api/v1')
    app.include_router(importlib.import_module('platform_app.routes.readonly').router, prefix='/api/v1')
    client = TestClient(app, base_url=config.public_origin)
    token, csrf, _ = create_session(db, guest)
    client.cookies.set('dba_session', token, path='/api/v1')
    headers = {'Origin':config.public_origin,'X-CSRF-Token':csrf}
    yield dict(db=db, client=client, headers=headers, actor=actor_for_user(db,guest.id), guest=guest, role=role, grant=grant, a=a, b=b, demo=demo, private=private, source=source, hidden=hidden, policy=policy, task=task, mixed=mixed)
    client.close(); db.close(); engine.dispose()


def test_readonly_overview_exposes_actual_service_policy_and_scoped_config(readonly_env):
    e=readonly_env
    r=e['client'].get('/api/v1/readonly/overview')
    assert r.status_code==200, r.text
    data=r.json()
    assert data['access']['read_only'] is True
    assert [s['id'] for s in data['sources']]==[e['source'].id]
    assert [p['id'] for p in data['policies']]==[e['policy'].id]
    assert [t['id'] for t in data['tasks']]==[e['task'].id]
    assert data['model']['provider']=='DeepSeek'
    encoded=json.dumps(data)
    for forbidden in ['PRIVATE', 'DO-NOT-EXPOSE', 'personal@example.invalid', 'private-owner', 'private-host', 'private-endpoint', 'key_path', 'secret_ref', 'credential_ref', 'error_message']:
        assert forbidden not in encoded, forbidden


def test_readonly_scopes_rechecked_for_filter_and_revocation(readonly_env):
    e=readonly_env; client=e['client']
    assert client.get('/api/v1/readonly/overview',params={'instance_id':e['b'].id}).status_code==404
    assert client.get('/api/v1/readonly/overview',params={'system_id':e['private'].id}).json()['sources']==[]
    e['role'].permissions=['data.read','event.read']; e['db'].commit()
    assert client.get('/api/v1/readonly/overview').status_code==403


def test_readonly_catalog_post_is_selected_metadata_only_and_does_not_queue(readonly_env):
    e=readonly_env; db=e['db']
    before=db.scalar(select(func.count()).select_from(m.Task))
    r=e['client'].post(f"/api/v1/readonly/sources/{e['source'].id}/items/query",json={'search':'latency','limit':20},headers=e['headers'])
    assert r.status_code==200,r.text
    data=r.json()
    assert data['total']==1 and data['items'][0]['itemid']=='201'
    assert data['catalog_scope']=='selected_items'
    assert 'DO-NOT-EXPOSE' not in r.text and 'personal@example.invalid' not in r.text and 'PRIVATE DESCRIPTION' not in r.text
    assert e['client'].post(f"/api/v1/readonly/sources/{e['hidden'].id}/items/query",json={},headers=e['headers']).status_code==404
    assert e['client'].post(f"/api/v1/readonly/sources/{e['source'].id}/items/query",json={}).status_code==403
    assert db.scalar(select(func.count()).select_from(m.Task))==before


def test_valid_csrf_cannot_turn_configuration_read_into_any_write(readonly_env):
    e=readonly_env; c=e['client']; h=e['headers']; db=e['db']
    requests=[
        ('post','/monitoring/sources',{'name':'x','url':'https://x.invalid','system_id':e['demo'].id,'api_token':'A-PRIVATE-TOKEN'}),
        ('put',f"/monitoring/sources/{e['source'].id}",{'name':'x','url':'https://x.invalid','system_id':e['demo'].id}),
        ('put','/intelligence/policy',{'enabled':False}),
        ('post','/intelligence/policy/run',{}),
        ('post',f"/tasks/{e['task'].id}/cancel",{}),
        ('post','/model-profiles',{}),
        ('patch','/settings/model-policies',{'scope_type':'system','scope_id':e['demo'].id,'mode':'disabled','reason':'test'}),
    ]
    before=db.scalar(select(func.count()).select_from(m.Task))
    for method,path,body in requests:
        r=getattr(c,method)('/api/v1'+path,json=body,headers=h)
        assert r.status_code in (403,404),(path,r.status_code,r.text)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(m.Task))==before
    assert db.get(m.InspectionPolicy,e['policy'].id).enabled
    assert db.get(m.MonitoringSource,e['source'].id).name=='Demo Zabbix'


def test_configuration_read_without_business_read_cannot_see_configuration(readonly_env):
    e=readonly_env
    e['role'].permissions=['configuration.read']; e['db'].commit()
    assert e['client'].get('/api/v1/readonly/overview').status_code==403


def test_task_projection_checks_linked_report_scope_even_without_input_reference(readonly_env):
    e=readonly_env; db=e['db']
    inv=m.Investigation(created_by=e['guest'].id, question='private', task_id=e['task'].id,
                        window_start=utcnow()-timedelta(minutes=5), window_end=utcnow(), permission_version=1)
    db.add(inv); db.flush()
    db.add_all([m.InvestigationResource(investigation_id=inv.id, instance_id=i.id) for i in [e['a'],e['b']]])
    db.commit()
    assert e['task'].id not in [r['id'] for r in e['client'].get('/api/v1/readonly/overview').json()['tasks']]


def test_configuration_grant_does_not_expand_business_evidence_scope(readonly_env):
    e=readonly_env; db=e['db']
    # The broad control grant cannot widen the narrower independent data grant.
    e['grant'].scope_type='global'; e['grant'].scope_id='*'
    e['role'].permissions=['configuration.read']
    business=m.Role(name='narrow-business-reader',permissions=['data.read','event.read'])
    db.add(business); db.flush()
    db.add(m.UserGrant(user_id=e['guest'].id,role_id=business.id,scope_type='instance',scope_id=e['a'].id)); db.commit()
    data=e['client'].get('/api/v1/readonly/overview').json()
    assert [r['id'] for r in data['objects']]==[e['a'].id]
    assert 'PRIVATE BUSINESS' not in json.dumps(data)
    assert e['client'].get('/api/v1/readonly/overview',params={'instance_id':e['b'].id}).status_code==404


def test_system_scoped_demo_sees_new_business_objects_without_new_grants(readonly_env):
    e=readonly_env; db=e['db']
    assert e['client'].get('/api/v1/readonly/overview').json()['access']['object_count']==1
    new=m.Instance(name='Inventory database',system_id=e['demo'].id,endpoint='private-new-target',enabled=True)
    db.add(new); db.flush()
    db.add(m.MonitoringMapping(source_id=e['source'].id,instance_id=new.id,layer='group',hostid='group:inventory',items=[dict(hostid='102',itemid='202',name='Inventory QPS',key='mysql.queries.rate',host_name='Inventory DB',value_type=0,value_kind='numeric',units='qps')]))
    db.commit()
    data=e['client'].get('/api/v1/readonly/overview').json()
    assert {r['id'] for r in data['objects']}=={e['a'].id,new.id}
    assert data['sources'][0]['group_count']==2
    assert data['access']['object_count']==2
    assert e['b'].id not in [r['id'] for r in data['objects']]


def test_future_worker_heartbeat_is_unknown_and_never_reveals_worker_identity(readonly_env):
    e=readonly_env
    e['db'].add(m.WorkerHeartbeat(worker_id='private-owner@example.invalid',queue='ai',last_seen_at=utcnow()+timedelta(hours=2),details={'password':'PRIVATE HEARTBEAT'}))
    e['db'].commit()
    r=e['client'].get('/api/v1/readonly/overview')
    assert r.json()['platform']['workers']==[{'queue':'ai','status':'unknown'}]
    assert 'private-owner' not in r.text and 'PRIVATE HEARTBEAT' not in r.text
