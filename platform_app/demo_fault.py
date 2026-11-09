"""One fixed, rollback-only Lab fault. No browser supplied SQL or connection data.

Direct observations here are safety checks, not platform monitoring evidence.
Zabbix remains the source of the investigation's business observations.
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import timedelta, timezone
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import threading
import time

from .common import iso
from .connectors import open_mysql, open_ssh
from .db import utcnow

FAULT_SQL = 'UPDATE lab_orders.orders SET amount_cents=amount_cents+1 WHERE id=1'
SCENARIO_USER = 'demo_scenario'
MARKER = {'marker': 'signallens-lab-v1', 'container': 'signallens-lab-target',
          'owner': 'signallens-lab', 'role': 'target', 'database': 'lab_orders'}


def holder_idle_seconds(remaining):
    # After assigning this timeout only SET's reply and the bounded UPDATE remain:
    # write <=3s + read <=8s + next write <=3s + row wait <=2s. Keep 20s slack.
    seconds = math.floor(remaining) - 20
    if seconds < 3:
        raise DemoFaultError('demo_deadline_expired')
    return seconds


class DemoFaultError(RuntimeError):
    def __init__(self, code='demo_target_unavailable'):
        self.code = code
        super().__init__(code)


def target_config(config):
    """Read only the operator-configured absolute private file; never echo it."""
    settings = getattr(config, 'demo', {}) or {}
    path = Path(settings.get('target_config_path', ''))
    if not path.is_absolute():
        raise DemoFaultError('demo_target_configuration_invalid')
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        profile = value['profile']
        address = ipaddress.ip_address(profile['db_host'])
        if (profile['mode'] != 'ssh' or not address.is_private
                or profile.get('tls_mode') != 'disabled'
                or profile.get('db_port') != 3306
                or not profile.get('expected_server_uuid')
                or value['scenario']['username'] != SCENARIO_USER
                or value['monitor']['username'] != 'monitor'
                or value['expected_marker'] != MARKER
                or len(value['baseline_sha256']) != 64
                or not value['ssh'].get('known_hosts')):
            raise ValueError()
        return value
    except (OSError, ValueError, KeyError, TypeError):
        raise DemoFaultError('demo_target_configuration_invalid') from None


def validate_container(data, cfg):
    try:
        labels = data['Config']['Labels']
        networks = data['NetworkSettings']['Networks']
        if (data['Name'] != '/signallens-lab-target'
                or not data['State']['Running']
                or data['HostConfig']['NetworkMode'] == 'host'
                or labels.get('sl.owner') != 'signallens-lab'
                or labels.get('sl.role') != 'target'
                or labels.get('com.docker.compose.project') != 'signallens-lab'
                or set(networks) != {'signallens-lab_lab', 'signallens-internal'}
                or networks['signallens-lab_lab']['IPAddress'] != cfg['profile']['db_host']
                or any(data['NetworkSettings'].get('Ports', {}).values())):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise DemoFaultError('demo_target_identity_mismatch') from None


def _container_guard(cfg):
    with open_ssh(cfg['ssh'], time.monotonic() + 10) as ssh:
        _, stdout, _ = ssh.exec_command('docker inspect signallens-lab-target', timeout=8)
        payload = stdout.read(65537)
        if len(payload) > 65536 or stdout.channel.recv_exit_status() != 0:
            raise DemoFaultError('demo_target_identity_mismatch')
        validate_container(json.loads(payload)[0], cfg)


def _connect(stack, cfg, identity, deadline=None):
    return stack.enter_context(open_mysql(
        cfg['profile'], cfg[identity], deadline,
        ssh_profile_resolver=lambda _: cfg['ssh'],
    ))


def _marker(connection, cfg):
    with connection.cursor() as cursor:
        cursor.execute('SELECT marker,container,owner,role,`database` FROM lab_orders.identity_marker WHERE id=1')
        if cursor.fetchone() != cfg['expected_marker']:
            raise DemoFaultError('demo_target_identity_mismatch')


def dataset_sha256(connection):
    digest = hashlib.sha256()
    with connection.cursor() as cursor:
        cursor.execute('SELECT id,customer_id,amount_cents,status FROM lab_orders.orders ORDER BY id')
        for row in cursor.fetchall():
            digest.update((json.dumps(row, sort_keys=True, separators=(',', ':')) + '\n').encode())
    return digest.hexdigest()


def _idle(connection, cfg):
    with connection.cursor() as cursor:
        cursor.execute('SELECT COUNT(*) AS n FROM performance_schema.threads WHERE PROCESSLIST_USER=%s', (SCENARIO_USER,))
        sessions = int(cursor.fetchone()['n'])
        cursor.execute('SELECT COUNT(*) AS n FROM performance_schema.data_lock_waits w '
                       'JOIN performance_schema.threads r ON r.THREAD_ID=w.REQUESTING_THREAD_ID '
                       'JOIN performance_schema.threads b ON b.THREAD_ID=w.BLOCKING_THREAD_ID '
                       'WHERE r.PROCESSLIST_USER=%s OR b.PROCESSLIST_USER=%s', (SCENARIO_USER, SCENARIO_USER))
        waits = int(cursor.fetchone()['n'])
    return {'sessions_closed': sessions == 0, 'locks_cleared': waits == 0,
            'dataset_unchanged': dataset_sha256(connection) == cfg['baseline_sha256'],
            'observed_at': iso(utcnow())}


def verify_idle(config):
    """Crash recovery: no injection, no KILL, fail closed on any unknown check."""
    try:
        cfg = target_config(config)
        _container_guard(cfg)
        with ExitStack() as stack:
            monitor = _connect(stack, cfg, 'monitor', time.monotonic() + 15)
            _marker(monitor, cfg)
            return _idle(monitor, cfg)
    except Exception:
        return {'sessions_closed': False, 'locks_cleared': False, 'dataset_unchanged': False,
                'error_code': 'demo_cleanup_unverified', 'observed_at': iso(utcnow())}


class FixedLabFault:
    def __init__(self, config, *, expires_in_seconds=None, expires_at=None):
        self.config = config
        self.cfg = target_config(config)
        settings = getattr(config, 'demo', {}) or {}
        seconds = settings.get('max_duration_seconds', 180) if expires_in_seconds is None else expires_in_seconds
        self._expires_at = expires_at or (utcnow() + timedelta(seconds=float(seconds)))
        if self._expires_at.tzinfo is not None:
            self._expires_at = self._expires_at.astimezone(timezone.utc).replace(tzinfo=None)
        seconds = min(float(seconds), (self._expires_at-utcnow()).total_seconds())
        self._deadline = time.monotonic() + max(0, min(180, seconds))
        self._stack = ExitStack()
        self._holder = self._waiter = self._monitor = None
        self._holder_id = self._waiter_id = None
        self._stop_event = threading.Event()
        self._mutex = threading.RLock()
        self._thread = self._timer = None
        self._cleanup = None
        self._waiter_failed = False

    def _remaining(self):
        value = min(self._deadline - time.monotonic(), (self._expires_at-utcnow()).total_seconds())
        if value < 3:
            raise DemoFaultError('demo_deadline_expired')
        return value

    def start(self):
        try:
            with self._mutex:
                self._remaining()
                _container_guard(self.cfg)
                self._monitor = _connect(self._stack, self.cfg, 'monitor', self._deadline)
                _marker(self._monitor, self.cfg)
                idle = _idle(self._monitor, self.cfg)
                if not all(idle[key] for key in ('sessions_closed', 'locks_cleared', 'dataset_unchanged')):
                    raise DemoFaultError('demo_target_not_idle')
                self._holder = _connect(self._stack, self.cfg, 'scenario', self._deadline)
                self._waiter = _connect(self._stack, self.cfg, 'scenario', self._deadline)
                # Set autocommit=False at the driver and server; neither UPDATE is committed.
                for connection in (self._holder, self._waiter):
                    connection.autocommit(False)
                with self._waiter.cursor() as cursor:
                    cursor.execute('SET SESSION innodb_lock_wait_timeout=2')
                    cursor.execute('SET SESSION wait_timeout=10')
                    cursor.execute('SELECT CONNECTION_ID() AS id')
                    self._waiter_id = int(cursor.fetchone()['id'])
                with self._holder.cursor() as cursor:
                    # No later holder keepalive resets this server-side bound.
                    cursor.execute('SET SESSION innodb_lock_wait_timeout=2')
                    cursor.execute('SELECT CONNECTION_ID() AS id')
                    self._holder_id = int(cursor.fetchone()['id'])
                    idle_seconds = holder_idle_seconds(self._remaining())
                    cursor.execute('SET SESSION wait_timeout=%s', (idle_seconds,))
                    self._remaining()
                    self._timer = threading.Timer(max(0, self._deadline-time.monotonic()), self.stop)
                    self._timer.daemon = True
                    self._timer.start()
                    cursor.execute(FAULT_SQL)
                self._thread = threading.Thread(target=self._wait, name='fixed-demo-waiter', daemon=True)
                self._thread.start()
            until = min(self._deadline, time.monotonic()+5)
            while time.monotonic() < until:
                sample = self.sample()
                if sample['active']:
                    return {'confirmed': True, 'active': True,
                            'sessions': [self._holder_id, self._waiter_id], 'observed_at': sample['observed_at']}
                if self._stop_event.wait(.1):
                    break
            raise DemoFaultError('demo_fault_not_confirmed')
        except Exception as exc:
            if self._holder is not None or self._waiter is not None or self._monitor is not None:
                self.stop()
            else:
                self._stop_event.set()
                self._stack.close()
            if isinstance(exc, DemoFaultError):
                raise
            raise DemoFaultError('demo_target_unavailable') from None

    def _wait(self):
        while not self._stop_event.is_set() and time.monotonic() < self._deadline:
            try:
                with self._waiter.cursor() as cursor:
                    cursor.execute(FAULT_SQL)
            except Exception as exc:
                if not getattr(exc, 'args', ()) or exc.args[0] != 1205:
                    self._waiter_failed = True
                    break
            finally:
                try:
                    self._waiter.rollback()
                except Exception:
                    self._waiter_failed = True
            self._stop_event.wait(.03)

    def sample(self):
        with self._mutex:
            if self._cleanup is not None or self._stop_event.is_set():
                return {'active': False, 'row_count': 0, 'observed_at': iso(utcnow())}
            self._remaining()
            with self._monitor.cursor() as cursor:
                cursor.execute('SELECT COUNT(*) AS n FROM performance_schema.data_lock_waits w '
                               'JOIN performance_schema.threads r ON r.THREAD_ID=w.REQUESTING_THREAD_ID '
                               'JOIN performance_schema.threads b ON b.THREAD_ID=w.BLOCKING_THREAD_ID '
                               'WHERE r.PROCESSLIST_ID=%s AND b.PROCESSLIST_ID=%s '
                               'AND r.PROCESSLIST_USER=%s AND b.PROCESSLIST_USER=%s',
                               (self._waiter_id, self._holder_id, SCENARIO_USER, SCENARIO_USER))
                count = int(cursor.fetchone()['n'])
            return {'active': count > 0 and not self._waiter_failed, 'row_count': count, 'observed_at': iso(utcnow())}

    def stop(self):
        """Always rollback/close both, even when one transport operation fails."""
        with self._mutex:
            if self._cleanup and all(self._cleanup.get(k) for k in ('sessions_closed', 'locks_cleared', 'dataset_unchanged')):
                return dict(self._cleanup)
            self._stop_event.set()
            if self._timer:
                self._timer.cancel()
            # Release holder first; waiter has a 2s lock timeout and always rolls back.
            for connection in (self._holder,):
                if connection is not None:
                    try: connection.rollback()
                    except Exception: pass
                    try: connection.close()
                    except Exception: pass
            if self._thread and self._thread is not threading.current_thread():
                self._thread.join(timeout=4)
            if self._waiter is not None:
                try: self._waiter.rollback()
                except Exception: pass
                try: self._waiter.close()
                except Exception: pass
            try: self._stack.close()
            except Exception: pass
            self._holder = self._waiter = self._monitor = None
            self._cleanup = verify_idle(self.config)
            return dict(self._cleanup)
