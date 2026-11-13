"""Evidence linkage must not issue a database round trip for every sample."""
from datetime import timedelta
import pytest
from sqlalchemy import event,select

from platform_app import models as m
from platform_app.evidence import digest
from platform_app.investigations import run_investigation
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_ai_failures import prepared_run,provider_response


@pytest.mark.parametrize('prelinked',[False,True])
def test_investigation_links_frozen_evidence_with_one_existing_reference_query(ai_env,prelinked):
    e=ai_env;db=e['db'];old=e['evidence'];refs={old.id}
    for index in range(50):
        row=m.Evidence(instance_id=e['a'].id,run_id=old.run_id,source='mysql.sessions',source_mode='live',
            observed_at=e['start']+timedelta(minutes=2,seconds=index),payload={'rows':[]},
            payload_sha256=digest({'rows':[]}),coverage={'availability':'available'})
        db.add(row);db.flush();refs.add(row.id)
    db.commit();row,_,lease,config=prepared_run(e);queries=[]
    if prelinked:
        db.add(m.InvestigationEvidence(investigation_id=row.id,evidence_id=old.id))
        other=next(ref for ref in refs if ref!=old.id)
        db.add(m.InvestigationEvidence(investigation_id=e['investigation'].id,evidence_id=other))
        db.commit()
    engine=e['factory'].kw['bind']
    def capture(_conn,_cursor,statement,*_args):
        if statement.lstrip().upper().startswith('SELECT') and 'FROM investigation_evidence' in statement:
            queries.append(statement)
    event.listen(engine,'before_cursor_execute',capture)
    try:result=run_investigation(e['factory'],lease,config,model_client=lambda *_:provider_response(old.id))
    finally:event.remove(engine,'before_cursor_execute',capture)
    assert result.status=='completed',result.report
    assert len(queries)<=1
    retained=set(db.scalars(select(m.InvestigationEvidence.evidence_id).where(m.InvestigationEvidence.investigation_id==row.id)))
    assert retained==refs
