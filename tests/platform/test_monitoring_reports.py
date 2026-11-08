from datetime import datetime, timedelta, timezone
import os
from sqlalchemy import select
import pytest
from tests.platform.test_monitoring import monitoring_env
from tests.platform.test_ai_scope import ai_env
from platform_app.db import utcnow
from platform_app import models as m


def test_external_manifest_freezes_curves_and_authorizes(monitoring_env):
    from platform_app import monitoring
    from platform_app.evidence import create_manifest
    from platform_app.investigations import create_investigation, get_frozen_metric
    e = monitoring_env
    ids = monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])['instance_ids']
    upper = utcnow()
    investigation, task = create_investigation(e['actor'], ids, upper-timedelta(minutes=5), upper, '检查实际证据')
    manifest = create_manifest(e['db'], 'investigation', investigation.id, ids, investigation.window_start, upper)
    investigation.manifest_id = manifest.id
    e['db'].flush()
    item = e['db'].scalar(select(m.ManifestItem).where(m.ManifestItem.manifest_id == manifest.id, m.ManifestItem.item_type == 'time_series_window'))
    assert item is not None
    original = get_frozen_metric(e['actor'], investigation.id, item.id)
    assert any(p['value'] == 12 for p in original['points'])
    assert any(p['value'] is None for p in original['points'])
    e['fake'].values = []
    assert get_frozen_metric(e['actor'], investigation.id, item.id) == original
    e['db'].delete(e['grant']); e['db'].commit()
    from fastapi import HTTPException
    with pytest.raises(HTTPException): get_frozen_metric(e['actor'], investigation.id, item.id)


def test_external_projection_retains_host_and_sql_timer_semantics(monitoring_env):
    from platform_app.tools import project_evidence
    e = monitoring_env['evidence']
    e.source = 'linux.host'
    e.payload = {'values': {'host_cpu_percent': 92, 'host_disk_read_bytes_per_second': 2000}, 'secret': 'DO-NOT-SEND'}
    e.coverage = {'availability': 'available', 'complete': True}
    projected = project_evidence(e)
    assert projected['data']['values']['host_cpu_percent'] == 92
    assert 'DO-NOT-SEND' not in str(projected)
    e.source = 'mysql.statement_digest'
    e.payload = {'rows': [{'DIGEST': 'a'*64, 'COUNT_STAR': 7, 'SUM_TIMER_WAIT': 5000000000000, 'MAX_TIMER_WAIT': 3000000000000}], 'field_units': {'SUM_TIMER_WAIT':'ps','MAX_TIMER_WAIT':'ps'}, 'snapshot_scope': 'server_lifetime'}
    projected = project_evidence(e)
    assert projected['data']['timer_unit'] == 'ps'
    assert projected['data']['snapshot_scope'] == 'server_lifetime'
    assert projected['data']['rows'][0]['SUM_TIMER_WAIT'] == 5000000000000


def test_frozen_projection_does_not_upgrade_truncated_history_to_complete():
    from types import SimpleNamespace
    from platform_app.tools import project_metric_window
    from platform_app.common import iso
    end = utcnow(); start = end - timedelta(minutes=5)
    row = SimpleNamespace(id='item', instance_id='object', item_type='time_series_window', payload_sha256='a'*64, payload={
        'template': 'host_cpu_percent', 'owner_collector': 'host',
        'points': [{'time': iso(start), 'value': 91, 'validity': 'valid'}],
        'coverage': {'complete': False, 'truncated': True},
        'gaps': [{'reason': 'history_truncated'}],
        'provenance': {'transport': 'zabbix', 'itemid': '21'},
    })
    result = project_metric_window(row, start, end)
    assert result['points'][0]['value'] == 91
    assert result['coverage']['complete'] is False
    assert result['coverage']['truncated'] is True
    assert 'history_truncated' in str(result['coverage']['gaps'])
    assert result['provenance']['transport'] == 'zabbix'


@pytest.mark.parametrize('epochs', [(None, None), ('before-restart', 'after-restart')])
def test_digest_delta_requires_observed_counter_epoch(epochs):
    import json
    from platform_app.monitoring import digest_window
    end = utcnow(); start = end - timedelta(minutes=1)
    first = {'rows': [{'DIGEST': 'a'*64, 'COUNT_STAR': 10, 'SUM_TIMER_WAIT': 10_000_000_000_000}]}
    last = {'rows': [{'DIGEST': 'a'*64, 'COUNT_STAR': 100, 'SUM_TIMER_WAIT': 20_000_000_000_000}]}
    for payload, epoch in zip((first, last), epochs):
        if epoch is not None: payload['counter_epoch'] = epoch
    result = digest_window([(start, json.dumps(first)), (end, json.dumps(last))], {'field_units': {'SUM_TIMER_WAIT':'ps'}, 'snapshot_scope':'server_lifetime'})
    assert result['valid'] is False
    assert 'unverified' in result['reason'] or 'epoch' in result['reason']


def test_digest_delta_with_matching_observed_epoch_keeps_real_units():
    import json
    from platform_app.monitoring import digest_window
    end = utcnow(); start = end - timedelta(minutes=1)
    rows = [(start, json.dumps({'counter_epoch': 'actual-observed-epoch', 'rows': [{'DIGEST':'a'*64,'COUNT_STAR':10,'SUM_TIMER_WAIT':10_000_000_000_000}]})),
            (end, json.dumps({'counter_epoch': 'actual-observed-epoch', 'rows': [{'DIGEST':'a'*64,'COUNT_STAR':20,'SUM_TIMER_WAIT':15_000_000_000_000}]}))]
    result = digest_window(rows, {'field_units': {'SUM_TIMER_WAIT':'ps'}, 'snapshot_scope':'server_lifetime'})
    assert result['valid'] is True
    assert result['rows'][0]['count_delta'] == 10
    assert result['rows'][0]['total_time_seconds_delta'] == 5
    assert result['rows'][0]['avg_latency_seconds'] == .5


def test_safe_structure_links_sql_locks_and_indexes_without_names_or_literals(monitoring_env):
    import json
    from platform_app.tools import project_evidence
    row = monitoring_env['evidence']; row.coverage = {'availability':'available', 'complete':True}
    row.source = 'mysql.lock_waits'
    row.payload = {'rows':[{'OBJECT_SCHEMA':'private_hr','OBJECT_NAME':'private_payroll','LOCK_MODE':'X','LOCK_STATUS':'WAITING','TRX_STATE':'LOCK WAIT','waiting_trx_id':7}]}
    lock = project_evidence(row)
    assert lock['data']['rows'][0]['LOCK_MODE'] == 'X'
    assert lock['data']['rows'][0]['LOCK_STATUS'] == 'WAITING'
    assert lock['data']['rows'][0]['TRX_STATE'] == 'LOCK WAIT'
    table_ref = lock['data']['rows'][0]['table_ref']
    row.source = 'mysql.indexes'
    row.payload = {'rows':[{'TABLE_SCHEMA':'private_hr','TABLE_NAME':'private_payroll','COLUMN_NAME':'private_email','INDEX_NAME':'private_lookup','NON_UNIQUE':1,'SEQ_IN_INDEX':1}]}
    index = project_evidence(row)
    assert index['data']['rows'][0]['table_ref'] == table_ref
    row.source = 'mysql.statement_digest'
    row.payload = {'rows':[{'DIGEST':'a'*64,'SCHEMA_NAME':'private_hr','DIGEST_TEXT':"SELECT private_email FROM private_hr.private_payroll WHERE private_email = 'alice@example.invalid' AND account_id = 123456789 /* secret comment */"}]}
    sql = project_evidence(row)
    shape = sql['data']['rows'][0]['sql_shape']
    assert 'SELECT' in shape and 'WHERE' in shape and '=' in shape and '?' in shape
    assert table_ref in shape
    encoded = json.dumps([lock,index,sql])
    for raw in ('private_hr','private_payroll','private_lookup','private_email','alice@example.invalid','123456789','secret comment'):
        assert raw not in encoded


def test_standard_mysql_and_exact_linux_cpu_do_not_require_lab_tags(monitoring_env):
    from platform_app import monitoring
    e = monitoring_env
    e['fake'].hosts = [{'hostid':'101','name':'linux','host':'linux','tags':[]}, {'hostid':'102','name':'mysql','host':'mysql','tags':[]}]
    e['fake'].items = [
        {'itemid':'31','hostid':'101','key_':'system.cpu.util','value_type':'0','status':'0','state':'0','delay':'60s'},
        {'itemid':'32','hostid':'101','key_':'system.cpu.util[,idle]','value_type':'0','status':'0','state':'0','delay':'60s'},
        {'itemid':'33','hostid':'101','key_':'system.cpu.util[,iowait]','value_type':'0','status':'0','state':'0','delay':'60s'},
        {'itemid':'34','hostid':'102','key_':'mysql.threads_running[one-endpoint]','value_type':'3','status':'0','state':'0','delay':'60s'},
    ]
    ids = monitoring.select_hosts(e['actor'], e['source'], ['101','102'], e['config'])['instance_ids']
    objects = {r.name:r for r in e['db'].scalars(select(m.Instance).where(m.Instance.id.in_(ids)))}
    assert objects['linux'].engine == 'host'
    assert objects['mysql'].engine == 'mysql'
    mapping = monitoring.mapping_for(e['db'], objects['linux'].id)
    assert [(r['itemid'],r['metric'],r['unit']) for r in mapping.items] == [('31','host_cpu_percent','%')]
    assert monitoring.normalize_items([e['fake'].items[1],e['fake'].items[2]]) == []


def test_unmapped_source_repoint_does_not_inherit_old_success(monitoring_env):
    from platform_app import monitoring
    e = monitoring_env
    monitoring.save_source(e['actor'],e['config'],{'url':'https://other.invalid'},e['source'].id)
    assert e['source'].status == 'unknown'
    assert e['source'].last_success_at is None


def test_mapping_version_only_changes_when_its_binding_changes(monitoring_env):
    from platform_app import monitoring
    e=monitoring_env
    ids=monitoring.select_hosts(e['actor'],e['source'],['101'],e['config'])['instance_ids']
    mapping=monitoring.mapping_for(e['db'],ids[0]); initial=mapping.version
    monitoring.select_hosts(e['actor'],e['source'],['101'],e['config'])
    assert mapping.version == initial
    e['fake'].items=[dict(item,itemid='replacement') if item['itemid']=='1' else item for item in e['fake'].items]
    monitoring.select_hosts(e['actor'],e['source'],['101'],e['config'])
    assert mapping.version == initial + 1


def test_structure_without_private_alias_key_omits_names_and_states_the_limit(ai_env):
    from platform_app.tools import project_evidence
    row=ai_env['evidence']; row.source='mysql.tables'; row.coverage={'availability':'available'}
    row.payload={'rows':[{'TABLE_NAME':'private_pii','TABLE_ROWS':5}]}
    result=project_evidence(row)
    assert 'private_pii' not in str(result)
    assert 'table_ref' not in result['data']['rows'][0]
    assert result['data']['identifier_policy'] == 'names_omitted_no_alias_key'


def test_malformed_prior_digest_snapshot_remains_unverified():
    from platform_app.monitoring import digest_window
    end=utcnow(); start=end-timedelta(minutes=1)
    result=digest_window([(start,'[]'),(end,'{"counter_epoch":"observed","rows":[]}')],{'field_units':{'SUM_TIMER_WAIT':'ps'},'snapshot_scope':'server_lifetime'})
    assert result['valid'] is False


def test_dependent_total_cpu_uses_observed_master_sampling_interval(monitoring_env):
    from platform_app import monitoring
    e=monitoring_env
    e['fake'].items=[
        {'itemid':'31','hostid':'101','key_':'system.cpu.util','type':'18','master_itemid':'32','delay':'0','value_type':'0'},
        {'itemid':'32','hostid':'101','key_':'system.cpu.util[,idle]','type':'0','master_itemid':'0','delay':'60s','value_type':'0'},
    ]
    ids=monitoring.select_hosts(e['actor'],e['source'],['101'],e['config'])['instance_ids']
    mapping=monitoring.mapping_for(e['db'],ids[0])
    assert len(mapping.items) == 1
    assert mapping.items[0]['interval'] == 60


def test_unknown_dependent_interval_does_not_claim_complete_history(monitoring_env):
    from platform_app import monitoring
    e=monitoring_env
    e['fake'].items=[{'itemid':'31','hostid':'101','key_':'system.cpu.util','type':'18','master_itemid':'missing','delay':'0','value_type':'0'}]
    ids=monitoring.select_hosts(e['actor'],e['source'],['101'],e['config'])['instance_ids']
    end=utcnow()
    result=monitoring.object_metrics(e['actor'],ids[0],end-timedelta(seconds=90),end,e['config'])
    assert result['charts'][0]['coverage']['complete'] is False
    assert 'sample_interval_unverified' in str(result['charts'][0]['gaps'])


@pytest.mark.parametrize('offsets,window_seconds,missing', [
    ([0, 120, 180], 240, [(60, 120)]),
    ([0, 60, 120], 240, [(180, 240)]),
    ([120, 180], 240, [(0, 120)]),
    ([], 240, [(0, 240)]),
    ([0], 180, [(60, 180)]),
    ([0], 60, []),
    ([0, 60, 120, 180], 240, []),
    ([0, 89, 149], 209, []),
])
def test_history_gap_uses_real_sample_times_and_half_open_boundaries(monitoring_env, offsets, window_seconds, missing):
    from platform_app import monitoring
    from platform_app.common import iso
    e = monitoring_env
    e['fake'].items = [dict(e['fake'].items[0], delay='60s')]
    start = datetime(2026, 1, 1)
    clock = int(start.replace(tzinfo=timezone.utc).timestamp())
    e['fake'].values = [{'clock': str(clock + offset), 'value': '0', 'ns': '0'} for offset in offsets]
    identifier = monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])['instance_ids'][0]
    chart = monitoring.object_metrics(e['actor'], identifier, start, start + timedelta(seconds=window_seconds), e['config'])['charts'][0]
    assert chart['coverage']['complete'] is (not missing)
    assert chart['gaps'] == [{'from': iso(start + timedelta(seconds=left)), 'to': iso(start + timedelta(seconds=right)), 'reason': 'missing_samples'} for left, right in missing]
    assert [point['time'] for point in chart['points'] if point['validity'] == 'unknown_gap'] == [iso(start + timedelta(seconds=left)) for left, _ in missing]
    assert [point['value'] for point in chart['points'] if point['validity'] == 'valid'] == [0.0] * len(offsets)


@pytest.mark.parametrize('raw', [None, 'NaN', 'not-a-number'])
def test_invalid_history_sample_stays_null_and_makes_coverage_incomplete(monitoring_env, raw):
    from platform_app import monitoring
    from platform_app.common import iso
    e = monitoring_env
    e['fake'].items = [dict(e['fake'].items[0], delay='60s')]
    start = datetime(2026, 1, 1)
    clock = int(start.replace(tzinfo=timezone.utc).timestamp())
    e['fake'].values = [{'clock': str(clock + offset), 'value': value, 'ns': '0'} for offset, value in [(0, '0'), (60, raw), (120, '3')]]
    identifier = monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])['instance_ids'][0]
    chart = monitoring.object_metrics(e['actor'], identifier, start, start + timedelta(seconds=180), e['config'])['charts'][0]
    assert [point['value'] for point in chart['points']] == [0.0, None, 3.0]
    assert chart['points'][1]['validity'] == 'invalid_value'
    assert chart['coverage']['complete'] is False
    assert chart['gaps'] == [{'from': iso(start + timedelta(seconds=60)), 'to': iso(start + timedelta(seconds=60)), 'reason': 'invalid_value'}]


@pytest.mark.parametrize('snapshot_index', [0, 1, 2])
@pytest.mark.parametrize('bad_row', [None, 'malformed', {}])
def test_malformed_digest_row_does_not_crash_or_verify_other_rows(snapshot_index, bad_row):
    import json
    from platform_app.monitoring import digest_window
    start = datetime(2026, 1, 1)
    snapshots = [{'counter_epoch': 'observed', 'rows': [{'DIGEST': 'a'*64, 'COUNT_STAR': count, 'SUM_TIMER_WAIT': count * 1_000_000_000_000}]} for count in (10, 15, 20)]
    snapshots[snapshot_index]['rows'].append(bad_row)
    rows = [(start + timedelta(seconds=offset), json.dumps(snapshot)) for offset, snapshot in zip((0, 30, 60), snapshots)]
    result = digest_window(rows, {'field_units': {'SUM_TIMER_WAIT': 'ps'}, 'snapshot_scope': 'server_lifetime'})
    assert result['valid'] is False
    assert result['rows'] == []
    assert result['reason'] == 'invalid_digest_rows'


@pytest.mark.parametrize('layer,engine', [('host', 'host'), ('net', 'network')])
def test_mapping_layer_refresh_preserves_identity_and_replaces_collectors(monitoring_env, layer, engine):
    from platform_app import monitoring
    e = monitoring_env
    identifier = monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])['instance_ids'][0]
    instance = e['db'].get(m.Instance, identifier)
    revision = instance.config_revision
    e['fake'].hosts = [dict(e['fake'].hosts[0], tags=[{'tag': 'sl.layer', 'value': layer}])]
    e['fake'].items = [{'hostid': '101', 'itemid': 'cpu', 'key_': 'system.cpu.util', 'delay': '60s', 'value_type': '0'}]
    selected = monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])
    assert selected['instance_ids'] == [identifier]
    assert instance.engine == engine
    assert instance.config_revision == revision + 1
    mapping = monitoring.mapping_for(e['db'], identifier)
    assert mapping.layer == layer and mapping.selected is True
    schedules = {row.collector: row for row in e['db'].scalars(select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id == identifier))}
    assert {owner for owner, row in schedules.items() if row.enabled} == {'host'}
    assert all(row.config_revision == instance.config_revision for row in schedules.values())


def test_same_layer_collector_removal_and_return_refresh_schedules(monitoring_env):
    from platform_app import monitoring
    e = monitoring_env
    identifier = monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])['instance_ids'][0]
    instance = e['db'].get(m.Instance, identifier)
    revision = instance.config_revision
    all_items = e['fake'].items
    e['fake'].items = all_items[:1]
    monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])
    schedules = {row.collector: row for row in e['db'].scalars(select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id == identifier))}
    assert {owner for owner, row in schedules.items() if row.enabled} == {'identity_status'}
    assert instance.engine == 'mysql' and instance.config_revision == revision + 1
    assert all(row.config_revision == instance.config_revision for row in schedules.values())
    runtime_schedule_id = schedules['runtime'].id
    e['fake'].items = all_items
    monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])
    assert schedules['runtime'].id == runtime_schedule_id and schedules['runtime'].enabled is True
    assert instance.config_revision == revision + 2
    assert all(row.config_revision == instance.config_revision for row in schedules.values())


def test_unchanged_mapping_refresh_is_idempotent_even_if_upstream_item_order_changes(monitoring_env):
    from platform_app import monitoring
    e = monitoring_env
    identifier = monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])['instance_ids'][0]
    instance = e['db'].get(m.Instance, identifier)
    mapping = monitoring.mapping_for(e['db'], identifier)
    revision, version = instance.config_revision, mapping.version
    schedules = list(e['db'].scalars(select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id == identifier)))
    original = {row.id: (row.collector, row.enabled, row.config_revision, row.next_due_at) for row in schedules}
    monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])
    e['fake'].items = list(reversed(e['fake'].items))
    monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])
    assert instance.config_revision == revision
    assert mapping.version == version
    assert {row.id: (row.collector, row.enabled, row.config_revision, row.next_due_at) for row in e['db'].scalars(select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id == identifier))} == original


@pytest.mark.parametrize('refresh_during_read', [False, True])
def test_obsolete_external_collection_cannot_write_after_mapping_refresh(monitoring_env, monkeypatch, refresh_during_read):
    from platform_app import monitoring
    from platform_app.common import PlatformError
    from platform_app.scheduling import enqueue, claim
    e = monitoring_env
    identifier = monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])['instance_ids'][0]
    instance = e['db'].get(m.Instance, identifier)
    for old in e['db'].scalars(select(m.Task)):
        old.status = 'succeeded'
    task = enqueue(e['db'], 'collect_instance', instance_id=identifier, payload={'config_revision': instance.config_revision, 'collectors': ['identity_status']})
    e['db'].commit()
    lease = claim(e['factory'], 'obsolete-monitoring-worker', 'collection')
    assert lease and lease.id == task.id
    original_call = e['fake'].call

    def refresh():
        e['fake'].items = [dict(e['fake'].items[0], itemid='replacement-qps')]
        monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])
        e['db'].commit()

    if refresh_during_read:
        def call(method, params):
            if method == 'history.get': refresh()
            return original_call(method, params)
        monkeypatch.setattr(e['fake'], 'call', call)
    else:
        refresh()
        def call(method, params):
            if method == 'history.get': raise AssertionError('Obsolete task read the upstream')
            return original_call(method, params)
        monkeypatch.setattr(e['fake'], 'call', call)
    with pytest.raises(PlatformError) as raised:
        monitoring.handle_collection(e['factory'], lease, e['config'])
    assert raised.value.detail['code'] == 'configuration_changed'
    e['db'].expire_all()
    assert not list(e['db'].scalars(select(m.Evidence).where(m.Evidence.instance_id == identifier)))


def test_worker_rejects_obsolete_monitoring_task_without_poisoning_source(monitoring_env, monkeypatch):
    from platform_app import monitoring, worker
    from platform_app.scheduling import enqueue, claim
    e = monitoring_env
    identifier = monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])['instance_ids'][0]
    instance = e['db'].get(m.Instance, identifier)
    for old in e['db'].scalars(select(m.Task)): old.status = 'succeeded'
    task = enqueue(e['db'], 'collect_instance', instance_id=identifier, payload={'config_revision': instance.config_revision, 'collectors': ['identity_status']})
    e['db'].commit()
    lease = claim(e['factory'], 'obsolete-wrapper-worker', 'collection')
    assert lease and lease.id == task.id
    original_success = e['source'].last_success_at
    e['fake'].items = [dict(e['fake'].items[0], itemid='replacement-qps')]
    monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])
    e['db'].commit()
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('Obsolete task opened the upstream client')
    monkeypatch.setattr(monitoring, 'client_for', forbidden)
    assert worker.execute(e['factory'], lease, e['config'])['error_code'] == 'configuration_changed'
    assert calls == []
    e['db'].expire_all()
    assert e['source'].status == 'ready' and e['source'].error_code is None
    assert e['source'].last_success_at == original_success


@pytest.mark.parametrize('failure,source_failed', [
    ('monitoring_upstream_unavailable', True),
    ('monitoring_credential_unavailable', True),
    ('secret_unavailable', True),
    ('task_cancelled', False),
    ('stale_task_fence', False),
    ('task_deadline_exceeded', False),
    ('monitoring_budget_exceeded', False),
    ('internal_parser_error', False),
])
def test_worker_only_marks_actual_monitoring_retrieval_failures(monitoring_env, monkeypatch, failure, source_failed):
    from platform_app import monitoring, worker
    from platform_app.common import PlatformError
    from platform_app.secrets import SecretUnavailable
    from platform_app.scheduling import enqueue, claim
    e = monitoring_env
    identifier = monitoring.select_hosts(e['actor'], e['source'], ['101'], e['config'])['instance_ids'][0]
    instance = e['db'].get(m.Instance, identifier)
    for old in e['db'].scalars(select(m.Task)): old.status = 'succeeded'
    task = enqueue(e['db'], 'collect_instance', instance_id=identifier, payload={'config_revision': instance.config_revision, 'collectors': ['identity_status']})
    e['db'].commit()
    lease = claim(e['factory'], 'source-health-worker', 'collection')
    assert lease and lease.id == task.id
    def fail_collection(*args, **kwargs):
        if failure == 'secret_unavailable': raise SecretUnavailable('Test unavailable key')
        if failure == 'internal_parser_error': raise TypeError('Test parser failure')
        raise PlatformError(503, failure, 'Test bounded failure')
    monkeypatch.setattr(monitoring, 'handle_collection', fail_collection)
    result = worker.execute(e['factory'], lease, e['config'])
    assert result['status'] == 'failed'
    e['db'].expire_all()
    assert e['source'].status == ('failed' if source_failed else 'ready')
    assert e['source'].error_code == ('monitoring_collection_failed' if source_failed else None)


@pytest.mark.skipif(os.environ.get('DBA_PLATFORM_MYSQL_TEST') != '1', reason='requires real private MySQL; SQLite cannot prove row-lock order')
def test_real_mysql_selection_wait_does_not_block_collector_mapping_update(monkeypatch):
    """Synthetic discovery, real row locks; concurrent updates both roll back."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, get_ident
    from types import SimpleNamespace
    from uuid import uuid4
    from sqlalchemy import event, delete, update
    from platform_app import monitoring
    from platform_app.config import load_config
    from platform_app.db import create_db_engine, create_session_factory
    engine = create_db_engine(load_config())
    factory = create_session_factory(engine)
    label = 'selection-lock-test-' + uuid4().hex[:8]
    objects = []
    entering_instance_lock = Event()
    refresh_thread = []
    item = monitoring.normalize_items([{'itemid': '1', 'key_': 'sl.mysql.qps', 'delay': '60s', 'value_type': '0'}])[0]
    try:
        with factory.begin() as db:
            department = m.Department(name=label); db.add(department); db.flush()
            system = m.System(name=label, department_id=department.id); db.add(system); db.flush()
            instance = m.Instance(name=label, system_id=system.id, endpoint='zabbix:validation', connection_mode='zabbix', enabled=False, lifecycle_state='Paused'); db.add(instance); db.flush()
            source = m.MonitoringSource(name=label, url='http://monitor.invalid/api_jsonrpc.php', system_id=system.id, secret_ref=str(uuid4())); db.add(source); db.flush()
            mapping = m.MonitoringMapping(source_id=source.id, instance_id=instance.id, hostid='validation', selected=False, items=[item]); db.add(mapping); db.flush()
            schedule = m.CollectionSchedule(instance_id=instance.id, collector='identity_status', enabled=False); db.add(schedule); db.flush()
            instance_id, source_id = instance.id, source.id
            objects = [(m.CollectionSchedule, schedule.id), (m.MonitoringMapping, mapping.id), (m.Instance, instance.id), (m.MonitoringSource, source.id), (m.System, system.id), (m.Department, department.id)]
        monkeypatch.setattr(monitoring, 'authorize_system', lambda *args: None)
        monkeypatch.setattr(monitoring, 'discover', lambda *args: {'truncated': False, 'items': [{'hostid': 'validation', 'name': label, 'layer': 'db', 'items': [item], 'capabilities': monitoring.capabilities([item]), 'tags': {}, 'binding_error': None}]})
        @event.listens_for(engine, 'before_cursor_execute')
        def observe(connection, cursor, statement, parameters, context, many):
            if refresh_thread and get_ident() == refresh_thread[0] and 'FROM instances' in statement and 'FOR UPDATE' in statement:
                entering_instance_lock.set()
        def refresh():
            refresh_thread.append(get_ident())
            with factory() as db:
                try:
                    return monitoring.select_hosts(SimpleNamespace(db=db), db.get(m.MonitoringSource, source_id), ['validation'], None)
                finally: db.rollback()
        with factory() as collector, ThreadPoolExecutor(max_workers=1) as pool:
            collector.scalar(select(m.Instance).where(m.Instance.id == instance_id).with_for_update())
            future = pool.submit(refresh)
            try:
                assert entering_instance_lock.wait(8), 'Selection never attempted its instance lock'
                collector.execute(update(m.MonitoringMapping).where(m.MonitoringMapping.instance_id == instance_id).values(last_observed_at=utcnow()))
            finally: collector.rollback()
            assert future.result(timeout=8)['selected'] == 1
    finally:
        with factory.begin() as db:
            for model, identifier in objects: db.execute(delete(model).where(model.id == identifier))
        engine.dispose()
