"""Run alone: live API cases, mutual exclusion and injected-model-failure cleanup.

Uses only this registered synthetic lab. Failure injection replaces the model
function locally; it is NOT evidence of an actual provider outage.
"""
import json
import threading
import time
from unittest.mock import patch
import httpx
import lab
import incidents


def main():
    result = {'started_at': lab.now(), 'mode': 'live_synthetic_lab', 'positive_cases': [],
              'injected_model_failure_cases': [], 'progress_observations': []}
    def checkpoint(phase):
        result['phase'] = phase
        (lab.RUNTIME / 'incident-check-progress.json').write_text(json.dumps(result, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
        print(phase, lab.now(), flush=True)

    with httpx.Client(base_url='http://127.0.0.1:8765', trust_env=False, timeout=300) as client:
        assert not client.get('/api/task').json()['busy'], 'run alone, without page operations'
        for scenario in ('index', 'lock'):
            checkpoint('api_' + scenario)
            response_box = {}
            def request():
                try:
                    response = client.post('/api/incident/' + scenario)
                    response.raise_for_status()
                    response_box['case'] = response.json()
                except Exception as exc:
                    response_box['error_type'] = type(exc).__name__
            thread = threading.Thread(target=request)
            thread.start()
            for _ in range(40):
                observation = client.get('/api/task').json()
                if observation['busy']:
                    result['progress_observations'].append(observation)
                    assert client.post('/api/collect').status_code == 409, 'concurrent mutation was not rejected'
                    result['concurrent_operation_status'] = 409
                    break
                if not thread.is_alive():
                    break
                time.sleep(.1)
            thread.join(300)
            assert not thread.is_alive() and 'case' in response_box, response_box
            case = response_box['case']
            assert case['data']['status'] == 'passed', case['data']
            if scenario == 'lock':
                assert case['data']['waiter']['status'] == 'completed', case['data']
            report = lab.get_record(case['data']['investigation_id'])
            assert report['data']['model_responses'], 'model response audit missing'
            evidence = lab.get_record(report['data']['evidence_snapshot_id'])
            result['positive_cases'].append({'case': case, 'investigation': report,
                'evidence_snapshot': evidence,
                'initial_snapshot': lab.get_record(case['data']['initial_snapshot_id']),
                'fault_snapshot': lab.get_record(case['data']['fault_snapshot_id']),
                'after_snapshot': lab.get_record(case['data']['after_snapshot_id']),
                'model_responses': [lab.get_record(i) for i in report['data']['model_responses']]})

        for scenario in ('index', 'lock'):
            checkpoint('injected_model_timeout_' + scenario)
            assert not client.get('/api/task').json()['busy']
            with patch('investigation.investigate', side_effect=httpx.ReadTimeout('injected model timeout; no provider request')):
                case = incidents.run_case(scenario)
            assert case['data']['status'] == 'failed', case['data']
            assert case['data']['cleanup']['status'] == 'passed', case['data']
            if scenario == 'lock':
                assert case['data']['waiter']['status'] == 'completed', case['data']
            result['injected_model_failure_cases'].append(case)
            result.setdefault('test_annotations', []).append(lab.store('incident_test', {
                'incident_id': case['id'], 'test_type': 'injected_model_timeout',
                'result': 'passed', 'expected_diagnosis_status': 'failed',
                'expected_cleanup_status': 'passed',
                'note': 'Locally injected model-function ReadTimeout; not an observed provider outage.'}))

    checkpoint('checking_monitor_template_isolation')
    samples = [lab.collect(), lab.collect()]
    digest_samples = [next(e for e in s['data']['evidence'] if e['source'] == 'mysql.sql_digest') for s in samples]
    known = [{row['digest_hash']: row['executions'] for row in e['excluded_known_templates']} for e in digest_samples]
    assert known[0] and known[0] == known[1], 'old collector-template counters changed'
    with lab.connection(database=None) as db:
        default_database = lab.query(db, 'SELECT DATABASE() AS selected_database')[0]['selected_database']
    assert default_database is None
    result['monitor_template_isolation'] = {'samples': samples, 'old_template_counts_unchanged': True,
                                            'collector_default_database': default_database}
    result.update(status='passed', finished_at=lab.now())
    checkpoint('finished')
    (lab.ROOT / 'reports/incident-verification.json').write_text(json.dumps(result, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
    print('PASS: live API cases; 409 mutual exclusion; injected model failures cleaned both faults; old collector counters unchanged', flush=True)


if __name__ == '__main__':
    main()
