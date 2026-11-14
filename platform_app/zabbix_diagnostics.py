"""Strict contracts for bounded SQL files collected by Zabbix Agent2.

This module neither connects to MySQL nor executes SQL. Labels alone are not
evidence: exact key, declared contract and payload must agree.
"""
import csv
import math
import re
from datetime import datetime, timezone

from .common import iso

CONTRACT = 'db-evidence-mysql-diagnostics-v1'
LEGACY_LOCK_CONTRACT = 'db-evidence-lock-snapshot-v2'
APP_CONTRACT = 'db-evidence-application-events-v1'
SPECS = {
    'configuration': ('mysql.configuration', 50, 'current_snapshot'),
    'transactions': ('mysql.transactions', 50, 'current_snapshot'),
    'sessions': ('mysql.sessions', 50, 'current_snapshot'),
    'table_io': ('mysql.table_io', 20, 'server_cumulative_top_n'),
    'file_io': ('mysql.file_io', 20, 'server_cumulative_top_n'),
    'statements': ('mysql.statement_statistics', 20, 'server_cumulative_top_n'),
    'error_log': ('mysql.error_log', 100, 'last_10m_events'),
    'slow_log': ('mysql.slow_log.table', 100, 'last_10m_events'),
    'locks': ('mysql.lock_waits', 50, 'current_snapshot'),
    'indexes': ('mysql.indexes', 100, 'current_snapshot'),
    'tables': ('mysql.tables', 100, 'current_snapshot'),
    'application_logs': ('application.events',100,'last_5m_events'),
}


def definition(item):
    key = item.get('key', item.get('key_', ''))
    if key=='app.diagnostics.events':
        tags=item.get('tags')
        if not isinstance(tags,list):return None
        values=[t.get('value')for t in tags if isinstance(t,dict)and t.get('tag')=='dbe.contract']
        if not values or any(v!='application-events-v1'for v in values):return None
        source,limit,scope=SPECS['application_logs']
        return {'query':'application_logs','source':source,'limit':limit,'scope':scope}
    if not isinstance(key, str) or not key.startswith('mysql.custom.query[') or not key.endswith(']'):
        return None
    try:
        args = next(csv.reader([key[len('mysql.custom.query['):-1]], strict=True))
    except (csv.Error, StopIteration):
        return None
    if len(args) != 4 or not re.fullmatch('[A-Za-z_][A-Za-z0-9_.-]{0,63}',args[0]) or args[1:3] != ['', ''] or args[3] not in SPECS or args[3]=='application_logs':
        return None
    tags = item.get('tags')
    if not isinstance(tags, list): return None
    def declared(name, expected):
        values = [t.get('value') for t in tags if isinstance(t, dict) and t.get('tag') == name]
        return bool(values) and all(value == expected for value in values)
    query = args[3]
    if not declared('dbe.contract', 'mysql-diagnostics-v1') or not declared('dbe.query', query):
        return None
    source, limit, scope = SPECS[query]
    return {'query': query, 'source': source, 'limit': limit, 'scope': scope}


def legacy_lock_definition(item):
    """Only the registered complete-snapshot protocol, never name inference."""
    if (item.get('key', item.get('key_', '')) not in
            ('dbops.mysql.lock_snapshot', 'sl.mysql.lock_snapshot')
            or item.get('enhancement') != 'mysql.lock_waits'):
        return None
    tags = item.get('tags')
    if not isinstance(tags, list): return None
    values = [t.get('value') for t in tags
              if isinstance(t, dict) and t.get('tag') == 'sl.snapshot_contract']
    if not values or any(v != LEGACY_LOCK_CONTRACT for v in values): return None
    return {'query': 'locks', 'source': 'mysql.lock_waits', 'limit': 50,
            'scope': 'current_snapshot', 'contract': LEGACY_LOCK_CONTRACT}


def legacy_lock_producer(item, parsed):
    """Authenticate future legacy producer proof before the payload is hashed."""
    if legacy_lock_definition(item) is None or not isinstance(parsed, dict): return None
    rows, count = parsed.get('rows'), parsed.get('total_row_count')
    if (parsed.get('producer_contract') != LEGACY_LOCK_CONTRACT
            or parsed.get('snapshot_complete') is not True
            or parsed.get('snapshot_truncated') is not False
            or type(count) is not int or not 0 <= count <= 50
            or not isinstance(rows, list) or len(rows) != count
            or any(not isinstance(row, dict) for row in rows)):
        return None
    return {'contract': LEGACY_LOCK_CONTRACT, 'query_name': 'locks',
            'scope': 'current_snapshot', 'returned_row_count': count,
            'snapshot_row_count': count, 'snapshot_complete': True,
            'snapshot_truncated': False}


def preprocessing(query):
    """Agent2 serializes an empty successful result as null (not an error)."""
    limit=SPECS[query][1]
    return ("var r=JSON.parse(value);if(r===null)r=[];"
            "if(!Array.isArray(r))throw 'invalid SQL result';"
            "return JSON.stringify({producer_contract:'"+CONTRACT+"',query_name:'"+query+
            "',rows:r.slice(0,"+str(limit)+"),snapshot_complete:r.length<="+str(limit)+
            ",snapshot_truncated:r.length>"+str(limit)+",returned_row_count:r.length});")


def _event_time(value, epoch=None):
    if epoch is not None:
        try:
            if isinstance(epoch,bool) or not math.isfinite(float(epoch)): raise ValueError()
            return iso(datetime.fromtimestamp(float(epoch),timezone.utc))
        except (TypeError,ValueError,OverflowError,OSError):
            raise ValueError('diagnostic_event_epoch_invalid') from None
    if not isinstance(value, str): raise ValueError('diagnostic_event_time_missing')
    try: stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError: raise ValueError('diagnostic_event_time_invalid') from None
    # Never guess a local session time zone. Fixed SQL emits UNIX_TIMESTAMP.
    if stamp.tzinfo is None: raise ValueError('diagnostic_event_timezone_missing')
    return iso(stamp)


def normalize(item, parsed):
    spec = definition(item)
    if spec is None: raise ValueError('diagnostic_contract_not_declared')
    contract=APP_CONTRACT if spec['query']=='application_logs'else CONTRACT
    if not isinstance(parsed, dict) or parsed.get('producer_contract') != contract or parsed.get('query_name') != spec['query']:
        raise ValueError('diagnostic_contract_mismatch')
    rows = parsed.get('rows'); count = parsed.get('returned_row_count')
    complete = parsed.get('snapshot_complete'); truncated = parsed.get('snapshot_truncated')
    if (not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows)
        or len(rows) > spec['limit'] or type(count) is not int or count < 0
        or type(complete) is not bool or type(truncated) is not bool
        or complete == truncated or count != len(rows) + int(truncated)
        or truncated and len(rows) != spec['limit']):
        raise ValueError('diagnostic_snapshot_invalid')
    payload = {'item': item, 'rows': rows, 'snapshot_scope': spec['scope'],
               'counter_epoch': None, 'cumulative_counters': spec['scope'] == 'server_cumulative_top_n',
               'window': {'valid': False, 'reason': 'current_snapshot_not_interval_delta'}}
    # Preserve validated producer proof inside the payload hashed at ingestion.
    # Collection/observation identity is bound by the fenced collector, never
    # copied from the upstream envelope. Legacy rows are not backfilled.
    payload['diagnostic_origin'] = {'version': 'diagnostic-origin-v1', 'producer': {
        'contract': contract, 'query_name': spec['query'], 'returned_row_count': count,
        'snapshot_row_count': len(rows), 'snapshot_complete': complete,
        'snapshot_truncated': truncated, 'scope': spec['scope'],
    }}
    if spec['query'] in ('table_io','file_io','statements'):
        payload['timer_unit'] = 'picoseconds'
    if spec['query'] in ('error_log','slow_log','application_logs'):
        events=[]
        for row in rows:
            if spec['query']=='error_log':
                event={'server_event_at':_event_time(row.get('LOGGED'),row.get('event_epoch')),
                       'severity':row.get('PRIO'),'error_code':row.get('ERROR_CODE')}
            elif spec['query']=='slow_log':
                event={'server_event_at':_event_time(row.get('start_time'),row.get('event_epoch')),'severity':'slow',
                       **{k:row[k] for k in ('query_time_seconds','lock_time_seconds','rows_sent','rows_examined') if k in row}}
            else:
                kind=row.get('event_kind');severity=row.get('severity');event_id=row.get('event_id')
                if kind not in ('request_slow','request_failed','request_window_summary') or severity not in ('info','warning','error') or not isinstance(event_id,str) or not re.fullmatch('[0-9a-f]{32}:[0-9]{1,12}',event_id):
                    raise ValueError('application_event_invalid')
                event={'server_event_at':_event_time(row.get('server_event_at'),row.get('event_epoch')),
                    'event_id':event_id,'event_kind':kind,'severity':severity,
                    **{k:row[k]for k in ('request_ms','db_ms','pool_wait_ms','request_count','error_count',
                        'request_max_ms','db_max_ms','pool_wait_max_ms','window_seconds')if k in row}}
                if kind=='request_window_summary':
                    start=_event_time(row.get('interval_start_at'));end=_event_time(row.get('interval_end_at'))
                    if start>=end or end!=event['server_event_at']:raise ValueError('application_summary_interval_invalid')
                    event.update(interval_start_at=start,interval_end_at=end)
                code=row.get('error_code')
                if isinstance(code,str)and re.fullmatch('mysql_[0-9]{1,5}|request_failed',code):event['error_code']=code
            events.append(event)
        payload['events']=events
        # Raw SQL, error bodies and request values never enter typed log data.
        payload['rows']=[]
    gaps=parsed.get('capture_gaps',[])
    if not isinstance(gaps,list)or any(g not in ('ring_evicted_inside_window','process_started_inside_window','summary_crosses_window_boundary')for g in gaps):raise ValueError('diagnostic_capture_gaps_invalid')
    return {'source': spec['source'], 'payload':payload, 'sensitivity':'restricted',
            'truncated':truncated,'coverage':{'snapshot_complete':complete,
            'snapshot_truncated':truncated,'snapshot_scope':spec['scope'],
            'snapshot_contract':contract,'snapshot_row_count':len(rows),'capture_gaps':gaps,
            'late_arrival_possible':spec['query']=='slow_log',
            'ring_coverage_unverified':spec['query']=='error_log'}}
