"""Sanitized regressions from truncated JSON and misclassified SQL metrics."""
import json

import pytest

from platform_app.common import iso
from platform_app.evidence import digest
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_ai_failures import prepared_run, provider_response


def numeric_evidence(env):
    row = env['evidence']
    row.source = 'zabbix.item.numeric:measured'
    row.payload = {'item': {'hostid': '101', 'itemid': '51543',
        'name': 'Database query latency', 'key': 'db.query_ms', 'value_kind': 'numeric',
        'value_type': 0, 'units': 'ms', 'value_semantics': 'gauge',
        'semantics_source': 'zabbix_tag', 'tags': []},
        'rows': [{'time': iso(row.observed_at), 'value': 12}]}
    row.payload_sha256 = digest(row.payload)
    env['db'].commit()
    return row.id


def response(evidence_id, *, finding=None, finish_reason='stop'):
    value = provider_response(evidence_id)
    body = json.loads(value['choices'][0]['message']['content'])
    body['facts'][0]['statement'] = 'A supplied numeric observation was retained'
    for name in ['phenomenon', 'conclusion']:
        body['executive_summary'][name]['statement'] = 'The retained observation requires review'
    body['executive_summary']['headline'] = 'Observed values require review'
    body['executive_summary']['surface_causes'] = []
    body['executive_summary']['recommended_actions'] = []
    body['findings'] = [finding] if finding else []
    value['choices'][0]['finish_reason'] = finish_reason
    value['choices'][0]['message']['content'] = json.dumps(body)
    return value


@pytest.mark.parametrize('configured,expected', [(4000, 4000), (50000, 4096), (3000, 3000), (0, 1)])
def test_configured_output_limit_is_effective_but_remains_bounded(ai_env, configured, expected):
    from platform_app.investigations import run_investigation
    ref = numeric_evidence(ai_env)
    _, _, lease, config = prepared_run(ai_env)
    config.model['max_tokens'] = configured
    captured = []

    def provider(config, payload, timeout):
        captured.append(payload)
        return response(ref)

    result = run_investigation(ai_env['factory'], lease, config, model_client=provider)
    assert result.status == 'completed' and len(captured) == 1, result.report
    payload = captured[0]
    assert payload['max_tokens'] == expected
    input_bound = len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()) + 512
    assert input_bound <= 24000 and input_bound + payload['max_tokens'] <= 32000


def test_non_lock_request_omits_irrelevant_lock_rules_without_assuming_no_locks(ai_env):
    from platform_app.investigations import run_investigation
    ref = numeric_evidence(ai_env)
    _, _, lease, config = prepared_run(ai_env)  # Question deliberately mentions locks.
    captured = []

    def provider(config, payload, timeout):
        captured.append(payload['messages'][0]['content'])
        return response(ref)

    result = run_investigation(ai_env['factory'], lease, config, model_client=provider)
    assert result.status == 'completed' and len(captured) == 1, result.report
    prompt = captured[0]
    assert 'REQUESTING_ENGINE_LOCK_ID' not in prompt
    assert 'PROCESSLIST_ID不能直接当作P_S.THREAD_ID' not in prompt
    assert '缺少锁证据不等于没有锁问题' in prompt
    assert '只分析本次授权冻结历史窗口' in prompt
    assert 'requires_human_approval=true' in prompt


def test_real_lock_sources_keep_role_duration_identity_and_chronology_rules():
    from platform_app.investigations import _system_prompt_for_sources
    for source in ['mysql.lock_waits', 'mysql.transactions', 'mysql.sessions']:
        prompt = _system_prompt_for_sources({source})
        for guard in ['REQUESTING_ENGINE_LOCK_ID关联的LOCK_MODE属于请求侧',
                      'data_locks/data_lock_waits没有持锁开始时间或持锁时长',
                      'PROCESSLIST_ID不能直接当作P_S.THREAD_ID',
                      'empty_after_last.count=0']:
            assert guard in prompt


def test_prompt_explains_numeric_sql_clues_are_correlation_not_sql_digest_findings():
    from platform_app.investigations import _system_prompt_for_sources
    prompt = _system_prompt_for_sources({'zabbix.item.numeric:measured'})
    assert "category='sql'仅限实际digest" in prompt
    assert "category='correlation'、dimension.kind='metric'" in prompt
    assert '真实itemid' in prompt
    assert '缺digest时禁止虚构digest' in prompt


def test_truncated_length_response_stays_failed_without_json_repair(ai_env):
    from platform_app.investigations import run_investigation
    ref = numeric_evidence(ai_env)
    _, _, lease, config = prepared_run(ai_env)

    def provider(*_):
        value = response(ref, finish_reason='length')
        value['choices'][0]['message']['content'] = '{"facts":[{"statement":"cut off'
        value['usage']['completion_tokens'] = 3000
        return value

    result = run_investigation(ai_env['factory'], lease, config, model_client=provider)
    assert result.status == 'failed' and result.report['error_code'] == 'invalid_model_response'
    assert 'facts' not in result.report and 'executive_summary' not in result.report


@pytest.mark.parametrize('category,expected', [('sql', 'failed'), ('correlation', 'completed')])
def test_sql_metric_finding_failure_is_not_silently_relabelled(ai_env, category, expected):
    from platform_app.investigations import run_investigation
    ref = numeric_evidence(ai_env)
    _, _, lease, config = prepared_run(ai_env)
    finding = {'intent': 'review_issue', 'category': category, 'instance_id': ai_env['a'].id,
               'dimension': {'kind': 'metric', 'value': '51543'},
               'statement': 'Measured query latency warrants human review', 'evidence_ids': [ref]}
    result = run_investigation(ai_env['factory'], lease, config,
                              model_client=lambda *_: response(ref, finding=finding))
    assert result.status == expected, result.report
    if expected == 'failed':
        assert result.report['error_code'] == 'invalid_model_report'
        assert 'findings' not in result.report
    else:
        assert result.report['findings'][0]['category'] == 'correlation'
        assert result.report['findings'][0]['dimension'] == {'kind': 'metric', 'value': '51543'}
