"""Pin an open reporting contract, without pretending to verify prose semantics.

Strict XFAILs are intentional unimplemented acceptance cases (FACT-ADMISSION01).
Run with --runxfail to reproduce RED. No provider or live service is used.
"""
from copy import deepcopy

import pytest
from fastapi import HTTPException

from platform_app.investigations import PROMPT_VERSION, investigation_detail, validate_report
from platform_app.model_context import refresh_snapshot_chronology
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_showcase import showcase_env
from tests.platform.test_frozen_rule_facts import frozen_capacity
from tests.platform.test_report_observation_bounds import snapshots
from tests.platform.test_report_summary import summary
from tests.platform.test_snapshot_chronology import snap


OVERCLAIM = '窗口内出现一次短暂记录锁等待，随后自行解除。'
CAUTIOUS = '无法确认是否短暂、发生一次或自行解除，只能确认保留的采样状态。'


class MissingGroundingContract(AssertionError):
    """Only this exact missing feature is an expected failure."""


def candidate(ref, statement=OVERCLAIM):
    body = {'facts': [{'statement': '某时点的锁快照含等待行。', 'evidence_ids': [ref]}],
            'hypotheses': [], 'refutations': [], 'unknowns': [],
            'next_readonly_checks': [], 'executive_summary': summary(ref)}
    body['executive_summary']['headline'] = statement
    for field in ('phenomenon', 'conclusion'):
        body['executive_summary'][field].update(status='observed', statement=statement)
    return body


def test_valid_shape_and_citations_do_not_prove_temporal_truth():
    body = candidate('nonempty-snapshot')
    accepted = validate_report(body, {'nonempty-snapshot'}, expected_version=PROMPT_VERSION)
    assert accepted['executive_summary']['conclusion']['statement'] == OVERCLAIM
    assert accepted['executive_summary']['conclusion']['status'] == 'observed'
    # This is model-authored status, not independent semantic verification.
    context = {'evidence': [snap('nonempty-snapshot', '2026-11-13T06:53:47Z', 1),
                            snap('later-empty', '2026-11-13T06:54:07Z', 0)]}
    refresh_snapshot_chronology(context)
    assert {'continuous_duration', 'incident_count', 'release_mechanism'} <= set(
        context['snapshot_chronology']['not_established_by_snapshots'])


def test_reference_guard_remains_effective_independently_of_the_open_contract():
    with pytest.raises(HTTPException) as error:
        validate_report(candidate('foreign-snapshot'), {'authorized-snapshot'},
                        expected_version=PROMPT_VERSION)
    assert error.value.detail['code'] == 'unverified_evidence_citation'


def persist_legacy(e, statement):
    report = validate_report(candidate(e['used'].id, statement), {e['used'].id},
                             expected_version=PROMPT_VERSION)
    e['investigation'].report = deepcopy(report)
    e['db'].commit()
    detail = investigation_detail(e['reader'], e['investigation'], include_audit=False)
    e['db'].expire(e['investigation'], ['report'])
    assert detail['report'] == report == e['investigation'].report
    return detail


def test_persisted_frozen_detail_reproducer_has_real_authorized_observations(snapshots):
    detail = persist_legacy(snapshots, OVERCLAIM)
    assert detail['observation_bounds']['groups'][0]['sample_count'] == 2
    assert detail['observation_bounds']['groups'][0]['nonempty_sample_count'] == 1
    assert detail['observation_bounds']['groups'][0]['gap_to_next_empty_seconds'] == .5
    assert detail['report']['executive_summary']['conclusion']['status'] == 'observed'


@pytest.mark.xfail(strict=True, raises=MissingGroundingContract,
                   reason='FACT-ADMISSION01: delivery-bound report grounding is not implemented')
@pytest.mark.parametrize('wording', [OVERCLAIM, CAUTIOUS])
def test_legacy_prose_cannot_gain_verified_authority_without_a_delivery_receipt(snapshots, wording):
    detail = persist_legacy(snapshots, wording)
    # Both overclaim and its negation stay unverified: this is not a lexical detector.
    if 'report_grounding' not in detail:
        raise MissingGroundingContract('Missing server-owned, receipt-qualified grounding contract')
    grounding = detail['report_grounding']
    assert grounding['status'] == 'unverified'
    assert 'actual_delivery_receipt_missing' in grounding['reason_codes']
    assert grounding['model_narrative_verification'] == 'unverified'
    assert grounding['admitted_facts'] == []
    assert grounding['verified_headline'] is None
