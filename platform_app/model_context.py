"""Compact, citation-preserving model context derived only from frozen values."""
from collections import Counter
from copy import deepcopy
import json
from .common import iso, parse_time, PlatformError
from .zabbix_items import safe_item,safe_relations

SERIES_COLUMNS=['evidence_id','resource','hostid','itemid','name','key','unit','sample_count','first','last','min','max','missing_count','complete','reason_codes','last_time','original_point_count','sampling_method','value_semantics','semantics_source']


def _snapshot_time(item):
    try:
        value = item.get('observed_at')
        if not isinstance(value, str): return None
        return parse_time(value)
    except (PlatformError, AttributeError, ValueError, TypeError):
        return None


def refresh_snapshot_chronology(container, *, item_key='evidence', sort=False):
    """Recompute only from the supplied projection; never infer a diagnosis."""
    rows = container.get(item_key, [])
    buckets = {}
    invalid = 0
    for item in rows:
        data = item.get('data') or {}
        if data.get('text_omitted') or data.get('observation_unavailable'): continue
        count = data.get('row_count')
        if type(count) is not int or count < 0: continue
        if not all(isinstance(item.get(k), str) and item[k] for k in ('evidence_id','instance_id','source')): continue
        stamp = _snapshot_time(item)
        if stamp is None:
            invalid += 1
            continue
        meta = data.get('item') or {}
        # Missing identity stays a separate group; never guess from display names.
        template = item.get('template', data.get('metric'))
        identity = (item['instance_id'], item['source'], template, meta.get('hostid'), meta.get('itemid'))
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
        groups.append(dict(zip(('instance_id','source','template','hostid','itemid'),identity),
            sample_count=len(samples), first_nonempty=reference(first) if first else None,
            last_nonempty=reference(last) if last else None,
            empty_before_first=empty_summary([r for r in empty if first and r[0] < first[0]]),
            empty_after_last=empty_summary([r for r in empty if last and r[0] > last[0]]),
            empty_at_nonempty_time=empty_summary([r for r in empty if r[0] in anchors]),
            empty_between=empty_summary([r for r in empty if first and first[0] < r[0] < last[0] and r[0] not in anchors]),
            empty_without_nonempty_anchor=empty_summary(empty if not nonempty else [])))
    # Spend the small chronology allowance on groups with actual nonempty rows.
    groups.sort(key=lambda g:(g['first_nonempty'] is None,-g['sample_count'],g['instance_id'],g['source'],g['template'] or '',g['hostid'] or '',g['itemid'] or ''))
    container.pop('snapshot_chronology', None)
    container.pop('snapshot_chronology_omitted', None)
    if groups or invalid:
        container['snapshot_chronology'] = {'scope':'supplied_retained_samples_only', 'groups':groups[:4],
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
    series['rows'].append(['manifest:'+metric.id,series['resources'].index(resource),item.get('hostid'),item.get('itemid'),item.get('name','')[:40],item.get('key','')[:56],item.get('units','')[:24],len(values),values[0] if values else None,values[-1] if values else None,min(values) if values else None,max(values) if values else None,len(payload.get('points',[]))-len(values),coverage.get('complete') is True,reasons,valid[-1]['time'] if valid else None,sampling.get('original_point_count',len(payload.get('points',[]))),sampling.get('method','none'),item.get('value_semantics','unknown'),item.get('semantics_source','unverified')])


def compact_coverage(coverage):
    coverage=coverage or {}; reasons=Counter()
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


def compact_initial(bundle):
    initial=deepcopy(bundle); initial['coverage']=compact_coverage(initial.get('coverage'))
    series=initial.get('numeric_series') or {}; refs={r[0] for r in series.get('rows',[])}
    # Intern repeated metadata without deleting a metric, changing a number or
    # making partial coverage appear complete. Each *_ref is an explicit index
    # into its dictionary; the frozen manifest itself remains unchanged.
    columns=series.get('columns',[])
    for name in ('reason_codes','sampling_method','semantics_source'):
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
    identities={(resources[r[1]]['instance_id'],r[2],r[3]) for r in series.get('rows',[])}
    original=initial.get('evidence',[])
    trigger_refs={ref for signal in initial.get('inspection_signals',[]) for ref in signal.get('evidence_ids',[])}
    initial['evidence']=[item for item in original if item['evidence_id'] in trigger_refs or (item['evidence_id'] not in refs and not (str(item.get('source','')).startswith('zabbix.item.numeric:') and (item.get('instance_id'),(item.get('data') or {}).get('item',{}).get('hostid'),(item.get('data') or {}).get('item',{}).get('itemid')) in identities))]
    initial['coverage']['duplicate_numeric_projections_omitted']=len(original)-len(initial['evidence'])
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
        if isinstance(data.get('item'),dict):
            result['data']['item']={k:v for k,v in data['item'].items() if k in ('hostid','itemid') and isinstance(v,str)}
        if isinstance(data.get('rows'),list): result['data']['rows']=data['rows'][:2]; result['data']['rows_omitted']=max(0,len(data['rows'])-2)
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
