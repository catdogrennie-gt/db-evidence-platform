"""Compact, citation-preserving model context derived only from frozen values."""
from collections import Counter
from copy import deepcopy
import json
from .zabbix_items import safe_item,safe_relations

SERIES_COLUMNS=['evidence_id','resource','hostid','itemid','name','key','unit','sample_count','first','last','min','max','missing_count','complete','reason_codes','last_time','original_point_count','sampling_method']


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
    series['rows'].append(['manifest:'+metric.id,series['resources'].index(resource),item.get('hostid'),item.get('itemid'),item.get('name','')[:40],item.get('key','')[:56],item.get('units','')[:24],len(values),values[0] if values else None,values[-1] if values else None,min(values) if values else None,max(values) if values else None,len(payload.get('points',[]))-len(values),coverage.get('complete') is True,reasons,valid[-1]['time'] if valid else None,sampling.get('original_point_count',len(payload.get('points',[]))),sampling.get('method','none')])


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
    resources=series.get('resources',[])
    identities={(resources[r[1]]['instance_id'],r[2],r[3]) for r in series.get('rows',[])}
    original=initial.get('evidence',[])
    initial['evidence']=[item for item in original if item['evidence_id'] not in refs and not (str(item.get('source','')).startswith('zabbix.item.numeric:') and (item.get('instance_id'),(item.get('data') or {}).get('item',{}).get('hostid'),(item.get('data') or {}).get('item',{}).get('itemid')) in identities)]
    initial['coverage']['duplicate_numeric_projections_omitted']=len(original)-len(initial['evidence'])
    return initial


def compact_tool_item(item):
    """A summary is of returned frozen samples; it is never a new observation."""
    result={k:item[k] for k in ('evidence_id','instance_id','template','source','unit') if k in item}
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
        if isinstance(data.get('rows'),list): result['data']['rows']=data['rows'][:2]; result['data']['rows_omitted']=max(0,len(data['rows'])-2)
    return result


def bounded_tool_result(result,max_bytes=1600):
    items=[compact_tool_item(item) for item in result.get('items',[])]
    result={'items':[],'coverage':{'complete':False,'truncated':True,'projection':'context_bounded_summary','omitted_item_count':len(items)}}
    for item in items:
        if len(json.dumps(dict(result,items=result['items']+[item]),ensure_ascii=False,separators=(',',':')).encode())>max_bytes: continue
        result['items'].append(item); result['coverage']['omitted_item_count']-=1
    return result
