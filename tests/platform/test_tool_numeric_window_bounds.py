"""Tool evidence rows obey the requested half-open frozen window."""
import copy
import json
from datetime import timedelta, timezone

import pytest

from platform_app.common import iso, parse_time
from platform_app.model_context import bounded_tool_result, prepare_tool_context
from platform_app.tools import ToolBroker, project_evidence
from tests.platform.test_ai_scope import ai_env


def numeric_evidence(env, points):
    from platform_app.models import CollectionRun

    row = env['evidence']
    env['db'].get(CollectionRun, row.run_id).collector = 'monitoring'
    row.source = 'zabbix.item.numeric:frozen-history'
    row.observed_at = env['start'] + timedelta(minutes=3)
    row.payload = {'item': {'hostid':'101', 'itemid':'701', 'name':'Measured latency',
        'key':'measured.latency', 'units':'ms', 'value_kind':'numeric', 'value_type':0,
        'value_semantics':'gauge', 'semantics_source':'zabbix_tag', 'interval':5,
        'interval_verified':True, 'history_semantics':'periodic_samples'},
        'rows':[{'time':iso(stamp), 'value':value} for stamp, value in points]}
    row.coverage = {'availability':'available', 'complete':False,
                    'gaps':['history_truncated']}
    env['db'].commit()
    return row


def call_evidence(env, row, **arguments):
    tool = ToolBroker(env['actor'], env['investigation'], [env['a'].id],
        env['start'], env['end'], allowed_evidence_ids={row.id},
        allowed_tools={'get_evidence'}, live=True, max_result_bytes=2000)
    result = tool.call('get_evidence', {'instance_id':env['a'].id,
                                      'evidence_ids':[row.id], **arguments})
    return prepare_tool_context(result, tool.max_result_bytes)


@pytest.mark.parametrize('narrow', [False, True])
def test_get_evidence_clips_numeric_rows_before_model_counts_and_sampling(ai_env, narrow):
    start, end = ai_env['start'], ai_env['end']
    points = [(start-timedelta(seconds=1),900), (start,1),
        (start+timedelta(minutes=2),2), (start+timedelta(minutes=3),3),
        (start+timedelta(minutes=4),4), (end-timedelta(seconds=1),5),
        (end,800), (end+timedelta(seconds=1),700)]
    row = numeric_evidence(ai_env, points)
    before = copy.deepcopy((row.payload, row.coverage, row.payload_sha256))
    lower, upper = (start+timedelta(minutes=2), start+timedelta(minutes=4)) if narrow else (start,end)
    args = {'from':iso(lower.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8)))),
            'to':iso(upper)} if narrow else {}
    result = call_evidence(ai_env, row, **args)
    assert len(result['items']) == 1
    item = result['items'][0]
    expected = [{'time':iso(stamp), 'value':float(value)}
                for stamp,value in points if lower <= stamp < upper]
    retained = expected[:1]+expected[-3:] if len(expected)>4 else expected
    assert item['data']['rows'] == retained
    assert item['data']['sample_count'] == len(expected)
    assert item['data']['row_count'] == len(expected)
    assert item['data']['first_time'] == expected[0]['time']
    assert item['data']['last_time'] == expected[-1]['time']
    assert item['data']['rows_omitted'] == max(0,len(expected)-4)
    assert all(lower <= parse_time(p['time']) < upper for p in item['data']['rows'])
    assert item['evidence_id'] == row.id
    assert item['data']['item']['hostid'] == '101'
    assert item['data']['item']['units'] == 'ms'
    assert item['data']['item']['value_semantics'] == 'gauge'
    assert item['coverage']['gaps'] == ['history_truncated']
    ai_env['db'].expire(row)
    assert (row.payload,row.coverage,row.payload_sha256) == before
    assert len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode()) <= 2000


def test_get_evidence_100_history_points_count_after_clipping_before_display_selection(ai_env):
    start = ai_env['start']
    row = numeric_evidence(ai_env, [(start+timedelta(seconds=i-10),i) for i in range(100)])
    item = call_evidence(ai_env,row)['items'][0]
    assert item['data']['sample_count'] == item['data']['row_count'] == 90
    assert [p['value'] for p in item['data']['rows']] == [10,97,98,99]
    assert item['data']['rows_omitted'] == 86
    assert item['data']['first_time'] == iso(start)
    assert item['data']['last_time'] == iso(start+timedelta(seconds=89))


def test_get_evidence_empty_numeric_window_is_unknown_without_inventing_upstream_gap(ai_env):
    row = numeric_evidence(ai_env, [(ai_env['start']-timedelta(seconds=1),900),
        (ai_env['end'],800), (ai_env['end']+timedelta(seconds=1),700)])
    before = copy.deepcopy((row.payload,row.coverage,row.payload_sha256))
    item = call_evidence(ai_env,row)['items'][0]
    assert item['data']['rows'] == []
    assert item['data']['sample_count'] == item['data']['row_count'] == 0
    assert item['data']['observation_unavailable'] is True
    assert item['data']['first_time'] is None and item['data']['last_time'] is None
    assert item['coverage']['complete'] is False
    assert item['coverage']['availability'] == 'unknown'
    assert item['coverage']['gaps'] == ['history_truncated']
    assert (row.payload,row.coverage,row.payload_sha256) == before


def test_numeric_tool_budget_recompaction_preserves_true_counts_and_is_idempotent(ai_env):
    start = ai_env['start']
    row = numeric_evidence(ai_env, [(start+timedelta(seconds=i-10),i) for i in range(100)])
    original = call_evidence(ai_env,row)
    before = copy.deepcopy(original)
    result = bounded_tool_result(original,1000)
    assert len(result['items']) == 1
    data = result['items'][0]['data']
    assert data['sample_count'] == data['row_count'] == 90
    assert data['first_time'] == iso(start)
    assert data['last_time'] == iso(start+timedelta(seconds=89))
    assert data['projection_scope'] == 'retained_samples_subset_not_upstream_gap'
    assert data['item']['units'] == 'ms'
    assert data['item']['value_semantics'] == 'gauge'
    assert data['item']['semantics_source'] == 'zabbix_tag'
    assert [p['value'] for p in data['rows']] == [10,97]
    assert data['rows_omitted'] == 88
    assert bounded_tool_result(result,1000) == result
    assert original == before
    assert len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode()) <= 1000
    assert bounded_tool_result(original,300)['items'] == []


def test_get_evidence_nonnumeric_projection_and_permissions_stay_unchanged(ai_env):
    from fastapi import HTTPException

    row = ai_env['evidence']
    row.coverage = {'availability':'available','complete':True,'gaps':[]}
    ai_env['db'].commit()
    assert call_evidence(ai_env,row)['items'][0] == project_evidence(row)
    tool = ToolBroker(ai_env['actor'],ai_env['investigation'],[ai_env['a'].id],
        ai_env['start'],ai_env['end'],allowed_evidence_ids={row.id},
        allowed_tools={'get_evidence'},live=True,max_result_bytes=2000)
    for name,args in [('get_evidence',{'evidence_ids':[ai_env['late'].id]}),
        ('get_evidence',{'from':iso(ai_env['start']-timedelta(seconds=1))}),
        ('get_lock_graph',{})]:
        with pytest.raises(HTTPException):
            tool.call(name,{'instance_id':ai_env['a'].id,**args})
    ai_env['db'].delete(ai_env['grant'])
    ai_env['db'].commit()
    with pytest.raises(HTTPException):
        call_evidence(ai_env,row)
