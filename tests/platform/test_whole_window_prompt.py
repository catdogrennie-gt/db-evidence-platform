"""Prompt-policy regression, not an assertion that an LLM diagnoses correctly."""
import pytest

from platform_app.investigations import _system_prompt_for_sources


@pytest.mark.parametrize('sources,context', [
    (set(), None),
    ({'zabbix.item.numeric:authorized'}, {'numeric_series': {'rows': []}}),
    ({'mysql.lock_waits'}, {}),
    ({'mysql.lock_waits', 'zabbix.item.numeric:authorized'}, {}),
    ({'mysql.lock_waits', 'zabbix.item.numeric:authorized'},
     {'signal_calculation_columns': ['observed_at']}),
])
def test_source_qualified_prompt_does_not_prioritize_the_end_over_supplied_history(sources, context):
    prompt = _system_prompt_for_sources(sources, context=context)
    # This precise conflicting directive was in the actual failed AI32 request.
    assert '当前判断以窗口末端实际观测和触发线索为中心' not in prompt
    # The general scope contract must precede diagnostic/source-specific detail.
    first = prompt.index('判断须覆盖整个冻结窗口')
    assert first < prompt.index('综合同窗业务')
    for requirement in ('历史现象、末端观测与覆盖限制', '历史高值不当作当前故障',
                        '变化不自动定性故障'):
        assert requirement in prompt
    # Replacing a temporal priority must not weaken existing safety boundaries.
    for guard in ('只能读取冻结平台观测', 'epoch未验证不推断增量',
                  '缺失不是零', '原本裸UUID也原样复制'):
        assert guard in prompt


def test_normal_headline_does_not_silently_mean_only_the_latest_sample_is_normal():
    prompt = _system_prompt_for_sources({'zabbix.item.numeric:authorized'})
    assert '未发现问题则写正常巡检结论' not in prompt
    assert '全窗未见有据问题才写未见明确问题' in prompt
    assert '根因' in prompt  # Existing hypothesis/unknown distinctions remain.
