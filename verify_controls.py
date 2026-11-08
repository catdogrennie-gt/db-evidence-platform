"""Paid-model controls against real lab snapshots; no writes or fault injection."""
import json
import lab
import investigation


def main():
    checks = json.loads((lab.ROOT/'reports/diagnosis-verification.json').read_text(encoding='utf-8'))
    assert checks['status'] == 'passed'
    snapshot = lab.collect()
    assert snapshot['data']['status'] == 'complete'
    waits = next(e['value'] for e in snapshot['data']['evidence'] if e['source'] == 'mysql.lock_waits')
    assert not waits, 'run after all fault injections have finished'
    explain = investigation.execute_read_tool('explain_order_lookup', {})
    assert explain['value'][0]['key'] == 'lab_customer_idx'
    explain['id'] = 'E7'
    healthy = lab.store('snapshot', {**snapshot['data'], 'purpose':'healthy_evaluation',
        'evidence':snapshot['data']['evidence']+[explain]})
    result = {'mode':'live_model_historical_and_current_evidence', 'started_at':lab.now()}
    def checkpoint():
        (lab.ROOT/'runtime/control-check-progress.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    result['healthy_evidence'] = healthy
    result['healthy_report'] = lab.analyze(healthy['id'], '该采样是否证实当前锁等待或固定orders查询缺索引？区分当前与历史，证据不足写未知；不要建议修改。')
    checkpoint()
    limited = lab.store('snapshot', {'purpose':'insufficient_evaluation', 'status':'partial',
        'evidence':[e for e in snapshot['data']['evidence'] if e['source']=='mysql.identity'],
        'errors':[{'source':'evaluation','error':'All other sources intentionally withheld'}]})
    result['insufficient_evidence'] = limited
    result['insufficient_report'] = lab.analyze(limited['id'], '有人说昨晚业务卡顿。仅根据这份证据，能确认根因、业务影响和故障时长吗？')
    assert result['insufficient_report']['data']['answer']['unknowns'], 'expected explicit unknowns'
    checkpoint()
    initial = checks['lock_initial_subset']
    active = checks['lock_investigation']
    result['comparison'] = {
        'initial_evidence':initial,
        'fixed_snapshot_report':lab.analyze(initial['id'], active['data']['question']),
        'active_report':active,
        'active_evidence':checks['lock_evidence'],
        'method':'Same saved initial snapshot and question. Fixed analysis runs later without tools; active run captured a real wait during injection. This is one functional comparison, not an accuracy benchmark or latency experiment.'}
    result.update(finished_at=lab.now(), status='completed_for_manual_review')
    (lab.ROOT/'reports/control-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print('SAVED: healthy/insufficient evidence controls and same-input fixed/active comparison; review report semantics before claiming success')


if __name__ == '__main__':
    main()
