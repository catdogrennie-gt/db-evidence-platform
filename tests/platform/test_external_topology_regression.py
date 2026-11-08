from types import SimpleNamespace
import pytest
from platform_app import models as m
from platform_app.common import iso
from tests.platform.test_ai_scope import ai_env


@pytest.mark.parametrize('source_uuid', [None, '', 'b'])
def test_unknown_external_uuid_never_matches_replication_but_real_uuid_does(ai_env, source_uuid):
    from platform_app.routes.observability import topology, snapshot_view
    e=ai_env
    e['grant'].scope_type='system'; e['grant'].scope_id=e['system'].id
    e['b'].expected_server_uuid = 'b' if source_uuid == 'b' else None
    e['b'].connection_mode='zabbix'
    row=e['evidence']; row.source='mysql.replication'
    row.payload={'rows':[{'Source_UUID':source_uuid}]}
    row.coverage={'availability':'available','complete':True}
    e['db'].commit()
    graph=topology(e['actor'],from_time=iso(e['start']),to_time=iso(e['end']))
    edges=[r for r in graph['edges'] if r['type']=='observed_replication']
    assert len(edges)==1
    if source_uuid == 'b': assert edges[0]['target']==e['b'].id
    else: assert edges[0]['target'].startswith('restricted-upstream-')
    result=snapshot_view(e['a'].id,SimpleNamespace(url=SimpleNamespace(path='/instances/review/replication')),e['actor'],iso(e['start']),iso(e['end']),50,None,'')
    assert result['items'][0]['upstream_instance_id'] == (e['b'].id if source_uuid == 'b' else None)
