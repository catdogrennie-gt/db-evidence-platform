import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from fastapi.testclient import TestClient
from app import app
from lab import validate_targets
import lab
from contextlib import contextmanager

def test_empty_database_result_has_consistent_array_type():
    class Cursor:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,*args):pass
        def fetchall(self):return ()  # PyMySQL returns an empty tuple for zero rows.
    class Database:
        def cursor(self):return Cursor()
    assert lab.query(Database(),'SELECT fixture')==[]

def test_restore_refuses_source_uuid():
    with pytest.raises(ValueError):validate_targets('same','same')
    with pytest.raises(ValueError):validate_targets('', 'target')
    validate_targets('source','target')
def test_cross_origin_operations_rejected():
    client=TestClient(app)
    assert client.post('/api/collect',headers={'Origin':'https://untrusted.example'}).status_code==403
def test_rebinding_host_rejected():
    client=TestClient(app)
    assert client.get('/api/state',headers={'Host':'untrusted.example:8765'}).status_code==403

def test_collection_failure_is_not_healthy(monkeypatch,tmp_path):
    @contextmanager
    def offline(*args,**kwargs):
        raise OSError('synthetic offline test')
        yield
    monkeypatch.setattr(lab,'RUNTIME',tmp_path)
    monkeypatch.setattr(lab,'connection',offline)
    monkeypatch.setattr(lab,'ssh',offline)
    result=lab.collect()['data']
    assert result['status']=='unavailable'
    assert result['evidence']==[]
    assert {e['source'] for e in result['errors']}=={'mysql.connection','linux.host'}

def test_model_cannot_cite_invented_evidence(monkeypatch,tmp_path):
    monkeypatch.setattr(lab,'RUNTIME',tmp_path)
    key=tmp_path/'test.key';key.write_text('synthetic-test-key')
    monkeypatch.setattr(lab,'config',lambda:{'key_path':str(key),'model':'test'})
    snapshot=lab.store('snapshot',{'evidence':[{'id':'E1','value':'synthetic'}]})
    class FakeClient:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,*args,**kwargs):
            import httpx,json
            answer={'summary':'test','findings':[{'title':'test','reasoning':'test','next_readonly_check':'test','evidence_ids':['E999']}],'unknowns':[]}
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(answer)}}]},request=httpx.Request('POST','https://example.invalid'))
    monkeypatch.setattr(lab.httpx,'Client',FakeClient)
    with pytest.raises(ValueError,match='nonexistent evidence'):lab.analyze(snapshot['id'])
    assert lab.records('analysis')==[]
    assert len(lab.records('model_response'))==2
    assert all(x['data']['status']=='rejected' for x in lab.records('model_response'))

def test_one_format_correction_is_audited(monkeypatch,tmp_path):
    import httpx,json
    monkeypatch.setattr(lab,'RUNTIME',tmp_path)
    key=tmp_path/'test.key';key.write_text('synthetic-test-key')
    monkeypatch.setattr(lab,'config',lambda:{'key_path':str(key),'model':'test'})
    snap=lab.store('snapshot',{'evidence':[{'id':'E1','value':'synthetic'}]})
    answers=[{'summary':'fixture','findings':[{'title':'unknown','reasoning':'missing','next_readonly_check':'collect','evidence_ids':[]}],'unknowns':[]},
             {'summary':'fixture','findings':[],'unknowns':['missing evidence']}]
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def post(self,*a,**kw):return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(answers.pop(0))}}]},request=httpx.Request('POST','https://example.invalid'))
    monkeypatch.setattr(lab.httpx,'Client',Client)
    report=lab.analyze(snap['id'])['data']
    assert [a['data']['status'] for a in report['attempts']]==['rejected','validated']
    assert report['answer']['unknowns']==['missing evidence'] and not answers
