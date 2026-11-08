"""Opt-in, read-only C TLS acceptance; no target configuration or grants change.

Set DBA_PLATFORM_CONFIG and DBA_PLATFORM_TLS_TARGET to private JSON files.
The target must be the dedicated synthetic C server on its fixed lab port.
Only public CA/server certificates are read over the pinned SSH connection.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path, PurePosixPath
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest

from platform_app.config import load_config
from platform_app.connectors import ConnectorError, open_mysql, open_ssh


@dataclass(repr=False)
class PrivateTLSFixture:
    config: object = field(repr=False)
    target: dict = field(repr=False)
    ssh_profile: dict = field(repr=False)
    certificate_identity: str
    ca_path: Path
    wrong_ca_path: Path

    def connect(self, hostname, ca):
        return open_mysql(
            {
                "mode": "ssh",
                "db_host": self.target["host"],
                "db_port": self.target["port"],
                "ssh_profile_ref": self.ssh_profile["id"],
                "tls_mode": "verify_identity",
                "tls_ca": str(ca),
                "tls_server_name": hostname,
                "expected_server_uuid": self.target["server_uuid"],
            },
            {"username": self.target["username"], "password": self.target["password"]},
            time.monotonic() + 20,
            ssh_profile_resolver=lambda _: self.ssh_profile,
        )


@pytest.fixture(scope="module")
def live_tls(tmp_path_factory):
    config_path = os.environ.get("DBA_PLATFORM_CONFIG")
    target_path = os.environ.get("DBA_PLATFORM_TLS_TARGET")
    if not config_path or not target_path:
        pytest.skip("Explicit private synthetic-C TLS acceptance config required")
    config = load_config(config_path)
    target = json.loads(Path(target_path).read_text(encoding="utf-8-sig"))
    directory = PurePosixPath(target["directory"])
    if (
        target.get("port") != 3310
        or not str(directory).startswith("/opt/tdx-lab/")
        or ".." in directory.parts
    ):
        pytest.fail("Dedicated synthetic C target guard rejected the configuration", pytrace=False)
    ssh_profile = next(
        profile for profile in config.ssh_profiles if profile["id"] == "lab-vm-pinned"
    )
    ca = cert = None
    with open_ssh(ssh_profile, time.monotonic() + 15) as ssh:
        with ssh.open_sftp() as sftp:
            for folder in (directory / "data", directory):
                try:
                    with sftp.open(str(folder / "ca.pem"), "rb") as stream:
                        ca = stream.read(65537)
                    with sftp.open(str(folder / "server-cert.pem"), "rb") as stream:
                        cert = stream.read(65537)
                    break
                except FileNotFoundError:
                    continue
    if not ca or not cert or len(ca) > 65536 or len(cert) > 65536:
        pytest.fail("Bounded public C certificates unavailable", pytrace=False)
    certificate = x509.load_pem_x509_certificate(cert)
    try:
        dns = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        dns = []
    common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    identity = dns[0] if dns else common_names[0].value if common_names else None
    if not identity:
        pytest.fail("No certificate DNS identity available", pytrace=False)
    private = tmp_path_factory.mktemp("tls-public-certificates")
    ca_path = private / "ca.pem"
    ca_path.write_bytes(ca)
    # A new unrelated test CA, not a disabled verification context.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Unrelated acceptance authority")])
    wrong = (
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
    wrong_path = private / "wrong-ca.pem"
    wrong_path.write_bytes(wrong.public_bytes(serialization.Encoding.PEM))
    return PrivateTLSFixture(config, target, ssh_profile, identity, ca_path, wrong_path)


@pytest.mark.parametrize(
    "case", ["certificate_identity", "configured_endpoint_identity", "wrong_identity", "wrong_ca"]
)
def test_real_mysql_tls_identity_and_cipher_readonly(live_tls, case):
    hostname = live_tls.certificate_identity
    ca = live_tls.ca_path
    if case == "configured_endpoint_identity":
        hostname = live_tls.target["host"]
    elif case == "wrong_identity":
        hostname = "wrong.fixture.invalid"
    elif case == "wrong_ca":
        ca = live_tls.wrong_ca_path
    if case == "certificate_identity":
        with live_tls.connect(hostname, ca) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
                cipher = cursor.fetchone().get("Value")
                cursor.execute("SELECT @@server_uuid AS identity")
                matches = cursor.fetchone()["identity"] == live_tls.target["server_uuid"]
            assert bool(cipher) and matches
    else:
        with pytest.raises(ConnectorError) as error:
            with live_tls.connect(hostname, ca):
                pass
        assert error.value.code == "tls_verification_failed"
