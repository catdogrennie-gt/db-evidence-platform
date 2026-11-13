"""A running PID alone is not evidence that the expected component owns it."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import json
import pytest

from scripts.platform import runtime_preflight as preflight


def environment():
    row={'pid':10,'parent_pid':1,'executable':'C:/runtime/python.exe',
         'command_sha256':'a'*64,'created_at':'2026-11-13T00:00:00Z'}
    spec={'version':1,'components':[dict(row,name='api',ports=[18180],dependencies=[])]}
    observed={'captured_at':datetime.now(timezone.utc).isoformat(),
              'processes':[row],'listeners':[{'port':18180,'pid':10}]}
    return spec,observed


def test_normal_snapshot_passes_without_mutation_or_private_values_in_output():
    spec,actual=environment();before=deepcopy((spec,actual))
    result=preflight.evaluate(spec,actual)
    assert result=={'ready':True,'components':[{'name':'api','status':'ready','reasons':[]}]}
    assert (spec,actual)==before
    assert 'runtime'not in json.dumps(result) and 'a'*64 not in json.dumps(result)


@pytest.mark.parametrize('field,value',[('parent_pid',99),('created_at','2026-11-13T00:01:00Z'),
    ('command_sha256','b'*64),('executable','C:/other/python.exe')])
def test_pid_reuse_or_identity_drift_is_not_ready(field,value):
    spec,actual=environment();actual['processes'][0][field]=value
    result=preflight.evaluate(spec,actual)
    assert result['ready']is False
    assert result['components'][0]['status']=='identity_changed'


def test_missing_is_not_healthy_and_occupied_port_is_not_adopted():
    spec,actual=environment();actual['processes']=[];actual['listeners']=[]
    assert preflight.evaluate(spec,actual)['components'][0]['status']=='missing'
    actual['listeners']=[{'port':18180,'pid':99}]
    assert preflight.evaluate(spec,actual)['components'][0]['status']=='port_conflict'


def test_duplicate_matching_consumer_is_not_ready():
    spec,actual=environment();other=dict(actual['processes'][0],pid=11,parent_pid=2)
    actual['processes'].append(other)
    assert preflight.evaluate(spec,actual)['components'][0]['status']=='duplicate'


def test_wrong_port_owner_blocks_otherwise_correct_process():
    spec,actual=environment();actual['listeners'][0]['pid']=99
    assert preflight.evaluate(spec,actual)['components'][0]['status']=='port_conflict'


def test_missing_listener_is_distinct_from_running_process():
    spec,actual=environment();actual['listeners']=[]
    assert preflight.evaluate(spec,actual)['components'][0]['status']=='listener_missing'


def test_dependency_failure_propagates_transitively_without_rewriting_root_reason():
    spec,actual=environment()
    for label,dependency,pid in [('db',None,12),('worker','db',13)]:
        row=dict(spec['components'][0],name=label,pid=pid,command_sha256=f'{pid:064x}',ports=[],dependencies=[dependency]if dependency else [])
        spec['components'].append(row)
        if dependency:actual['processes'].append({k:v for k,v in row.items()if k not in ('name','ports','dependencies')})
    spec['components'][0]['dependencies']=['worker']
    result=preflight.evaluate(spec,actual)
    assert [r['status']for r in result['components']]==['dependency_blocked','missing','dependency_blocked']
    assert result['components'][0]['reasons']==['dependency_unavailable']


@pytest.mark.parametrize('seconds',[-61,6])
def test_stale_or_future_snapshot_is_never_current_readiness(seconds):
    spec,actual=environment();now=datetime.now(timezone.utc)
    actual['captured_at']=(now+timedelta(seconds=seconds)).isoformat()
    assert preflight.evaluate(spec,actual,now=now)=={'ready':False,'error':'stale_observation'}


def test_cli_error_never_exposes_private_json_or_paths(tmp_path,capsys):
    path=tmp_path/'PRIVATE-SECRET.json';path.write_text('PRIVATE-SECRET',encoding='utf-8')
    assert preflight.main(['--manifest',str(path),'--observed',str(path)])==2
    text=capsys.readouterr().out
    assert 'PRIVATE-SECRET'not in text
    assert json.loads(text)['ready']is False


@pytest.mark.parametrize('difference',['extra_flag','other_executable'])
def test_same_consumer_scope_is_duplicate_even_with_a_different_command(difference):
    spec,actual=environment()
    spec['components'][0].update(consumer_kind='ai',consumer_scope='c'*64)
    actual['processes'][0].update(consumer_kind='ai',consumer_scope='c'*64)
    extra=dict(actual['processes'][0],pid=11,command_sha256='b'*64)
    if difference=='other_executable':extra['executable']='C:/other/python.exe'
    actual['processes'].append(extra)
    assert preflight.evaluate(spec,actual)['components'][0]['status']=='duplicate'


def test_other_known_schema_consumer_does_not_conflict_but_unknown_scope_blocks():
    spec,actual=environment()
    spec['components'][0].update(consumer_kind='ai',consumer_scope='c'*64)
    actual['processes'][0].update(consumer_kind='ai',consumer_scope='c'*64)
    extra=dict(actual['processes'][0],pid=11,command_sha256='b'*64,consumer_scope='d'*64)
    actual['processes'].append(extra)
    assert preflight.evaluate(spec,actual)['ready']is True
    extra['consumer_scope']=None
    assert preflight.evaluate(spec,actual)['components'][0]['status']=='consumer_scope_unknown'


def test_expected_consumer_requires_a_verifiable_scope():
    spec,actual=environment();spec['components'][0]['consumer_kind']='ai'
    with pytest.raises(ValueError,match='^invalid_preflight_input$'):
        preflight.evaluate(spec,actual)


@pytest.mark.parametrize('change',['empty','duplicate_name','unknown_dependency','cycle','duplicate_port',
    'bad_pid','bad_digest','bad_name','bad_time','version','bad_process','bad_listener'])
def test_invalid_input_fails_with_generic_error_without_echoing_private_input(change):
    spec,actual=environment()
    if change=='empty':spec['components']=[]
    if change=='duplicate_name':spec['components']*=2
    if change=='unknown_dependency':spec['components'][0]['dependencies']=['secret-unknown']
    if change=='cycle':spec['components'][0]['dependencies']=['api']
    if change=='duplicate_port':spec['components'][0]['ports']=[18180,18180]
    if change=='bad_pid':spec['components'][0]['pid']=True
    if change=='bad_digest':spec['components'][0]['command_sha256']='PRIVATE-SECRET'
    if change=='bad_name':spec['components'][0]['name']='PRIVATE-SECRET/COMMAND'
    if change=='bad_time':actual['processes'][0]['created_at']='naive'
    if change=='version':spec['version']=2
    if change=='bad_process':actual['processes'][0]['pid']=False
    if change=='bad_listener':actual['listeners'][0]['port']='PRIVATE-SECRET'
    with pytest.raises(ValueError,match='^invalid_preflight_input$'):
        preflight.evaluate(spec,actual)
