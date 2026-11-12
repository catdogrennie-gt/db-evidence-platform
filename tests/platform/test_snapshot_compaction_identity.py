"""Compression cannot turn another source's empty sample into recovery."""
from copy import deepcopy
import json

import pytest
from datetime import timedelta
from fastapi import HTTPException

from platform_app import models as m
from platform_app.evidence import create_manifest
from platform_app.investigations import _bundle
from platform_app.model_context import compact_initial, compact_tool_item, refresh_snapshot_chronology
from platform_app.tools import project_evidence
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_snapshot_source_identity import observation


@pytest.mark.parametrize('path', ['initial', 'tool'])
def test_compaction_preserves_source_qualified_snapshot_identity_and_originals(path):
    active = project_evidence(observation('active-a', 'source-a'))
    other = project_evidence(observation('empty-b', 'source-b', count=0))
    other['observed_at'] = '2026-11-12T18:01:00Z'
    source = {'evidence': [active, other], 'coverage': {'complete': True}}
    before = deepcopy(source)
    if path == 'initial':
        result = compact_initial(source)
    else:
        result = {'evidence': [compact_tool_item(item) for item in source['evidence']]}
    refresh_snapshot_chronology(result)
    groups = result['snapshot_chronology']['groups']
    assert len(groups) == 2, 'Independent source IDs must survive every compaction path'
    positive = next(group for group in groups if group['first_nonempty'])
    assert positive['source_id'] == 'source-a'
    assert positive['empty_after_last']['count'] == 0
    assert {item['data']['item']['source_id'] for item in result['evidence']} == {'source-a', 'source-b'}
    assert {item['evidence_id'] for item in result['evidence']} == {'active-a', 'empty-b'}
    assert result['evidence'][0]['observed_at'] == active['observed_at']
    assert source == before
    assert 'PRIVATE-PASSWORD' not in json.dumps(result) and 'PRIVATE-KEY' not in json.dumps(result)


def test_bundle_local_allowance_cannot_silently_erase_protected_nonempty_sources(ai_env):
    e = ai_env
    e['db'].add(m.InvestigationResource(investigation_id=e['investigation'].id, instance_id=e['a'].id))
    expected = set()
    for index in range(25):
        row = observation('independent-anchor-' + str(index), 'source-' + str(index))
        row.instance_id = e['a'].id
        row.run_id = e['evidence'].run_id
        row.observed_at = e['start'] + timedelta(seconds=index + 1)
        e['db'].add(row)
        expected.add(row.id)
    e['db'].flush()
    inv = e['investigation']
    manifest = create_manifest(e['db'], 'investigation', inv.id, [e['a'].id], inv.window_start, inv.window_end)
    try:
        bundle, *_ = _bundle(e['db'], inv, manifest)
    except HTTPException as error:
        assert error.status_code == 409
        assert error.detail['code'] == 'model_token_budget_exhausted'
    else:
        actual = {item['evidence_id'] for item in bundle['evidence']
                  if item.get('selection_reason') == 'nonempty_lock_snapshot'}
        assert expected <= actual, 'Return protected anchors for the hard request guard, or fail closed'
