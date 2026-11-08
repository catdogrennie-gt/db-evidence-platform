"""Compare two observed counter samples, never infer rates from one sample."""
from datetime import datetime


def counter_window(older, newer):
    def extract(snapshot):
        evidence = {e['source']: e for e in snapshot['data']['evidence']}
        identity = evidence['mysql.identity']['value'][0]['uuid']
        sample = evidence['mysql.status']
        status = {x['Variable_name']: int(x['Value']) for x in sample['value']}
        return identity, datetime.fromisoformat(sample['collected_at']), status
    try:
        old_id, old_time, old = extract(older)
        new_id, new_time, new = extract(newer)
        if old_id != new_id:
            return {'status': 'incomparable', 'reason': 'instance_changed'}
        seconds = (new_time - old_time).total_seconds()
        if seconds <= 0:
            return {'status': 'incomparable', 'reason': 'nonpositive_interval'}
        if new['Uptime'] < old['Uptime']:
            return {'status': 'incomparable', 'reason': 'instance_restarted'}
        rates = {}
        for counter in ['Questions', 'Slow_queries', 'Innodb_buffer_pool_reads', 'Innodb_buffer_pool_read_requests']:
            if counter in old and counter in new:
                if new[counter] < old[counter]:
                    return {'status': 'incomparable', 'reason': 'counter_reset'}
                delta = new[counter] - old[counter]
                rates[counter] = {'delta': delta, 'per_second': round(delta / seconds, 3)}
        return {'status': 'comparable', 'seconds': round(seconds, 3), 'rates': rates,
                'sample_ids': [older['id'], newer['id']], 'instance_uuid': new_id}
    except (KeyError, IndexError, TypeError, ValueError):
        return {'status': 'incomparable', 'reason': 'missing_or_invalid_samples'}
