"""Isolated real-MySQL/process acceptance with an explicitly synthetic provider.

Requires an already started cleanroom control DB; never starts the main stack.
The only transport override lives in this validation subprocess. Production
HTTPS verification and the global certificate store remain unchanged.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from platform_app import models as m
from platform_app.config import load_config
from platform_app.db import create_db_engine, create_session_factory, utcnow
from platform_app.common import iso

FAKE_KEY = "ValidationStub-only-not-a-provider-credential"


def require_cleanroom(config):
    if (
        config.control_db.host != "127.0.0.1"
        or config.control_db.port != 13316
        or config.public_origin != "http://127.0.0.1:18081"
        or not config.private_dir.name.startswith("platform-cleanroom-runtime-")
    ):
        raise RuntimeError("This acceptance is restricted to the separate cleanroom.")


def validation_worker(config_path, lease_path):
    from platform_app import ai_worker, investigations

    config = load_config(config_path)
    require_cleanroom(config)
    model = config.model
    if (
        model.get("provider") != "ValidationStub"
        or urlsplit(model.get("base_url", "")).hostname != "127.0.0.1"
        or Path(model["key_path"]).read_text(encoding="utf-8") != FAKE_KEY
    ):
        raise RuntimeError("Only the explicit local synthetic provider is permitted.")

    def transport(runtime, payload, timeout):
        # Deliberate validation seam; no production call_model/TLS modification.
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout) as client:
                response = client.post(
                    model["base_url"] + "/chat/completions",
                    headers={"Authorization": "Bearer " + FAKE_KEY},
                    json=payload,
                )
            if response.status_code != 200:
                raise investigations.ModelTransportError("validation_stub_http_error", True)
            return response.json()
        except (httpx.TransportError, ValueError):
            raise investigations.ModelTransportError("model_call_outcome_unknown", True) from None

    original_claim = ai_worker.claim

    def capture_claim(*args, **kwargs):
        lease = original_claim(*args, **kwargs)
        if lease is not None:
            value = asdict(lease)
            value["deadline_at"] = iso(lease.deadline_at)
            Path(lease_path).write_text(json.dumps(value), encoding="utf-8")
        return lease

    investigations.call_model = transport
    ai_worker.claim = capture_claim
    ai_worker.serve(config, once=True)


class Stub:
    def __init__(self):
        self.received, self.release = threading.Event(), threading.Event()
        self.requests = []
        self.instance_id = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                valid = (
                    self.path == "/chat/completions"
                    and self.headers.get("Authorization") == "Bearer " + FAKE_KEY
                )
                owner.requests.append(
                    {
                        "at": iso(utcnow()),
                        "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "fake_key_only": valid,
                    }
                )
                owner.received.set()
                if not valid:
                    self.send_error(403)
                    return
                owner.release.wait(50)
                body = json.dumps(
                    {
                        "id": "validation-stub",
                        "model": "validation-synthetic",
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {
                                            "id": "validation-tool",
                                            "type": "function",
                                            "function": {
                                                "name": "get_metric_window",
                                                "arguments": json.dumps(
                                                    {"instance_id": owner.instance_id}
                                                ),
                                            },
                                        }
                                    ]
                                }
                            }
                        ],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                    }
                ).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def reset(self):
        self.received.clear()
        self.release.clear()
        return len(self.requests)

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class Actor:
    def __init__(self, origin, username, password):
        self.origin = origin
        self.client = httpx.Client(
            base_url=origin, trust_env=False, follow_redirects=False, timeout=15
        )
        nonce = self.client.get("/api/v1/auth/csrf")
        nonce.raise_for_status()
        response = self.client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
            headers={"Origin": origin, "X-CSRF-Token": nonce.json()["csrf_token"]},
        )
        if response.status_code != 200:
            raise RuntimeError("Validation login failed")
        self.csrf = response.json()["csrf_token"]

    def request(self, method, path, body=None, expected=(200,)):
        response = self.client.request(
            method,
            "/api/v1" + path,
            json=body,
            headers={"Origin": self.origin, "X-CSRF-Token": self.csrf},
        )
        if response.status_code not in expected:
            raise RuntimeError("Validation HTTP status " + str(response.status_code))
        return response.json() if response.content else None


def owned_processes(process, token):
    """Read exact process identity before stopping our own launcher and child."""
    if process.poll() is not None:
        return []
    # CREATE_NO_WINDOW also creates a Windows-owned conhost child. We never
    # target it: only the independently verified Python launcher/interpreter.
    script = f"""$items=Get-CimInstance Win32_Process | Where-Object {{
        ($_.ProcessId -eq {process.pid} -or $_.ParentProcessId -eq {process.pid}) -and $_.Name -eq 'python.exe'
    }}; @($items | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine,
        @{{Name='creation_utc';Expression={{$_.CreationDate.ToUniversalTime().ToString('o')}}}}) | ConvertTo-Json -Compress"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, check=True
    )
    rows = json.loads(result.stdout)
    if isinstance(rows, dict):
        rows = [rows]
    import ctypes
    from ctypes import wintypes

    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    ctypes.windll.kernel32.LocalFree.restype = ctypes.c_void_p
    expected_executables = {Path(sys.executable).resolve(), Path(sys._base_executable).resolve()}
    for row in rows:
        count = ctypes.c_int()
        argv = parse(row["CommandLine"], ctypes.byref(count))
        try:
            arguments = [argv[i] for i in range(count.value)]
        finally:
            ctypes.windll.kernel32.LocalFree(argv)
        config_index = arguments.index("--config") if "--config" in arguments else -1
        config_matches = (
            config_index >= 0
            and config_index + 1 < len(arguments)
            and Path(arguments[config_index + 1]).resolve() == Path(token).resolve()
        )
        created = datetime.fromisoformat(row["creation_utc"]).replace(tzinfo=None)
        checks = {
            "exact_executable": Path(row["ExecutablePath"]).resolve() in expected_executables,
            "worker_argument": "--worker" in arguments,
            "exact_config": config_matches,
            "exact_script": any(
                Path(arg).resolve() == Path(__file__).resolve()
                for arg in arguments[1:]
                if arg.endswith(".py")
            ),
            "creation_time": created >= process.validation_started_at - timedelta(seconds=3),
            "parent_identity": row["ProcessId"] == process.pid
            or row["ParentProcessId"] == process.pid,
        }
        process.validation_identity_checks = checks
        if not all(checks.values()):
            raise RuntimeError(
                "Owned validation process identity mismatch: "
                + ",".join(k for k, passed in checks.items() if not passed)
            )
    if not any(row["ProcessId"] == process.pid for row in rows):
        raise RuntimeError("Owned validation parent missing")
    return rows


def stop_owned(process, token):
    rows = owned_processes(process, token)
    if rows:
        # Every numeric PID was independently matched to our live Popen parent,
        # exact validation config token and Python executable above.
        ids = [r["ProcessId"] for r in rows if r["ProcessId"] != process.pid] + [process.pid]
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Stop-Process -Id "
                + ",".join(str(x) for x in ids)
                + " -Force -ErrorAction SilentlyContinue",
            ],
            capture_output=True,
            check=True,
        )
        process.wait(timeout=10)
    return rows


def snapshot(factory, task_id):
    with factory() as db:
        task = db.get(m.Task, task_id)
        inv = db.get(m.Investigation, task.input_ref)
        calls = list(db.scalars(select(m.ModelCall).where(m.ModelCall.investigation_id == inv.id)))
        return {
            "task_id": task.id,
            "investigation_id": inv.id,
            "task_status": task.status,
            "investigation_status": inv.status,
            "error_code": task.error_code,
            "attempt": task.attempt,
            "fence": task.fence,
            "deadline_at": iso(task.deadline_at),
            "finished_at": iso(task.finished_at),
            "report_published": inv.status in ("completed", "partial"),
            "tool_calls": db.scalar(
                select(func.count())
                .select_from(m.ToolCall)
                .where(m.ToolCall.investigation_id == inv.id)
            ),
            "calls": [
                {
                    "status": c.status,
                    "cost": c.cost,
                    "input_tokens": c.input_tokens,
                    "output_tokens": c.output_tokens,
                    "provider": c.provider,
                }
                for c in calls
            ],
        }


def seed(factory, admin_id, label):
    with factory.begin() as db:
        department = m.Department(name=label)
        role = m.Role(
            name=label, permissions=["data.read", "investigation.create", "model.external_use"]
        )
        db.add_all([department, role])
        db.flush()
        system = m.System(department_id=department.id, name=label, model_policy="live_redacted")
        db.add(system)
        db.flush()
        instance = m.Instance(
            system_id=system.id,
            name=label,
            endpoint="validation.invalid",
            expected_server_uuid=str(uuid4()),
            is_lab=True,
            enabled=False,
            model_policy="live_redacted",
        )
        db.add(instance)
        db.flush()
        for kind, identifier in [("system", system.id), ("instance", instance.id)]:
            db.add(
                m.PlatformSetting(
                    key=f"model_policy:{kind}:{identifier}",
                    value={
                        "mode": "live_redacted",
                        "allowed_sources": ["runtime"],
                        "reason": "isolated synthetic ValidationStub fixture; not actual target collection",
                    },
                )
            )
        grant = db.scalar(
            select(m.UserGrant).where(
                m.UserGrant.user_id == admin_id,
                m.UserGrant.scope_type == "global",
                m.UserGrant.scope_id == "*",
            )
        )
        if grant is None:
            raise RuntimeError("Cleanroom administrator authority not found")
        old_delegation = {
            "roles": list(grant.delegatable_roles),
            "actions": list(grant.delegated_actions),
        }
        grant.delegatable_roles = [*grant.delegatable_roles, role.name]
        grant.delegated_actions = sorted(set(grant.delegated_actions) | set(role.permissions))
        stamp = utcnow() - timedelta(minutes=2)
        task = m.Task(
            type="validation_synthetic_fixture", instance_id=instance.id, status="succeeded"
        )
        db.add(task)
        db.flush()
        run = m.CollectionRun(
            task_id=task.id,
            instance_id=instance.id,
            collector="runtime",
            status="succeeded",
            started_at=stamp,
            finished_at=stamp,
            error_details={"source_mode": "synthetic_validation_fixture_not_target_collection"},
        )
        db.add(run)
        db.flush()
        db.add(
            m.MetricFrame(
                run_id=run.id,
                instance_id=instance.id,
                collector="runtime",
                server_uuid=instance.expected_server_uuid,
                collector_boot_id=str(uuid4()),
                source_epoch="synthetic-validation-fixture",
                observed_monotonic_ns=1,
                observed_at=stamp,
                uptime_seconds=1,
                raw_values={},
                rate_validity="unknown",
                derived_values={
                    "values": {},
                    "window_validity": "unknown",
                    "rate_reasons": ["synthetic_validation_fixture"],
                },
            )
        )
        return {
            "instance_id": instance.id,
            "role_id": role.id,
            "grant_id": grant.id,
            "old_delegation": old_delegation,
            "from": iso(stamp - timedelta(seconds=30)),
            "to": iso(stamp + timedelta(seconds=30)),
        }


def main(args):
    config = load_config(args.config)
    require_cleanroom(config)
    if config.model:
        raise RuntimeError("Use the cleanroom base config with no real model credentials")
    directory = config.private_dir / ("ai-validation-" + uuid4().hex[:8])
    directory.mkdir()
    output = Path(args.output).resolve()
    if output.is_relative_to(ROOT):
        raise RuntimeError("Proof must remain outside the repository")
    stub = Stub()
    processes = []
    actors = []
    users = []
    fixture = None
    engine = create_db_engine(config)
    factory = create_session_factory(engine, config)
    report = {
        "source": "isolated_real_mysql_and_subprocess_with_synthetic_loopback_provider",
        "started_at": iso(utcnow()),
        "production_provider_called": False,
        "scope": "process loss, DB-clock/fence recovery and CSRF HTTP grant revocation; not production HTTPS/provider failure",
        "checks": [],
        "cleanup": {},
    }
    validation_config = directory / "config.json"
    key = directory / "fake-key.txt"
    key.write_text(FAKE_KEY, encoding="utf-8")
    raw = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    raw["model"] = {
        "provider": "ValidationStub",
        "base_url": f"http://127.0.0.1:{stub.server.server_port}",
        "model": "validation-synthetic",
        "key_path": str(key),
        "timeout_seconds": 50,
        "max_calls": 2,
        "max_tokens": 500,
    }
    validation_config.write_text(json.dumps(raw), encoding="utf-8")

    def launch(name):
        lease_path = directory / (name + "-lease.json")
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--config",
            str(validation_config),
            "--lease-file",
            str(lease_path),
        ]
        stream = (directory / (name + "-process.log")).open("w", encoding="utf-8")
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=stream,
            stderr=stream,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        process.validation_started_at = utcnow()
        processes.append((process, stream))
        return process, lease_path

    try:
        base_plan = json.loads(
            (config.private_dir / "bootstrap-plan.json").read_text(encoding="utf-8-sig")
        )
        admin_info = next(u for u in base_plan["users"] if u["username"] == "admin")
        admin = Actor(config.public_origin, "admin", admin_info["password"])
        actors.append(admin)
        with factory() as db:
            admin_id = db.scalar(select(m.User.id).where(m.User.username == "admin"))
            if db.scalar(
                select(func.count())
                .select_from(m.Task)
                .where(m.Task.queue == "ai", m.Task.status.in_(["queued", "running", "retry_wait"]))
            ):
                raise RuntimeError("Cleanroom has unrelated active AI work; refused")
        fixture = seed(factory, admin_id, "validation-ai-" + uuid4().hex[:8])
        stub.instance_id = fixture["instance_id"]
        for case in ("process_loss", "inflight_revocation"):
            username = "validation-" + uuid4().hex[:12]
            password = secrets.token_urlsafe(32)
            user = admin.request(
                "POST",
                "/users",
                {
                    "username": username,
                    "password": password,
                    "display_name": "Automated isolated AI acceptance",
                    "grants": [
                        {
                            "role_id": fixture["role_id"],
                            "scope_type": "instance",
                            "scope_id": fixture["instance_id"],
                        }
                    ],
                },
                expected=(201,),
            )
            users.append(user["id"])
            actor = Actor(config.public_origin, username, password)
            actors.append(actor)
            created = actor.request(
                "POST",
                "/investigations",
                {
                    "instance_ids": [fixture["instance_id"]],
                    "from": fixture["from"],
                    "to": fixture["to"],
                    "question": "synthetic validation metrics only",
                    "mode": "live_redacted",
                    "external_confirmed": True,
                    "max_duration_seconds": 40,
                    "max_tool_calls": 1,
                },
                expected=(202,),
            )
            baseline = stub.reset()
            process, lease_path = launch(case)
            if not stub.received.wait(20):
                raise RuntimeError("Validation request not received; inspect private process log")
            before = snapshot(factory, created["id"])
            if before["calls"] != [
                {
                    "status": "pending",
                    "cost": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "provider": "ValidationStub",
                }
            ]:
                raise RuntimeError("Expected exactly one pending unknown-cost call")
            if case == "process_loss":
                identity = stop_owned(process, str(validation_config))
                stub.release.set()
                # Wait for the real deadline, without editing lease/task clocks.
                deadline = datetime.fromisoformat(
                    before["deadline_at"].replace("Z", "+00:00")
                ).replace(tzinfo=None)
                while utcnow() <= deadline + timedelta(seconds=1):
                    time.sleep(0.5)
                replacement, _ = launch("reconcile")
                replacement.wait(timeout=20)
                after = snapshot(factory, created["id"])
                from platform_app.scheduling import LeasedTask, check_fence
                from fastapi import HTTPException

                stale = json.loads(lease_path.read_text(encoding="utf-8"))
                stale["deadline_at"] = datetime.fromisoformat(
                    stale["deadline_at"].replace("Z", "+00:00")
                ).replace(tzinfo=None)
                denied = False
                with factory() as db:
                    try:
                        check_fence(db, LeasedTask(**stale), allow_inactive=True)
                    except HTTPException as exc:
                        denied = exc.status_code == 409
                passed = (
                    after["task_status"] == after["investigation_status"] == "expired"
                    and after["attempt"] == 1
                    and len(after["calls"]) == 1
                    and after["calls"][0]["status"] == "outcome_unknown"
                    and after["calls"][0]["cost"] is None
                    and denied
                )
                extra = {"owned_process_identity": identity, "stale_fence_denied": denied}
            else:
                admin.request("PATCH", "/users/" + user["id"], {"grants": []})
                revoked_status = actor.client.get("/api/v1/auth/me").status_code
                stub.release.set()
                process.wait(timeout=20)
                after = snapshot(factory, created["id"])
                passed = (
                    after["task_status"] == after["investigation_status"] == "failed"
                    and revoked_status == 401
                    and after["tool_calls"] == 0
                    and len(after["calls"]) == 1
                    and after["calls"][0]["status"] == "succeeded"
                    and after["calls"][0]["input_tokens"] == 100
                    and after["calls"][0]["output_tokens"] == 10
                )
                extra = {"old_session_status_after_revoke": revoked_status}
            time.sleep(1)
            count = len(stub.requests) - baseline
            passed = passed and count == 1 and not after["report_published"]
            report["checks"].append(
                {
                    "case": case,
                    "passed": passed,
                    "request_count": count,
                    "before": before,
                    "after": after,
                    **extra,
                }
            )
            print(json.dumps({"case": case, "passed": passed, "request_count": count}), flush=True)
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        raise
    finally:
        for process, stream in processes:
            try:
                stop_owned(process, str(validation_config))
            except Exception as exc:
                report["cleanup"]["process_stop_error_type"] = type(exc).__name__
            finally:
                stream.close()
        stub.close()
        for user_id in users:
            try:
                admin.request("PATCH", "/users/" + user_id, {"active": False, "grants": []})
            except Exception:
                report["cleanup"]["disable_error"] = True
        if fixture:
            try:
                with factory.begin() as db:
                    grant = db.get(m.UserGrant, fixture["grant_id"])
                    grant.delegatable_roles = fixture["old_delegation"]["roles"]
                    grant.delegated_actions = fixture["old_delegation"]["actions"]
                    instance = db.get(m.Instance, fixture["instance_id"])
                    instance.model_policy = "rules_only"
                    instance.policy_version += 1
                    system = db.get(m.System, instance.system_id)
                    system.model_policy = "rules_only"
                    system.policy_version += 1
                    for kind, identifier in [("instance", instance.id), ("system", system.id)]:
                        setting = db.get(m.PlatformSetting, f"model_policy:{kind}:{identifier}")
                        if setting:
                            setting.value = {
                                **setting.value,
                                "mode": "rules_only",
                                "allowed_sources": [],
                            }
                            setting.version += 1
                    report["cleanup"]["all_temporary_users_disabled"] = not bool(
                        db.scalar(
                            select(func.count())
                            .select_from(m.User)
                            .where(m.User.id.in_(users), m.User.active.is_(True))
                        )
                    )
            except Exception as exc:
                report["cleanup"]["restore_error_type"] = type(exc).__name__
        for actor in actors:
            actor.client.close()
        engine.dispose()
        report["finished_at"] = iso(utcnow())
        report["cleanup"]["owned_children_stopped"] = all(
            p.poll() is not None for p, _ in processes
        )
        report["cleanup"]["unknown_call_history_retained"] = True
        report["complete"] = (
            len(report["checks"]) == 2
            and all(c["passed"] for c in report["checks"])
            and not report.get("error_type")
            and report["cleanup"].get("all_temporary_users_disabled")
            and report["cleanup"]["owned_children_stopped"]
            and not any(
                k.endswith("_error_type") or k.endswith("_error") for k in report["cleanup"]
            )
        )
        report["stub_requests"] = stub.requests
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report["complete"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--lease-file")
    args = parser.parse_args()
    if args.worker:
        validation_worker(args.config, args.lease_file)
    else:
        if not args.output:
            parser.error("--output is required")
        raise SystemExit(0 if main(args) else 1)
