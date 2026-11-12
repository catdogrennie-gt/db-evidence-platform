"""Model-visible metric references round-trip through the scoped read tool."""
from datetime import timedelta

import pytest
from fastapi import HTTPException

from tests.platform.test_ai_scope import ai_env


@pytest.fixture
def frozen_metric_scope(ai_env):
    from platform_app.common import iso
    from platform_app.models import EvidenceManifest, ManifestItem
    from platform_app.tools import ToolBroker

    env = ai_env
    db, inv = env['db'], env['investigation']

    def manifest(owner):
        row = EvidenceManifest(owner_type='investigation', owner_id=owner,
            instance_ids=[env['a'].id], window_start=env['start'],
            window_end=env['end'], manifest_sha256='0' * 64,
            retain_until=env['end'] + timedelta(days=1))
        db.add(row)
        db.flush()
        return row

    own, other = manifest(inv.id), manifest('separate-investigation')
    inv.manifest_id = own.id

    def metric(owner, instance):
        row = ManifestItem(manifest_id=owner.id, instance_id=instance,
            item_type='prometheus_window', payload_sha256='1' * 64,
            payload={'template': 'connections', 'owner_collector': 'identity_status',
                'private_host': 'NOT-FOR-MODEL', 'points': [
                    {'time': iso(env['start'] + timedelta(seconds=1)), 'value': 7,
                     'validity': 'valid'},
                    {'time': iso(env['end'] + timedelta(seconds=1)), 'value': 999,
                     'validity': 'valid'}]})
        db.add(row)
        db.flush()
        return row

    selected = metric(own, env['a'].id)
    wrong_instance = metric(own, env['b'].id)
    wrong_manifest = metric(other, env['a'].id)
    db.commit()
    broker = ToolBroker(env['actor'], inv, [env['a'].id], env['start'], env['end'],
                        allowed_evidence_ids=[])
    return env, broker, selected, wrong_instance, wrong_manifest


@pytest.mark.parametrize('prefixed', [True, False])
def test_metric_reference_returned_to_model_can_be_used_for_exact_read(frozen_metric_scope, prefixed):
    env, broker, selected, _, _ = frozen_metric_scope
    visible_ref = 'manifest:' + selected.id
    result = broker.call('get_metric_window', {'instance_id': env['a'].id,
        'manifest_item_ids': [visible_ref if prefixed else selected.id]})
    assert [item['evidence_id'] for item in result['items']] == [visible_ref]
    assert [p['value'] for p in result['items'][0]['points']] == [7]
    assert 'NOT-FOR-MODEL' not in str(result)


@pytest.mark.parametrize('which', ['instance', 'manifest'])
def test_prefixed_reference_stays_bound_to_manifest_and_instance(frozen_metric_scope, which):
    env, broker, _, wrong_instance, wrong_manifest = frozen_metric_scope
    selected = wrong_instance if which == 'instance' else wrong_manifest
    with pytest.raises(HTTPException) as exc:
        broker.call('get_metric_window', {'instance_id': env['a'].id,
            'manifest_item_ids': ['manifest:' + selected.id]})
    assert exc.value.status_code == 404


@pytest.mark.parametrize('bad', [
    'manifest:not-a-uuid', 'manifest:manifest:1234', 'manifest:',
    'manifest:{12345678-1234-1234-1234-123456789abc}',
    'manifest:12345678123412341234123456789abc',
])
def test_malformed_manifest_namespace_is_not_guessed(frozen_metric_scope, bad):
    env, broker, *_ = frozen_metric_scope
    with pytest.raises(HTTPException) as exc:
        broker.call('get_metric_window', {'instance_id': env['a'].id,
            'manifest_item_ids': [bad]})
    assert exc.value.status_code == 404
