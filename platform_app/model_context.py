"""Compact, citation-preserving model context derived only from frozen values."""
from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from .common import iso, parse_time, PlatformError
from .zabbix_items import safe_item,safe_relations

SERIES_COLUMNS=['evidence_id','resource','hostid','itemid','name','key','unit','sample_count','first','last','min','max','missing_count','complete','reason_codes','last_time','original_point_count','sampling_method','value_semantics','semantics_source','first_time']
_WINDOW_UNSET=object()


def _numeric_identity(resource,hostid,itemid):
    source_id=resource.get('source_id')
    if not all(isinstance(value,str) and value for value in (source_id,hostid,itemid)):return None
    source='zabbix.item.numeric:'+hashlib.sha256(f'{source_id}:{hostid}:{itemid}'.encode()).hexdigest()[:24]
    return resource.get('instance_id'),source,hostid,itemid


def _sampled_numeric_rows(series):
    columns=(series or {}).get('columns',[])
    if 'sample_count' not in columns:return []
    index=columns.index('sample_count')
    return [row for row in (series or {}).get('rows',[])
            if len(row)>index and type(row[index]) is int and row[index]>0]


def compact_numeric_evidence(item, series=None, *, window_start=_WINDOW_UNSET, window_end=_WINDOW_UNSET):
    """Bound an already-authorized projection, never its frozen source payload.

    Counts and endpoints are recorded before display sampling. Upstream gaps
    remain in coverage; omitting middle points here adds no upstream gap.
    Compacted projections may only re-enter the same frozen window. A changed
    window must start from the original Evidence projection, not its subset.
    """
    item=deepcopy(item); data=item.get('data') or {}
    if (not str(item.get('source','')).startswith('zabbix.item.numeric:')
            or not isinstance(data.get('rows'),list) or data.get('observation_unavailable')
            or data.get('text_omitted')): return item
    if data.get('projection_scope')=='retained_samples_subset_not_upstream_gap': return item
    rows=data['rows']
    bounded=window_start is not _WINDOW_UNSET or window_end is not _WINDOW_UNSET
    if bounded:
        # ORM boundaries are UTC-naive datetimes; external strings must carry
        # their own timezone. An invalid or partial window never means all time.
        try:
            lower=parse_time(iso(window_start) if isinstance(window_start,datetime) else window_start)
            upper=parse_time(iso(window_end) if isinstance(window_end,datetime) else window_end)
            if lower>=upper:raise ValueError()
        except (PlatformError,AttributeError,ValueError,TypeError):
            rows=[]
        else:
            retained=[]
            for point in rows:
                try:stamp=parse_time(point.get('time'))
                except (PlatformError,AttributeError,ValueError,TypeError):continue
                if lower<=stamp<upper:retained.append(point)
            rows=retained
        data['row_count']=len(rows)
        if not rows:
            data['observation_unavailable']=True
            item['coverage']=dict(item.get('coverage') or {},complete=False,availability='unknown')
    count=len(rows)
    meta=data.get('item') or {}
    keep=('hostid','itemid','units','value_semantics','semantics_source','interval','interval_verified','history_semantics')
    data['item']={k:meta[k] for k in keep if k in meta}
    resources=(series or {}).get('resources',[])
    identity=(item.get('instance_id'),item.get('source'),meta.get('hostid'),meta.get('itemid'))
    related=next((r[0] for r in _sampled_numeric_rows(series)
                  if _numeric_identity(resources[r[1]],r[2],r[3])==identity),None)
    if related:
        data['numeric_series_ref']=related
        # Scheduling labels are not measurement values. Keep this observation's
        # identity, units, semantics and names even if its window is later
        # omitted; a removed window must never leave a metadata alias.
        for key in ('interval','interval_verified','history_semantics'):
            data['item'].pop(key,None)
    for key,limit in [('name',40),('key',56)]:
        if key in meta:data['item'][key]=str(meta[key])[:limit]
    data.update(sample_count=count,first_time=rows[0].get('time') if rows else None,
        last_time=rows[-1].get('time') if rows else None,
        rows=rows[:1]+rows[-3:] if count>4 else rows,
        rows_omitted=max(0,count-4),projection_scope='retained_samples_subset_not_upstream_gap')
    return item


def _snapshot_time(item):
    try:
        value = item.get('observed_at')
        if not isinstance(value, str): return None
        return parse_time(value)
    except (PlatformError, AttributeError, ValueError, TypeError):
        return None


def snapshot_identity(item):
    data=item.get('data') or {}
    meta=data.get('item') or {}
    return (item.get('instance_id'),item.get('source'),item.get('template',data.get('metric')),
            meta.get('source_id'),meta.get('hostid'),meta.get('itemid'))


def snapshot_boundary_refs(items):
    """Protect nearest supplied observations, never infer continuity/recovery.

    Only source-qualified point snapshots participate. An empty numeric curve,
    unknown source identity or unavailable sample is not a normal counterexample.
    This pure selector cannot recover evidence omitted before this projection.
    """
    groups = {}
    for item in items:
        if item.get('source') not in ('mysql.lock_waits','mysql.transactions','mysql.sessions'):
            continue
        data = item.get('data') or {}
        if (item.get('source_mode') != 'live'
                or (item.get('coverage') or {}).get('availability') != 'available'
                or data.get('observation_unavailable') or data.get('text_omitted')
                or data.get('snapshot_complete') is False
                or data.get('snapshot_truncated') is True):
            continue
        identity = snapshot_identity(item)
        if (not all(isinstance(value,str) and value for value in (*identity[:2],*identity[3:]))
                or identity[2] is not None and not isinstance(identity[2],str)):
            continue
        ref, count, stamp = item.get('evidence_id'), data.get('row_count'), _snapshot_time(item)
        if not isinstance(ref,str) or not ref or type(count) is not int or count < 0 or stamp is None:
            continue
        groups.setdefault(identity, []).append((stamp,ref,count))
    protected = set()
    for rows in groups.values():
        rows = sorted(set(rows))
        nonempty = [row for row in rows if row[2] > 0]
        if not nonempty:
            continue
        first,last = nonempty[0],nonempty[-1]
        protected.update((first[1],last[1]))
        before = [row for row in rows if row[2] == 0 and row[0] < first[0]]
        after = [row for row in rows if row[2] == 0 and row[0] > last[0]]
        if before: protected.add(before[-1][1])
        if after: protected.add(after[0][1])
    return protected


def refresh_snapshot_chronology(container, *, item_key='evidence', sort=False):
    """Recompute only from the supplied projection; never infer a diagnosis."""
    rows = container.get(item_key, [])
    buckets = {}
    invalid = 0
    for item in rows:
        # A retained history curve is not an empty/nonempty SQL/log snapshot.
        if (str(item.get('source','')).startswith('zabbix.item.numeric:')
                or item.get('source') in ('mysql.statement_digest','mysql.statement_statistics',
                    'mysql.table_io','mysql.file_io','mysql.configuration')): continue
        data = item.get('data') or {}
        if data.get('text_omitted') or data.get('observation_unavailable'): continue
        count = data.get('row_count')
        if type(count) is not int or count < 0: continue
        if not all(isinstance(item.get(k), str) and item[k] for k in ('evidence_id','instance_id','source')): continue
        stamp = _snapshot_time(item)
        if stamp is None:
            invalid += 1
            continue
        # Missing identity stays a separate group; never guess from display names.
        identity = snapshot_identity(item)
        if any(value is not None and not isinstance(value, str) for value in identity): continue
        buckets.setdefault(identity, []).append((stamp, item['evidence_id'], count))
    groups = []
    def reference(sample):
        return {'evidence_id':sample[1], 'observed_at':iso(sample[0]), 'row_count':sample[2]}
    def empty_summary(samples):
        ids = list(dict.fromkeys(row[1] for row in samples))
        return {'count':len(samples), 'evidence_ids':ids[:3], 'refs_omitted':max(0,len(ids)-3)}
    for identity, samples in buckets.items():
        samples = sorted(set(samples))
        nonempty = [row for row in samples if row[2] > 0]
        empty = [row for row in samples if row[2] == 0]
        anchors = {row[0] for row in nonempty}
        first, last = (nonempty[0], nonempty[-1]) if nonempty else (None,None)
        groups.append(dict(zip(('instance_id','source','template','source_id','hostid','itemid'),identity),
            sample_count=len(samples), first_nonempty=reference(first) if first else None,
            last_nonempty=reference(last) if last else None,
            empty_before_first=empty_summary([r for r in empty if first and r[0] < first[0]]),
            empty_after_last=empty_summary([r for r in empty if last and r[0] > last[0]]),
            empty_at_nonempty_time=empty_summary([r for r in empty if r[0] in anchors]),
            empty_between=empty_summary([r for r in empty if first and first[0] < r[0] < last[0] and r[0] not in anchors]),
            empty_without_nonempty_anchor=empty_summary(empty if not nonempty else [])))
    # Spend the small chronology allowance on groups with actual nonempty rows.
    groups.sort(key=lambda g:(g['first_nonempty'] is None,-g['sample_count'],g['instance_id'],g['source'],g['template'] or '',g['source_id'] or '',g['hostid'] or '',g['itemid'] or ''))
    container.pop('snapshot_chronology', None)
    container.pop('snapshot_chronology_omitted', None)
    if groups or invalid:
        container['snapshot_chronology'] = {'scope':'supplied_retained_samples_only',
            'sample_kind':'point_in_time',
            'sample_count_scope':'retained_per_source_group',
            'not_established_by_snapshots':[
                'release_mechanism','continuous_duration','incident_count','numeric_series_population'],
            'groups':groups[:4],
            'groups_omitted':max(0,len(groups)-4), 'invalid_time_count':invalid}
    if sort:
        # Call only after priority selection, so sorting cannot alter retention.
        rows.sort(key=lambda item:(_snapshot_time(item) is None, _snapshot_time(item) or parse_time('9999-01-01T00:00:00Z'),item.get('evidence_id','')))
    return container


def new_series():
    return {'columns':SERIES_COLUMNS,'resources':[],'rows':[],'registered_relationships':[],'summary_scope':'frozen_retained_samples_only','omitted_count':0}


def add_series(series,metric,payload,values):
    item=safe_item(payload.get('item') or {}); provenance=payload.get('provenance') or {}
    resource={'instance_id':metric.instance_id,'source_id':provenance.get('source_id'),'mapping_version':provenance.get('mapping_version'),'config_revision':provenance.get('config_revision')}
    if resource not in series['resources']: series['resources'].append(resource)
    for relation in safe_relations(payload):
        relation=dict(relation,resource=series['resources'].index(resource))
        if relation not in series['registered_relationships']: series['registered_relationships'].append(relation)
    coverage=payload.get('coverage') or {}
    reasons=sorted(set(str(g.get('reason',g.get('reason_code','unknown'))) if isinstance(g,dict) else str(g) for g in [*(payload.get('gaps') or []),*(coverage.get('reason_codes') or [])]))[:3]
    valid=[p for p in payload.get('points',[]) if isinstance(p.get('value'),(int,float)) and p.get('validity','valid')=='valid']
    sampling=coverage.get('sampling') or {}
    series['rows'].append(['manifest:'+metric.id,series['resources'].index(resource),item.get('hostid'),item.get('itemid'),item.get('name','')[:40],item.get('key','')[:56],item.get('units','')[:24],len(values),values[0] if values else None,values[-1] if values else None,min(values) if values else None,max(values) if values else None,len(payload.get('points',[]))-len(values),coverage.get('complete') is True,reasons,valid[-1]['time'] if valid else None,sampling.get('original_point_count',len(payload.get('points',[]))),sampling.get('method','none'),item.get('value_semantics','unknown'),item.get('semantics_source','unverified'),valid[0]['time'] if valid else None])


def compact_coverage(coverage):
    coverage=coverage or {}; reasons=Counter()
    if 'gap_reason_counts' in coverage:
        return {key:deepcopy(value) for key,value in coverage.items() if key in (
            'complete','truncated','manifest_id','initial_evidence_bounded','gaps',
            'gap_reason_counts','gap_count','gap_reasons_omitted','manifest_source_count',
            'duplicate_numeric_projections_omitted')}
    for value in coverage.get('gaps',[]):
        reason=value.get('reason_code',value.get('reason','unknown')) if isinstance(value,dict) else str(value).rsplit(':',1)[-1]
        reasons[str(reason)[:80]]+=1
    result={k:v for k,v in coverage.items() if k in ('complete','truncated','manifest_id','initial_evidence_bounded')}
    result['gaps']=sorted(reasons)[:12]; result['gap_reason_counts']=dict(reasons.most_common(12)); result['gap_count']=sum(reasons.values())
    result['gap_reasons_omitted']=max(0,len(reasons)-12)
    result['manifest_source_count']=len(coverage.get('manifest_sources',[]))
    return result


def context_refs(bundle):
    return {item['evidence_id'] for item in bundle.get('evidence',[])} | {row[0] for row in (bundle.get('numeric_series') or {}).get('rows',[])}


def trim_numeric_summary(bundle):
    """Prefer coverage of distinct dimensions before duplicate trigger windows."""
    series=bundle.get('numeric_series') or {};rows=series.get('rows',[])
    if not rows:return False
    columns=bundle.get('numeric_observation_columns',[])
    index=columns.index('data.numeric_series_ref') if 'data.numeric_series_ref' in columns else None
    observations=[item['numeric_observation'] for item in bundle.get('evidence',[])
                  if isinstance(item.get('numeric_observation'),list)]
    represented={row[index] for row in observations if index is not None and row[index] is not None}
    candidates=[i for i,row in enumerate(rows) if row[0] in represented] or list(range(len(rows)))
    counts=Counter((rows[i][1],rows[i][2]) for i in candidates)
    largest=max(counts,key=counts.get)
    selected=next(i for i in reversed(candidates) if (rows[i][1],rows[i][2])==largest)
    ref=rows.pop(selected)[0]
    for row in observations:
        if index is not None and row[index]==ref:row[index]=None
    for position,name in enumerate(series.get('columns',[])):
        if not name.endswith('_ref'):continue
        key=name[:-4]+'_dictionary';dictionary=series.get(key)
        if not isinstance(dictionary,list):continue
        used=sorted({row[position] for row in rows})
        replacements={old:new for new,old in enumerate(used)}
        series[key]=[dictionary[old] for old in used]
        for row in rows:row[position]=replacements[row[position]]
    series['omitted_count']=series.get('omitted_count',0)+1
    return True


def _encode_numeric_context(initial):
    """Intern repeated field names/times, not observations or measurement values.

    Numeric observations retain their original evidence IDs. SQL/log/lock
    objects deliberately retain their existing, separately scoped projection.
    """
    def flatten(value,prefix=''):
        result={}
        for key,child in value.items():
            path=prefix+key
            if isinstance(child,dict):result.update(flatten(child,path+'.'))
            else:result[path]=child
        return result
    def time_ref(value):
        times=initial.setdefault('numeric_time_dictionary',[])
        if value not in times:times.append(value)
        return times.index(value)
    def intern_values(columns,rows,label):
        dictionaries={}
        for index,key in enumerate(list(columns)):
            if key.endswith('_ref'):continue
            values=[row[index] for row in rows]
            if not values or not all(isinstance(value,str) for value in values):continue
            dictionary=list(dict.fromkeys(values)); refs=[dictionary.index(value) for value in values]
            # Do not replace short labels or unique values with larger aliases.
            before=len(json.dumps(values,ensure_ascii=False))
            after=len(json.dumps(refs))+len(json.dumps({key:dictionary},ensure_ascii=False))+40
            if after>=before:continue
            for row,ref in zip(rows,refs):row[index]=ref
            columns[index]=key+'_ref';dictionaries[key]=dictionary
        if dictionaries:initial[label]=dictionaries
    numeric=[item for item in initial.get('evidence',[])
             if str(item.get('source','')).startswith('zabbix.item.numeric:')
             and (item.get('data') or {}).get('projection_scope')=='retained_samples_subset_not_upstream_gap']
    if numeric:
        flattened=[]
        for item in numeric:
            values=flatten({key:value for key,value in item.items() if key!='evidence_id'})
            for key in ('observed_at','data.first_time','data.last_time'):
                if isinstance(values.get(key),str):values[key+'_ref']=time_ref(values.pop(key))
            points=values.get('data.rows',[])
            values['data.rows']=[]
            for point in points:
                point=dict(point)
                if isinstance(point.get('time'),str):point['time_ref']=time_ref(point.pop('time'))
                values['data.rows'].append(point)
            flattened.append(values)
        columns=sorted({key for value in flattened for key in value})
        by_id={item['evidence_id']:value for item,value in zip(numeric,flattened)}
        encoded=[[by_id[item['evidence_id']].get(key) for key in columns] for item in numeric]
        intern_values(columns,encoded,'numeric_observation_dictionaries')
        initial['numeric_observation_columns']=columns
        by_id=dict(zip((item['evidence_id'] for item in numeric),encoded))
        initial['evidence']=[{'evidence_id':item['evidence_id'],
                             'numeric_observation':by_id[item['evidence_id']]}
                            if item['evidence_id'] in by_id else item for item in initial['evidence']]
    calculations=[]
    for signal in initial.get('inspection_signals',[]):
        if not isinstance(signal.get('calculation'),dict):continue
        # Capacity is a paired, configured risk fact. Keep its operands, concern
        # threshold, state and sample times readable together rather than mixing
        # them into the sparse positional table of unrelated baseline changes.
        # This changes representation only; normal/recovered facts stay normal.
        if signal.get('category')=='capacity_pressure':continue
        values=flatten(signal['calculation'])
        for key in list(values):
            if (key in ('observed_at','episode_started_at','returned_at') or key.endswith(('.from','.to'))) and isinstance(values[key],str):
                values[key+'_ref']=time_ref(values.pop(key))
        calculations.append((signal,values))
    if calculations:
        columns=sorted({key for _,value in calculations for key in value})
        encoded=[[value.get(key) for key in columns] for _,value in calculations]
        intern_values(columns,encoded,'signal_calculation_dictionaries')
        initial['signal_calculation_columns']=columns
        for (signal,_),row in zip(calculations,encoded):signal['calculation']=row


def compact_initial(bundle):
    initial=deepcopy(bundle); initial['coverage']=compact_coverage(initial.get('coverage'))
    series=initial.get('numeric_series') or {}; refs={r[0] for r in series.get('rows',[])}
    # Intern repeated metadata without deleting a metric, changing a number or
    # making partial coverage appear complete. Each *_ref is an explicit index
    # into its dictionary; the frozen manifest itself remains unchanged.
    columns=series.get('columns',[])
    for name in ('reason_codes','sampling_method','semantics_source','first_time','last_time'):
        if name not in columns: continue
        index=columns.index(name); dictionary=[]
        for row in series.get('rows',[]):
            value=row[index]
            if value not in dictionary: dictionary.append(value)
            row[index]=dictionary.index(value)
        if dictionary:
            columns[index]=name+'_ref'
            series[name+'_dictionary']=dictionary
    resources=series.get('resources',[])
    identities={_numeric_identity(resources[r[1]],r[2],r[3]) for r in _sampled_numeric_rows(series)}
    identities.discard(None)
    window={'window_start':initial.get('from'),'window_end':initial.get('to')} if 'from' in initial or 'to' in initial else {}
    original=[compact_numeric_evidence(item,series,**window) for item in initial.get('evidence',[])]
    for item in original:
        if str(item.get('source','')).startswith('zabbix.item.numeric:'):continue
        data=item.get('data') or {};meta=data.get('item')
        if isinstance(meta,dict):
            data['item']={key:value for key,value in meta.items() if key in (
                'source_id','hostid','itemid','name','key','units','value_semantics','semantics_source',
                'interval','interval_verified','history_semantics')}
    trigger_refs={ref for signal in initial.get('inspection_signals',[]) for ref in signal.get('evidence_ids',[])}
    initial['evidence']=[item for item in original if item['evidence_id'] in trigger_refs or (item['evidence_id'] not in refs and not (str(item.get('source','')).startswith('zabbix.item.numeric:') and (item.get('instance_id'),item.get('source'),(item.get('data') or {}).get('item',{}).get('hostid'),(item.get('data') or {}).get('item',{}).get('itemid')) in identities))]
    initial['coverage']['duplicate_numeric_projections_omitted']=initial['coverage'].get('duplicate_numeric_projections_omitted',0)+len(original)-len(initial['evidence'])
    for signal in initial.get('inspection_signals',[]):
        # Persistence keys identify internal settings, not measured mechanisms.
        if isinstance(signal.get('calculation'),dict):
            for key in ('reference_key','source_id','mapping_version','config_revision','calculation'):
                signal['calculation'].pop(key,None)
    _encode_numeric_context(initial)
    return initial


def compact_tool_item(item):
    """A summary is of returned frozen samples; it is never a new observation."""
    result={k:item[k] for k in ('evidence_id','instance_id','template','source','unit','observed_at') if k in item}
    coverage=item.get('coverage') or {}; compact=compact_coverage(coverage)
    result['coverage']={'source_complete':coverage.get('source_complete',coverage.get('complete')) is True,'complete':False,'truncated':True,'gaps':compact.get('gaps',[])[:3],'projection':'bounded_summary'}
    if isinstance(item.get('item'),dict):
        meta=item['item']; result['item']={k:meta[k] for k in ('hostid','itemid','units') if k in meta}
        for k in ('name','key'): result['item'][k]=str(meta.get(k,''))[:40]
    if 'points' in item:
        points=[p for p in item['points'] if isinstance(p,dict)]
        valid=[p for p in points if isinstance(p.get('value'),(int,float)) and not isinstance(p.get('value'),bool)]
        if valid:
            result['summary']={'scope':'frozen_retained_samples','sample_count':len(valid),'first':valid[0]['value'],'last':valid[-1]['value'],'min':min(p['value'] for p in valid),'max':max(p['value'] for p in valid),'last_time':valid[-1]['time'],'missing_count':len(points)-len(valid)}
        result['points_omitted']=len(points)
        if coverage.get('sampling'): result['coverage']['sampling']=coverage['sampling']
    elif isinstance(item.get('summary'),dict):
        result['summary']=item['summary']
        result['points_omitted']=item.get('points_omitted',0)
    elif 'data' in item:
        data=item['data']; result['data']={k:v for k,v in data.items() if k in ('row_count','event_count','observation_unavailable','text_omitted','timer_unit','snapshot_scope','window','values')}
        numeric_subset=(str(item.get('source','')).startswith('zabbix.item.numeric:')
                        and data.get('projection_scope')=='retained_samples_subset_not_upstream_gap')
        if numeric_subset:
            result['data'].update({k:data[k] for k in ('sample_count','first_time','last_time','projection_scope') if k in data})
        if isinstance(data.get('item'),dict):
            metadata=('source_id','hostid','itemid','units','value_semantics','semantics_source') if numeric_subset else ('source_id','hostid','itemid')
            result['data']['item']={k:v for k,v in data['item'].items() if k in metadata and isinstance(v,str)}
        if isinstance(data.get('rows'),list):
            result['data']['rows']=data['rows'][:2]
            result['data']['rows_omitted']=max(0,len(data['rows'])-2)+(data.get('rows_omitted',0) if numeric_subset else 0)
    return result


def bounded_tool_result(result,max_bytes=1600):
    items=[compact_tool_item(item) for item in result.get('items',[])]
    result={'items':[],'coverage':{'complete':False,'truncated':True,'projection':'context_bounded_summary','omitted_item_count':len(items)}}
    for item in items:
        candidate=deepcopy(result)
        candidate['items'].append(item); candidate['coverage']['omitted_item_count']-=1
        refresh_snapshot_chronology(candidate,item_key='items')
        if len(json.dumps(candidate,ensure_ascii=False,separators=(',',':')).encode())>max_bytes and 'snapshot_chronology' in candidate:
            # Preserve actual snapshot evidence before optional derived metadata.
            candidate.pop('snapshot_chronology')
            candidate['snapshot_chronology_omitted']='tool_result_budget'
        if len(json.dumps(candidate,ensure_ascii=False,separators=(',',':')).encode())>max_bytes: continue
        result=candidate
    return result


def prepare_tool_context(result, max_bytes):
    result=deepcopy(result)
    # Tool order is the broker's retention priority, including numeric windows
    # without observed_at. Chronology orders its own samples, never this list.
    refresh_snapshot_chronology(result,item_key='items')
    if len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode()) > max_bytes:
        result=bounded_tool_result(result,max_bytes)
    return result


def _unpack_final_tables(initial):
    """Decode only our explicit model tables before deriving another projection."""
    result=deepcopy(initial); times=result.get('numeric_time_dictionary',[])
    def fields(columns,values,dictionaries):
        decoded={}
        for name,value in zip(columns,values):
            key=name[:-4] if name.endswith('_ref') else name
            if name.endswith('_ref') and key in dictionaries:
                value=dictionaries[key][value]; name=key
            elif name.endswith('_ref') and type(value) is int:
                value=times[value]; name=key
            target=decoded
            parts=name.split('.')
            for part in parts[:-1]:target=target.setdefault(part,{})
            target[parts[-1]]=value
        return decoded
    for index,item in enumerate(result.get('evidence',[])):
        if not isinstance(item.get('numeric_observation'),list):continue
        decoded=fields(result.get('numeric_observation_columns',[]),item['numeric_observation'],
                       result.get('numeric_observation_dictionaries',{}))
        decoded['evidence_id']=item['evidence_id']
        for point in (decoded.get('data') or {}).get('rows',[]):
            if 'time_ref' in point:point['time']=times[point.pop('time_ref')]
        result['evidence'][index]=decoded
    for signal in result.get('inspection_signals',[]):
        if isinstance(signal.get('calculation'),list):
            signal['calculation']=fields(result.get('signal_calculation_columns',[]),signal['calculation'],
                                         result.get('signal_calculation_dictionaries',{}))
    for key in ('numeric_time_dictionary','numeric_observation_columns','numeric_observation_dictionaries',
                'signal_calculation_columns','signal_calculation_dictionaries'):result.pop(key,None)
    return result


def compact_final_context(initial, *, max_bytes, measure, protected_evidence_ids=()):
    """Pack frozen evidence against the caller's complete final-request budget.

    Keep all supplied signal/required references. Reduce display detail before
    removing independent unprotected dimensions. If the protected context alone
    cannot fit, return it over budget for the caller's existing fail-closed guard.
    The callback must be pure and include the complete assistant/tool group.
    """
    original=deepcopy(initial)
    original.setdefault('coverage',{})['retained_evidence_count']=len(original.get('evidence',[]))
    refresh_snapshot_chronology(original,sort=True)
    if measure(original)<=max_bytes:return original
    working=_unpack_final_tables(original)
    protected=set(protected_evidence_ids) | snapshot_boundary_refs(working.get('evidence',[]))
    protected.update(ref for signal in working.get('inspection_signals',[])
                     for ref in signal.get('evidence_ids',[]))
    # Preserve actual source families and their nonempty/latest snapshot anchors,
    # including later empty evidence needed for truthful chronology.
    layers={}
    for item in working.get('evidence',[]):
        if str(item.get('source','')).startswith('zabbix.item.numeric:'):continue
        data=item.get('data') or {};meta=data.get('item') or {}
        if data.get('observation_unavailable') or data.get('text_omitted'):continue
        identity=snapshot_identity(item)
        layers.setdefault(identity,[]).append(item)
        if item.get('selection_reason')=='nonempty_lock_snapshot':protected.add(item['evidence_id'])
    for items in layers.values():
        valid=[item for item in items if _snapshot_time(item) is not None]
        ordered=sorted(valid or items,key=lambda item:(_snapshot_time(item) or parse_time('9999-01-01T00:00:00Z'),item['evidence_id']))
        protected.add(ordered[-1]['evidence_id'])
        nonempty=[item for item in ordered if (item.get('data') or {}).get('row_count',0)>0
                  or (item.get('data') or {}).get('event_count',0)>0]
        if nonempty:protected.update((nonempty[0]['evidence_id'],nonempty[-1]['evidence_id']))

    def packed():
        result=deepcopy(working)
        series=result.get('numeric_series') or {}
        for position,name in enumerate(series.get('columns',[])):
            if not name.endswith('_ref'):continue
            key=name[:-4]+'_dictionary';dictionary=series.get(key)
            if not isinstance(dictionary,list):continue
            used=sorted({row[position] for row in series.get('rows',[])})
            replacements={old:new for new,old in enumerate(used)}
            series[key]=[dictionary[old] for old in used]
            for row in series.get('rows',[]):row[position]=replacements[row[position]]
        if isinstance(series.get('rows'),list) and not series['rows']:
            # Empty tables have no row that can use their decoding schema.
            # Keep resource identity, relationships and omission/scope metadata.
            series.pop('columns',None)
            for key in list(series):
                if key.endswith('_dictionary') and series[key]==[]:
                    series.pop(key)
        _encode_numeric_context(result)
        result.setdefault('coverage',{})['retained_evidence_count']=len(result.get('evidence',[]))
        refresh_snapshot_chronology(result,sort=True)
        return result
    def thin_points(limit):
        for item in working.get('evidence',[]):
            data=item.get('data') or {}
            if (not str(item.get('source','')).startswith('zabbix.item.numeric:')
                    or data.get('projection_scope')!='retained_samples_subset_not_upstream_gap'):continue
            rows=data.get('rows')
            if isinstance(rows,list) and len(rows)>limit:
                retained=rows[-1:] if limit==1 else [rows[0],rows[-1]]
                data['rows_omitted']=data.get('rows_omitted',0)+len(rows)-len(retained)
                data['rows']=retained
            meta=data.get('item') or {}
            for name in ('interval','interval_verified','history_semantics'):meta.pop(name,None)
    def omit_series_columns(names):
        series=working.get('numeric_series') or {};columns=series.get('columns',[])
        positions=[i for i,name in enumerate(columns) if name.removesuffix('_ref') in names]
        if not positions:return
        omitted=series.setdefault('projection_columns_omitted',[])
        for i in positions:
            if columns[i] not in omitted:omitted.append(columns[i])
            if columns[i].endswith('_ref'):series.pop(columns[i][:-4]+'_dictionary',None)
        series['columns']=[name for i,name in enumerate(columns) if i not in positions]
        series['rows']=[[value for i,value in enumerate(row) if i not in positions] for row in series.get('rows',[])]

    thin_points(2)
    # Snapshot display labels and polling settings are redundant in a final
    # bounded request. Keep source-qualified identity and interpretation fields
    # before sacrificing any independent measured dimension or actual lock edge.
    for item in working.get('evidence',[]):
        if str(item.get('source','')).startswith('zabbix.item.numeric:'):continue
        meta=(item.get('data') or {}).get('item')
        if isinstance(meta,dict):
            for name in ('name','key','interval','interval_verified','history_semantics'):
                meta.pop(name,None)
    result=packed()
    if measure(result)<=max_bytes:return result
    omit_series_columns({'key','sampling_method'})
    for item in working.get('evidence',[]):
        data=item.get('data') or {}
        if (str(item.get('source','')).startswith('zabbix.item.numeric:')
                and data.get('projection_scope')=='retained_samples_subset_not_upstream_gap'):
            # IDs, human labels, units and verified semantics remain. Scheduling
            # keys and absent cross-projection aliases are redundant display data.
            (data.get('item') or {}).pop('key',None)
            if data.get('numeric_series_ref') is None:data.pop('numeric_series_ref',None)
    result=packed()
    if measure(result)<=max_bytes:return result
    thin_points(1)
    result=packed()
    if measure(result)<=max_bytes:return result
    # Latest measured values, true sample counts/ranges, source gaps and semantics
    # remain. Missing display statistics are explicit columns, never invented zeroes.
    omit_series_columns({'first','min','max'})
    result=packed()
    if measure(result)<=max_bytes:return result
    while measure(result)>max_bytes:
        series=working.get('numeric_series') or {};rows=series.get('rows',[])
        candidates=[i for i,row in enumerate(rows) if row[0] not in protected]
        if candidates:
            columns=series.get('columns',[])
            def features(index):
                row=rows[index];values={('resource',row[1]),('host',row[1],row[2])}
                for field in ('value_semantics',):
                    name=field if field in columns else field+'_ref'
                    if name not in columns:continue
                    value=row[columns.index(name)]
                    if name.endswith('_ref'):value=series[field+'_dictionary'][value]
                    if isinstance(value,str):values.add((field,row[1],value))
                return values
            # Balance the unprotected normal candidates themselves. Protected
            # triggers on one host must not exhaust another host's normal data.
            observed={i:features(i) for i in candidates}
            counts=Counter(feature for values in observed.values() for feature in values)
            index=min(candidates,key=lambda i:(sum(counts[f]==1 for f in observed[i]),
                                               -sum(counts[f] for f in observed[i]),-i))
            removed=rows.pop(index)[0]
            series['omitted_count']=series.get('omitted_count',0)+1
            series['omission_reason']='remaining_total_context_budget'
            for item in working.get('evidence',[]):
                data=item.get('data') or {}
                if data.get('numeric_series_ref')==removed:data['numeric_series_ref']=None
        else:
            candidates=[i for i,item in enumerate(working.get('evidence',[])) if item['evidence_id'] not in protected]
            if not candidates:break
            working['evidence'].pop(candidates[-1])
            coverage=working.setdefault('coverage',{})
            coverage['context_omitted_evidence_count']=coverage.get('context_omitted_evidence_count',0)+1
        result=packed()
    return result
