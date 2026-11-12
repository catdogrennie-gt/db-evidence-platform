"""Repeated projection metadata must not displace actual measured metrics."""
import copy
import json
from datetime import timedelta

from tests.platform.test_ai_scope import ai_env

from platform_app.model_context import compact_initial, SERIES_COLUMNS


def test_compact_numeric_metadata_is_lossless_and_does_not_mutate_frozen_projection():
    rows = [
        [f'manifest:{i}', 0, str(101+i%2), str(50000+i), 'Observed metric '+str(i),
         'application.metric_'+str(i), 'ms', 400, 5, 20, 3, 24, 2, False,
         ['external_history_partial', 'missing_samples', 'source_window_point_budget'],
         '2026-11-12T08:51:31Z', 720, 'original_point_bucket_minmax', 'gauge', 'zabbix_tag',
         '2026-11-12T08:01:31Z']
        for i in range(32)
    ]
    bundle = {'coverage': {}, 'evidence': [], 'numeric_series': {
        'columns': list(SERIES_COLUMNS), 'rows': rows,
        'resources': [{'instance_id':'scope-1'}], 'omitted_count':0}}
    before = copy.deepcopy(bundle)
    initial = compact_initial(bundle)
    series = initial['numeric_series']
    assert bundle == before
    assert len(series['rows']) == 32 and series['omitted_count'] == 0
    assert len(json.dumps(series)) < len(json.dumps(before['numeric_series'])) - 2500
    for original, encoded in zip(rows, series['rows']):
        values = dict(zip(series['columns'], encoded))
        assert len(encoded)==len(series['columns'])==len(original)
        for field in ('reason_codes', 'sampling_method', 'semantics_source','first_time','last_time'):
            index = values.pop(field+'_ref')
            assert type(index) is int and index >= 0
            values[field] = series[field+'_dictionary'][index]
        assert values == dict(zip(SERIES_COLUMNS, original))


def test_compaction_can_read_an_already_compacted_projection_without_encoding_twice():
    bundle={'coverage':{},'evidence':[], 'numeric_series':{
        'columns':list(SERIES_COLUMNS), 'resources':[{'instance_id':'scope-1'}],
        'rows':[['manifest:one',0,'101','501','Metric','key','ms',3,1,2,1,2,0,True,[],
                 '2026-11-12T08:51:31Z',3,'none','gauge','zabbix_tag','2026-11-12T08:41:31Z']], 'omitted_count':0}}
    result=compact_initial(bundle)
    assert compact_initial(result)['numeric_series']==result['numeric_series']


def test_numeric_snapshots_do_not_starve_available_statement_context(ai_env):
    from platform_app.common import iso
    from platform_app import models as m
    from platform_app.investigations import _bundle, create_investigation
    from platform_app.evidence import create_manifest
    e=ai_env; db=e['db']; original=e['evidence']
    original.source='mysql.statement_digest'
    original.payload={'rows':[{'DIGEST':'a'*64,'COUNT_STAR':12,'SUM_TIMER_WAIT':12000}]}
    for i in range(24):
        db.add(m.Evidence(instance_id=e['a'].id,run_id=original.run_id,
            source='zabbix.item.numeric:'+str(i), observed_at=original.observed_at+timedelta(seconds=i+1),
            payload_sha256='0'*64,source_mode='live',coverage={'availability':'available'},
            payload={'item':{'hostid':'101','itemid':str(i),'value_type':0,'value_kind':'numeric',
                'name':'Actual measured numeric item '+str(i),'key':'application.measured_'+str(i),
                'units':'ms'}, 'rows':[{'time':iso(original.observed_at),'value':i+1}]}))
    db.commit()
    inv,_=create_investigation(e['actor'],[e['a'].id],e['start'],e['end'],'Compare measured context',mode='rules_only')
    db.flush()
    manifest=create_manifest(db,'investigation',inv.id,[e['a'].id],e['start'],e['end'])
    inv.manifest_id=manifest.id;db.flush()
    bundle,*_=_bundle(db,inv,manifest)
    assert any(item['source']=='mysql.statement_digest' and item['data']['row_count']==1
               for item in bundle['evidence'])
