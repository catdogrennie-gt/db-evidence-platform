"""Shared, cheap report authority labels. Never derive authority from raw prose.

Only the server's full grounding projection can supply a verified point headline.
List callers intentionally leave facts unevaluated instead of reading private
audits or frozen payloads for each card. This is not a grounding cache.
"""


def report_presentation(investigation, *, grounding=None):
    origin = {'rule': 'rules', 'live_model': 'model'}.get(investigation.conclusion_type, 'unknown')
    complete = investigation.status in ('completed', 'partial', 'succeeded')
    pending = investigation.status in ('queued', 'running', 'preparing', 'analyzing', 'pending', 'collecting_evidence')
    result = {'version': 'report-presentation-v1', 'origin': origin,
        'primary_basis': 'analysis_record', 'primary_headline': '历史分析记录',
        'fact_assessment': 'not_evaluated',
        'model_narrative_verification': 'not_applicable' if origin == 'rules' else 'unverified',
        'interpretation_label': {'rules': '规则巡检结果', 'model': 'AI 未核实解释',
            'unknown': '原始报告（来源未核实）'}[origin]}
    if not complete:
        result['primary_headline'] = '分析尚未完成' if pending else '分析未成功完成'
        return result
    if origin == 'rules':
        result.update(primary_basis='rule_result', primary_headline='规则巡检结果',
            fact_assessment='not_applicable')
        return result
    if grounding is None:
        return result
    result['fact_assessment'] = 'unverified'
    if (origin == 'model' and grounding.get('version') == 'report-grounding-v1'
            and grounding.get('status') == 'point_facts_only' and grounding.get('admitted_facts')):
        result['fact_assessment'] = 'point_facts_only'
        # Mixed identities deliberately have no whole-report factual headline.
        if isinstance(grounding.get('verified_headline'), str) and grounding['verified_headline']:
            result.update(primary_basis='historical_source_points',
                primary_headline=grounding['verified_headline'])
    return result
