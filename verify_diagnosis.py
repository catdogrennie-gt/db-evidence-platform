"""Real, synthetic-data diagnosis checks. Runs mutations ONLY through registered lab operator.

Run alone, without UI write tasks. Restores the test index and rolls back test transactions.
"""
import json
import threading
import time
import lab
import investigation


def tool_values(record, name):
    snapshot = lab.get_record(record['data']['evidence_snapshot_id'])
    return [e['value'] for e in snapshot['data']['evidence'] if e['source'] == 'mysql.tool.' + name]


def main():
    result = {'mode': 'live_synthetic_lab', 'started_at': lab.now()}
    def checkpoint(phase):
        result['phase'] = phase
        (lab.ROOT/'runtime/diagnosis-check-progress.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        print(phase, lab.now(), flush=True)
    checkpoint('starting')
    with lab.connection('operator') as db:
        if lab.query(db, "SHOW INDEX FROM orders WHERE Key_name='lab_customer_idx'"):
            lab.query(db, 'ALTER TABLE orders DROP INDEX lab_customer_idx')
    try:
        snapshot = lab.collect()
        result['index_before'] = snapshot
        record = investigation.investigate(snapshot['id'], '实验orders查询变慢。请先读取orders表结构，再读取固定customer_id查询的EXPLAIN，判断能确认的索引/扫描问题和未知，不要给生产收益。')
        result['index_investigation'] = record
        result['index_evidence'] = lab.get_record(record['data']['evidence_snapshot_id'])
        assert record['data']['status'] == 'complete', record['data'].get('error_type')
        plans = tool_values(record, 'explain_order_lookup')
        assert plans and plans[0][0]['type'] == 'ALL', 'missing full scan evidence'
    finally:
        with lab.connection('operator') as db:
            if not lab.query(db, "SHOW INDEX FROM orders WHERE Key_name='lab_customer_idx'"):
                lab.query(db, 'ALTER TABLE orders ADD INDEX lab_customer_idx(customer_id)')
    result['index_after'] = investigation.execute_read_tool('explain_order_lookup', {})
    assert result['index_after']['value'][0]['key'] == 'lab_customer_idx'
    checkpoint('index_restored')
    with lab.connection('operator') as blocker, lab.connection('operator', read_timeout=260) as waiter:
        result['accounts_before'] = lab.query(blocker, 'SELECT * FROM accounts ORDER BY id')
        blocker.begin()
        waiter.begin()
        lab.query(blocker, 'UPDATE accounts SET balance=balance+1 WHERE id=1')
        lab.query(waiter, 'SET SESSION innodb_lock_wait_timeout=240')
        waiter_result = {}
        def waiting():
            try:
                lab.query(waiter, 'UPDATE accounts SET balance=balance+1 WHERE id=1')
                waiter_result['status'] = 'completed'
            except Exception as exc:
                waiter_result['status'] = type(exc).__name__
        thread = threading.Thread(target=waiting, daemon=True)
        thread.start()
        try:
            time.sleep(1)
            snapshot = lab.collect()
            result['lock_before'] = snapshot
            # Withhold a source deliberately, so the agent must obtain genuinely missing
            # evidence rather than wasting calls repeating an already complete snapshot.
            limited = lab.store('snapshot', {'purpose':'diagnosis_evaluation', 'status':'partial',
                'evidence':[e for e in snapshot['data']['evidence'] if e['source']!='mysql.lock_waits'],
                'errors':[], 'withheld_sources':['mysql.lock_waits'],
                'evaluation_note':'Live evidence subset; lock relationship intentionally withheld to test active collection.'})
            result['lock_initial_subset'] = limited
            record = investigation.investigate(limited['id'], '当前accounts更新阻塞，但初始证据缺锁关系。请读取锁等待关系确认等待者与阻塞者，区分事实和假设，不要KILL或执行修复。')
            result['lock_investigation'] = record
            result['lock_evidence'] = lab.get_record(record['data']['evidence_snapshot_id'])
            checkpoint('lock_diagnosis_saved')
            assert record['data']['status'] == 'complete', record['data'].get('error_type')
            locks = tool_values(record, 'get_lock_waits')
            assert locks and locks[0] and locks[0][0]['waiting_query'], 'no real linked wait captured'
        finally:
            blocker.rollback()
            thread.join(25)
            if thread.is_alive():
                raise RuntimeError('waiter did not exit after blocker rollback')
            waiter.rollback()
    assert waiter_result['status'] == 'completed', waiter_result
    result['cleanup_observations'] = []
    for _ in range(4):
        result['lock_after'] = investigation.execute_read_tool('get_lock_waits', {})
        result['cleanup_observations'].append(result['lock_after'])
        checkpoint('cleanup_observation')
        if not result['lock_after']['value']:
            break
        time.sleep(1)
    assert result['lock_after']['value'] == []
    with lab.connection() as db:
        result['accounts_after'] = lab.query(db, 'SELECT * FROM accounts ORDER BY id')
    assert result['accounts_before'] == result['accounts_after'], 'test changed account balances'
    result['finished_at'] = lab.now()
    result['status'] = 'passed'
    (lab.ROOT/'reports/diagnosis-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print('PASS: live missing-index and lock-wait investigations; model-selected tools; index repaired; transactions rolled back; wait cleared')


if __name__ == '__main__':
    main()
