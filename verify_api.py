"""Exercise the running local HTTP service, including a real synthetic index drill."""
import json
import httpx
import lab


def main():
    result = {'mode':'live_http_service', 'started_at':lab.now()}
    with httpx.Client(base_url='http://127.0.0.1:8765', timeout=240, trust_env=False) as client:
        def post(path, body=None):
            response = client.post(path, json=body, headers={'Origin':'http://127.0.0.1:8765'})
            response.raise_for_status()
            return response.json()
        result['collection'] = post('/api/collect')
        assert result['collection']['data']['status'] == 'complete'
        result['index_drill'] = post('/api/drill/index')
        drill = result['index_drill']['data']
        assert drill['before_explain'][0]['type'] == 'ALL'
        assert drill['after_explain'][0]['key'] == 'lab_customer_idx'
        assert drill['clock'] == 'perf_counter' and drill['after_seconds'] > 0
        result['drill_before'] = client.get('/api/record/'+drill['snapshot_id']).json()
        result['drill_after'] = client.get('/api/record/'+drill['after_snapshot_id']).json()
        before = client.get('/api/state').json()['snapshots'][0]
        result['investigation'] = post('/api/investigate', {'snapshot_id':before['id'],
            'question':'请读取orders表结构和固定customer_id=1234查询的EXPLAIN，并检查锁等待关系。仅确认本次采样的事实，未知直接说未知，不猜测变更原因。'})
        investigation = result['investigation']['data']
        assert investigation['status'] == 'complete', investigation.get('error_type')
        result['investigation_evidence'] = client.get('/api/record/'+investigation['evidence_snapshot_id']).json()
        assert all(t['data']['status']=='complete' for t in investigation['tool_calls'])
        assert any(t['data']['name']=='explain_order_lookup' for t in investigation['tool_calls'])
        result['latest_snapshot_at_finish'] = client.get('/api/state').json()['snapshots'][0]['timestamp']
    result.update(status='passed', finished_at=lab.now())
    (lab.ROOT/'reports/api-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print('PASS: live HTTP collection, high-resolution index drill, record retrieval and bounded read-only model investigation')


if __name__ == '__main__':
    main()
