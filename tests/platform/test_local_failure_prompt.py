"""Policy regression only; real diagnosis remains a separate acceptance."""
import pytest
from platform_app.investigations import _system_prompt_for_sources

@pytest.mark.parametrize('sources,context',[(set(),None),({'zabbix.item.numeric:authorized'},{}),({'mysql.lock_waits'},{}),({'mysql.lock_waits','zabbix.item.numeric:authorized'},{}),({'zabbix.item.numeric:authorized'},{'signal_calculation_columns':['observed_at']})])
def test_local_failure_is_not_erased_by_unaffected_business_or_unknown_cause(sources,context):
    prompt=_system_prompt_for_sources(sources,context=context)
    for guard in ('业务正常不抵消局部失败','缺根因不否认现象','单项负载变化不自动确证故障','相关性不证明因果','只能读取冻结平台观测','全窗未见有据问题才写未见明确问题','变化不自动定性故障'):
        assert guard in prompt
    paragraph=prompt.split('surface_causes最多2个Claim，recommended_actions最多2个Action。',1)[1].split('\n',1)[0]
    assert len(paragraph.encode())<=303
