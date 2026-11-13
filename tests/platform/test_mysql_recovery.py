from copy import deepcopy
from datetime import datetime,timezone,timedelta
import importlib.util
import json
from pathlib import Path

import pytest

path=Path(__file__).resolve().parents[2]/'scripts/platform/mysql_recovery.py'
spec=importlib.util.spec_from_file_location('mysql_recovery',path)
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
NOW=datetime(2026,11,13,12,tzinfo=timezone.utc)
UUID='11111111-2222-4333-8444-555555555555'

def fixture():
    contract={'version':1,'executable':r'D:\mysql\bin\mysqld.exe','executable_sha256':'a'*64,
              'defaults_file':r'D:\private\my.ini','defaults_sha256':'b'*64,
              'datadir':r'D:\private\data','server_uuid':UUID,'host':'127.0.0.1','port':13306}
    observed={'captured_at':NOW.isoformat(),'files':{
        'executable_sha256':'a'*64,'defaults_sha256':'b'*64,'datadir':contract['datadir'],
        'datadir_uuid':UUID,'data_present':True,'defaults_datadir':contract['datadir'],
        'defaults_port':13306,'defaults_bind_address':'127.0.0.1'},'processes':[],'listeners':[],'probe':None}
    return contract,observed

def present():
    c,o=fixture()
    o['processes']=[{'pid':12,'executable':c['executable'],'defaults_file':c['defaults_file'],
                     'datadir':c['datadir'],'port':13306,'command_valid':True}]
    o['listeners']=[{'pid':12,'host':'127.0.0.1','port':13306}]
    o['probe']={'ok':True,'pid':12,'server_uuid':UUID,'port':13306,'datadir':c['datadir']}
    return c,o

def check(c,o):return mod.evaluate(c,o,now=NOW)

def test_legal_missing_requires_start_but_does_not_execute():
    c,o=fixture();assert check(c,o)=={'status':'start_required','reasons':['registered_service_missing']}

def test_running_healthy_is_noop():
    c,o=present();assert check(c,o)=={'status':'present_ready','reasons':[]}

@pytest.mark.parametrize('change',[
    lambda o:o.update(probe=None),
    lambda o:o.update(probe={'ok':False}),
    lambda o:o.update(listeners=[],probe=None),
])
def test_present_but_starting_or_unavailable_never_requests_second_start(change):
    c,o=present();change(o);assert check(c,o)['status']=='present_not_ready'

@pytest.mark.parametrize('field,value',[
    ('executable_sha256','c'*64),('defaults_sha256','c'*64),
    ('datadir',r'D:\elsewhere'),('defaults_datadir',r'D:\elsewhere'),
    ('datadir_uuid','aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'),
    ('data_present',False),('defaults_port',3306),('defaults_bind_address','0.0.0.0'),
])
def test_changed_storage_or_configuration_refuses_start_even_when_missing(field,value):
    c,o=fixture();o['files'][field]=value;assert check(c,o)['status']=='refused'

@pytest.mark.parametrize('field,value',[
    ('executable',r'D:\other\mysqld.exe'),('defaults_file',r'D:\other\my.ini'),
    ('port',3306),('command_valid',False),('datadir',None),
])
def test_unknown_or_changed_process_never_adopted(field,value):
    c,o=present();o['processes'][0][field]=value;assert check(c,o)['status']=='refused'

def test_same_datadir_two_processes_refused():
    c,o=present();other=deepcopy(o['processes'][0]);other['pid']=13;o['processes'].append(other)
    assert check(c,o)['status']=='refused'

def test_port_owned_by_other_process_refused():
    c,o=fixture();o['listeners']=[{'pid':99,'host':'127.0.0.1','port':13306}]
    assert check(c,o)['status']=='refused'

@pytest.mark.parametrize('field,value',[
    ('pid',99),('server_uuid','aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'),
    ('port',3306),('datadir',r'D:\elsewhere'),
])
def test_live_server_mismatch_refused(field,value):
    c,o=present();o['probe'][field]=value;assert check(c,o)['status']=='refused'

def test_known_other_datadir_process_is_left_alone():
    c,o=fixture();o['processes']=[{'pid':88,'executable':r'D:\other\mysqld.exe',
      'defaults_file':r'D:\other\my.ini','datadir':r'D:\other\data','port':3307,'command_valid':True}]
    o['listeners']=[{'pid':88,'host':'127.0.0.1','port':3307}]
    assert check(c,o)['status']=='start_required'

@pytest.mark.parametrize('offset',[-61,6])
def test_stale_or_future_snapshot_refused(offset):
    c,o=fixture();o['captured_at']=(NOW+timedelta(seconds=offset)).isoformat()
    assert check(c,o)=={'status':'refused','reasons':['stale_observation']}

@pytest.mark.parametrize('change',[
    lambda c,o:c.update(port=True), lambda c,o:c.update(host='example.com'),
    lambda c,o:c.update(datadir=r'..\relative'),lambda c,o:c.update(defaults_file=r'\\server\share\my.ini'),
    lambda c,o:o.update(processes='secret'),lambda c,o:o['files'].update(data_present=1),
])
def test_malformed_input_sanitized(change):
    c,o=fixture();change(c,o)
    assert check(c,o)=={'status':'refused','reasons':['invalid_recovery_input']}

def test_output_has_no_private_values():
    c,o=present();o['files']['datadir_uuid']='wrong';result=json.dumps(check(c,o))
    for value in [UUID,c['datadir'],c['defaults_file'],'wrong']:assert value not in result

def test_equivalent_windows_case_and_slashes():
    c,o=present();o['processes'][0]['datadir']='d:/PRIVATE/data'
    assert check(c,o)['status']=='present_ready'

@pytest.mark.parametrize('port',[135,445])
def test_unrelated_system_listeners_do_not_block_healthy_mysql(port):
    c,o=present();o['listeners'].append({'pid':99,'host':'0.0.0.0','port':port})
    assert check(c,o)['status']=='present_ready'

def test_other_mysql_privileged_port_not_adopted_or_blocked():
    c,o=fixture();o['processes']=[{'pid':88,'executable':r'D:\other\mysqld.exe',
      'defaults_file':r'D:\other\my.ini','datadir':r'D:\other\data','port':330,'command_valid':True}]
    assert check(c,o)['status']=='start_required'

def test_cli_unreadable_file_outputs_only_reason(tmp_path,capsys):
    assert mod.main(['--contract',str(tmp_path/'secret-name'), '--observed',str(tmp_path/'other')])==1
    result=capsys.readouterr().out
    assert 'secret-name'not in result
    assert json.loads(result)=={'status':'refused','reasons':['invalid_or_unreadable_recovery_input']}
