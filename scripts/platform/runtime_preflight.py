"""Read-only deployment identity comparison; never starts or adopts a service.

Inputs belong in a private directory. Output contains logical component labels
and reason codes only. A passing snapshot is not a cold-start verification.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import json
import ntpath
from pathlib import Path
import re


def _time(value):
    if not isinstance(value,str): raise ValueError()
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None: raise ValueError()
    return result


def _positive(value):
    return type(value) is int and value > 0


def _signature(row):
    return ntpath.normpath(row['executable']).casefold(),row['command_sha256']


def _process(row):
    if (not isinstance(row,dict) or not _positive(row.get('pid'))
            or type(row.get('parent_pid')) is not int or row['parent_pid']<0
            or not isinstance(row.get('executable'),str) or not ntpath.isabs(row['executable'])
            or not isinstance(row.get('command_sha256'),str)
            or not re.fullmatch('[0-9a-f]{64}',row['command_sha256'])):
        raise ValueError()
    _time(row.get('created_at'))
    kind,scope=row.get('consumer_kind'),row.get('consumer_scope')
    if (kind is not None and kind not in ('ai','collector')
            or scope is not None and (kind is None or not isinstance(scope,str)
                                      or not re.fullmatch('[0-9a-f]{64}',scope))):
        raise ValueError()


def _validate(manifest,observed):
    if not isinstance(manifest,dict) or type(manifest.get('version')) is not int or manifest['version']!=1:
        raise ValueError()
    rows=manifest.get('components')
    if not isinstance(rows,list) or not 1<=len(rows)<=64: raise ValueError()
    names=set();signatures=set();ports=set();pids=set()
    for row in rows:
        _process(row)
        if row.get('consumer_kind') and row.get('consumer_scope') is None:raise ValueError()
        name=row.get('name');dependencies=row.get('dependencies');owned=row.get('ports')
        if (not isinstance(name,str) or not re.fullmatch('[a-z][a-z0-9_-]{0,39}',name)
                or name in names or row['pid']in pids or _signature(row)in signatures
                or not isinstance(dependencies,list) or not all(isinstance(n,str)for n in dependencies)
                or len(set(dependencies))!=len(dependencies)
                or not isinstance(owned,list) or len(owned)>16
                or not all(type(p)is int and 1024<=p<=65535 for p in owned)
                or len(set(owned))!=len(owned) or ports.intersection(owned)):
            raise ValueError()
        names.add(name);pids.add(row['pid']);signatures.add(_signature(row));ports.update(owned)
    graph={r['name']:r['dependencies']for r in rows}
    if any(not set(deps)<=names for deps in graph.values()):raise ValueError()
    resolved=set()
    while len(resolved)<len(names):
        ready={name for name,deps in graph.items()if name not in resolved and set(deps)<=resolved}
        if not ready:raise ValueError()
        resolved.update(ready)
    if not isinstance(observed,dict):raise ValueError()
    _time(observed.get('captured_at'))
    processes=observed.get('processes');listeners=observed.get('listeners')
    if (not isinstance(processes,list) or len(processes)>4096
            or not isinstance(listeners,list) or len(listeners)>4096):raise ValueError()
    seen=set()
    for row in processes:
        _process(row)
        if row['pid']in seen:raise ValueError()
        seen.add(row['pid'])
    for row in listeners:
        if (not isinstance(row,dict) or type(row.get('port'))is not int
                or not 1<=row['port']<=65535 or not _positive(row.get('pid'))):raise ValueError()


def evaluate(manifest,observed,*,now=None):
    """Reject invalid/ambiguous inputs without echoing their private contents."""
    try:_validate(manifest,observed)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
        raise ValueError('invalid_preflight_input') from None
    age=((now or datetime.now(timezone.utc))-_time(observed['captured_at'])).total_seconds()
    if not -5<=age<=60:return {'ready':False,'error':'stale_observation'}
    by_pid={r['pid']:r for r in observed['processes']}
    owners={}
    for row in observed['listeners']:owners.setdefault(row['port'],set()).add(row['pid'])
    results=[]
    for expected in manifest['components']:
        actual=by_pid.get(expected['pid'])
        matching=[r for r in observed['processes']if _signature(r)==_signature(expected)]
        kind,scope=expected.get('consumer_kind'),expected.get('consumer_scope')
        consumers=[r for r in observed['processes']if kind and r.get('consumer_kind')==kind]
        duplicate_scope=sum(r.get('consumer_scope')==scope for r in consumers)>1
        conflict=any(owners.get(port,set())-{expected['pid']}for port in expected['ports'])
        if conflict:status='port_conflict'
        elif len(matching)>1 or duplicate_scope:status='duplicate'
        elif any(r.get('consumer_scope')is None for r in consumers):status='consumer_scope_unknown'
        elif actual is None:status='missing'
        elif (_signature(actual)!=_signature(expected)
                or actual['parent_pid']!=expected['parent_pid']
                or (kind and (actual.get('consumer_kind'),actual.get('consumer_scope'))!=(kind,scope))
                or _time(actual['created_at'])!=_time(expected['created_at'])):
            status='identity_changed'
        elif any(expected['pid']not in owners.get(port,set())for port in expected['ports']):
            status='listener_missing'
        else:status='ready'
        results.append({'name':expected['name'],'status':status,
                        'reasons':[]if status=='ready'else[status]})
    by_name={r['name']:r for r in results}
    for _ in results:
        changed=False
        for expected in manifest['components']:
            row=by_name[expected['name']]
            if row['status']=='ready'and any(by_name[d]['status']!='ready'for d in expected['dependencies']):
                row.update(status='dependency_blocked',reasons=['dependency_unavailable']);changed=True
        if not changed:break
    return {'ready':all(r['status']=='ready'for r in results),'components':results}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True,type=Path)
    parser.add_argument('--observed',required=True,type=Path)
    args=parser.parse_args(argv)
    try:
        result=evaluate(json.loads(args.manifest.read_text(encoding='utf-8-sig')),
                        json.loads(args.observed.read_text(encoding='utf-8-sig')))
    except (OSError,UnicodeError,ValueError,RecursionError):
        print(json.dumps({'ready':False,'error':'invalid_or_unreadable_preflight_input'}))
        return 2
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['ready']else 1


if __name__=='__main__':
    raise SystemExit(main())
