"""A current deterministic capacity fact is separate from the frozen AI report."""
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import event

from platform_app import models as m
from platform_app.common import iso
from platform_app.db import utcnow
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_showcase import showcase_env
from tests.platform.test_capacity_signals import configure, metadata, points, scan, write


@pytest.fixture
def capacity_env(showcase_env, monkeypatch):
    from platform_app import showcase
    e=showcase_env;now=utcnow()
    e['config'].showcase['projection_mode']='numeric'
    key=e['tmp_path']/'alias.private';key.write_bytes(b'local-test-only-alias-key-value')
    e['config'].fernet_key_path=key
    used,limit,a,b=configure(e,now)
    finding=scan(e,now)[0]
    monkeypatch.setattr(showcase,'db_now',lambda db:now+timedelta(seconds=2))
    e.update(now=now,used=used,limit=limit,used_evidence=a,limit_evidence=b,finding=finding)
    e['db'].commit()
    return e


def snapshot(e):
    from platform_app.showcase import numeric_snapshot
    return numeric_snapshot(e['db'],e['config'])


def test_current_capacity_fact_is_readable_and_snapshot_does_not_write(capacity_env):
    e=capacity_env; statements=[]
    def record(conn,cursor,statement,parameters,context,executemany):
        statements.append(statement.lstrip().split()[0].upper())
    before=deepcopy(e['finding'].calculation)
    event.listen(e['db'].bind,'before_cursor_execute',record)
    try: result=snapshot(e)
    finally: event.remove(e['db'].bind,'before_cursor_execute',record)
    [fact]=result['rule_findings']
    assert set(fact)=={'title','summary','observed_at','evidence_ids','status'}
    assert 'Selected measurement 501' in fact['title'] and '容量余量不足' in fact['title']
    for actual in ('34','40','85%','6','80%'): assert actual in fact['summary']
    assert fact['status']=='active' and fact['observed_at']==iso(e['now'])
    assert set(fact['evidence_ids'])=={e['used_evidence'].id,e['limit_evidence'].id}
    assert result['observation']['signals'][0]['category']=='capacity_pressure'
    assert before==e['finding'].calculation
    assert not set(statements)&{'INSERT','UPDATE','DELETE'}


@pytest.mark.parametrize('damage', ['limit_unavailable','used_unavailable','limit_sparse',
    'used_counter','limit_stale','latest_invalid','mixed','recovered','skew',
    'threshold','group','role','missing_tags','mapping','config','source','selection','stale_observation'])
def test_both_latest_operands_and_configuration_must_still_support_active_fact(capacity_env,damage):
    e=capacity_env;new=e['now']+timedelta(seconds=1)
    if damage in ('limit_unavailable','used_unavailable'):
        meta=e['limit'] if damage.startswith('limit') else e['used']
        write(e,meta,points(e['now'],(40,40,40) if damage.startswith('limit') else (34,34,34)),
              observed_at=new,availability='unavailable')
    elif damage in ('limit_sparse','used_counter'):
        row=e['limit_evidence'] if damage=='limit_sparse' else e['used_evidence']
        meta=deepcopy(row.payload['item'])
        if damage=='limit_sparse':meta['history_semantics']='sparse_on_change'
        else:meta['value_semantics']='counter'
        row.payload={**row.payload,'item':meta}
    elif damage=='limit_stale':
        e['limit_evidence'].payload={**e['limit_evidence'].payload,'rows':points(e['now']-timedelta(seconds=21),(40,40,40))}
    elif damage in ('latest_invalid','mixed','recovered'):
        values=(34,34,None) if damage=='latest_invalid' else (34,34,9) if damage=='mixed' else (9,9,9)
        write(e,e['used'],points(e['now'],values),observed_at=new)
    elif damage=='skew':
        e['limit_evidence'].payload={**e['limit_evidence'].payload,'rows':points(e['now'],(40,40,40),shift=-6)}
    elif damage in ('threshold','group','role','missing_tags'):
        meta=metadata('502','limit',warning='90' if damage=='threshold' else '80',
                      group='different' if damage=='group' else 'connection-budget')
        if damage=='role':meta=metadata('502','used')
        if damage=='missing_tags':meta['tags']=[]
        e['limit_evidence'].payload={**e['limit_evidence'].payload,'item':meta}
    elif damage=='mapping':e['mapping'].version+=1
    elif damage=='config':e['a'].config_revision+=1
    elif damage=='source':
        other=m.MonitoringSource(name='other',url='https://other.invalid',secret_ref='unused',
                                 system_id=e['a'].system_id,status='ready')
        e['db'].add(other);e['db'].flush();e['mapping'].source_id=other.id
    elif damage=='selection':e['mapping'].items=[e['used']]
    elif damage=='stale_observation':e['limit_evidence'].observed_at=e['now']-timedelta(seconds=30)
    e['db'].flush()
    result=snapshot(e)
    assert not any(signal['category']=='capacity_pressure' for signal in result['observation']['signals'])
    assert result['rule_findings']==[]
    # A read must not turn an unknown latest observation into stored recovery.
    assert e['finding'].calculation['observation_state']=='active'


def test_current_fact_uses_new_refs_values_without_rewriting_finding_or_report(capacity_env):
    e=capacity_env;db=e['db'];inv=e['investigation']
    inv.status='completed';inv.window_start=e['now']-timedelta(hours=1);inv.window_end=e['now']
    inv.report={'executive_summary':{'headline':'Original frozen model conclusion'},'coverage':{'complete':False}}
    db.add(m.InvestigationResource(investigation_id=inv.id,instance_id=e['a'].id))
    e['finding'].latest_investigation_id=inv.id
    before=deepcopy(e['finding'].calculation);original=deepcopy(inv.report)
    # The stored observation is older; GET must verify and expose actual latest operands.
    a=write(e,e['used'],points(e['now'],(35,35,35)),observed_at=e['now']+timedelta(microseconds=1))
    b=write(e,e['limit'],points(e['now'],(40,40,40)),observed_at=e['now']+timedelta(microseconds=1))
    db.commit();result=snapshot(e)
    [fact]=result['rule_findings']
    assert '87.5%' in fact['summary'] and '35 / 40' in fact['summary']
    assert set(fact['evidence_ids'])=={a.id,b.id}
    assert result['report']['executive_summary']==original['executive_summary']
    assert inv.report==original and e['finding'].calculation==before


def test_only_target_own_capacity_findings_are_exposed(capacity_env):
    from platform_app.intelligence import upsert_finding
    e=capacity_env;own=e['finding']
    foreign=upsert_finding(e['db'],e['b'].id,'capacity_pressure',own.dimension,
                           own.evidence_ids,{**own.calculation},kind='risk',now=e['now'])
    e['db'].commit();result=snapshot(e)
    assert len(result['rule_findings'])==1
    assert foreign.id not in str(result) and foreign.event_id not in str(result)


def test_rule_fact_count_is_bounded(capacity_env):
    e=capacity_env;selected=list(e['mapping'].items)
    for index in range(1,8):
        used=metadata(str(600+index*2),'used',group='budget-'+str(index))
        limit=metadata(str(601+index*2),'limit',group='budget-'+str(index))
        selected.extend([used,limit])
        write(e,used,points(e['now']));write(e,limit,points(e['now'],(40,40,40)))
    e['mapping'].items=selected;scan(e,e['now']);e['db'].commit()
    assert len(snapshot(e)['rule_findings'])==6
