"""New private deployment templates; never opens a target or existing runtime."""

import base64
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/platform/prepare_local.py"


def generator():
    spec = importlib.util.spec_from_file_location("prepare_local_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_private_templates_are_loadable_and_have_no_business_grants(tmp_path, capsys):
    from platform_app.config import load_config
    from cryptography.fernet import Fernet

    module = generator()
    target = tmp_path / "private"
    module.prepare_local(
        target,
        mysql_dir=tmp_path / "official mysql",
        prometheus_dir=tmp_path / "official prometheus",
    )
    config = load_config(target / "config.json")
    Fernet((target / "fernet-master.key").read_bytes())
    assert len(base64.urlsafe_b64decode((target / "fernet-master.key").read_bytes())) == 32
    assert config.allowed_networks == ["127.0.0.0/8", "::1/128"]
    assert config.ssh_profiles == [] and not config.model
    plan = json.loads((target / "bootstrap-plan.json").read_text())
    assert plan["departments"] == [] and plan["systems"] == []
    assert plan["users"][0]["grants"] == [{"role": "platform_admin", "scope_type": "global"}]
    assert "init-file" not in (target / "my.ini").read_text()
    sql = (target / "mysql-first-start.sql").read_text()
    assert "CREATE DATABASE" in sql and "GRANT ALL PRIVILEGES ON" in sql
    assert all(
        "dba_platform.*" in line
        for line in sql.splitlines()
        if line.startswith("GRANT ") and "TO 'dba_platform'" in line
    )
    assert "SELECT" not in sql and "evidence_lab" not in sql
    secrets = json.loads((target / "bootstrap-private.json").read_text())
    for value in (
        secrets["mysql_root"]["password"],
        secrets["platform_admin"]["password"],
        config.control_db.password.get_secret_value(),
    ):
        assert len(value) >= 32 and value not in capsys.readouterr().out
    example = json.loads((target / "dba-example-plan.json").read_text())
    assert example["users"][0]["grants"] == [
        {"role": "dba", "scope_type": "system", "scope_ref": "example"}
    ]
    assert example["systems"][0]["model_policy"] == "rules_only"


def test_existing_directory_is_never_overwritten(tmp_path):
    module = generator()
    target = tmp_path / "existing"
    target.mkdir()
    marker = target / "fernet-master.key"
    marker.write_text("keep existing key")
    with pytest.raises(ValueError, match="already_exists"):
        module.prepare_local(target)
    assert marker.read_text() == "keep existing key"
    assert list(target.iterdir()) == [marker]


def test_first_start_creates_tcp_maintenance_identity_for_skip_name_resolve(tmp_path):
    module = generator()
    target = tmp_path / "tcp-root"
    module.prepare_local(target)
    sql = (target / "mysql-first-start.sql").read_text()
    if "CREATE USER 'root'@'127.0.0.1'" not in sql:
        pytest.fail("Missing loopback maintenance identity", pytrace=False)
    if "GRANT SHUTDOWN ON *.* TO 'root'@'127.0.0.1'" not in sql:
        pytest.fail("Missing loopback shutdown permission", pytrace=False)
    if "GRANT ALL PRIVILEGES ON *.*" in sql:
        pytest.fail("Unexpected global all privileges", pytrace=False)
    assert "host=127.0.0.1" in (target / "mysql-root-client.cnf").read_text()


def test_repository_path_and_symlink_into_repository_are_rejected(tmp_path, monkeypatch):
    module = generator()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(module, "REPO", repo)
    with pytest.raises(ValueError, match="outside_repository"):
        module.prepare_local(repo / "private")
    assert not (repo / "private").exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"api_port": 13306},
        {"control_port": 0},
        {"mysql_dir": Path("relative")},
        {"mysql_dir": Path("C:/bad\ninjection")},
    ],
)
def test_invalid_ports_or_paths_leave_no_private_directory(tmp_path, kwargs):
    module = generator()
    target = tmp_path / "invalid"
    with pytest.raises(ValueError):
        module.prepare_local(target, **kwargs)
    assert not target.exists()


def test_credentials_are_unique_between_new_deployments(tmp_path):
    module = generator()
    for label in ("one", "two"):
        module.prepare_local(tmp_path / label)
    assert (tmp_path / "one/fernet-master.key").read_bytes() != (
        tmp_path / "two/fernet-master.key"
    ).read_bytes()
    a = json.loads((tmp_path / "one/bootstrap-private.json").read_text())
    b = json.loads((tmp_path / "two/bootstrap-private.json").read_text())
    assert a["platform_admin"]["password"] != b["platform_admin"]["password"]


def test_acl_failure_occurs_before_any_secret_is_written(tmp_path, monkeypatch):
    module = generator()
    target = tmp_path / "acl-failure"

    def deny(_):
        raise OSError("synthetic ACL failure")

    monkeypatch.setattr(module, "_protect", deny)
    with pytest.raises(OSError):
        module.prepare_local(target)
    assert target.is_dir() and list(target.iterdir()) == []
    with pytest.raises(ValueError, match="already_exists"):
        module.prepare_local(target)


def test_manage_specs_consume_generated_custom_component_paths_without_process_actions(tmp_path):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        pytest.skip("PowerShell required for function-only specification check")
    module = generator()
    target = tmp_path / "private"
    module.prepare_local(
        target,
        mysql_dir=tmp_path / "official mysql",
        prometheus_dir=tmp_path / "official prometheus",
        control_port=23306,
        api_port=28080,
        prometheus_port=29090,
        bridge_port=29120,
    )
    code = r"""
param($SourcePath,$ConfigPath,$Repo)
$platformConfig=Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$platformPrivate=$platformConfig.private_dir
$platformMysqlDir=$platformConfig.components.mysql_dir
$platformPrometheusDir=$platformConfig.components.prometheus_dir
$platformPrometheusConfig=$platformConfig.prometheus_config_path
$platformPrometheusListen=$platformConfig.prometheus_listen_address
$platformPython=Join-Path $Repo '.venv\Scripts\python.exe'
$platformPort=([Uri]$platformConfig.public_origin).Port
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($SourcePath,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Script parse failed'}
$definition=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-PlatformSpec'},$true)
. ([ScriptBlock]::Create($definition.Extent.Text))
@{mysql=(Get-PlatformSpec 'mysql');prometheus=(Get-PlatformSpec 'prometheus');api=(Get-PlatformSpec 'api')} | ConvertTo-Json -Depth 8
"""
    check = tmp_path / "spec-only.ps1"
    check.write_text(code, encoding="utf-8")
    result = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(check),
            str(SCRIPT.parent / "manage.ps1"),
            str(target / "config.json"),
            str(SCRIPT.parents[2]),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    specs = json.loads(result.stdout)
    assert Path(specs["mysql"]["exe"]) == tmp_path / "official mysql/bin/mysqld.exe"
    assert Path(specs["prometheus"]["exe"]) == tmp_path / "official prometheus/prometheus.exe"
    assert "--web.listen-address=127.0.0.1:29090" in specs["prometheus"]["args"]
    assert (
        "--config.file=" + str(target / "prometheus.yml").replace("\\", "/")
        in specs["prometheus"]["args"]
    )
    assert specs["api"]["args"][-2:] == ["28080", "--no-access-log"]
