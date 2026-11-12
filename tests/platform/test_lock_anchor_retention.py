"""Ordinary empty samples must not crowd out distinct abnormal anchors."""
from datetime import timedelta

from platform_app import models as m
from platform_app.evidence import create_manifest, digest
from platform_app.investigations import _bundle
from tests.platform.test_ai_scope import ai_env


def test_first_and_latest_nonempty_lock_snapshots_survive_a_chatty_healthy_history(ai_env):
    e = ai_env
    db, inv = e['db'], e['investigation']
    db.add(m.InvestigationResource(investigation_id=inv.id, instance_id=e['a'].id))
    for index in range(80):
        db.add(m.Evidence(instance_id=e['a'].id, run_id=e['evidence'].run_id,
            source='mysql.lock_waits', observed_at=e['start']+timedelta(seconds=1+index*6),
            payload={'rows': []}, coverage={'availability': 'available', 'complete': True},
            payload_sha256=digest({'rows': []})))
    later = m.Evidence(instance_id=e['a'].id, run_id=e['evidence'].run_id,
        source='mysql.lock_waits', observed_at=e['start']+timedelta(minutes=4),
        payload={'rows': [{'waiting_trx_id': 24, 'blocking_trx_id': 13}]},
        coverage={'availability': 'available', 'complete': True}, payload_sha256='b'*64)
    later.payload_sha256 = digest(later.payload)
    db.add(later); db.flush()
    manifest = create_manifest(db, 'investigation', inv.id, [e['a'].id], inv.window_start, inv.window_end)
    bundle, *_ = _bundle(db, inv, manifest)
    anchors = {item['evidence_id'] for item in bundle['evidence']
               if item.get('selection_reason') == 'nonempty_lock_snapshot'}
    assert anchors == {e['evidence'].id, later.id}
    snapshots = [item for item in bundle['evidence'] if item['source'] == 'mysql.lock_waits']
    assert any(item['data']['row_count'] == 0 and item['observed_at'] < next(
        row['observed_at'] for row in snapshots if row['evidence_id'] == e['evidence'].id)
        for item in snapshots)
    assert max(snapshots, key=lambda item: item['observed_at'])['data']['row_count'] == 0
