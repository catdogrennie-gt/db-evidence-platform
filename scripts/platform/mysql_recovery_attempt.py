"""Engineering-only, one-shot recovery orchestration with trusted bounded ports.

No SQL, process launcher, shell, API route or executable CLI is provided here.
The private adapter must fix a protected shared state directory and implement
bounded observe/start capabilities. Python cannot interrupt a stuck callback.
Never remove an intent or stale lock automatically to retry an unknown start.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time

from scripts.platform.mysql_recovery import evaluate


class _Deadline(Exception):
    pass


class _ObservationLimit(Exception):
    pass


def _result(status, reason=None, attempts=0):
    return {'status': status, 'reasons': [reason] if reason else [],
            'start_attempts': attempts}


def run_attempt(contract, *, state_dir, observe, start, timeout_seconds=20,
                poll_seconds=1, monotonic=time.monotonic, sleep=time.sleep,
                utcnow=lambda: datetime.now(timezone.utc)):
    """Run at most one fixed start; retained intent blocks subsequent replay.

    A healthy service is always a no-op, even with an old intent. A missing
    service after ANY prior intent requires independent review. This is not
    an automatic multi-boot recovery manager. All cooperating callers must
    use the same protected directory, not a new directory per invocation.
    """
    try:
        if (type(timeout_seconds) not in (int, float)
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60
                or type(poll_seconds) not in (int, float)
                or not math.isfinite(poll_seconds) or not 0 < poll_seconds <= timeout_seconds
                or not all(callable(x) for x in (observe, start, monotonic, sleep, utcnow))):
            return _result('refused', 'invalid_attempt_input')
        root = Path(state_dir)
        if root.is_symlink() or not root.is_dir():
            return _result('refused', 'invalid_state_directory')
        c = deepcopy(contract)
        fingerprint = hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()
        started = last_clock = monotonic()
        if not math.isfinite(started):
            return _result('refused', 'invalid_attempt_clock')
        deadline = started + timeout_seconds
    except Exception:
        return _result('refused', 'invalid_attempt_input')

    lock = root / 'control-mysql-recovery.lock'
    intent = root / 'control-mysql-attempt.json'
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
    try:
        lock_fd = os.open(lock, flags, 0o600)
    except FileExistsError:
        return _result('busy', 'recovery_lock_present')
    except OSError:
        return _result('refused', 'recovery_lock_unavailable')

    attempts = reads = 0
    lock_identity = os.fstat(lock_fd)

    def remaining():
        nonlocal last_clock
        current = monotonic()
        if not math.isfinite(current) or current < last_clock:
            raise ValueError('invalid clock')
        last_clock = current
        budget = deadline - current
        if budget <= 0:
            raise _Deadline()
        return budget

    def inspect():
        nonlocal reads
        if reads >= 128:
            raise _ObservationLimit()
        reads += 1
        observation = observe(timeout_seconds=remaining())
        remaining()
        return evaluate(c, observation, now=utcnow())

    def terminal(decision):
        if decision['status'] == 'present_ready':
            return _result('ready_after_attempt' if attempts else 'present_ready', attempts=attempts)
        if decision['status'] == 'refused':
            return _result('refused', decision['reasons'][0], attempts)
        return None

    try:
        decision = inspect()
        result = terminal(decision)
        if result is not None:
            return result
        if decision['status'] == 'start_required':
            # An unresolved, malformed, or completed intent is never auto-replaced.
            if os.path.lexists(intent):
                return _result('pending', 'prior_attempt_requires_review')
            decision = inspect()  # Fresh check inside the interlock, just before action.
            result = terminal(decision)
            if result is not None:
                return result
            if decision['status'] == 'start_required':
                remaining()
                payload = json.dumps({'version': 1, 'state': 'intent',
                                      'contract_sha256': fingerprint,
                                      'at': utcnow().isoformat()}).encode()
                try:
                    fd = os.open(intent, flags, 0o600)
                except FileExistsError:
                    return _result('pending', 'prior_attempt_requires_review')
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                budget = remaining()
                attempts = 1  # Count dispatch even if the outcome becomes unknown.
                try:
                    start(timeout_seconds=budget)
                except Exception:
                    return _result('start_outcome_unknown', 'start_requires_review', attempts)

        # Never start again, including if an existing-but-unready process disappears.
        while True:
            result = terminal(inspect())
            if result is not None:
                return result
            sleep(min(poll_seconds, remaining()))
    except _Deadline:
        return _result('timeout', 'readiness_deadline_exceeded', attempts)
    except _ObservationLimit:
        return _result('timeout', 'observation_limit_exceeded', attempts)
    except Exception:
        return _result('refused', 'attempt_observation_or_state_unavailable', attempts)
    finally:
        os.close(lock_fd)
        try:
            current = lock.stat()
            if (current.st_dev, current.st_ino) == (lock_identity.st_dev, lock_identity.st_ino):
                lock.unlink()
        except OSError:
            pass  # A stale/changed lock is left for independent review, never stolen.
