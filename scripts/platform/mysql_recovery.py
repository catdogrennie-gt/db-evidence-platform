"""Read-only control-MySQL recovery decision; never starts or adopts a process.

The observer is trusted local engineering code, not an API or model tool.
start_required is a plan requiring a separately guarded executor, not success.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import json
import ntpath
from pathlib import Path
import re
from uuid import UUID


def _path(value):
    if (not isinstance(value,str) or not re.match(r'^[A-Za-z]:[\\/]',value)
            or any(ch in value for ch in '\0\r\n"<>|*?') or ':' in value[2:]
            or any(part in ('.','..') or part.endswith((' ','.')) for part in re.split(r'[\\/]',value)[1:])):
        raise ValueError()
    return ntpath.normcase(ntpath.normpath(value))


def _hash(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):raise ValueError()


def _uuid(value):
    if not isinstance(value,str) or str(UUID(value))!=value:raise ValueError()
    return value


def _port(value,minimum=1):
    if type(value)is not int or not minimum<=value<=65535:raise ValueError()


def _time(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError()
    return result


def _validate(c,o):
    if not isinstance(c,dict) or type(c.get('version'))is not int or c['version']!=1:raise ValueError()
    for key in ('executable','defaults_file','datadir'):_path(c[key])
    for key in ('executable_sha256','defaults_sha256'):_hash(c[key])
    _uuid(c['server_uuid']);_port(c['port'],minimum=1024)
    if c['host']!='127.0.0.1':raise ValueError()
    if not isinstance(o,dict):raise ValueError()
    _time(o['captured_at']);f=o['files']
    for key in ('executable_sha256','defaults_sha256'):_hash(f[key])
    for key in ('datadir','defaults_datadir'):_path(f[key])
    _uuid(f['datadir_uuid']);_port(f['defaults_port'])
    if type(f['data_present'])is not bool or not isinstance(f['defaults_bind_address'],str):raise ValueError()
    if not isinstance(o['processes'],list) or len(o['processes'])>128:raise ValueError()
    if not isinstance(o['listeners'],list) or len(o['listeners'])>4096:raise ValueError()
    seen=set()
    for p in o['processes']:
        if type(p.get('pid'))is not int or p['pid']<=0 or p['pid']in seen:raise ValueError()
        seen.add(p['pid'])
        for key in ('executable','defaults_file','datadir'):
            if p.get(key)is not None:_path(p[key])
        if p.get('port')is not None:_port(p['port'])
        if type(p.get('command_valid'))is not bool:raise ValueError()
    for row in o['listeners']:
        if type(row.get('pid'))is not int or row['pid']<=0 or not isinstance(row.get('host'),str):raise ValueError()
        _port(row['port'])
    probe=o['probe']
    if probe is not None:
        if not isinstance(probe,dict) or type(probe.get('ok'))is not bool:raise ValueError()
        if probe['ok']:
            if type(probe.get('pid'))is not int or probe['pid']<=0:raise ValueError()
            _uuid(probe['server_uuid']);_port(probe['port']);_path(probe['datadir'])


def _result(status,reason=None):
    return {'status':status,'reasons':[reason]if reason else[]}


def evaluate(contract,observed,*,now=None):
    """Return only state/reason codes; untrusted or ambiguous inputs fail closed."""
    c,o=contract,observed
    try:
        _validate(c,o)
        age=((now or datetime.now(timezone.utc))-_time(o['captured_at'])).total_seconds()
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
        return _result('refused','invalid_recovery_input')
    if not -5<=age<=60:return _result('refused','stale_observation')
    f=o['files']
    if any(f[k]!=c[k]for k in ('executable_sha256','defaults_sha256')):
        return _result('refused','registered_file_changed')
    if (not f['data_present'] or f['datadir_uuid']!=c['server_uuid']
            or _path(f['datadir'])!=_path(c['datadir'])):
        return _result('refused','registered_storage_changed')
    if (_path(f['defaults_datadir'])!=_path(c['datadir']) or f['defaults_port']!=c['port']
            or f['defaults_bind_address']!=c['host']):
        return _result('refused','registered_configuration_changed')
    for p in o['processes']:
        if not p['command_valid'] or any(p.get(k)is None for k in ('executable','defaults_file','datadir','port')):
            return _result('refused','mysql_process_scope_unknown')
    candidates=[p for p in o['processes']if _path(p['datadir'])==_path(c['datadir'])
                or _path(p['defaults_file'])==_path(c['defaults_file']) or p['port']==c['port']]
    if len(candidates)>1:return _result('refused','duplicate_mysql_process')
    listeners=[r for r in o['listeners']if r['port']==c['port']]
    owners={r['pid']for r in listeners}
    probe=o['probe']
    if not candidates:
        if owners:return _result('refused','port_conflict')
        if probe and probe['ok']:return _result('refused','unmatched_live_server')
        return _result('start_required','registered_service_missing')
    p=candidates[0]
    if (any(_path(p[k])!=_path(c[k])for k in ('executable','defaults_file','datadir')) or p['port']!=c['port']):
        return _result('refused','mysql_process_identity_changed')
    if owners-{p['pid']}:return _result('refused','port_conflict')
    if any(r['host']!=c['host']for r in listeners):return _result('refused','listener_binding_changed')
    if probe and probe['ok']:
        if (probe['pid']!=p['pid'] or probe['server_uuid']!=c['server_uuid']
                or probe['port']!=c['port'] or _path(probe['datadir'])!=_path(c['datadir'])):
            return _result('refused','live_server_identity_changed')
    if not owners or not probe or not probe['ok']:return _result('present_not_ready','existing_service_not_ready')
    return _result('present_ready')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contract',required=True,type=Path)
    parser.add_argument('--observed',required=True,type=Path)
    args=parser.parse_args(argv)
    try:
        result=evaluate(json.loads(args.contract.read_text(encoding='utf-8-sig')),
                        json.loads(args.observed.read_text(encoding='utf-8-sig')))
    except (OSError,UnicodeError,ValueError,RecursionError):
        result=_result('refused','invalid_or_unreadable_recovery_input')
    print(json.dumps(result))
    return 0 if result['status']=='present_ready'else 1


if __name__=='__main__':raise SystemExit(main())
