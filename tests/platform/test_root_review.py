"""Independent regressions for manifest coverage and process identity checks."""

from pathlib import Path
import json
import shutil
import subprocess
from types import SimpleNamespace
from datetime import timedelta
import pytest
from sqlalchemy import select
from tests.platform.test_events import db, graph, observe
from platform_app import models as m
from platform_app.db import utcnow
from platform_app.evidence import create_manifest
from tests.platform.test_leases import lease_env


def test_old_configuration_failures_do_not_starve_current_terminal_activation(lease_env):
    from platform_app import worker
    from platform_app.scheduling import enqueue

    factory, (a, _) = lease_env
    with factory.begin() as session:
        instance = session.get(m.Instance, a)
        instance.config_revision = 2
        instance.enabled = False
        instance.lifecycle_state = "Activating"
        for _ in range(101):
            task = enqueue(
                session, "activate_instance", instance_id=a, payload={"config_revision": 1}
            )
            task.status = "failed"
        current = enqueue(
            session, "activate_instance", instance_id=a, payload={"config_revision": 2}
        )
        current.status = "failed"
    worker.reconcile_interrupted(factory)
    with factory() as session:
        assert session.get(m.Instance, a).lifecycle_state == "Ready"


@pytest.mark.parametrize(
    "source_coverage,truncated",
    [
        ({"complete": False, "gaps": ["rotation_old_file_not_reconciled"]}, False),
        ({"complete": False, "reason": "top_n_cumulative_digest_not_slow_log"}, True),
    ],
)
def test_manifest_cannot_claim_complete_when_raw_source_is_explicitly_partial(
    db, monkeypatch, source_coverage, truncated
):
    _, a, _, _ = graph(db)
    stamp = utcnow() - timedelta(minutes=1)
    _, item = observe(db, a, stamp, source="mysql.slow_log.file", rows=[])
    item.coverage = {**item.coverage, **source_coverage}
    item.truncated = truncated
    db.info["platform_config"] = SimpleNamespace()
    monkeypatch.setattr(
        "platform_app.metrics.materialize_metric_window",
        lambda *args: {
            "items": [],
            "coverage": {"materialized": True, "complete": True, "reason_codes": []},
        },
    )
    manifest = create_manifest(
        db,
        "event",
        "source-coverage",
        [a.id],
        stamp - timedelta(seconds=1),
        stamp + timedelta(seconds=1),
    )
    marker = db.scalar(
        select(m.ManifestItem).where(
            m.ManifestItem.manifest_id == manifest.id, m.ManifestItem.item_type == "coverage"
        )
    )
    assert marker.payload["coverage_complete"] is False
    assert marker.payload["gaps"]
    if truncated:
        assert marker.payload["truncated"] is True


@pytest.mark.parametrize(
    "command_line,record_changes,accepted",
    [
        ("python.exe -m platform_app.worker --config D:/fixture/config.json", {}, True),
        (
            '"D:/fixture/python.exe" "-m" "platform_app.worker" "--config" "D:/fixture/config.json"',
            {},
            True,
        ),
        (
            "python.exe -m platform_app.worker --config D:/fixture/config.json",
            {"creation_utc": None, "started_at": "2026-01-01T00:00:02.1230000Z"},
            True,
        ),
        ("python.exe -m platform_app.worker_extra --config D:/fixture/config.json", {}, False),
        ("python.exe -m platform_app.worker --config D:/other-deployment/config.json", {}, False),
        (
            "python.exe -m platform_app.worker --config D:/fixture/config.json",
            {"creation_utc": "2026-01-01T00:00:00.1330000Z"},
            False,
        ),
        (
            "python.exe -m platform_app.worker --config D:/fixture/config.json",
            {"config_path": "D:/other-deployment/config.json"},
            False,
        ),
        (
            "python.exe -m platform_app.worker --config D:/fixture/config.json",
            {"module": "platform_app.ai_worker"},
            False,
        ),
        (
            "python.exe -m platform_app.worker --config D:/fixture/config.json",
            {"creation_utc": "2020-01-01T00:00:00Z"},
            False,
        ),
    ],
    ids=[
        "valid_exact",
        "valid_quoted",
        "valid_legacy",
        "wrong_module",
        "wrong_argv_config",
        "wrong_creation_time",
        "wrong_record_config",
        "wrong_record_module",
        "recycled_pid",
    ],
)
def test_manage_identity_accepts_only_verified_fixture(command_line, record_changes, accepted):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        pytest.skip("PowerShell is required for an isolated function-only identity check")
    manage = Path(__file__).resolve().parents[2] / "scripts/platform/manage.ps1"
    # Parse only the verified-process function; never execute manage's start,
    # stop, config loading, or process enumeration against the host computer.
    record = {
        "pid": 424242,
        "exe": "D:/fixture/python.exe",
        "module": "platform_app.worker",
        "started_at": "2026-01-01T00:00:00.1230000Z",
        "creation_utc": "2026-01-01T00:00:00.1230000Z",
        "config_path": "D:/fixture/config.json",
    }
    record.update(record_changes)
    code = r"""
param($SourcePath,$FixturePath)
$fixture=Get-Content -LiteralPath $FixturePath -Raw | ConvertFrom-Json
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($SourcePath,[ref]$tokens,[ref]$errors)
$definition=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-VerifiedPlatformProcess'},$true)
. ([ScriptBlock]::Create($definition.Extent.Text))
$platformPrivate='D:\fixture-private'
$ConfigPath='D:/fixture/config.json'
$script:fakeLine=$fixture.command_line
$script:fakeRecord=$fixture.record | ConvertTo-Json
$script:fakeCreated=([DateTimeOffset]'2026-01-01T00:00:00.1230000Z').UtcDateTime
function Test-Path {param($LiteralPath) return $true}
function Get-Content {param($LiteralPath,[switch]$Raw) return $script:fakeRecord}
function Get-CimInstance {param($ClassName,$Filter) return [pscustomobject]@{ProcessId=424242;ExecutablePath='D:/fixture/python.exe';CommandLine=$script:fakeLine;CreationDate=$script:fakeCreated} }
$spec=@{exe='D:/fixture/python.exe';module='platform_app.worker';args=@('-m','platform_app.worker','--config','D:/fixture/config.json')}
$rejected=$false
try {$null=Get-VerifiedPlatformProcess 'worker' $spec} catch {$rejected=$true}
if($rejected){'rejected'}else{'accepted'}
"""
    # Synthetic values only. No real PID is looked up and Stop-Process is
    # never available to this test flow.
    import tempfile

    with tempfile.TemporaryDirectory() as temp:
        script = Path(temp) / "identity.ps1"
        script.write_text(code, encoding="utf-8")
        fixture = Path(temp) / "fixture.json"
        fixture.write_text(
            json.dumps({"command_line": command_line, "record": record}), encoding="utf-8"
        )
        result = subprocess.run(
            [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(script),
                str(manage),
                str(fixture),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("accepted" if accepted else "rejected")
