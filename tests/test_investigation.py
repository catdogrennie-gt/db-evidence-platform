import pytest
import investigation as inv


@pytest.mark.parametrize('name,args', [('run_sql', {'sql':'DROP TABLE orders'}), ('describe_table', {'table':'orders; DROP DATABASE evidence_lab'}), ('get_status', {'sql':'SELECT 1'}), ('describe_table', {'table':'orders','extra':True})])
def test_invalid_tool_never_connects(monkeypatch,name,args):
    def forbidden(*a,**kw):raise AssertionError('invalid request reached database')
    monkeypatch.setattr(inv.lab,'connection',forbidden)
    with pytest.raises(ValueError):inv.execute_read_tool(name,args)


def test_report_must_reference_observed_evidence():
    valid={'summary':'test','unknowns':[],'findings':[{'title':'t','reasoning':'r','next_readonly_check':'check','evidence_ids':['E999']}]}
    with pytest.raises(ValueError):inv.validate_answer(valid,{'E1'})
    valid['findings'][0]['evidence_ids']=['E1']
    inv.validate_answer(valid,{'E1'})


def test_model_timeout_preserves_evidence_without_a_success_report(monkeypatch,tmp_path):
    monkeypatch.setattr(inv.lab,'RUNTIME',tmp_path)
    key=tmp_path/'test.key';key.write_text('synthetic-test-key')
    monkeypatch.setattr(inv.lab,'config',lambda:{'model':'test','key_path':str(key)})
    snap=inv.lab.store('snapshot',{'status':'complete','evidence':[{'id':'E1','value':'fixture'}],'errors':[]})
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def post(self,*a,**kw):raise inv.httpx.ReadTimeout('synthetic timeout')
    monkeypatch.setattr(inv.httpx,'Client',Client)
    result=inv.investigate(snap['id'],'test')['data']
    assert result['status']=='incomplete' and result['error_type']=='ReadTimeout'
    assert 'answer' not in result and inv.lab.records('analysis')==[]
    assert inv.lab.get_record(result['evidence_snapshot_id'])['data']['evidence']==snap['data']['evidence']


def test_duplicate_tool_call_is_audited_without_reexecution(monkeypatch,tmp_path):
    import json
    from types import SimpleNamespace
    monkeypatch.setattr(inv.lab,'RUNTIME',tmp_path)
    key=tmp_path/'test.key';key.write_text('synthetic-test-key')
    monkeypatch.setattr(inv.lab,'config',lambda:{'model':'test','key_path':str(key)})
    snap=inv.lab.store('snapshot',{'status':'complete','evidence':[{'id':'E1','value':'fixture'},{'id':'E3','value':'sparse fixture'}],'errors':[]})
    executed=[]
    def execute(name,args):
        executed.append(name)
        return {'source':'test','value':[],'mode':'synthetic_fixture'}
    monkeypatch.setattr(inv,'execute_read_tool',execute)
    messages=[{'content':None,'tool_calls':[{'id':str(i),'type':'function','function':{'name':'get_status','arguments':'{}'}} for i in range(2)]},
              {'content':json.dumps({'summary':'test','findings':[],'unknowns':['fixture only']})}]
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def post(self,*a,**kw):
            payload={'choices':[{'message':messages.pop(0)}]}
            return SimpleNamespace(raise_for_status=lambda:None,json=lambda:payload)
    monkeypatch.setattr(inv.httpx,'Client',Client)
    report=inv.investigate(snap['id'],'test')['data']
    assert executed==['get_status']
    assert [x['data']['status'] for x in report['tool_calls']]==['complete','rejected_or_failed']
    assert report['status']=='complete'
    assert inv.lab.get_record(report['evidence_snapshot_id'])['data']['status']=='partial'
    assert [x['id'] for x in inv.lab.get_record(report['evidence_snapshot_id'])['data']['evidence']]==['E1','E3','E4']
    assert [inv.lab.get_record(i)['data']['status'] for i in report['model_responses']]==['tool_requests_received','validated']


def test_invalid_final_response_is_retained_for_review(monkeypatch,tmp_path):
    import json
    from types import SimpleNamespace
    monkeypatch.setattr(inv.lab,'RUNTIME',tmp_path)
    key=tmp_path/'test.key';key.write_text('synthetic-test-key')
    monkeypatch.setattr(inv.lab,'config',lambda:{'model':'test','key_path':str(key)})
    snap=inv.lab.store('snapshot',{'status':'complete','evidence':[{'id':'E1','value':'fixture'}],'errors':[]})
    answer={'summary':'fixture','findings':[{'title':'unsupported','reasoning':'fixture','next_readonly_check':'check','evidence_ids':['E999']}],'unknowns':[]}
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def post(self,*a,**kw):
            return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'choices':[{'message':{'content':json.dumps(answer)}}]})
    monkeypatch.setattr(inv.httpx,'Client',Client)
    report=inv.investigate(snap['id'],'test')['data']
    assert report['status']=='incomplete' and not inv.lab.records('analysis')
    assert inv.lab.get_record(report['model_responses'][0])['data']['status']=='rejected'
