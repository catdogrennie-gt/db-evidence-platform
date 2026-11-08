from datetime import timedelta
from sqlalchemy import select
from tests.platform.test_ai_scope import ai_env
from platform_app import models as m


def test_manifest_limit_preserves_rare_diagnostic_layer_and_baseline(ai_env):
    from platform_app.evidence import create_manifest
    e=ai_env;old=e['evidence'];old.source='mysql.statement_digest'
    for index in range(1005):
        e['db'].add(m.Evidence(instance_id=e['a'].id,run_id=old.run_id,source='mysql.global_status',source_mode='live',observed_at=e['start']+timedelta(minutes=2,seconds=index/100),payload={},payload_sha256='a'*64))
    e['db'].flush()
    manifest=create_manifest(e['db'],'investigation',e['investigation'].id,[e['a'].id],e['start'],e['end'])
    refs=set(e['db'].scalars(select(m.ManifestItem.evidence_id).where(m.ManifestItem.manifest_id==manifest.id,m.ManifestItem.evidence_id.is_not(None))))
    assert old.id in refs
    assert len(refs)<=1000
    coverage=e['db'].scalar(select(m.ManifestItem).where(m.ManifestItem.manifest_id==manifest.id,m.ManifestItem.item_type=='coverage'))
    assert coverage.payload['truncated']
