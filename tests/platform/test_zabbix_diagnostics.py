"""Fixed Zabbix SQL packs become typed evidence, never arbitrary raw model text."""
import json

import pytest

from platform_app import zabbix_diagnostics as d


def item(query='configuration'):
    return {'key':f'mysql.custom.query[DbA,,,{query}]','tags':[
        {'tag':'dbe.contract','value':'mysql-diagnostics-v1'},
        {'tag':'dbe.query','value':query}], 'hostid':'101','itemid':'900'}


def snapshot(query='configuration',rows=None):
    rows=[] if rows is None else rows
    return {'producer_contract':'db-evidence-mysql-diagnostics-v1','query_name':query,
            'rows':rows,'snapshot_complete':True,'snapshot_truncated':False,
            'returned_row_count':len(rows)}


@pytest.mark.parametrize('query,source',[
    ('configuration','mysql.configuration'),('transactions','mysql.transactions'),
    ('sessions','mysql.sessions'),('table_io','mysql.table_io'),('file_io','mysql.file_io'),
    ('statements','mysql.statement_statistics'),('error_log','mysql.error_log'),
    ('slow_log','mysql.slow_log.table'),('locks','mysql.lock_waits'),
    ('indexes','mysql.indexes'),('tables','mysql.tables')])
def test_only_named_fixed_query_with_matching_contract_is_typed(query,source):
    assert d.definition(item(query))['source']==source
    for changed in [dict(item(query),key='app.metrics'),dict(item(query),tags=[]),
                    dict(item(query),key=f'mysql.custom.query[DbA,,,arbitrary]')]:
        assert d.definition(changed) is None


def test_conflicting_tags_or_nonempty_query_arguments_are_not_a_contract():
    bad=item();bad['tags'].append({'tag':'dbe.query','value':'sessions'})
    assert d.definition(bad) is None
    assert d.definition(dict(item(),key='mysql.custom.query[DbA,,,configuration,extra]')) is None
    assert d.definition(dict(item(),key='mysql.custom.query[DbA,user,password,configuration]')) is None


@pytest.mark.parametrize('change',[
    {'query_name':'sessions'}, {'producer_contract':'other'}, {'rows':'not a list'},
    {'rows':[None]}, {'snapshot_complete':'true'}, {'snapshot_truncated':0},
    {'returned_row_count':True}, {'returned_row_count':5},
    {'snapshot_complete':False,'snapshot_truncated':False}])
def test_invalid_pack_never_upgrades_missing_or_malformed_observation(change):
    with pytest.raises(ValueError):d.normalize(item(),dict(snapshot(),**change))


def test_top_n_is_explicit_and_never_becomes_interval_delta():
    pack=snapshot('file_io',[{'COUNT_READ':9}]*20)
    pack.update(snapshot_complete=False,snapshot_truncated=True,returned_row_count=21)
    result=d.normalize(item('file_io'),pack)
    assert result['truncated'] is True
    assert result['coverage']['snapshot_complete'] is False
    assert result['payload']['snapshot_scope']=='server_cumulative_top_n'
    assert result['payload']['counter_epoch'] is None
    assert result['payload']['window']['valid'] is False


def test_error_and_slow_log_metadata_keep_event_time_without_literal_sql_or_message():
    error=d.normalize(item('error_log'),snapshot('error_log',[
        {'LOGGED':'2026-11-13T00:59:00.123456Z','PRIO':'Error','ERROR_CODE':'MY-010000',
         'DATA':'DO-NOT-SEND password=secret'}]))
    event=error['payload']['events'][0]
    assert event['server_event_at']=='2026-11-13T00:59:00.123456Z'
    assert event['severity']=='Error'
    assert event['error_code']=='MY-010000'
    assert 'DATA' not in str(event)
    slow=d.normalize(item('slow_log'),snapshot('slow_log',[
        {'start_time':'2026-11-13T00:59:00.123456Z','query_time_seconds':'0.25',
         'rows_examined':'500','sql_text':"SELECT 'private@example.com'"}]))
    assert slow['payload']['events'][0]['query_time_seconds']=='0.25'
    assert 'private@example.com' not in str(slow)


def test_log_epoch_overrides_local_display_time_and_naive_time_is_rejected():
    from datetime import datetime,timezone
    epoch=datetime(2026,11,13,0,59,0,tzinfo=timezone.utc).timestamp()
    log=d.normalize(item('error_log'),snapshot('error_log',[
        {'LOGGED':'2026-11-13 08:59:00','event_epoch':epoch,'PRIO':'Note'}]))
    assert log['payload']['events'][0]['server_event_at']=='2026-11-13T00:59:00.000000Z'
    with pytest.raises(ValueError):d.normalize(item('error_log'),snapshot('error_log',[{'LOGGED':'2026-11-13 08:59:00'}]))


def test_application_log_is_typed_without_leaking_exception_text_or_losing_restart_gap():
    meta={'key':'app.diagnostics.events','tags':[{'tag':'dbe.contract','value':'application-events-v1'}]}
    pack=snapshot('application_logs',[{'server_event_at':'2026-11-13T00:59:00Z',
        'event_id':'a'*32+':1','event_kind':'request_failed','severity':'error',
        'error_code':'mysql_1205','request_ms':600,'message':'DO-NOT-SEND private SQL'}])
    pack.update(producer_contract=d.APP_CONTRACT,capture_gaps=['process_started_inside_window'])
    result=d.normalize(meta,pack)
    assert result['source']=='application.events'
    assert result['coverage']['capture_gaps']==['process_started_inside_window']
    assert result['payload']['events'][0]['error_code']=='mysql_1205'
    assert 'DO-NOT-SEND' not in str(result)


def test_new_sources_expose_allowlisted_numbers_and_no_arbitrary_strings():
    from platform_app.tools import project_evidence
    from platform_app.models import Evidence
    row=Evidence(instance_id='one',source='mysql.table_io',observed_at=None,
        payload={'rows':[{'COUNT_READ':100,'SUM_TIMER_READ':1e12,'OBJECT_NAME':'secret-name',
                          'password':'secret','message':'private@example.com'}],
                 'snapshot_scope':'server_cumulative_top_n','counter_epoch':None},
        coverage={'availability':'available'},id='x',sensitivity='restricted',source_mode='live',truncated=False)
    result=project_evidence(row)
    assert result['data']['rows'][0]['COUNT_READ']==100
    assert result['data']['timer_unit']=='ps'
    assert result['data']['counter_epoch_known'] is False
    assert 'secret-name' not in json.dumps(result)
    assert 'private@example.com' not in json.dumps(result)


def test_diagnostic_log_projection_cannot_include_events_outside_frozen_window():
    from datetime import datetime
    from platform_app.models import Evidence
    from platform_app.tools import project_evidence
    normalized=d.normalize(item('error_log'),snapshot('error_log',[
        {'LOGGED':stamp,'PRIO':'Error','ERROR_CODE':'MY-010000'} for stamp in
        ['2026-11-13T00:58:00Z','2026-11-13T00:59:00Z','2026-11-13T01:00:00Z']]))
    row=Evidence(id='x',instance_id='one',source=normalized['source'],
        payload=normalized['payload'],coverage={'availability':'available'},
        source_mode='live',truncated=False)
    result=project_evidence(row,window_start=datetime(2026,11,13,0,59),window_end=datetime(2026,11,13,1))
    assert result['data']['event_count']==1
    assert len(result['data']['events'])==1
    assert result['data']['events'][0]['event_time']=='2026-11-13T00:59:00.000000Z'
    assert result['data']['events_outside_requested_window_or_invalid']==2


def test_application_summary_partial_overlap_is_not_clipped_into_false_counts():
    from datetime import datetime
    from platform_app.models import Evidence
    from platform_app.tools import project_evidence
    meta={'key':'app.diagnostics.events','tags':[{'tag':'dbe.contract','value':'application-events-v1'}]}
    pack=snapshot('application_logs',[{'server_event_at':'2026-11-13T01:00:00Z',
        'event_id':'a'*32+':1','event_kind':'request_window_summary','severity':'info',
        'interval_start_at':'2026-11-13T00:50:00Z','interval_end_at':'2026-11-13T01:00:00Z',
        'request_count':100,'error_count':1,'window_seconds':600}])
    pack['producer_contract']=d.APP_CONTRACT
    normalized=d.normalize(meta,pack)
    row=Evidence(id='x',instance_id='one',source=normalized['source'],payload=normalized['payload'],
        coverage={'availability':'available'},source_mode='live',truncated=False)
    result=project_evidence(row,window_start=datetime(2026,11,13,0,55),window_end=datetime(2026,11,13,1,1))
    assert result['data']['event_count']==0 and result['data']['events']==[]
    assert result['data']['events_outside_requested_window_or_invalid']==1


def test_file_io_has_safe_category_identity_without_exposing_paths():
    from platform_app.models import Evidence
    from platform_app.tools import project_evidence
    row=Evidence(id='x',instance_id='one',source='mysql.file_io',payload={'rows':[
        {'EVENT_NAME':'wait/io/file/innodb/innodb_log_file','COUNT_WRITE':3,'FILE_NAME':'/private/secret'}]},
        coverage={'availability':'available'},source_mode='live',truncated=False)
    result=project_evidence(row)
    assert result['data']['rows'][0]['io_category']=='redo'
    assert '/private/secret' not in str(result)
