"""Profile-driven, bounded MySQL connections; no import-time secret loading."""

from contextlib import contextmanager
import select
import ipaddress
import socket
import ssl
import threading
import time
import paramiko
import pymysql
from pymysql.constants import CLIENT


class ConnectorError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _VerifiedTLSContext:
    """Verify the actual transport before the driver sends authentication."""

    def __init__(self, context):
        if (
            not isinstance(context, ssl.SSLContext)
            or context.verify_mode != ssl.CERT_REQUIRED
            or not context.check_hostname
        ):
            raise ConnectorError("tls_verification_required")
        self.context = context

    def wrap_socket(self, *args, **kwargs):
        wrapped = self.context.wrap_socket(*args, **kwargs)
        if not isinstance(wrapped, ssl.SSLSocket) or not wrapped.cipher():
            wrapped.close()
            raise ConnectorError("tls_not_established")
        return wrapped


class RequiredTLSConnection(pymysql.Connection):
    """PyMySQL 1.1.2 otherwise skips TLS when CLIENT.SSL is not advertised.

    Keep the pinned driver's authentication implementation and guard only its
    TLS entry point: capability before auth, validated cipher before auth bytes.
    Primary source: PyMySQL v1.1.2 pymysql/connections.py:_request_authentication.
    """

    def _request_authentication(self):
        if not self.ssl:
            raise ConnectorError("tls_required")
        if not self.server_capabilities & CLIENT.SSL:
            raise ConnectorError("tls_not_supported")
        original = self.ctx
        self.ctx = _VerifiedTLSContext(original)
        try:
            return super()._request_authentication()
        finally:
            self.ctx = original


def safe_error_code(exc):
    if isinstance(exc, ConnectorError):
        return exc.code
    if isinstance(exc, (PermissionError, paramiko.AuthenticationException)):
        return (
            "permission_denied" if isinstance(exc, PermissionError) else "ssh_authentication_failed"
        )
    if isinstance(exc, paramiko.BadHostKeyException):
        return "ssh_host_key_mismatch"
    if isinstance(exc, ssl.SSLError):
        return "tls_verification_failed"
    # PyMySQL wraps socket/SSL failures as OperationalError. Inspect typed
    # causes only, never copy driver messages, endpoints or credential values.
    cause = getattr(exc, "original_exception", None) or exc.__cause__ or exc.__context__
    for _ in range(4):
        if isinstance(cause, ssl.SSLError):
            return "tls_verification_failed"
        if cause is None:
            break
        cause = getattr(cause, "original_exception", None) or cause.__cause__ or cause.__context__
    code = exc.args[0] if getattr(exc, "args", ()) else None
    if code in (1044, 1045, 1142, 1227, 1370):
        return "authentication_failed" if code == 1045 else "permission_denied"
    if code in (1054, 1109, 1146, 1193, 1235, 1064):
        return "unsupported"
    if code in (3024, 1969) or isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if code in (2002, 2003, 2006, 2013):
        return "unreachable"
    if isinstance(exc, paramiko.SSHException):
        return "ssh_host_key_or_transport_failed"
    return "collection_failed"


def remaining(deadline, cap=8):
    result = cap if deadline is None else min(cap, deadline - time.monotonic())
    if result <= 0:
        raise ConnectorError("deadline_exceeded")
    return max(0.001, result)


@contextmanager
def open_ssh(profile, deadline=None):
    client = paramiko.SSHClient()
    entered = False
    try:
        known_hosts = profile.get("known_hosts")
        if not known_hosts:
            raise ConnectorError("ssh_known_hosts_required")
        client.load_host_keys(known_hosts)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        timeout = remaining(deadline, 3)
        client.connect(
            profile["host"],
            port=int(profile.get("port", 22)),
            username=profile["username"],
            password=profile.get("password"),
            key_filename=profile.get("key_filename"),
            timeout=timeout,
            auth_timeout=remaining(deadline, 8),
            banner_timeout=remaining(deadline, 8),
            allow_agent=False,
            look_for_keys=False,
        )
        entered = True
        yield client
    except ConnectorError:
        raise
    except Exception as exc:
        if entered:
            raise
        raise ConnectorError(safe_error_code(exc)) from None
    finally:
        client.close()


@contextmanager
def _channel_socket(channel, deadline):
    # TLS needs an actual socket (Paramiko Channel cannot be SSL-wrapped).
    local, peer = socket.socketpair()
    stop = threading.Event()

    def forward():
        try:
            while not stop.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                readers, _, _ = select.select([peer, channel], [], [], 0.1)
                for reader in readers:
                    data = reader.recv(65536)
                    if not data:
                        return
                    (channel if reader is peer else peer).sendall(data)
        except (OSError, EOFError):
            pass
        finally:
            peer.close()
            channel.close()

    thread = threading.Thread(target=forward, name="mysql-ssh-tunnel", daemon=True)
    thread.start()
    try:
        yield local
    finally:
        stop.set()
        local.close()
        channel.close()
        thread.join(1)


@contextmanager
def open_mysql(profile, secret_handle, deadline=None, *, ssh_profile_resolver=None):
    """Yield DictCursor connection. SSH db_host is relative to the SSH server.

    Direct mode always verifies TLS hostname/CA. An explicitly isolated SSH
    profile may omit DB TLS: SSH host authentication still remains mandatory.
    """
    from contextlib import ExitStack

    value = getattr(secret_handle, "value", secret_handle)
    connection = None
    try:
        mode = profile.get("mode", profile.get("connection_mode", "direct"))
        host = profile.get("db_host", profile.get("endpoint"))
        port = int(profile.get("db_port", profile.get("port", 3306)))
        if not host or not 1 <= port <= 65535 or mode not in ("direct", "ssh"):
            raise ConnectorError("invalid_connection_profile")
        tls = None
        tls_mode = profile.get("tls_mode") or "verify_identity"
        if tls_mode not in ("verify_identity", "disabled"):
            raise ConnectorError("invalid_tls_mode")
        if tls_mode == "verify_identity":
            if not profile.get("tls_ca"):
                raise ConnectorError("tls_required")
            try:
                tls = ssl.create_default_context(cafile=profile["tls_ca"])
            except (OSError, ssl.SSLError, ValueError):
                raise ConnectorError("tls_configuration_invalid") from None
            tls.check_hostname = True
            tls.verify_mode = ssl.CERT_REQUIRED
            if profile.get("tls_cert"):
                tls.load_cert_chain(profile["tls_cert"], profile.get("tls_key"))
        elif mode == "direct":
            try:
                isolated_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                isolated_loopback = False
            if not isolated_loopback:
                raise ConnectorError("tls_required")
        with ExitStack() as stack:
            channel = None
            if mode == "ssh":
                if ssh_profile_resolver is None or not profile.get("ssh_profile_ref"):
                    raise ConnectorError("ssh_profile_required")
                ssh_profile = ssh_profile_resolver(profile["ssh_profile_ref"])
                ssh = stack.enter_context(open_ssh(ssh_profile, deadline))
                channel = ssh.get_transport().open_channel(
                    "direct-tcpip", (host, port), ("127.0.0.1", 0), timeout=remaining(deadline, 3)
                )
                stack.callback(channel.close)
                if tls:
                    channel = stack.enter_context(_channel_socket(channel, deadline))
            else:
                # The caller may resolve/allowlist and pin db_host as an IP.
                # Keep the TLS name separate so PyMySQL cannot re-resolve it.
                channel = socket.create_connection((host, port), remaining(deadline, 3))
                stack.callback(channel.close)
            driver = RequiredTLSConnection if tls is not None else pymysql.Connection
            connection = driver(
                host=profile.get("tls_server_name") or host,
                port=port,
                user=value["username"],
                password=value["password"],
                database=None,
                autocommit=True,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                ssl=tls,
                ssl_disabled=tls is None,
                connect_timeout=remaining(
                    deadline, min(3, float(profile.get("connect_timeout_seconds", 3)))
                ),
                read_timeout=remaining(deadline, 8),
                write_timeout=remaining(deadline, 3),
                defer_connect=True,
            )
            stack.callback(connection.close)
            connection.connect(sock=channel)
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION time_zone='+00:00'")
                cursor.execute("SET SESSION MAX_EXECUTION_TIME=2000")
                cursor.execute("SELECT @@server_uuid AS server_uuid")
                row = cursor.fetchone()
                if not row or not row.get("server_uuid"):
                    raise ConnectorError("server_identity_unavailable")
                expected = profile.get("expected_server_uuid")
                if expected and expected != row["server_uuid"]:
                    raise ConnectorError("identity_mismatch")
            yield connection
    except ConnectorError:
        raise
    except Exception as exc:
        raise ConnectorError(safe_error_code(exc)) from None
