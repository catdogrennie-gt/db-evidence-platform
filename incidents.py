"""User-started synthetic fault cases. Model tools remain read-only.

Cleanup is deterministic and runs even when diagnosis fails. Final acceptance
separately checks observed fault evidence, model completion and cleanup.
"""
import threading
import time
import uuid
import lab
import investigation


def run_case(scenario, progress=None):
    if scenario not in ('index', 'lock'):
        raise ValueError('unknown scenario')
    if not lab._drill_lock.acquire(False):
        raise ValueError('another drill is running')
    started = time.monotonic()
    result = {'run_id': str(uuid.uuid4()), 'scenario': scenario, 'mode': 'live_synthetic_lab',
              'started_at': lab.now(), 'events': [], 'cleanup': {'status': 'not_started'}}

    def phase(name):
        result['phase'] = name
        try:
            event = lab.store('incident_event', {'run_id': result['run_id'], 'phase': name})
            result['events'].append(event)
        except Exception as exc:
            result.setdefault('audit_errors', []).append({'phase': name, 'error_type': type(exc).__name__})
        if progress:
            try:
                progress({'run_id': result['run_id'], 'scenario': scenario, 'phase': name,
                          'started_at': result['started_at'], 'updated_at': lab.now()})
            except Exception:
                pass  # Progress display cannot prevent deterministic cleanup.

    def diagnose(question, withheld=()):
        phase('collecting_fault')
        truth = lab.collect()
        result['fault_snapshot_id'] = truth['id']
        initial = lab.store('snapshot', {**truth['data'], 'purpose': 'incident_diagnosis',
            'evidence': [e for e in truth['data']['evidence'] if e['source'] not in withheld],
            'status': 'partial' if withheld else truth['data']['status'],
            'withheld_sources': list(withheld),
            'experiment_note': 'Synthetic fault held during diagnosis; any withheld sources are explicit evaluation gaps.'})
        result['initial_snapshot_id'] = initial['id']
        phase('diagnosing_live_fault')
        try:
            report = investigation.investigate(initial['id'], question)
        except Exception as exc:
            result.update(diagnosis_status='incomplete', diagnosis_error_type=type(exc).__name__,
                          failure_origin='diagnosing_live_fault')
            raise
        result['investigation_id'] = report['id']
        result['diagnosis_status'] = report['data']['status']
        if report['data'].get('error_type'):
            result['diagnosis_error_type'] = report['data']['error_type']
        evidence = lab.get_record(report['data']['evidence_snapshot_id'])['data']['evidence']
        name = 'mysql.tool.explain_order_lookup' if scenario == 'index' else 'mysql.tool.get_lock_waits'
        values = [e['value'] for e in evidence if e['source'] == name]
        result['diagnostic_evidence_confirmed'] = any(
            any(row.get('type') == 'ALL' for row in value) if scenario == 'index'
            else any(row.get('table_name') == 'accounts' and row.get('waiting_query') for row in value)
            for value in values)

    try:
        phase('checking_scope')
        if scenario == 'index':
            with lab.connection('operator') as db:
                lab.query(db, 'SET SESSION lock_wait_timeout=10')
                baseline = lab.query(db, "SHOW INDEX FROM orders WHERE Key_name='lab_customer_idx'")
                if not baseline or len(baseline) != 1 or baseline[0]['Column_name'] != 'customer_id':
                    raise ValueError('expected baseline lab_customer_idx(customer_id) is missing or different')
                try:
                    phase('injecting_fault')
                    lab.query(db, 'ALTER TABLE orders DROP INDEX lab_customer_idx')
                    result['before_explain'] = lab.query(db, 'EXPLAIN SELECT * FROM orders WHERE customer_id=1234')
                    diagnose('实验orders查询变慢。请先读取orders表结构，再读取固定customer_id查询的EXPLAIN；依据实际证据判断扫描与索引问题，说明未知，不执行修复。')
                finally:
                    phase('cleaning_up')
                    result['cleanup']['status'] = 'running'
                    if not lab.query(db, "SHOW INDEX FROM orders WHERE Key_name='lab_customer_idx'"):
                        lab.query(db, 'ALTER TABLE orders ADD INDEX lab_customer_idx(customer_id)')
                    result['after_explain'] = lab.query(db, 'EXPLAIN SELECT * FROM orders WHERE customer_id=1234')
                    result['cleanup'] = {'status': 'passed' if result['after_explain'][0]['key'] == 'lab_customer_idx' else 'failed',
                                         'index_restored': result['after_explain'][0]['key'] == 'lab_customer_idx'}
        else:
            with lab.connection('operator') as blocker, lab.connection('operator', read_timeout=260) as waiter:
                before = lab.query(blocker, 'SELECT * FROM accounts ORDER BY id')
                result['accounts_before'] = before
                waiter_result = {}
                thread = None
                try:
                    phase('injecting_fault')
                    blocker.begin()
                    waiter.begin()
                    lab.query(blocker, 'UPDATE accounts SET balance=balance+1 WHERE id=1')
                    lab.query(waiter, 'SET SESSION innodb_lock_wait_timeout=240')

                    def waiting():
                        try:
                            lab.query(waiter, 'UPDATE accounts SET balance=balance+1 WHERE id=1')
                            waiter_result['status'] = 'completed'
                        except Exception as exc:
                            waiter_result['status'] = type(exc).__name__

                    thread = threading.Thread(target=waiting, daemon=True)
                    thread.start()
                    for _ in range(10):
                        observed = investigation.execute_read_tool('get_lock_waits', {})
                        if any(row.get('table_name') == 'accounts' for row in observed['value']):
                            break
                        time.sleep(.2)
                    else:
                        raise RuntimeError('injected wait was not observed')
                    result['injection_observation'] = observed
                    diagnose('当前accounts更新阻塞，但初始证据缺锁关系。请读取锁等待关系确认等待者与阻塞者，区分事实和假设，不要KILL或执行修复。', ('mysql.lock_waits',))
                finally:
                    phase('cleaning_up')
                    result['cleanup']['status'] = 'running'
                    blocker.rollback()
                    if thread:
                        thread.join(25)
                    if thread and thread.is_alive():
                        raise RuntimeError('waiter did not exit after blocker rollback')
                    waiter.rollback()
                    result['waiter'] = waiter_result
                    after = lab.query(blocker, 'SELECT * FROM accounts ORDER BY id')
                    result['accounts_after'] = after
                    waits = investigation.execute_read_tool('get_lock_waits', {})['value']
                    for _ in range(3):
                        if not waits:
                            break
                        time.sleep(.3)
                        waits = investigation.execute_read_tool('get_lock_waits', {})['value']
                    waiter_completed = waiter_result.get('status') == 'completed'
                    result['cleanup'] = {'status': 'passed' if before == after and not waits and waiter_completed else 'failed',
                                         'balances_unchanged': before == after, 'remaining_waits': waits,
                                         'waiter_completed': waiter_completed}
        phase('verifying_after')
        result['after_snapshot_id'] = lab.collect()['id']
        result['status'] = ('passed' if result.get('diagnosis_status') == 'complete'
                            and result.get('diagnostic_evidence_confirmed')
                            and result['cleanup']['status'] == 'passed' and not result.get('audit_errors') else 'incomplete')
    except Exception as exc:
        failed_phase = (result['phase'] if result['cleanup']['status'] in ('running', 'failed')
                        else result.get('failure_origin', result['phase']))
        result.update(status='failed', error_type=type(exc).__name__, failed_phase=failed_phase)
        if result['cleanup']['status'] == 'running':
            result['cleanup']['status'] = 'failed'
    finally:
        try:
            phase('finished')
            if result.get('status') == 'passed' and result.get('audit_errors'):
                result['status'] = 'incomplete'
            result['elapsed_seconds'] = round(time.monotonic() - started, 3)
            result['finished_at'] = lab.now()
        finally:
            lab._drill_lock.release()
    return lab.store('incident', result)
