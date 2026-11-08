"""A real queue lease and fenced transaction; only the upstream RPC is replaced."""
from sqlalchemy import select, func
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_monitoring import monitoring_env
from platform_app import models as m


def test_external_collection_uses_original_queue_fence_and_live_evidence(monitoring_env,monkeypatch):
    from platform_app import monitoring
    from platform_app.scheduling import enqueue,claim
    from platform_app.worker import execute
    import platform_app.collection_service as direct
    e=monitoring_env
    identifiers=monitoring.select_hosts(e['actor'],e['source'],['101'],e['config'])['instance_ids']
    instance=e['db'].get(m.Instance,identifiers[0])
    for existing in e['db'].scalars(select(m.Task)):
        existing.status='succeeded'  # ai_env's pre-existing evidence fixture is already collected.
    task=enqueue(e['db'],'collect_instance',instance_id=instance.id,payload={'config_revision':instance.config_revision,'collectors':['identity_status','runtime']})
    e['db'].commit()
    def never_open(*args,**kwargs): raise AssertionError('External monitoring opened MySQL')
    monkeypatch.setattr(direct,'open_mysql',never_open)
    lease=claim(e['factory'],'test-monitoring-worker','collection')
    assert lease and lease.id==task.id
    result=execute(e['factory'],lease,e['config'])
    assert result and len(result['evidence_ids'])==2
    e['db'].expire_all()
    assert e['db'].get(m.Task,task.id).status=='succeeded'
    rows=list(e['db'].scalars(select(m.Evidence).where(m.Evidence.id.in_(result['evidence_ids']))))
    assert {r.source for r in rows}=={'mysql.global_status','mysql.lock_waits'}
    assert all(r.source_mode=='live' and r.coverage['transport']=='zabbix' for r in rows)
    assert e['db'].scalar(select(func.count()).select_from(m.MetricFrame))==0
