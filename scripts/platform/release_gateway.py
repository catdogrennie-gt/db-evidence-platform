"""Checked local Caddy releases; no service stops, DB migration or tunnel control.

All inputs/state belong in a private runtime directory. Only literal loopback
HTTP upstreams are accepted. Existing application ports are never taken over.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4


class ReleaseError(RuntimeError):
    """A safe, non-secret operation error code."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ReleaseError("private_json_unavailable") from None


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".pending")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _origin(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or not ipaddress.ip_address(parsed.hostname).is_loopback
            or not parsed.port or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError()
        return f"http://{parsed.netloc}"
    except (ValueError, TypeError, AttributeError):
        raise ReleaseError("literal_loopback_http_required") from None


def _address(value):
    origin = _origin("http://" + str(value))
    if origin[7:] != value:
        raise ReleaseError("invalid_loopback_listener")
    return value


def _path(value):
    if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//")
        or "#" in value or any(ord(c) < 32 for c in value)):
        raise ReleaseError("invalid_probe_path")
    return value


def _http(url, value=None, *, host=None):
    data = None if value is None else json.dumps(value).encode()
    headers = {"Content-Type": "application/json"}
    if host:
        headers["Host"] = host
    request = Request(url, data=data, headers=headers)
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=5) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024 or response.status != 200:
                raise ReleaseError("http_probe_failed")
            # Caddy /load acknowledges a successful update with an empty body.
            if not raw and value is not None:
                return None
            return json.loads(raw)
    except (OSError, HTTPError, URLError, ValueError):
        raise ReleaseError("http_probe_failed") from None


class _References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {"src", "href"} and value:
                self.paths.append(value)


def _files(root):
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ReleaseError("frontend_link_not_allowed")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if (any(part.startswith(".") or ".private." in part.lower()
                    for part in path.relative_to(root).parts)
                or path.suffix.lower() in {".key", ".pem", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db"}
                or path.name.lower() == "credentials.json"):
                raise ReleaseError("private_material_in_frontend_build")
            if any(c in relative for c in "*{}[]\\?#%"):
                raise ReleaseError("unsupported_frontend_filename")
            result.append({"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "bytes": path.stat().st_size})
    return result


def _verify_index(root):
    try:
        parser = _References()
        parser.feed((root / "index.html").read_text(encoding="utf-8"))
        for value in parser.paths:
            parsed = urlsplit(value)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            relative = unquote(parsed.path).lstrip("/")
            if ".." in PurePosixPath(relative).parts or not (root / relative).is_file():
                raise ReleaseError("frontend_asset_missing")
    except (OSError, UnicodeError):
        raise ReleaseError("frontend_index_unavailable") from None


def snapshot_frontend(source, releases, release_id):
    """Create a new content-checked build. Existing releases are never replaced."""
    source, releases = Path(source).resolve(), Path(releases).resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", release_id):
        raise ReleaseError("invalid_release_id")
    if source.is_symlink() or not source.is_dir() or releases.is_relative_to(source):
        raise ReleaseError("invalid_frontend_source")
    _verify_index(source)
    files = _files(source)
    destination = releases / release_id
    if destination.exists():
        raise ReleaseError("release_already_exists")
    releases.mkdir(parents=True, exist_ok=True)
    pending = releases / (".pending-" + uuid4().hex)
    try:
        shutil.copytree(source, pending / "frontend")
        _atomic_json(pending / "manifest.json", {"format": 1, "release_id": release_id, "files": files})
        verify_frontend(pending)
        # rename refuses an existing destination on both supported platforms.
        pending.rename(destination)
    finally:
        if pending.exists():
            # This generated temporary directory was checked to remain in releases.
            assert pending.resolve().parent == releases
            shutil.rmtree(pending)
    return destination


def verify_frontend(snapshot):
    snapshot = Path(snapshot)
    if not snapshot.is_absolute() or snapshot.is_symlink():
        raise ReleaseError("invalid_frontend_release")
    manifest = _read_json(snapshot / "manifest.json")
    root = snapshot / "frontend"
    if (manifest.get("format") != 1 or root.is_symlink()
        or manifest.get("files") != _files(root)):
        raise ReleaseError("frontend_manifest_mismatch")
    _verify_index(root)
    return manifest


def _frontends(spec):
    values = [spec.get("frontend"), *spec.get("retained_frontends", [])]
    return list(dict.fromkeys(str(Path(value)) for value in values if value))


def _retain(spec, other):
    result = deepcopy(spec)
    result["retained_frontends"] = [p for p in dict.fromkeys(_frontends(spec) + _frontends(other))
                                    if p != result.get("frontend")]
    return result


def build_config(spec):
    """Build native Caddy JSON. HTTP API and SPA routes remain separate."""
    if type(spec.get("public_read_only", False)) is not bool:
        raise ReleaseError("public_read_only_boolean_required")
    listen, admin = _address(spec["listen"]), _address(spec["admin"])
    origin = _origin(spec["target"]["origin"])
    if len({listen, admin, origin[7:]}) != 3:
        raise ReleaseError("listener_or_upstream_collision")
    routes = [{"match": [{"path": ["/api/docs", "/api/docs/*", "/api/openapi.json", "/api/v1/health/ready"]}],
               "handle": [{"handler": "static_response", "status_code": 404}], "terminal": True}]
    if spec.get("frontend"):
        routes.insert(0, {"handle": [{"handler": "headers", "response": {"set": {
            "X-Content-Type-Options": ["nosniff"], "X-Frame-Options": ["DENY"],
            "Referrer-Policy": ["same-origin"], "Cache-Control": ["no-store"],
            "Content-Security-Policy": ["default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'"],
        }}}]})
        routes.append({"match": [{"path": ["/api", "/api/*"]}], "handle": [
            {"handler": "reverse_proxy", "upstreams": [{"dial": origin[7:]}]}], "terminal": True})
        assets = {}
        for snapshot in _frontends(spec):
            manifest = verify_frontend(snapshot)
            root = str(Path(snapshot) / "frontend")
            paths = []
            for entry in manifest["files"]:
                path = "/" + entry["path"]
                if not path.startswith("/assets/"):
                    continue
                if path in assets and assets[path] != entry["sha256"]:
                    raise ReleaseError("immutable_asset_name_collision")
                assets[path] = entry["sha256"]
                paths.append(path)
            if paths:
                routes.append({"match": [{"path": paths}], "handle": [
                    {"handler": "headers", "response": {"set": {"Cache-Control": ["public, max-age=31536000, immutable"]}}},
                    {"handler": "file_server", "root": root}], "terminal": True})
        routes.append({"match": [{"path": ["/assets/*"]}], "handle": [
            {"handler": "static_response", "status_code": 404}], "terminal": True})
        active = Path(spec["frontend"])
        active_files = ["/" + item["path"] for item in verify_frontend(active)["files"]
                        if not item["path"].startswith("assets/")]
        static = {"handler": "file_server", "root": str(active / "frontend")}
        routes.append({"match": [{"path": active_files}], "handle": [static], "terminal": True})
        routes.append({"handle": [{"handler": "rewrite", "uri": "/index.html"}, static]})
    else:
        routes.append({"handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": origin[7:]}]}]})
    if spec.get("public_read_only", False):
        routes.insert(1 if spec.get("frontend") else 0, {
            "match": [{"not": [{"method": ["GET", "HEAD", "OPTIONS"]}]}],
            "handle": [{"handler": "static_response", "status_code": 403}], "terminal": True,
        })
    return {"admin": {"listen": admin, "config": {"persist": False}}, "apps": {"http": {"servers": {"edge": {
        "listen": [listen], "protocols": ["h1"], "automatic_https": {"disable": True}, "routes": routes}}}}}


def _probe(origin, probe, *, host=None):
    value = _http(_origin(origin) + _path(probe["path"]), host=host)
    if not isinstance(value, dict):
        raise ReleaseError("probe_json_required")
    for key, expected in probe.get("equals", {}).items():
        if key not in value or value[key] != expected or type(value[key]) is not type(expected):
            raise ReleaseError("probe_value_mismatch")
    for key in probe.get("required_keys", []):
        if key not in value:
            raise ReleaseError("probe_field_missing")


def preflight(spec):
    build_config(spec)
    target = spec["target"]
    _probe(target["origin"], {"path": target["ready_path"], "equals": target.get("ready_equals", {"ready": True}),
                              "required_keys": target.get("required_keys", [])}, host=spec.get("probe_host"))


def build_front_door(listen, upstream, admin):
    """A separate, fixed Caddy listener shields reads from inner reload races.

    This configuration is started once, not reloaded for application releases.
    Only GET/HEAD may be retried after a transport error, never writes or login.
    """
    listen, admin, origin = _address(listen), _address(admin), _origin(upstream)
    if len({listen, admin, origin[7:]}) != 3:
        raise ReleaseError("listener_or_upstream_collision")
    proxy = {"handler": "reverse_proxy", "upstreams": [{"dial": origin[7:]}],
             "load_balancing": {"try_duration": 2000000000, "try_interval": 50000000,
                                "retry_match": [{"method": ["GET", "HEAD"]}]}}
    return {"admin": {"listen": admin, "config": {"persist": False}}, "apps": {"http": {"servers": {
        "front": {"listen": [listen], "protocols": ["h1"],
                  "automatic_https": {"disable": True}, "routes": [{"handle": [proxy]}]}}}}}


class DeploymentManager:
    def __init__(self, runtime, admin_url, caddy_binary):
        self.runtime = Path(runtime).resolve()
        self.admin = _origin(admin_url)
        self.caddy = str(Path(caddy_binary).resolve())
        self.runtime.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _lock(self):
        lock = self.runtime / ".gateway-lock"
        try:
            lock.mkdir()
        except FileExistsError:
            raise ReleaseError("release_operation_already_running") from None
        try:
            yield
        finally:
            lock.rmdir()

    def _validate(self, spec, config):
        if self.admin != "http://" + spec["admin"]:
            raise ReleaseError("admin_endpoint_mismatch")
        temporary = self.runtime / ("validate-" + uuid4().hex + ".json")
        try:
            _atomic_json(temporary, config)
            result = subprocess.run([self.caddy, "validate", "--config", str(temporary)],
                                    capture_output=True, timeout=15,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode:
                raise ReleaseError("caddy_configuration_rejected")
        except (OSError, subprocess.TimeoutExpired):
            raise ReleaseError("caddy_validation_unavailable") from None
        finally:
            temporary.unlink(missing_ok=True)

    def _live(self):
        return _http(self.admin + "/config/")

    def _gateway_probe(self, spec):
        _probe("http://" + spec["listen"], spec["gateway_probe"], host=spec.get("probe_host"))

    def adopt(self, spec):
        with self._lock():
            if (self.runtime / "state.private.json").exists():
                raise ReleaseError("gateway_already_adopted")
            config = build_config(spec)
            self._validate(spec, config)
            preflight(spec)
            if self._live() != config:
                raise ReleaseError("active_configuration_not_requested_release")
            self._gateway_probe(spec)
            _atomic_json(self.runtime / "state.private.json", {
                "format": 1, "active_spec": spec, "active_config": config,
                "previous_spec": None, "successful_switches": 0})
            return self.status()

    def status(self):
        state = _read_json(self.runtime / "state.private.json")
        return {"configured": True, "active_target_digest": digest(state["active_spec"]["target"]),
                "successful_switches": state["successful_switches"]}

    def switch(self, spec):
        return self._activate(spec)

    def rollback(self):
        return self._activate(None)

    def recover(self):
        """Deliberately restore an interrupted operation, without hiding it."""
        with self._lock():
            journal_path = self.runtime / "pending.private.json"
            journal = _read_json(journal_path)
            state = journal["before"]
            intended = build_config(journal["intended_spec"])
            if self._live() not in [state["active_config"], intended]:
                raise ReleaseError("active_configuration_changed_outside_release_tool")
            spec = _retain(state["active_spec"], journal["intended_spec"])
            config = build_config(spec)
            self._validate(spec, config)
            preflight(spec)
            _http(self.admin + "/load", config)
            if self._live() != config:
                raise ReleaseError("restored_configuration_mismatch")
            self._gateway_probe(spec)
            state.update(active_spec=spec, active_config=config)
            _atomic_json(self.runtime / "state.private.json", state)
            journal_path.unlink()
            return self.status()

    def _activate(self, requested):
        with self._lock():
            journal = self.runtime / "pending.private.json"
            if journal.exists():
                raise ReleaseError("unfinished_switch_requires_inspection")
            state = _read_json(self.runtime / "state.private.json")
            if self._live() != state["active_config"]:
                raise ReleaseError("active_configuration_changed_outside_release_tool")
            previous = state["active_spec"]
            spec = requested if requested is not None else state.get("previous_spec")
            if not spec:
                raise ReleaseError("no_previous_release")
            if spec["listen"] != previous["listen"] or spec["admin"] != previous["admin"]:
                raise ReleaseError("gateway_listeners_cannot_change_during_switch")
            spec = _retain(spec, previous)
            config = build_config(spec)
            self._validate(spec, config)
            preflight(spec)
            _atomic_json(journal, {"before": state, "intended_spec": spec,
                                   "started_at": datetime.now(timezone.utc).isoformat()})
            try:
                _http(self.admin + "/load", config)
                if self._live() != config:
                    raise ReleaseError("applied_configuration_mismatch")
                self._gateway_probe(spec)
                _atomic_json(self.runtime / "state.private.json", {
                    "format": 1, "active_spec": spec, "active_config": config,
                    "previous_spec": previous,
                    "successful_switches": state["successful_switches"] + 1})
            except Exception:
                # Retain both asset sets even when reverting a briefly visible release.
                restored_spec = _retain(previous, spec)
                restored_config = build_config(restored_spec)
                try:
                    _http(self.admin + "/load", restored_config)
                    if self._live() != restored_config:
                        raise ReleaseError("restored_configuration_mismatch")
                    self._gateway_probe(restored_spec)
                    state.update(active_spec=restored_spec, active_config=restored_config)
                    _atomic_json(self.runtime / "state.private.json", state)
                except Exception:
                    raise ReleaseError("rollback_failed_inspect_private_journal") from None
                journal.unlink()
                raise ReleaseError("activation_failed_previous_restored") from None
            journal.unlink()
            return self.status()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    snapshot = sub.add_parser("snapshot")
    for arg in ("source", "releases", "release-id"):
        snapshot.add_argument("--" + arg, required=True)
    front = sub.add_parser("front-door")
    for arg in ("listen", "upstream", "admin", "output"):
        front.add_argument("--" + arg, required=True)
    for name in ("generate", "preflight", "adopt", "switch", "rollback", "recover", "status"):
        command = sub.add_parser(name)
        if name not in {"rollback", "recover", "status"}:
            command.add_argument("--spec", required=True)
        if name == "generate":
            command.add_argument("--output", required=True)
        if name in {"adopt", "switch", "rollback", "recover", "status"}:
            for arg in ("runtime", "admin", "caddy"):
                command.add_argument("--" + arg, required=True)
    args = parser.parse_args()
    try:
        if args.action == "snapshot":
            path = snapshot_frontend(args.source, args.releases, args.release_id)
            result = {"action": "snapshot", "release_id": path.name, "verified": True}
        elif args.action == "front-door":
            _atomic_json(args.output, build_front_door(args.listen, args.upstream, args.admin))
            result = {"action": "front-door", "complete": True}
        elif args.action == "generate":
            _atomic_json(args.output, build_config(_read_json(args.spec)))
            result = {"action": "generate", "complete": True}
        elif args.action == "preflight":
            preflight(_read_json(args.spec))
            result = {"action": "preflight", "ready": True}
        else:
            manager = DeploymentManager(args.runtime, args.admin, args.caddy)
            result = getattr(manager, args.action)(*([_read_json(args.spec)] if args.action in {"adopt", "switch"} else []))
        print(json.dumps(result))
        return 0
    except (ReleaseError, KeyError, TypeError, ValueError, OSError) as error:
        code = str(error) if isinstance(error, ReleaseError) else "invalid_input_or_runtime"
        print(json.dumps({"error": code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
