from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import threading
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = load('mysql_recovery_attempt', 'scripts/platform/mysql_recovery_attempt.py')
fixtures = load('mysql_recovery_fixtures', 'tests/platform/test_mysql_recovery.py')


class Environment:
    def __init__(self, observations):
        self.observations = list(observations)
        self.last = self.observations[-1]
        self.starts = 0
        self.reads = 0
        self.time = 0.0
        self.budgets = []

    def observe(self, *, timeout_seconds):
        self.reads += 1
        self.budgets.append(timeout_seconds)
        if self.observations:
            self.last = self.observations.pop(0)
        if isinstance(self.last, Exception):
            raise self.last
        return deepcopy(self.last)

    def start(self, *, timeout_seconds):
        self.starts += 1
        self.budgets.append(timeout_seconds)

    def sleep(self, seconds):
        self.time += seconds


def run(c, env, directory, **overrides):
    options = dict(state_dir=directory, observe=env.observe, start=env.start,
                   timeout_seconds=3, poll_seconds=1, monotonic=lambda: env.time,
                   sleep=env.sleep, utcnow=lambda: fixtures.NOW)
    options.update(overrides)
    return mod.run_attempt(c, **options)


def test_existing_healthy_is_zero_start_and_no_intent(tmp_path):
    c, ready = fixtures.present()
    env = Environment([ready])
    assert run(c, env, tmp_path)['status'] == 'present_ready'
    assert env.starts == 0
    assert list(tmp_path.iterdir()) == []


def test_missing_starts_once_only_after_two_observations_and_intent(tmp_path):
    c, missing = fixtures.fixture()
    _, ready = fixtures.present()
    env = Environment([missing, missing, ready])

    def start(**kwargs):
        assert env.reads == 2
        intent = json.loads((tmp_path / 'control-mysql-attempt.json').read_text())
        assert intent['state'] == 'intent'
        env.start(**kwargs)

    result = run(c, env, tmp_path, start=start)
    assert result['status'] == 'ready_after_attempt'
    assert result['start_attempts'] == env.starts == 1
    assert (tmp_path / 'control-mysql-attempt.json').exists()
    assert not (tmp_path / 'control-mysql-recovery.lock').exists()


@pytest.mark.parametrize('second', ['ready', 'starting', 'conflict'])
def test_just_before_start_change_never_launches(second, tmp_path):
    c, missing = fixtures.fixture()
    _, changed = fixtures.present()
    if second == 'starting':
        changed['probe'] = None
    if second == 'conflict':
        changed['listeners'][0]['pid'] = 99
    env = Environment([missing, changed])
    result = run(c, env, tmp_path)
    assert result['status'] == {'ready': 'present_ready', 'starting': 'timeout', 'conflict': 'refused'}[second]
    assert env.starts == 0
    assert not (tmp_path / 'control-mysql-attempt.json').exists()


def test_present_starting_then_missing_never_starts(tmp_path):
    c, starting = fixtures.present()
    starting['probe'] = None
    _, missing = fixtures.fixture()
    env = Environment([starting, missing])
    assert run(c, env, tmp_path)['status'] == 'timeout'
    assert env.starts == 0


@pytest.mark.parametrize('field,value', [('defaults_sha256', 'c' * 64),
                                       ('datadir_uuid', 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee')])
def test_changed_identity_refuses_without_start(field, value, tmp_path):
    c, missing = fixtures.fixture()
    missing['files'][field] = value
    env = Environment([missing])
    assert run(c, env, tmp_path)['status'] == 'refused'
    assert env.starts == 0


def test_unknown_start_outcome_retains_intent_and_second_call_does_not_replay(tmp_path):
    c, missing = fixtures.fixture()
    env = Environment([missing])

    def start(**kwargs):
        env.start(**kwargs)
        raise TimeoutError('private secret / private command')

    result = run(c, env, tmp_path, start=start)
    assert result['status'] == 'start_outcome_unknown'
    original = (tmp_path / 'control-mysql-attempt.json').read_bytes()
    assert run(c, env, tmp_path)['status'] == 'pending'
    assert env.starts == 1
    assert (tmp_path / 'control-mysql-attempt.json').read_bytes() == original
    assert 'private' not in json.dumps(result)


def test_not_ready_after_start_times_out_with_no_retry(tmp_path):
    c, missing = fixtures.fixture()
    env = Environment([missing])
    result = run(c, env, tmp_path)
    assert result['status'] == 'timeout'
    assert env.starts == 1
    assert env.time <= 3
    assert run(c, env, tmp_path)['status'] == 'pending'
    assert env.starts == 1


def test_observation_error_is_sanitized_and_does_not_start(tmp_path):
    c, _ = fixtures.fixture()
    env = Environment([RuntimeError('password and private path')])
    result = run(c, env, tmp_path)
    assert result['status'] == 'refused'
    assert env.starts == 0
    assert 'password' not in json.dumps(result)
    assert not (tmp_path / 'control-mysql-recovery.lock').exists()


def test_existing_intent_still_permits_healthy_noop(tmp_path):
    (tmp_path / 'control-mysql-attempt.json').write_text('unresolved')
    c, ready = fixtures.present()
    env = Environment([ready])
    assert run(c, env, tmp_path)['status'] == 'present_ready'
    assert env.starts == 0
    assert (tmp_path / 'control-mysql-attempt.json').read_text() == 'unresolved'


def test_residual_lock_is_not_stolen_or_deleted(tmp_path):
    lock = tmp_path / 'control-mysql-recovery.lock'
    lock.write_text('another owner')
    c, missing = fixtures.fixture()
    env = Environment([missing])
    assert run(c, env, tmp_path)['status'] == 'busy'
    assert env.reads == env.starts == 0
    assert lock.read_text() == 'another owner'


def test_concurrent_call_cannot_observe_or_start_inside_other_attempt(tmp_path):
    c, ready = fixtures.present()
    env = Environment([ready])
    entered, release = threading.Event(), threading.Event()
    results = []

    def observe(**kwargs):
        entered.set()
        assert release.wait(5)
        return ready

    thread = threading.Thread(target=lambda: results.append(run(c, env, tmp_path, observe=observe)))
    thread.start()
    try:
        assert entered.wait(5)
        assert run(c, env, tmp_path)['status'] == 'busy'
        assert env.starts == env.reads == 0
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert results[0]['status'] == 'present_ready'


@pytest.mark.parametrize('options', [{'timeout_seconds': 0}, {'timeout_seconds': True},
                                     {'timeout_seconds': float('nan')}, {'timeout_seconds': 61},
                                     {'poll_seconds': 0}, {'poll_seconds': 4}])
def test_invalid_wait_budget_never_observes_or_starts(options, tmp_path):
    c, missing = fixtures.fixture()
    env = Environment([missing])
    assert run(c, env, tmp_path, **options)['status'] == 'refused'
    assert env.starts == env.reads == 0


def test_elapsed_observation_budget_prevents_launch(tmp_path):
    c, missing = fixtures.fixture()
    env = Environment([missing])

    def observe(**kwargs):
        env.time += 4
        return missing

    assert run(c, env, tmp_path, observe=observe)['status'] == 'timeout'
    assert env.starts == 0
    assert not (tmp_path / 'control-mysql-attempt.json').exists()


def test_changed_port_after_launch_refuses_without_second_start(tmp_path):
    c, missing = fixtures.fixture()
    _, wrong = fixtures.present()
    wrong['probe']['server_uuid'] = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
    env = Environment([missing, missing, wrong])
    assert run(c, env, tmp_path)['status'] == 'refused'
    assert env.starts == 1
    assert (tmp_path / 'control-mysql-attempt.json').exists()


def test_observation_limit_is_distinct_from_elapsed_timeout(tmp_path):
    c, starting = fixtures.present()
    starting['probe'] = None
    env = Environment([starting])
    result = run(c, env, tmp_path, poll_seconds=0.001)
    assert result['status'] == 'timeout'
    assert result['reasons'] == ['observation_limit_exceeded']
    assert env.reads == 128
    assert env.time < 3
    assert env.starts == 0


def test_cross_process_interlock_with_same_state_directory(tmp_path):
    c, ready = fixtures.present()
    env = Environment([ready])
    program = """
import json,sys
from scripts.platform.mysql_recovery_attempt import run_attempt
def forbidden(**kwargs):
    raise AssertionError('busy caller must not invoke capabilities')
print(json.dumps(run_attempt({},state_dir=sys.argv[1],observe=forbidden,start=forbidden)))
"""

    def observe(**kwargs):
        child = subprocess.run([sys.executable, '-B', '-c', program, str(tmp_path)],
                               cwd=ROOT, capture_output=True, text=True, timeout=10)
        assert child.returncode == 0, child.stderr
        assert json.loads(child.stdout)['status'] == 'busy'
        return ready

    assert run(c, env, tmp_path, observe=observe)['status'] == 'present_ready'
    assert env.starts == 0


def test_slow_start_cannot_be_reported_ready_after_budget_expiry(tmp_path):
    c, missing = fixtures.fixture()
    _, ready = fixtures.present()
    env = Environment([missing, missing, ready])

    def start(**kwargs):
        env.start(**kwargs)
        env.time += 4

    result = run(c, env, tmp_path, start=start)
    assert result['status'] == 'timeout'
    assert env.starts == 1
    assert env.reads == 2
    assert (tmp_path / 'control-mysql-attempt.json').exists()


def test_observer_cannot_change_frozen_contract(tmp_path):
    c, missing = fixtures.fixture()
    env = Environment([missing])

    def observe(**kwargs):
        c['server_uuid'] = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        altered = deepcopy(missing)
        altered['files']['datadir_uuid'] = c['server_uuid']
        return altered

    assert run(c, env, tmp_path, observe=observe)['status'] == 'refused'
    assert env.starts == 0


def test_intent_write_failure_never_dispatches_start(tmp_path, monkeypatch):
    c, missing = fixtures.fixture()
    env = Environment([missing])

    def fail(fd):
        raise OSError('private filesystem failure')

    monkeypatch.setattr(mod.os, 'fsync', fail)
    result = run(c, env, tmp_path)
    assert result['status'] == 'refused'
    assert env.starts == 0
    assert (tmp_path / 'control-mysql-attempt.json').exists()
    assert run(c, env, tmp_path)['status'] == 'pending'
