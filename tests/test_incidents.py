"""Meaningful failure-path checks: faults must be cleaned when diagnosis fails."""
from contextlib import contextmanager
import pytest
import lab
import incidents


def index_fixture(monkeypatch, tmp_path, present=True, cleanup_error=False):
    monkeypatch.setattr(lab, 'RUNTIME', tmp_path)
    state = {'index': present, 'mutations': []}

    @contextmanager
    def connection(*args, **kwargs):
        yield object()

    def query(db, sql, args=None):
        if sql.startswith('SHOW INDEX'):
            return [{'Column_name': 'customer_id'}] if state['index'] else []
        if sql.startswith('ALTER TABLE'):
            state['mutations'].append(sql)
            if 'ADD INDEX' in sql and cleanup_error:
                raise OSError('injected cleanup connection failure')
            state['index'] = 'ADD INDEX' in sql
        if sql.startswith('EXPLAIN'):
            return [{'type': 'ref' if state['index'] else 'ALL', 'key': 'lab_customer_idx' if state['index'] else None}]
        return []

    monkeypatch.setattr(lab, 'connection', connection)
    monkeypatch.setattr(lab, 'query', query)
    monkeypatch.setattr(lab, 'collect', lambda: lab.store('snapshot', {'status': 'complete', 'evidence': [{'id': 'E1', 'source': 'fixture', 'value': []}], 'errors': []}))

    def fail_model(*args):
        raise TimeoutError('injected model timeout')

    monkeypatch.setattr(incidents.investigation, 'investigate', fail_model)
    return state


def test_model_failure_restores_index_and_preserves_failure(monkeypatch, tmp_path):
    state = index_fixture(monkeypatch, tmp_path)
    report = incidents.run_case('index')['data']
    assert state['index'] is True
    assert report['status'] == 'failed' and report['error_type'] == 'TimeoutError'
    assert report['failed_phase'] == 'diagnosing_live_fault'
    assert report['cleanup'] == {'status': 'passed', 'index_restored': True}
    assert lab.records('incident_event') and lab.records('snapshot')


def test_cleanup_failure_cannot_be_reported_as_success(monkeypatch, tmp_path):
    index_fixture(monkeypatch, tmp_path, cleanup_error=True)
    report = incidents.run_case('index')['data']
    assert report['status'] == 'failed' and report['cleanup']['status'] == 'failed'


def test_missing_baseline_refuses_mutations(monkeypatch, tmp_path):
    state = index_fixture(monkeypatch, tmp_path, present=False)
    report = incidents.run_case('index')['data']
    assert report['status'] == 'failed' and not state['mutations']
    assert report['cleanup']['status'] == 'not_started'


def test_progress_callback_failure_does_not_prevent_cleanup(monkeypatch, tmp_path):
    state = index_fixture(monkeypatch, tmp_path)
    def failed_display(value):
        raise OSError('display unavailable')
    assert incidents.run_case('index', failed_display)['data']['cleanup']['status'] == 'passed'
    assert state['index']


def test_concurrent_case_refused_before_mutation(monkeypatch, tmp_path):
    state = index_fixture(monkeypatch, tmp_path)
    assert lab._drill_lock.acquire(False)
    try:
        with pytest.raises(ValueError, match='another drill'):
            incidents.run_case('index')
    finally:
        lab._drill_lock.release()
    assert not state['mutations']
