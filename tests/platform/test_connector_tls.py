"""TLS fail-closed boundaries and real TLS handshakes on private socket pairs."""

from datetime import datetime, timedelta, timezone
import socket
import ssl
import threading
from unittest.mock import Mock, patch

import pymysql
from pymysql.constants import CLIENT
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from platform_app.connectors import (
    ConnectorError,
    RequiredTLSConnection,
    _VerifiedTLSContext,
    open_mysql,
)


def test_required_tls_refuses_missing_server_ssl_before_authentication():
    connection = RequiredTLSConnection(
        user="fixture", password="test-only", ssl=ssl.create_default_context(), defer_connect=True
    )
    connection.server_capabilities = 0
    with patch.object(pymysql.Connection, "_request_authentication") as authenticate:
        with pytest.raises(ConnectorError) as error:
            connection._request_authentication()
    assert error.value.code == "tls_not_supported" and not authenticate.called


@pytest.mark.parametrize("mode", ["direct", "ssh"])
def test_verify_identity_always_requires_explicit_ca_even_with_ssh(mode):
    with (
        patch("platform_app.connectors.open_ssh") as ssh,
        patch("platform_app.connectors.socket.create_connection") as dial,
    ):
        with pytest.raises(ConnectorError) as error:
            with open_mysql(
                {"mode": mode, "db_host": "127.0.0.1", "tls_mode": "verify_identity"},
                {"username": "fixture", "password": "test-only"},
            ):
                pass
    assert error.value.code == "tls_required" and not ssh.called and not dial.called


def test_required_tls_refuses_cipherless_transport_before_auth_payload():
    context = ssl.create_default_context()
    connection = RequiredTLSConnection(
        user="fixture", password="test-only", ssl=context, defer_connect=True
    )
    connection.server_capabilities = CLIENT.SSL
    connection.server_version = "8.0.0"
    connection._sock = Mock()
    transport = Mock(spec=ssl.SSLSocket)
    transport.cipher.return_value = None
    connection.write_packet = Mock()
    with patch.object(ssl.SSLContext, "wrap_socket", return_value=transport):
        with pytest.raises(ConnectorError) as error:
            connection._request_authentication()
    # The unchanged pinned driver may send its 32-byte SSLRequest header first,
    # but no username or authentication response crosses a cipherless channel.
    packets = [call.args[0] for call in connection.write_packet.call_args_list]
    assert error.value.code == "tls_not_established" and transport.close.called
    assert len(packets) == 1 and len(packets[0]) == 32 and b"fixture" not in packets[0]


def test_required_tls_refuses_non_socket_or_unverified_context():
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with pytest.raises(ConnectorError) as error:
        _VerifiedTLSContext(context)
    assert error.value.code == "tls_verification_required"
    context = ssl.create_default_context()
    with patch.object(ssl.SSLContext, "wrap_socket", return_value=Mock()):
        with pytest.raises(ConnectorError) as error:
            _VerifiedTLSContext(context).wrap_socket(object(), server_hostname="mysql.fixture.test")
    assert error.value.code == "tls_not_established"


@pytest.fixture
def certificates(tmp_path):
    now = datetime.now(timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Private TLS fixture authority")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mysql.fixture.test")]))
        .issuer_name(name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("mysql.fixture.test")]), critical=False
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    ca_path = tmp_path / "ca.pem"
    cert_path = tmp_path / "server.pem"
    key_path = tmp_path / "server-key.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


@pytest.mark.parametrize("failure", [None, "wrong_ca", "wrong_hostname"])
def test_actual_tls_certificate_and_hostname_validation_on_socketpair(certificates, failure):
    ca, cert, key = certificates
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    context = ssl.create_default_context(cafile=None if failure == "wrong_ca" else ca)
    client, server = socket.socketpair()
    client.settimeout(3)
    server.settimeout(3)

    def serve():
        try:
            with server_context.wrap_socket(server, server_side=True) as secure:
                secure.recv(1)
        except OSError:
            pass

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        host = "wrong.fixture.test" if failure == "wrong_hostname" else "mysql.fixture.test"
        if failure:
            with pytest.raises(ssl.SSLCertVerificationError):
                _VerifiedTLSContext(context).wrap_socket(client, server_hostname=host)
        else:
            with _VerifiedTLSContext(context).wrap_socket(client, server_hostname=host) as secure:
                assert (
                    isinstance(secure, ssl.SSLSocket) and secure.cipher() and secure.getpeercert()
                )
                secure.sendall(b"1")
    finally:
        client.close()
        worker.join(3)
        server.close()
    assert not worker.is_alive()


def test_tls_server_name_only_comes_from_private_profile_not_browser_fields():
    from types import SimpleNamespace
    from platform_app.collection_service import _target

    config = SimpleNamespace(
        allowed_networks=["127.0.0.0/8"],
        allowed_db_ports=[3306],
        ssh_profiles=[],
        tls_profiles=[
            {
                "id": "trusted",
                "ca_file": "private-ca-fixture.pem",
                "server_name": "mysql.fixture.test",
            }
        ],
    )
    request = {
        "mode": "direct",
        "db_host": "127.0.0.1",
        "db_port": 3306,
        "tls_mode": "verify_identity",
        "tls_profile_ref": "trusted",
        "tls_server_name": "attacker.invalid",
        "tls_ca": "attacker-ca.pem",
    }
    actual = _target(config, request, "expected-fixture")
    assert (
        actual["tls_server_name"] == "mysql.fixture.test"
        and actual["tls_ca"] == "private-ca-fixture.pem"
    )
    request["tls_profile_ref"] = None
    actual = _target(config, request)
    assert actual["tls_server_name"] == "127.0.0.1" and "tls_ca" not in actual
