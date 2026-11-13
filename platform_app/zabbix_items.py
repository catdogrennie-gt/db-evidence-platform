"""Typed Zabbix item metadata and bounded history; no display-name inference."""
from datetime import datetime, timezone, timedelta
import math
import re
import json
from .common import PlatformError, iso

ITEM_OUTPUT = ['itemid','hostid','name','key_','units','value_type','description','status','state','error','lastclock','delay','type','master_itemid']
TAG_NAMES = {'component','application','service','layer','environment','role','source','semantics','scope'}


def numeric_semantics(row):
    """Explicit tags or Zabbix's rate transform; never infer from display names."""
    declared={t.get('value') for t in row.get('tags',[]) if isinstance(t,dict) and t.get('tag')=='semantics'}
    derived=any(str(p.get('type'))=='10' for p in row.get('preprocessing',[]) if isinstance(p,dict))
    if len(declared)>1 or declared and (not declared.issubset({'gauge','rate','counter'}) or derived and declared!={'rate'}):
        return 'unknown','conflicting_semantics'
    if derived: return 'rate','zabbix_change_per_second'
    if declared: return next(iter(declared)),'zabbix_tag'
    return 'unknown','unverified'


def value_kind(value_type):
    return {0:'numeric',3:'numeric',1:'text',4:'text',2:'log'}.get(int(value_type),'unsupported')


def tag_list(rows):
    return [{'tag':str(r.get('tag',''))[:255], 'value':str(r.get('value',''))[:1024]} for r in (rows or [])[:100] if isinstance(r,dict)]


def matches_tags(tags, filters):
    for f in filters or []:
        values=[t['value'] for t in tags if t['tag']==f['tag']]
        op=f.get('operator','equals')
        if not any(op=='exists' or (f.get('value','') in v if op=='contains' else v==f.get('value','')) for v in values): return False
    return True


def _metadata_base(row, host, by_id):
    from .monitoring import sampling_interval
    interval, verified=sampling_interval(row,by_id)
    key=str(row.get('key_',''))[:2048]
    tags=tag_list(row.get('tags'))
    enhancement={'sl.mysql.lock_snapshot':'mysql.lock_waits','dbops.mysql.lock_snapshot':'mysql.lock_waits','sl.mysql.sql_summary':'mysql.statement_digest','dbops.mysql.statement_digest_snapshot':'mysql.statement_digest'}.get(key)
    from .zabbix_diagnostics import definition
    diagnostic=definition(row)
    if diagnostic: enhancement=diagnostic['source']
    return {'hostid':str(row['hostid']),'host_name':str(host.get('name') or host.get('host') or host['hostid'])[:255], 'host_tags':tag_list(host.get('tags')), 'itemid':str(row['itemid']), 'name':str(row.get('name') or key)[:1024], 'key':key, 'units':str(row.get('units',''))[:255], 'unit':str(row.get('units',''))[:255], 'value_type':int(row.get('value_type',0)), 'value_kind':value_kind(row.get('value_type',0)), 'description':str(row.get('description',''))[:4096], 'tags':tags, 'status':str(row.get('status','0')), 'state':str(row.get('state','0')), 'error':str(row.get('error',''))[:1024], 'lastclock':str(row.get('lastclock','0')), 'interval':interval, 'interval_verified':verified, 'available':str(row.get('status','0'))=='0' and str(row.get('state','0'))=='0', 'selectable':True, 'enhancement':enhancement, 'kind':'metric' if value_kind(row.get('value_type',0))=='numeric' else 'snapshot', 'collector':'monitoring'}


def metadata(row,host,by_id):
    item=_metadata_base(row,host,by_id)
    from .zabbix_diagnostics import definition
    diagnostic=definition(row)
    if diagnostic:item['diagnostic_query']=diagnostic['query']
    item['value_semantics'],item['semantics_source']=numeric_semantics(row)
    item['poll_interval']=item['interval']; item['history_semantics']='periodic_samples'; item['heartbeat_seconds']=None
    preprocessing=[p for p in row.get('preprocessing',[]) if isinstance(p,dict) and str(p.get('type')) in ('19','20')]
    if preprocessing:
        item['history_semantics']='sparse_on_change'
        heartbeat=next((str(p.get('params','')).strip() for p in preprocessing if str(p.get('type'))=='20'),None)
        match=re.fullmatch(r'(\d+)([smhd]?)',heartbeat or '')
        seconds=int(match[1])*{'':1,'s':1,'m':60,'h':3600,'d':86400}[match[2]] if match else 0
        item['heartbeat_seconds']=seconds or None
        item['interval']=seconds or item['interval']; item['interval_verified']=bool(seconds)
    from .monitoring import KEYS
    base=item['key'].split('[',1)[0]
    item['metric_semantic']=KEYS.get(base)[0] if base in KEYS and not (base=='system.cpu.util' and item['key']!=base) else None
    return item


def catalog(client, source, hostids=None, itemids=None):
    params={'output':['hostid','host','name'],'selectTags':'extend','limit':1001}
    if source.allowed_host_group_ids: params['groupids']=source.allowed_host_group_ids
    if hostids: params['hostids']=list(hostids)
    hosts=client.call('host.get',params)
    if not isinstance(hosts,list): raise PlatformError(503,'invalid_monitoring_response','上游主机返回格式无效。')
    if len(hosts)>1000: raise PlatformError(422,'discovery_truncated','来源超过1000主机，请限制来源或选择具体主机。')
    hosts={str(h['hostid']):h for h in hosts if isinstance(h,dict) and 'hostid' in h and (not hostids or str(h['hostid']) in hostids)}
    if not hosts: return [],[]
    params={'hostids':list(hosts),'output':ITEM_OUTPUT,'selectTags':'extend','selectPreprocessing':'extend','limit':10001}
    if itemids: params['itemids']=list(itemids)
    rows=client.call('item.get',params)
    if not isinstance(rows,list): raise PlatformError(503,'invalid_monitoring_response','上游监控项返回格式无效。')
    if len(rows)>10000: raise PlatformError(422,'discovery_truncated','来源超过10000监控项，请先选择主机。')
    by_id={str(r.get('itemid')):r for r in rows if isinstance(r,dict)}
    # Fetch dependent masters as metadata only, never as selected history.
    masters={str(r.get('master_itemid')) for r in rows if isinstance(r,dict) and str(r.get('master_itemid','0')) not in ('0','None','')}-by_id.keys()
    if masters:
        for row in client.call('item.get',{'hostids':list(hosts),'itemids':sorted(masters),'output':ITEM_OUTPUT,'selectTags':'extend','limit':1001}):
            if isinstance(row,dict): by_id[str(row.get('itemid'))]=row
    items=[metadata(r,hosts[str(r['hostid'])],by_id) for r in rows if isinstance(r,dict) and str(r.get('hostid')) in hosts and (not itemids or str(r.get('itemid')) in itemids)]
    return sorted(items,key=lambda i:(i['hostid'],i['itemid'])),list(hosts.values())


def typed_history(client,item,start,end,limit=1000,*,latest=False):
    if start>=end or end-start>timedelta(days=14): raise PlatformError(422,'invalid_metric_window','指标窗口最多14天且必须递增。')
    if item['value_kind']=='unsupported': return [],{'complete':False,'truncated':False,'reason_codes':['unsupported_value_type']}
    if not item['available']: return [],{'complete':False,'truncated':False,'reason_codes':['item_unavailable']}
    raw=client.call('history.get',{'history':item['value_type'],'itemids':[item['itemid']],'time_from':int(start.replace(tzinfo=timezone.utc).timestamp()),'time_till':math.ceil(end.replace(tzinfo=timezone.utc).timestamp())-1,'sortfield':['clock','ns'],'sortorder':'DESC' if latest else 'ASC','limit':limit+1})
    if not isinstance(raw,list): raise PlatformError(503,'invalid_monitoring_response','上游历史返回格式无效。')
    rows=[]; reasons=[]; size=0
    if item.get('history_semantics')=='sparse_on_change': reasons.append('sparse_history_no_value_change_or_missing')
    if len(raw)>limit: reasons.append('history_truncated')
    for r in raw[:limit]:
        try:
            clock=int(r['clock']); ns=int(r.get('ns',0)); stamp=datetime.fromtimestamp(clock+ns/1e9,timezone.utc).replace(tzinfo=None)
            if not start<=stamp<end: continue
            value=r['value']; invalid=False
            if item['value_kind']=='numeric':
                try: value=int(value) if item['value_type']==3 else float(value); invalid=not math.isfinite(value)
                except (TypeError,ValueError,OverflowError): invalid=True
                if invalid: value=None; reasons.append('invalid_value')
            else:
                value=str(value)
                value_limit=64000 if item.get('enhancement') else 4096
                if len(value)>value_limit: value=value[:value_limit]; reasons.append('value_truncated')
            row={'time':iso(stamp),'clock':str(clock),'ns':str(ns),'value':value,'validity':'invalid_value' if invalid else 'valid'}
            if item['value_kind']=='numeric': row['raw_value']=str(r['value'])[:128]
            if item['value_kind']=='log': row.update({k:str(r[k])[:255] for k in ('timestamp','source','severity','logeventid') if k in r})
            size+=len(json.dumps(row,ensure_ascii=False).encode())
            if size>128000: reasons.append('history_byte_budget'); break
            rows.append(row)
        except (TypeError,ValueError,KeyError,OverflowError,OSError): reasons.append('invalid_history_row')
    rows.sort(key=lambda r:(int(r['clock']),int(r['ns'])))
    if not rows: reasons.append('no_samples')
    return rows,{'complete':bool(rows) and not reasons,'truncated':any(r in reasons for r in ('history_truncated','history_byte_budget','value_truncated')),'reason_codes':sorted(set(reasons))}


def safe_text(value,limit=160):
    """Conservative metadata vocabulary; SQL/URLs/credentials and PII are omitted."""
    text=str(value or '')
    if re.search(r"@|https?://|\b(?:token|password|passwd|secret|authorization|bearer)\b|api[_ -]?key|\b(?:sk-|AKIA|eyJ)[A-Za-z0-9_-]+|[A-Za-z0-9+/_=-]{32,}|\d{10,}|['\"`]|\b(?:select|insert|update|delete)\s",text,re.I): return '[metadata omitted]'
    return re.sub(r'[^\w\s.:%/()\-\[\],+]', '',text,flags=re.U)[:limit]


def safe_item(item):
    # Keys may embed DSNs/credentials as parameters. Identity is kept separately.
    result={k:item[k] for k in ('hostid','itemid','value_type','value_kind','interval','interval_verified','history_semantics','heartbeat_seconds','poll_interval','metric_semantic','value_semantics','semantics_source') if k in item}
    for k in ('name','host_name','units'): result[k]=safe_text(item.get(k),160)
    result['description_omitted']=True
    result['key']=safe_text(str(item.get('key','')).split('[',1)[0])
    result['tags']=[{'tag':t['tag'],'value':safe_text(t['value'],80)} for t in tag_list(item.get('tags')) if t['tag'] in TAG_NAMES]
    result['metadata_policy']='bounded_vocabulary_no_raw_text_v1'
    return result


def safe_relations(payload):
    result=[]
    for relation in (payload.get('registered_relationships') or [])[:32]:
        if not isinstance(relation,dict) or relation.get('provenance')!='user_registered': continue
        kind=relation.get('relationship_kind')
        if kind not in ('runs_on','depends_on','connects_to','related_to'): continue
        if not all(re.fullmatch(r'[0-9]{1,20}',str(relation.get(k,''))) for k in ('from_hostid','to_hostid')): continue
        result.append({'from_hostid':str(relation['from_hostid']),'to_hostid':str(relation['to_hostid']),'kind':kind,'provenance':'user_registered','causality':'unverified'})
    return result
