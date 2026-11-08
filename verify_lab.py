"""Run real experiments and export a portable, synthetic-only evidence report."""
import json
import lab
from unittest.mock import patch

def main():
    result={}
    cfg=lab.config();wrong={**cfg,'source_uuid':'deliberately-wrong-uuid'}
    with patch.object(lab,'config',return_value=wrong):
        try:
            with lab.connection('operator'):raise AssertionError('unregistered write target accepted')
        except ValueError as e:result['write_uuid_guard']={'status':'passed','message':str(e)}
    baseline=lab.collect();assert baseline['data']['status']=='complete';result['baseline']=baseline
    lock=lab.lock_drill();result['lock']=lock
    assert lock['data']['waiter']=='completed',lock
    after_lock=lab.get_record(lock['data']['after_snapshot_id'])
    assert not next(e['value'] for e in after_lock['data']['evidence'] if e['source']=='mysql.lock_waits')
    result['lock_after']=after_lock
    snapshot=next(x for x in lab.records('snapshot') if x['id']==lock['data']['snapshot_id'])
    result['lock_snapshot']=snapshot
    waits=next(e['value'] for e in snapshot['data']['evidence'] if e['source']=='mysql.lock_waits')
    assert waits,'lock experiment captured no waiting relation'
    result['ai_lock']=lab.analyze(snapshot['id'],'分析锁等待证据，明确阻塞与等待关系、未知及下一步只读检查。')
    index=lab.slow_drill();result['index']=index
    result['index_before']=lab.get_record(index['data']['snapshot_id'])
    result['index_after']=lab.get_record(index['data']['after_snapshot_id'])
    assert index['data']['before_explain'][0]['type']=='ALL'
    assert index['data']['after_explain'][0]['key']=='lab_customer_idx'
    result['ai_index']=lab.analyze(index['data']['snapshot_id'],'分析SQL摘要的扫描情况和索引证据，区分确定事实与猜测。')
    result['restore_success']=lab.recovery(False)
    assert result['restore_success']['data']['status']=='passed',result['restore_success']
    result['restore_corrupt']=lab.recovery(True)
    assert result['restore_corrupt']['data']['status']=='failed'
    result['restore_final']=lab.recovery(False)
    assert result['restore_final']['data']['status']=='passed'
    out=lab.ROOT/'reports';out.mkdir(exist_ok=True)
    (out/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print('PASS: live snapshots, real lock wait, index plan change, live AI, successful restore, corrupt backup failure, final clean restore')
if __name__=='__main__':main()
