"""External monitoring integrated with existing assets, evidence and collection leases."""
from datetime import datetime, timedelta, timezone
import json
import math
import re

from sqlalchemy import select
from . import models as m
from .access import authorize, authorize_system, allowed_scope_ids, scoped_instance
from .common import PlatformError, fields, iso
from .db import utcnow
from .secrets import SecretStore
from .zabbix import ZabbixClient, normalize_url

# key -> public metric, logical owner, unit. No display-name inference.
KEYS = {
    'sl.mysql.qps': ('questions_per_second', 'identity_status', 'qps'),
    'sl.mysql.threads_running': ('threads_running', 'identity_status', ''),
    'sl.mysql.lock_waits': ('lock_waits', 'runtime', ''),
    'sl.host.cpu_pct': ('host_cpu_percent', 'host', '%'),
    'sl.host.disk_read_bps': ('host_disk_read_bytes_per_second', 'host', 'Bps'),
    'sl.host.disk_write_bps': ('host_disk_write_bytes_per_second', 'host', 'Bps'),
    'sl.net.mysql_latency_ms': ('probe_latency_ms', 'host', 'ms'),
    'sl.net.mysql_available': ('probe_availability', 'host', ''),
    'system.cpu.util': ('host_cpu_percent', 'host', '%'),
    'mysql.threads_running': ('threads_running', 'identity_status', ''),
    'mysql.threads_connected': ('connections', 'identity_status', ''),
    'mysql.queries.rate': ('questions_per_second', 'identity_status', 'qps'),
    'mysql.innodb_row_lock_current_waits': ('lock_waits', 'runtime', ''),
}
SNAPSHOTS = {'sl.mysql.lock_snapshot': ('runtime', 'mysql.lock_waits'), 'sl.mysql.sql_summary': ('statement_digest', 'mysql.statement_digest')}
CAPABILITIES = ('metrics', 'runtime', 'statement_digest', 'host', 'error_log', 'slow_log', 'replication', 'schema_config')


def client_for(source, config):
    token = SecretStore(config).resolve(source.secret_ref, purpose='zabbix').value
    if not isinstance(token, str): raise PlatformError(503, 'monitoring_credential_unavailable', '监控凭据不可用。')
    return ZabbixClient(source.url, token)


def source_response(source):
    return {**fields(source, ['id', 'name', 'url', 'system_id', 'status', 'last_success_at', 'error_code']), 'has_token': bool(source.secret_ref)}


def get_source(actor, identifier):
    source = actor.db.get(m.MonitoringSource, identifier)
    if source is None: raise PlatformError(404, 'source_not_found', '监控来源不存在。')
    authorize_system(actor, source.system_id, 'asset.onboard')
    return source


def save_source(actor, config, body, identifier=None):
    old = get_source(actor, identifier) if identifier else None
    scope_changed = old is not None and 'allowed_host_group_ids' in body and set(body['allowed_host_group_ids']) != set(old.allowed_host_group_ids or [])
    system_id = body.get('system_id', old.system_id if old else None)
    authorize_system(actor, system_id, 'asset.onboard')
    url = normalize_url(body.get('url', old.url if old else ''))
    upstream_changed = old is not None and url != old.url
    mapped = old and actor.db.scalar(select(m.MonitoringMapping.id).where(m.MonitoringMapping.source_id == old.id).limit(1))
    if mapped and (url != old.url or system_id != old.system_id):
        raise PlatformError(409, 'monitoring_identity_locked', '已映射来源不可更换上游地址或所属系统，请新建来源。')
    token = body.get('api_token')
    if not old and not token: raise PlatformError(422, 'token_required', '新来源需要只读 API token。')
    if token:
        # Test before saving a secret; a malformed source must not become usable.
        probe = ZabbixClient(url, token)
        probe.call('host.get', {'output': ['hostid'], 'limit': 1})
        secret = SecretStore(config).put_secret(token, purpose='zabbix').ref
    else: secret = old.secret_ref
    if old is None:
        old = m.MonitoringSource(name=body.get('name', 'Zabbix'), url=url, system_id=system_id, secret_ref=secret)
        actor.db.add(old)
    if scope_changed:
        # Lock instances before any dirty source UPDATE can take its row lock.
        # Collection holds the instance and only then updates source health.
        identities=list(actor.db.execute(select(m.MonitoringMapping.id,m.MonitoringMapping.instance_id).where(m.MonitoringMapping.source_id==old.id)))
        instances=list(actor.db.scalars(select(m.Instance).where(m.Instance.id.in_([r.instance_id for r in identities])).order_by(m.Instance.id).execution_options(populate_existing=True).with_for_update()))
        mappings=list(actor.db.scalars(select(m.MonitoringMapping).where(m.MonitoringMapping.id.in_([r.id for r in identities])).order_by(m.MonitoringMapping.instance_id).execution_options(populate_existing=True).with_for_update()))
        for instance in instances:
            instance.config_revision+=1
            for schedule in actor.db.scalars(select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id==instance.id)):
                schedule.config_revision=instance.config_revision
        for mapping in mappings: mapping.version+=1
    old.name, old.url, old.system_id, old.secret_ref = body.get('name', old.name), url, system_id, secret
    old.allowed_host_group_ids = body.get('allowed_host_group_ids', old.allowed_host_group_ids or [])
    if token:
        old.status, old.error_code, old.last_success_at = 'ready', None, utcnow()
    elif upstream_changed:
        old.status, old.error_code, old.last_success_at = 'unknown', None, None
    actor.db.flush()
    return old


def tags(row): return {v.get('tag'): v.get('value') for v in row.get('tags', []) if isinstance(v, dict)}


def sampling_interval(row, by_id):
    """Dependent items inherit their master's cadence; zero is not one second."""
    visited = set()
    for _ in range(8):
        identifier = str(row.get('itemid', ''))
        if identifier in visited: break
        visited.add(identifier)
        period = tags(row).get('sl.sample_period', row.get('delay'))
        match = re.fullmatch(r'(\d+)([smh]?)', str(period))
        if match and int(match[1]) > 0:
            return int(match[1]) * {'': 1, 's': 1, 'm': 60, 'h': 3600}[match[2]], True
        master = by_id.get(str(row.get('master_itemid')))
        if master is None: break
        row = master
    return 60, False


def normalize_items(items):
    normalized, db_endpoints = [], set()
    by_id = {str(row.get('itemid')): row for row in items}
    for row in items:
        key = str(row.get('key_', ''))
        base = key.split('[', 1)[0]
        # Linux's total utilization dependent item is not idle/iowait utilization.
        if base == 'system.cpu.util' and key != base: continue
        if base.startswith('mysql.') and '[' in key: db_endpoints.add(key.split('[', 1)[1].rstrip(']'))
        if base not in KEYS and key not in SNAPSHOTS: continue
        tag = tags(row)
        enabled = str(row.get('status', '0')) == '0'
        available = enabled and str(row.get('state', '0')) == '0'
        interval, interval_verified = sampling_interval(row, by_id)
        if key in SNAPSHOTS:
            collector, source = SNAPSHOTS[key]
            spec = dict(kind='snapshot', collector=collector, source=source, metric=None, unit='')
        else:
            metric, collector, unit = KEYS[base]
            spec = dict(kind='metric', collector=collector, metric=metric, unit=unit)
        normalized.append({**spec, 'itemid': str(row['itemid']), 'key': key, 'value_type': int(row.get('value_type', 0)), 'interval': interval, 'interval_verified': interval_verified, 'available': available, 'field_units': {k[14:]: v for k, v in tag.items() if k.startswith('sl.field_unit.')}, 'snapshot_scope': tag.get('sl.snapshot_scope')})
    if len(db_endpoints) > 1: raise PlatformError(422, 'ambiguous_database_host', '同一 host 包含多个数据库端点，请拆分监控对象后接入。')
    # A public metric must resolve to one item, never merge two target endpoints.
    identities = [(r['kind'], r.get('metric') or r.get('source')) for r in normalized]
    if len(identities) != len(set(identities)): raise PlatformError(422, 'ambiguous_metric_binding', '监控对象有重复指标绑定，无法确定数据库身份。')
    return normalized[:32]


def capabilities(items):
    result = []
    for name in CAPABILITIES:
        matching = [r for r in items if (r['kind'] == 'metric' if name == 'metrics' else r['collector'] == name)]
        ok = any(r['available'] for r in matching)
        result.append({'name': name, 'status': 'available' if ok else 'unsupported' if matching else 'missing', 'reason': '' if ok else '上游 item 不可用' if matching else '未发现受支持的实际采集项'})
    return result


def discover(actor, source, config):
    authorize_system(actor, source.system_id, 'asset.onboard')
    client = client_for(source, config)
    params = {'output': ['hostid', 'host', 'name'], 'selectTags': 'extend', 'limit': 101}
    if source.allowed_host_group_ids: params['groupids'] = source.allowed_host_group_ids
    hosts = client.call('host.get', params)
    if not isinstance(hosts, list): raise PlatformError(503, 'invalid_monitoring_response', '上游返回格式无效。')
    hosts, truncated = hosts[:100], len(hosts) > 100
    rows = client.call('item.get', {'hostids': [str(h['hostid']) for h in hosts], 'output': ['itemid', 'hostid', 'key_', 'value_type', 'units', 'status', 'state', 'delay', 'type', 'master_itemid'], 'selectTags': 'extend', 'limit': 3001}) if hosts else []
    truncated |= len(rows) > 3000
    result = []
    for host in hosts:
        items = [r for r in rows[:3000] if str(r.get('hostid')) == str(host['hostid'])]
        error = None
        try: selected_items = normalize_items(items)
        except PlatformError as exc: selected_items, error = [], exc.detail['code']
        mapping = actor.db.scalar(select(m.MonitoringMapping).where(m.MonitoringMapping.source_id == source.id, m.MonitoringMapping.hostid == str(host['hostid'])))
        layer = tags(host).get('sl.layer')
        if layer not in ('db', 'host', 'net', 'network'):
            layer = 'db' if any(r['collector'] != 'host' for r in selected_items) else 'host' if selected_items else 'unknown'
        result.append({'hostid': str(host['hostid']), 'name': host.get('name') or host.get('host') or str(host['hostid']), 'layer': layer, 'capabilities': capabilities(selected_items), 'selected': bool(mapping and mapping.selected), 'instance_id': mapping.instance_id if mapping else None, 'item_count': len(items), 'items': selected_items, 'tags': tags(host), 'binding_error': error})
    return {'items': result, 'total': len(result), 'truncated': truncated}


def select_hosts(actor, source, hostids, config):
    authorize_system(actor, source.system_id, 'asset.onboard')
    discovery = discover(actor, source, config)
    if discovery['truncated']: raise PlatformError(422, 'discovery_truncated', '发现结果截断，请限制来源 host group 后重试。')
    lookup = {h['hostid']: h for h in discovery['items']}
    wanted = set(hostids)
    if not wanted.issubset(lookup): raise PlatformError(404, 'monitoring_host_not_found', '所选 host 不属于当前授权来源。')
    for hostid in wanted:
        host = lookup[hostid]
        if host['binding_error']: raise PlatformError(422, host['binding_error'], '该 host 的数据库/指标身份存在歧义。')
        if not host['items']: raise PlatformError(422, 'no_supported_items', '该 host 没有受支持的实际采集项。')
    identities = list(actor.db.execute(select(m.MonitoringMapping.id, m.MonitoringMapping.instance_id).where(m.MonitoringMapping.source_id == source.id, m.MonitoringMapping.layer != 'group')))
    # Collection locks its instance before updating the mapping. Refresh must
    # acquire those same locks in that order, including objects being paused.
    instances = {r.id: r for r in actor.db.scalars(select(m.Instance).where(m.Instance.id.in_([row.instance_id for row in identities])).order_by(m.Instance.id).execution_options(populate_existing=True).with_for_update())}
    mappings = {r.hostid: r for r in actor.db.scalars(select(m.MonitoringMapping).where(m.MonitoringMapping.id.in_([row.id for row in identities])).order_by(m.MonitoringMapping.instance_id).execution_options(populate_existing=True).with_for_update())}
    for hostid in wanted:
        host = lookup[hostid]
        mapping = mappings.get(hostid)
        existing_mapping = mapping is not None
        engine = 'mysql' if host['layer'] == 'db' else 'host' if host['layer'] == 'host' else 'network'
        items = sorted(host['items'], key=lambda item: item['itemid'])
        if mapping is None:
            instance = m.Instance(system_id=source.system_id, name=host['name'][:120], endpoint='zabbix:' + hostid, connection_mode='zabbix', expected_server_uuid=None, engine=engine, enabled=True, lifecycle_state='Active')
            actor.db.add(instance); actor.db.flush()
            actor.db.add(m.InstanceLease(instance_id=instance.id))
            mapping = m.MonitoringMapping(source_id=source.id, hostid=hostid, instance_id=instance.id)
            actor.db.add(mapping); actor.db.flush()
        else: instance = instances[mapping.instance_id]
        relationships = {'parent_hostid': host['tags'].get('sl.parent'), 'kind': 'registered', 'provenance': 'zabbix.host.tags.sl.parent'}
        if existing_mapping and (mapping.layer != host['layer'] or sorted(mapping.items, key=lambda item: item['itemid']) != items or mapping.relationships != relationships or instance.engine != engine):
            mapping.version += 1
            instance.config_revision += 1
        mapping.layer, mapping.items, mapping.capabilities = host['layer'], items, host['capabilities']
        mapping.relationships = relationships
        mapping.selected = instance.enabled = True
        instance.engine, instance.lifecycle_state = engine, 'Active'
        owners = {r['collector'] for r in mapping.items}
        schedules = {r.collector: r for r in actor.db.scalars(select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id == instance.id).with_for_update())}
        for owner, schedule in schedules.items():
            schedule.enabled = owner in owners
            schedule.config_revision = instance.config_revision
        for owner in owners - schedules.keys():
            actor.db.add(m.CollectionSchedule(instance_id=instance.id, collector=owner, interval_seconds=60, config_revision=instance.config_revision, enabled=True))
        mappings[hostid] = mapping
    for hostid, mapping in mappings.items():
        if hostid not in wanted:
            mapping.selected = False
            instance = instances[mapping.instance_id]; instance.enabled = False; instance.lifecycle_state = 'Paused'
            for schedule in actor.db.scalars(select(m.CollectionSchedule).where(m.CollectionSchedule.instance_id == instance.id)): schedule.enabled = False
    actor.db.flush()
    return {'selected': len(wanted), 'instance_ids': [mappings[h].instance_id for h in sorted(wanted)]}


def objects(actor, **scope):
    allowed = authorize(actor, 'data.read').instance_ids
    rows = actor.db.execute(select(m.Instance, m.MonitoringMapping, m.MonitoringSource, m.System).join(m.MonitoringMapping, m.MonitoringMapping.instance_id == m.Instance.id).join(m.MonitoringSource, m.MonitoringSource.id == m.MonitoringMapping.source_id).join(m.System, m.System.id == m.Instance.system_id).where(m.Instance.id.in_(allowed))).all()
    items = []
    for instance, mapping, source, system in rows:
        if any(scope.get(key) and scope[key] != value for key, value in [('instance_id', instance.id), ('system_id', system.id), ('department_id', system.department_id), ('environment', system.environment)]): continue
        items.append({'id': instance.id, 'name': instance.name, 'engine': instance.engine, 'system_id': system.id, 'system_name': system.name, 'source_id': source.id, 'source_name': source.name, 'enabled': instance.enabled and mapping.selected, 'capabilities': effective_capabilities(mapping, source), 'last_observed_at': iso(mapping.last_observed_at), 'status': 'paused' if not mapping.selected else source.status if source.status != 'ready' else 'unknown' if mapping.last_observed_at is None else 'stale' if mapping.last_observed_at < utcnow() - timedelta(minutes=5) else 'ready'})
    return {'items': items, 'total': len(items)}


def mapping_for(db, instance_id):
    return db.scalar(select(m.MonitoringMapping).where(m.MonitoringMapping.instance_id == instance_id))


def history(client, item, start, end):
    rows = client.call('history.get', {'history': item['value_type'], 'itemids': [item['itemid']], 'time_from': int(start.replace(tzinfo=timezone.utc).timestamp()), 'time_till': math.ceil(end.replace(tzinfo=timezone.utc).timestamp()) - 1, 'sortfield': ['clock', 'ns'], 'sortorder': 'ASC', 'limit': 2001})
    values = []
    for row in rows[:2000]:
        try:
            stamp = datetime.fromtimestamp(int(row['clock']) + int(row.get('ns', 0)) / 1e9, timezone.utc).replace(tzinfo=None)
            if start <= stamp < end: values.append((stamp, row['value']))
        except (TypeError, ValueError, KeyError, OverflowError): continue
    return sorted(values), len(rows) > 2000


def metric_charts(db, instance, start, end, config):
    if start >= end or end - start > timedelta(days=14): raise PlatformError(422, 'invalid_metric_window', '指标窗口最多14天且必须递增。')
    mapping = mapping_for(db, instance.id)
    if mapping is None: return []
    if mapping.layer == 'group':
        from .analysis_groups import group_charts
        return group_charts(db, instance, mapping, start, end, config)
    source = db.get(m.MonitoringSource, mapping.source_id)
    client = client_for(source, config)
    charts = []
    for item in mapping.items:
        if item['kind'] != 'metric': continue
        rows, truncated = history(client, item, start, end) if item['available'] else ([], False)
        points, gaps = [], []
        cadence_known = item.get('interval_verified', True)
        previous = None
        for stamp, raw in rows:
            if cadence_known and (stamp - (previous if previous is not None else start)).total_seconds() > item['interval'] * 1.5:
                missing_at = previous + timedelta(seconds=item['interval']) if previous is not None else start
                gap = {'from': iso(missing_at), 'to': iso(stamp), 'reason': 'missing_samples'}
                gaps.append(gap); points.append({'time': iso(missing_at), 'value': None, 'validity': 'unknown_gap'})
            try: value = float(raw); value = value if math.isfinite(value) else None
            except (ValueError, TypeError): value = None
            points.append({'time': iso(stamp), 'value': value, 'validity': 'valid' if value is not None else 'invalid_value'})
            if value is None: gaps.append({'from': iso(stamp), 'to': iso(stamp), 'reason': 'invalid_value'})
            previous = stamp
        if previous is None or cadence_known and (end - previous).total_seconds() > item['interval'] * 1.5:
            missing_at = previous + timedelta(seconds=item['interval']) if previous is not None else start
            gaps.append({'from': iso(missing_at), 'to': iso(end), 'reason': 'missing_samples'})
            points.append({'time': iso(missing_at), 'value': None, 'validity': 'unknown_gap'})
        if truncated: gaps.append({'reason': 'history_truncated'})
        if not cadence_known: gaps.append({'reason': 'sample_interval_unverified'})
        charts.append({'id': item['metric'], 'title': item['metric'], 'template': item['metric'], 'owner_collector': item['collector'], 'instance_id': instance.id, 'unit': item['unit'], 'source': 'zabbix_history', 'points': points, 'gaps': gaps, 'window': {'from': iso(start), 'to': iso(end)}, 'coverage': {'complete': bool(rows) and not gaps and item['available'], 'truncated': truncated}, 'provenance': {'source_id': source.id, 'hostid': mapping.hostid, 'itemid': item['itemid'], 'mapping_version': mapping.version, 'transport': 'zabbix'}})
    return charts


def object_metrics(actor, identifier, start, end, config):
    instance = scoped_instance(actor, identifier)
    mapping = mapping_for(actor.db, identifier)
    if mapping is None: raise PlatformError(404, 'monitoring_object_not_found', '监控对象不存在。')
    return {'object': {'id': instance.id, 'name': instance.name}, 'charts': metric_charts(actor.db, instance, start, end, config), 'capabilities': mapping.capabilities, 'window': {'from': iso(start), 'to': iso(end)}}


def handle_collection(factory, lease, config):
    from .evidence import append_batch
    from .scheduling import check_fence, complete, db_now
    from .events import evaluate
    revision = lease.payload.get('config_revision')
    with factory.begin() as db:
        check_fence(db, lease, config_revision=revision)
        instance = db.get(m.Instance, lease.instance_id)
        mapping = mapping_for(db, instance.id)
        if not mapping or not mapping.selected or lease.payload.get('config_revision') != instance.config_revision:
            raise PlatformError(409, 'monitoring_mapping_inactive', '监控映射已停用或变更。')
        source = db.get(m.MonitoringSource, mapping.source_id)
        if mapping.layer == 'group':
            from .analysis_groups import collect_group
            return collect_group(db, instance, mapping, source, lease, config)
        client = client_for(source, config)
        end, start = utcnow(), utcnow() - timedelta(minutes=10)
        evidence_ids, observations = [], []
        for owner in lease.payload.get('collectors', []):
            run = m.CollectionRun(task_id=lease.id, instance_id=instance.id, collector=owner, attempt=lease.attempt, config_revision=revision, fence=lease.fence, status='running', started_at=db_now(db))
            db.add(run); db.flush()
            entries = []
            for item in [r for r in mapping.items if r['collector'] == owner]:
                rows, truncated = history(client, item, start, end) if item['available'] else ([], False)
                if not rows: continue
                observed, raw = rows[-1]; observations.append(observed)
                payload, logical_source = {}, 'mysql.global_status' if owner != 'host' else 'linux.host'
                if item['kind'] == 'metric':
                    try: val = float(raw)
                    except (TypeError, ValueError): continue
                    if not math.isfinite(val): continue
                    payload = {'rows': [{'VARIABLE_NAME': item['metric'], 'VARIABLE_VALUE': val}], 'values': {item['metric']: val}}
                else:
                    logical_source = item['source']
                    try: parsed = json.loads(raw)
                    except (TypeError, ValueError): continue
                    if not isinstance(parsed, dict) or not isinstance(parsed.get('rows'), list): continue
                    source_rows = [r for r in parsed['rows'][:100] if isinstance(r, dict)]
                    if logical_source == 'mysql.lock_waits':
                        aliases = {'REQUESTING_ENGINE_TRANSACTION_ID': 'waiting_trx_id', 'BLOCKING_ENGINE_TRANSACTION_ID': 'blocking_trx_id', 'REQUESTING_THREAD_ID': 'waiting_thread_id', 'BLOCKING_THREAD_ID': 'blocking_thread_id'}
                        source_rows = [{aliases.get(k, k): v for k, v in r.items()} for r in source_rows]
                    payload = {'rows': source_rows, 'field_units': item.get('field_units'), 'snapshot_scope': item.get('snapshot_scope'), 'timer_unit': parsed.get('timer_unit'), 'cumulative_since_server_start': parsed.get('cumulative_since_server_start')}
                    if logical_source == 'mysql.statement_digest':
                        payload['window'] = digest_window(rows, item)
                fresh = observed >= end - timedelta(seconds=max(60, item['interval'] * 3))
                entries.append({'source': logical_source, 'source_mode': 'live', 'observed_at': iso(observed), 'payload': payload, 'availability': 'available' if fresh else 'stale', 'coverage': {'complete': fresh and not truncated and item.get('interval_verified', True), 'gaps': [] if item.get('interval_verified', True) else ['sample_interval_unverified'], 'transport': 'zabbix', 'source_id': source.id, 'hostid': mapping.hostid, 'itemid': item['itemid'], 'field_units': item.get('field_units'), 'snapshot_scope': item.get('snapshot_scope')}, 'truncated': truncated})
            check_fence(db, lease, config_revision=revision)
            run.status = 'succeeded' if entries else 'unavailable'; run.finished_at = db_now(db); run.row_count = len(entries)
            evidence_ids.extend(append_batch(db, instance, run, {'observed_at': iso(end), 'evidence_items': entries}, interval_seconds=60, collector_version='zabbix-1'))
            evaluate(db, instance.id, run_id=run.id, now=run.finished_at)
        if observations: mapping.last_observed_at = max(observations)
        source.status, source.error_code, source.last_success_at = 'ready', None, end
        instance.last_success_at = mapping.last_observed_at
        complete(db, lease, config_revision=revision, result={'evidence_ids': evidence_ids})
        return {'evidence_ids': evidence_ids}


def digest_window(rows, item):
    """Actual interval deltas only; lifetime MAX is never an interval percentile."""
    result = {'valid': False, 'rows': [], 'reason': 'insufficient_or_unverified_cumulative_snapshots'}
    if len(rows) < 2 or item.get('field_units', {}).get('SUM_TIMER_WAIT') != 'ps' or item.get('snapshot_scope') != 'server_lifetime': return result
    try:
        snapshots = [json.loads(raw) for _, raw in rows]
        if any(not isinstance(p, dict) or not isinstance(p.get('rows'), list) for p in snapshots): return result
        required = {'DIGEST', 'COUNT_STAR', 'SUM_TIMER_WAIT'}
        if any(not isinstance(row, dict) or not required.issubset(row) for snapshot in snapshots for row in snapshot['rows']):
            return {**result, 'reason': 'invalid_digest_rows'}
        epochs = [p.get('counter_epoch') for p in snapshots]
        if any(isinstance(epoch, bool) or not isinstance(epoch, (str, int)) or str(epoch).strip().lower() in ('', 'unknown', 'null', 'none', 'unverified') or len(str(epoch)) > 80 for epoch in epochs):
            return {**result, 'reason': 'counter_epoch_unverified'}
        if any(epoch != epochs[0] for epoch in epochs):
            return {**result, 'reason': 'counter_epoch_changed'}
        first, last = snapshots[0], snapshots[-1]
        baseline = {r['DIGEST']: r for r in first['rows'] if isinstance(r, dict) and r.get('DIGEST')}
        seconds = (rows[-1][0] - rows[0][0]).total_seconds()
        if seconds <= 0: return result
        deltas = []
        for row in last['rows'][:100]:
            prior = baseline.get(row.get('DIGEST'))
            if not prior: continue
            count = int(row['COUNT_STAR']) - int(prior['COUNT_STAR'])
            timer = int(row['SUM_TIMER_WAIT']) - int(prior['SUM_TIMER_WAIT'])
            if count < 0 or timer < 0: continue
            deltas.append({'DIGEST': row['DIGEST'], 'count_delta': count, 'total_time_seconds_delta': timer / 1e12, 'avg_latency_seconds': timer / 1e12 / count if count else None})
        return {'valid': bool(deltas), 'from': iso(rows[0][0]), 'to': iso(rows[-1][0]), 'window_seconds': seconds, 'rows': deltas, 'reason': 'verified_cumulative_delta' if deltas else 'counter_reset_or_no_common_digest'}
    except (TypeError, ValueError, KeyError): return result



def effective_capabilities(mapping, source):
    stale = not mapping.last_observed_at or mapping.last_observed_at < utcnow()-timedelta(minutes=5)
    result=[]
    for cap in mapping.capabilities:
        value=dict(cap)
        if value["status"]=="available" and (source.status!="ready" or stale):
            value["status"]="failed" if source.status=="failed" else "stale"
            value["reason"]="来源查询失败" if source.status=="failed" else "尚无近期有效样本"
        result.append(value)
    return result
