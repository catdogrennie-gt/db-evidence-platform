import copy
import pytest
from fastapi import HTTPException
from platform_app.investigations import PROMPT_VERSION, validate_report

VERSION = PROMPT_VERSION

def summary(evidence_id='real'):
    claim = {'status': 'observed', 'statement': '窗口内观测到锁等待', 'evidence_ids': [evidence_id], 'counterevidence_ids': [], 'unknowns': []}
    return {'headline': '观测到锁等待，联合原因待确认', 'phenomenon': copy.deepcopy(claim), 'surface_causes': [copy.deepcopy(claim)], 'joint_root_cause': {'status': 'insufficient_evidence', 'statement': '缺少事务来源，无法确认联合原因', 'evidence_ids': [], 'counterevidence_ids': [], 'unknowns': ['事务来源未知']}, 'conclusion': copy.deepcopy(claim), 'recommended_actions': [{'kind': 'readonly_check', 'description': '核对冻结锁快照', 'evidence_ids': [evidence_id], 'risk': 'low', 'requires_human_approval': False, 'prerequisites': [], 'verification': []}]}

def report():
    return {'facts': [], 'hypotheses': [], 'refutations': [], 'unknowns': [], 'next_readonly_checks': [], 'executive_summary': summary()}

def test_new_live_requires_summary_and_legacy_remains_valid():
    old = report(); old.pop('executive_summary')
    assert 'executive_summary' not in validate_report(old, {'real'})
    with pytest.raises(HTTPException):
        validate_report(old, {'real'}, expected_version=VERSION)
    assert validate_report(report(), {'real'}, expected_version=VERSION)['executive_summary'] == summary()

@pytest.mark.parametrize('section,field', [(section,field) for section in ['phenomenon','surface_causes','joint_root_cause','conclusion'] for field in ['evidence_ids','counterevidence_ids']] + [('recommended_actions','evidence_ids')])
def test_nested_references_are_frozen(section, field):
    value = report(); node = value['executive_summary'][section]
    if isinstance(node, list): node = node[0]
    node[field] = ['outside-frozen-scope']
    with pytest.raises(HTTPException) as error:
        validate_report(value, {'real'}, expected_version=VERSION)
    assert error.value.detail['code'] == 'unverified_evidence_citation'

@pytest.mark.parametrize('status', ['observed', 'supported_hypothesis', 'refuted', 'confirmed'])
def test_claim_requires_evidence_and_has_no_confirmed_status(status):
    value = report(); value['executive_summary']['phenomenon'].update(status=status, evidence_ids=[])
    with pytest.raises(HTTPException): validate_report(value, {'real'}, expected_version=VERSION)

@pytest.mark.parametrize('changes', [dict(requires_human_approval=False), dict(prerequisites=[]), dict(verification=[])])
def test_manual_change_needs_approval_conditions_and_verification(changes):
    value = report(); action = value['executive_summary']['recommended_actions'][0]
    action.update(kind='manual_change', requires_human_approval=True, prerequisites=['人工核对受影响事务'], verification=['重新检查锁快照']); action.update(changes)
    with pytest.raises(HTTPException): validate_report(value, {'real'}, expected_version=VERSION)

@pytest.mark.parametrize('section,key,value', [
    ('summary','headline','x'*101), ('summary','surface_causes',[summary()['phenomenon']]*4),
    ('summary','recommended_actions',summary()['recommended_actions']*5), ('summary','extra','no'),
    ('claim','statement','x'*501), ('claim','statement',''), ('claim','evidence_ids',['real']*13),
    ('claim','counterevidence_ids',['real']*13), ('claim','unknowns',['x']*4), ('claim','extra','no'),
    ('action','description','x'*301), ('action','description',''), ('action','evidence_ids',['real']*13),
    ('action','prerequisites',['x']*4), ('action','verification',['x']*4), ('action','extra','no'),
])
def test_strict_summary_bounds(section,key,value):
    data=report(); node=data['executive_summary']
    if section=='claim': node=node['phenomenon']
    if section=='action': node=node['recommended_actions'][0]
    node[key]=value
    with pytest.raises(HTTPException): validate_report(data, {'real'}, expected_version=VERSION)

def test_real_failure_string_claim_shape_stays_rejected():
    """Sanitized shape of the failed live response, with no original prose/IDs."""
    from pydantic import ValidationError
    from platform_app.investigations import Report
    value=report()
    for field in ['phenomenon','joint_root_cause','conclusion']:
        value['executive_summary'][field]='Sanitized model explanation'
    with pytest.raises(ValidationError) as error:
        Report.model_validate(value)
    assert [(e['loc'],e['type']) for e in error.value.errors()] == [
        (('executive_summary','phenomenon'),'model_type'),
        (('executive_summary','joint_root_cause'),'model_type'),
        (('executive_summary','conclusion'),'model_type'),
    ]
    with pytest.raises(HTTPException):
        validate_report(value,{'real'},expected_version=VERSION)


def test_live_prompt_example_binds_all_summary_claims_to_objects():
    """The previous prompt only typed surface_causes, leaving three ambiguous."""
    import json
    from platform_app.investigations import SYSTEM_PROMPT
    marker='EXECUTIVE_SUMMARY_JSON_EXAMPLE='
    assert marker in SYSTEM_PROMPT
    example=json.loads(SYSTEM_PROMPT.split(marker,1)[1])
    for field in ['phenomenon','joint_root_cause','conclusion']:
        assert isinstance(example[field],dict)
        assert set(example[field])=={'status','statement','evidence_ids','counterevidence_ids','unknowns'}
    value=report();value['executive_summary']=example
    validate_report(value,{'<provided-reference>'},expected_version=VERSION)

@pytest.mark.parametrize('guard', [
    '输入按异常优先排列，不代表时间先后',
    'Uptime正增长不能证明重启',
    'data_lock_waits不等于metadata_locks',
    '缺少session/transaction到digest的显式映射',
])
def test_prompt_contains_semantic_limits_from_live_review(guard):
    """Prompt delivery regression only; no assertion of model semantic accuracy."""
    from platform_app.investigations import SYSTEM_PROMPT
    assert guard in SYSTEM_PROMPT
    assert '恢复后的空锁快照只说明后来时刻' not in SYSTEM_PROMPT

@pytest.mark.parametrize('guard', [
    'LOCK_TYPE/LOCK_MODE来自data_locks关联',
    'SQL文本需threads与events_statements关联',
    '有主机数值时必须按resource/host评估支持、反证或证据不足',
    '时刻必须显式UTC或Z',
])
def test_prompt_declares_lock_join_boundaries_and_host_evaluation(guard):
    from platform_app.investigations import SYSTEM_PROMPT
    assert guard in SYSTEM_PROMPT
