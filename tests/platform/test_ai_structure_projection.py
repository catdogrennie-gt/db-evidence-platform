"""Real lowercase snapshot shape, sanitized names, no production fixture data."""
import json
from types import SimpleNamespace
import pytest
from tests.platform.test_ai_scope import ai_env
from platform_app.tools import project_evidence


def setup_aliases(env):
    key=env['tmp_path']/'projection-test-key'
    key.write_bytes(b'projection-only-test-key')
    env['db'].info['platform_config']=SimpleNamespace(fernet_key_path=key)


def test_lowercase_structure_matches_uppercase_without_raw_text_or_id_rounding(ai_env):
    setup_aliases(ai_env)
    fields={'object_schema':'private_schema','object_name':'private_table','index_name':'private_index','column_name':'private_column','lock_type':'RECORD','lock_mode':'X,REC_NOT_GAP','lock_status':'WAITING','trx_state':'LOCK WAIT','index_type':'BTREE'}
    identifiers={'waiting_trx_id':'18446744073709551615','blocking_trx_id':9007199254740993,'waiting_thread_id':4561,'blocking_thread_id':4510}
    row=ai_env['evidence']
    row.payload={'rows':[dict(fields,**identifiers,raw_text='NEVER-SEND-RAW-TEXT',sql_text='SELECT PRIVATE_LITERAL')]}
    lower=project_evidence(row)
    row.payload={'rows':[dict({k.upper():v for k,v in fields.items()},**identifiers)]}
    upper=project_evidence(row)
    assert lower==upper
    actual=lower['data']['rows'][0]
    for name in ['schema_ref','table_ref','index_ref','column_ref']: assert actual[name].startswith('id_')
    assert actual['LOCK_TYPE']=='RECORD' and actual['LOCK_MODE']=='X,REC_NOT_GAP'
    assert actual['waiting_trx_id']=='18446744073709551615'
    assert actual['blocking_trx_id']=='9007199254740993'
    encoded=json.dumps(lower)
    for secret in ['private_schema','private_table','private_index','private_column','NEVER-SEND-RAW-TEXT','PRIVATE_LITERAL']: assert secret not in encoded


@pytest.mark.parametrize('keys,values,output',[
    (('LOCK_TYPE','lock_type'),('RECORD','TABLE'),'LOCK_TYPE'),
    (('LOCK_MODE','lock_mode'),('X','S'),'LOCK_MODE'),
    (('OBJECT_NAME','object_name'),('first_private_name','second_private_name'),'table_ref'),
    (('OBJECT_SCHEMA','object_schema'),('first_private_schema','second_private_schema'),'schema_ref'),
    (('OBJECT_SCHEMA','schema_name'),('first_private_schema','second_private_schema'),'schema_ref'),
])
def test_conflicting_structure_aliases_are_omitted(ai_env,keys,values,output):
    setup_aliases(ai_env)
    ai_env['evidence'].payload={'rows':[dict(zip(keys,values))]}
    assert output not in project_evidence(ai_env['evidence'])['data']['rows'][0]


def test_equal_case_aliases_are_safe_but_unknown_enum_text_stays_local(ai_env):
    setup_aliases(ai_env)
    ai_env['evidence'].payload={'rows':[{'LOCK_TYPE':'RECORD','lock_type':'RECORD','object_name':'local_name','OBJECT_NAME':'local_name','lock_mode':'private unknown lock text','unknown_field':'private unknown raw text'}]}
    actual=project_evidence(ai_env['evidence'])['data']['rows'][0]
    assert actual['LOCK_TYPE']=='RECORD' and actual['table_ref'].startswith('id_')
    assert 'LOCK_MODE' not in actual
    assert 'private' not in json.dumps(actual) and 'local_name' not in json.dumps(actual)
