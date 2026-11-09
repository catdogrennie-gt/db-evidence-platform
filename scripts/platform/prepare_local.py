"""Create a NEW private Windows deployment plan; never installs or starts software.

Only the standard library is used. No prior lab/runtime configuration is read.
Existing destinations, including empty directories, are always refused.
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess

REPO = Path(__file__).resolve().parents[2]
MYSQL_VERSION = "8.4.13"
PROMETHEUS_VERSION = "3.13.5"


def _absolute(value):
    value = Path(value).expanduser()
    if not value.is_absolute() or any(c in str(value) for c in ("\0", "\r", "\n", '"')):
        raise ValueError("absolute_safe_path_required")
    return value.resolve()


def _protect(directory):
    if os.name == "nt":
        result = subprocess.run(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True
        )
        rows = list(csv.reader(io.StringIO(result.stdout)))
        sid = rows[-1][-1] if rows else ""
        if not re.fullmatch(r"S-1-\d+(?:-\d+)+", sid):
            raise ValueError("private_acl_identity_unavailable")
        # Restrict inheritance before writing any key or password. SYSTEM is
        # retained for local recovery; no Users/Everyone grant is introduced.
        subprocess.run(
            [
                "icacls.exe",
                str(directory),
                "/inheritance:r",
                "/grant:r",
                "*" + sid + ":(OI)(CI)F",
                "*S-1-5-18:(OI)(CI)F",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    else:
        directory.chmod(0o700)


def _write(path, value):
    data = value.encode("utf-8") if isinstance(value, str) else value
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _json(path, value):
    _write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prepare_local(
    private_dir,
    *,
    mysql_dir=None,
    prometheus_dir=None,
    control_port=13306,
    api_port=18080,
    prometheus_port=9090,
    bridge_port=9120,
):
    """Return non-secret paths after exclusive creation of a private template set."""
    original = Path(private_dir).expanduser()
    private = _absolute(original)
    repository = REPO.resolve()
    # Check both lexical and resolved locations, including directory junctions.
    if original.absolute().is_relative_to(repository) or private.is_relative_to(repository):
        raise ValueError("private_directory_must_be_outside_repository")
    if os.path.lexists(original) or private.exists():
        raise ValueError("private_directory_already_exists")
    if not private.parent.is_dir():
        raise ValueError("private_parent_directory_required")
    ports = (control_port, api_port, prometheus_port, bridge_port)
    if any(type(p) is not int or not 1 <= p <= 65535 for p in ports) or len(set(ports)) != len(
        ports
    ):
        raise ValueError("distinct_valid_loopback_ports_required")
    components = repository.parent / "components"
    mysql = _absolute(mysql_dir or components / f"mysql-{MYSQL_VERSION}-winx64")
    prometheus = _absolute(
        prometheus_dir or components / f"prometheus-{PROMETHEUS_VERSION}.windows-amd64"
    )
    private.mkdir(mode=0o700)
    _protect(private)
    for name in ("secrets", "logs", "pids", "mysql-data", "prometheus-data", "exports"):
        (private / name).mkdir(mode=0o700)
    root_password = secrets.token_urlsafe(36)
    database_password = secrets.token_urlsafe(36)
    admin_password = secrets.token_urlsafe(36)
    dba_password = secrets.token_urlsafe(36)
    key = base64.urlsafe_b64encode(secrets.token_bytes(32))
    _write(private / "fernet-master.key", key + b"\n")
    _write(private / "known_hosts", "")
    config = {
        "collection_mode": "zabbix_only",
        "control_db": {
            "host": "127.0.0.1",
            "port": control_port,
            "database": "dba_platform",
            "user": "dba_platform",
            "password": database_password,
        },
        "private_dir": str(private),
        "fernet_key_path": str(private / "fernet-master.key"),
        "secrets_dir": str(private / "secrets"),
        "known_hosts_path": str(private / "known_hosts"),
        "public_origin": f"http://127.0.0.1:{api_port}",
        "allowed_networks": ["127.0.0.0/8", "::1/128"],
        "ssh_profiles": [],
        "tls_profiles": [],
        "host_profiles": [],
        "log_profiles": [],
        "model": {},
        "prometheus_url": f"http://127.0.0.1:{prometheus_port}",
        "metrics_bridge_host": "127.0.0.1",
        "metrics_bridge_port": bridge_port,
        "components": {"mysql_dir": str(mysql), "prometheus_dir": str(prometheus)},
        "prometheus_config_path": str(private / "prometheus.yml"),
        "prometheus_listen_address": f"127.0.0.1:{prometheus_port}",
    }
    _json(private / "config.json", config)
    _json(
        private / "bootstrap-private.json",
        {
            "mysql_root": {"username": "root", "password": root_password},
            "platform_admin": {"username": "admin", "password": admin_password},
            "example_dba": {"username": "example_dba", "password": dba_password},
            "note": "Private credentials. Example DBA is not created by the default plan.",
        },
    )
    _json(
        private / "bootstrap-plan.json",
        {
            "departments": [],
            "systems": [],
            "users": [
                {
                    "username": "admin",
                    "display_name": "Local platform administrator",
                    "password": admin_password,
                    "grants": [{"role": "platform_admin", "scope_type": "global"}],
                }
            ],
        },
    )
    _json(
        private / "dba-example-plan.json",
        {
            "departments": [{"ref": "example", "name": "Example department"}],
            "systems": [
                {
                    "ref": "example",
                    "name": "Example system",
                    "department_ref": "example",
                    "environment": "lab",
                    "model_policy": "rules_only",
                }
            ],
            "users": [
                {
                    "username": "example_dba",
                    "display_name": "Example scoped DBA",
                    "password": dba_password,
                    "grants": [{"role": "dba", "scope_type": "system", "scope_ref": "example"}],
                }
            ],
        },
    )
    # Normal configuration intentionally has NO init-file. It must be an
    # explicit, temporary first-start argument and removed before normal start.
    _write(
        private / "my.ini",
        "[mysqld]\n"
        f'basedir="{mysql.as_posix()}"\ndatadir="{(private / "mysql-data").as_posix()}"\n'
        f"port={control_port}\nbind-address=127.0.0.1\nmysqlx=0\n"
        "skip-name-resolve=ON\nlocal-infile=OFF\nsecure-file-priv=NULL\n"
        "character-set-server=utf8mb4\ncollation-server=utf8mb4_0900_ai_ci\n"
        'default-time-zone="+00:00"\nmax_connections=100\n'
        f'log-error="{(private / "logs/mysql-error.log").as_posix()}"\n',
    )
    _write(
        private / "mysql-root-client.cnf",
        f"[client]\nuser=root\npassword={root_password}\nhost=127.0.0.1\nport={control_port}\nprotocol=TCP\nconnect-timeout=5\n",
    )
    _write(
        private / "mysql-first-start.sql",
        f"ALTER USER 'root'@'localhost' IDENTIFIED BY '{root_password}';\n"
        "CREATE DATABASE dba_platform CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;\n"
        f"CREATE USER 'root'@'127.0.0.1' IDENTIFIED BY '{root_password}';\n"
        "GRANT ALL PRIVILEGES ON dba_platform.* TO 'root'@'127.0.0.1';\n"
        "GRANT SHUTDOWN ON *.* TO 'root'@'127.0.0.1';\n"
        f"CREATE USER 'dba_platform'@'127.0.0.1' IDENTIFIED BY '{database_password}';\n"
        f"CREATE USER 'dba_platform'@'localhost' IDENTIFIED BY '{database_password}';\n"
        "GRANT ALL PRIVILEGES ON dba_platform.* TO 'dba_platform'@'127.0.0.1';\n"
        "GRANT ALL PRIVILEGES ON dba_platform.* TO 'dba_platform'@'localhost';\n",
    )
    _write(
        private / "prometheus.yml",
        "global:\n  scrape_interval: 15s\n  scrape_timeout: 5s\n  evaluation_interval: 15s\n"
        "scrape_configs:\n  - job_name: dba_platform\n    honor_timestamps: true\n"
        f"    static_configs:\n      - targets: ['127.0.0.1:{bridge_port}']\n",
    )
    _json(
        private / "components.json",
        {
            "mysql": {
                "reference_version": MYSQL_VERSION,
                "directory": str(mysql),
                "official_download": "https://dev.mysql.com/downloads/mysql/8.4.html",
                "executables": [
                    str(mysql / "bin" / v) for v in ("mysqld.exe", "mysql.exe", "mysqladmin.exe")
                ],
            },
            "prometheus": {
                "reference_version": PROMETHEUS_VERSION,
                "directory": str(prometheus),
                "official_download": "https://prometheus.io/download/",
                "executables": [str(prometheus / "prometheus.exe")],
            },
            "versions_are_reference_not_binary_attestation": True,
            "binaries_executed_or_downloaded": False,
            "operator_must_verify_version_signature_or_official_checksum": True,
        },
    )
    _write(
        private / "DEPLOYMENT-PLAN.txt",
        "New deployment templates only. Nothing has been initialized or started.\n"
        "1. Verify installed official component versions/signatures/checksums in components.json.\n"
        "2. Follow docs/platform/07-operations.md: dependencies, npm ci/build, fresh MySQL initialization.\n"
        "3. First start explicitly uses mysql-first-start.sql; stop gracefully, remove that SQL, restart normally.\n"
        "4. Set DBA_PLATFORM_CONFIG, migrate, apply bootstrap-plan.json, start managed services.\n"
        "5. Optional dba-example-plan.json is a separate deliberate grant decision; default admin has no business grants.\n"
        "6. Back up config/key/secrets/control database/Prometheus consistently into another private protected location.\n"
        "Never copy this directory into a source checkout or share it as an artifact.\n",
    )
    return {
        "private_dir": str(private),
        "config_path": str(private / "config.json"),
        "processes_started": False,
        "existing_configuration_read": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", required=True)
    parser.add_argument("--mysql-dir")
    parser.add_argument("--prometheus-dir")
    for name, default in [
        ("control", 13306),
        ("api", 18080),
        ("prometheus", 9090),
        ("bridge", 9120),
    ]:
        parser.add_argument("--" + name + "-port", type=int, default=default)
    args = parser.parse_args(argv)
    try:
        prepare_local(
            args.private_dir,
            mysql_dir=args.mysql_dir,
            prometheus_dir=args.prometheus_dir,
            control_port=args.control_port,
            api_port=args.api_port,
            prometheus_port=args.prometheus_port,
            bridge_port=args.bridge_port,
        )
    except (ValueError, OSError, subprocess.SubprocessError):
        # Do not expose command output, configuration or generated credentials.
        parser.exit(
            1,
            "Preparation refused. Use a new absolute private directory outside the repository, valid distinct ports, and a writable protected parent.\n",
        )
    print(
        "Private deployment templates created. No processes started; credentials were written only to private files."
    )


if __name__ == "__main__":
    main()
