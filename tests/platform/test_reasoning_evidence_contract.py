"""Generic request delivery contracts; these do not certify model reasoning."""
import copy
import json

import pytest

from tests.platform.test_ai_scope import ai_env
from tests.platform.test_ai_failures import prepared_run, provider_response


@pytest.mark.parametrize('sources', [
    {'zabbix.item.numeric:measured'}, {'mysql.lock_waits'},
])
def test_reasoning_contract_distinguishes_counts_partial_normality_and_attribution(sources):
    from platform_app.investigations import _system_prompt_for_sources

    prompt = _system_prompt_for_sources(sources)
    for rule in ('累计计数增长不等于流量上升', '单位时间rate',
                 '连接数/线程正常不代表数据库全部正常',
                 '同窗各层耗时', '所有实际变化线索与正常反证',
                 '现象、候选瓶颈层和具体根因',
                 '缺SQL/会话映射不妨碍', '不得断言具体SQL/锁'):
        assert rule in prompt
    for boundary in ('只分析本次授权冻结历史窗口', 'epoch未验证不推断增量',
                     '原本裸UUID也原样复制', 'requires_human_approval=true'):
        assert boundary in prompt


def test_reasoning_prompt_revision_adds_no_numeric_request_bytes_or_scenario_answers():
    from platform_app.investigations import PROMPT_VERSION, _system_prompt_for_sources

    prompt = _system_prompt_for_sources({'zabbix.item.numeric:measured'})
    assert PROMPT_VERSION == 'evidence-investigation-v4.3'
    # Measured v3.9 source-selected prompt size, not an increased hard budget.
    assert len(prompt.encode()) <= 8753
    assert all(key not in prompt for key in (
        'pool-pressure', 'temporary-work', 'application-queue', 'retained-view', 'stale-replica'))


def test_configured_capacity_breach_is_distinct_from_a_hard_limit_or_proven_cause():
    from platform_app.investigations import _system_prompt_for_sources
    prompt=_system_prompt_for_sources({'zabbix.item.numeric:measured'})
    for rule in ('capacity_pressure为配置水位超限事实', '未达硬上限或低活跃度不能否认容量风险',
                 '用量/容量/占比/余量', '不证明宕机或根因'):
        assert rule in prompt


def test_report_headline_names_the_supported_problem_instead_of_the_analysis_task():
    from platform_app.investigations import _system_prompt_for_sources
    prompt = _system_prompt_for_sources({'zabbix.item.numeric:measured'})
    assert '直接写具体问题或风险' in prompt
    assert '不用“分析”“待关联”作标题' in prompt
    assert '未发现问题则写正常巡检结论' in prompt


def test_direct_impacts_can_be_observed_without_claiming_a_confirmed_cause():
    from platform_app.investigations import _system_prompt_for_sources

    prompt = _system_prompt_for_sources({'zabbix.item.numeric:measured'})
    assert '有直接证据可明确已观察到的业务影响/故障现象' in prompt
    assert '相关性或单项变化不足以确证故障' in prompt
    assert '候选机制不得冒充已证实的具体根因' in prompt
    assert '不给置信百分比' in prompt
    assert '禁止声称已证明根因、确认故障' not in prompt


@pytest.mark.usefixtures('legacy_collection_compatibility')
def test_forced_final_request_keeps_all_measured_triggers_and_normal_cross_host_evidence(ai_env, monkeypatch):
    from platform_app.investigations import run_investigation
    from platform_app.model_context import context_refs
    from tests.platform.test_context_dimension_coverage import seed_dimensions, signals
    from tests.platform.test_intelligence import policy_env

    env = ai_env
    refs = seed_dimensions(env, monkeypatch)
    policy = policy_env(env)
    policy.mode = 'live_redacted'
    policy.external_confirmed = True
    env['db'].commit()
    _, _, lease, config = prepared_run(env, inspection={
        'policy_id': policy.id, 'version': policy.version, 'signals': signals(env, refs)})
    config.model['max_calls'] = 2
    calls = []

    def bound(payload):
        return len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()) + 512

    def provider(config, payload, timeout):
        calls.append(copy.deepcopy(payload))
        if len(calls) == 1:
            return {'choices': [{'message': {'tool_calls': [{
                'id': 'frozen-measurement-read', 'type': 'function',
                'function': {'name': 'get_evidence', 'arguments': json.dumps({
                    'instance_id': env['a'].id, 'evidence_ids': [refs[0]]})}}]}}],
                'usage': {'prompt_tokens': 10000, 'completion_tokens': 200}}
        return provider_response(refs[0])

    result = run_investigation(env['factory'], lease, config, model_client=provider)
    assert len(calls) == 2, result.report
    assert set(refs).issubset(context_refs(json.loads(calls[0]['messages'][1]['content'])))
    final = calls[-1]
    context = json.loads(final['messages'][1]['content'])
    assert set(refs).issubset(context_refs(context)), 'Budget compaction lost an actual changed dimension'
    assert {ref for signal in context['inspection_signals']
            for ref in signal['evidence_ids']} == set(refs)
    assert all(bound(payload) <= 24000 for payload in calls)
    assert final['tools'] == [] and final['tool_choice'] == 'none'
    assert 10200 + bound(final) + final['max_tokens'] <= 32000
    assert final['max_tokens'] == 2000
    assert any(message.get('tool_call_id') == 'frozen-measurement-read'
               for message in final['messages'] if message['role'] == 'tool')
    assert any(any(call['id'] == 'frozen-measurement-read' for call in message.get('tool_calls', []))
               for message in final['messages'] if message['role'] == 'assistant')
    series = context['numeric_series']
    columns = series['columns']
    def value(row, name):
        if name in columns:
            return row[columns.index(name)]
        return series[name + '_dictionary'][row[columns.index(name + '_ref')]]

    normal = [row for row in series['rows'] if int(value(row, 'itemid')) >= 706]
    assert len(normal) >= 2, 'Normal counterevidence must not disappear before redundant metadata'
    assert {value(row, 'hostid') for row in normal} == {'101', '102'}
    assert result.status == 'completed', result.report
