"""Exercise real HTTP scope boundaries without publishing credentials or bodies.

Run with the repository virtualenv. The default is read-only; --write-probes
adds explicitly labeled human validation records and an ephemeral account that
is disabled in a finally block. It never creates metrics or collection evidence.
Output belongs outside the repository. Missing real assets/sources are recorded
as not_tested, rather than silently counted as successful acceptance.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx


def utc_iso(value=None):
    return (value or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


class Verification:
    def __init__(self, base_url, origin, plan, ids, *, write_probes=False, wait_seconds=30):
        self.base = base_url.rstrip("/")
        self.origin, self.plan, self.ids = origin.rstrip("/"), plan, ids
        self.write_probes, self.wait_seconds = write_probes, wait_seconds
        self.clients, self.actors, self.checks = {}, {}, []
        self.passwords = [u["password"] for u in plan["users"]]
        self.schema = {}
        self.created = []
        self.started_at = utc_iso()

    def client(self):
        return httpx.Client(
            base_url=self.base,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(20, connect=3),
            headers={"Accept": "application/json"},
        )

    def record(self, name, passed=None, **safe):
        self.checks.append(
            {
                "name": name,
                "outcome": "not_tested" if passed is None else ("pass" if passed else "fail"),
                **safe,
            }
        )

    def request(self, account, method, path, *, body=None, params=None, csrf=True, headers=None):
        client = self.clients[account]
        request_headers = {"Origin": self.origin, **(headers or {})}
        if method != "GET" and csrf:
            request_headers["X-CSRF-Token"] = self.actors.get(account, {}).get("csrf_token", "")
        response = client.request(
            method, "/api/v1" + path, json=body, params=params, headers=request_headers
        )
        # Do not persist bodies, cookies, tokens, passwords, SQL, or raw logs.
        leaked = any(p and p in response.text for p in self.passwords)
        if leaked:
            self.record(f"{account}:{method}:{path}:secret_echo", False)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return response, payload

    def expect(self, name, account, method, path, expected, **kwargs):
        response, payload = self.request(account, method, path, **kwargs)
        state = {}
        if response.status_code == 200 and isinstance(payload, dict):
            # Authorization success does not itself assert healthy/live data.
            for field in ("source_mode", "conclusion_type", "status"):
                value = payload.get(field)
                if isinstance(value, str) and value in {
                    "live",
                    "replay",
                    "rule",
                    "live_model",
                    "human_assertion",
                    "synthetic",
                    "queued",
                    "running",
                    "succeeded",
                    "failed",
                    "expired",
                    "cancelled",
                    "completed",
                    "partial",
                    "draft",
                    "published",
                    "approved",
                    "open",
                    "closed",
                }:
                    state[field] = value
            state["source_classification"] = (
                "explicit_response_fields" if state else "not_returned_authorization_only"
            )
        self.record(
            name,
            response.status_code in expected,
            status=response.status_code,
            expected=sorted(expected),
            code=payload.get("error", {}).get("code") if isinstance(payload, dict) else None,
            **({"response_state": state} if state else {}),
        )
        return response, payload

    def login(self, account, password):
        client = self.client()
        self.clients[account] = client
        nonce = client.get("/api/v1/auth/csrf")
        if nonce.status_code != 200:
            self.record(f"{account}:anonymous_csrf", False, status=nonce.status_code)
            return False
        response = client.post(
            "/api/v1/auth/login",
            json={"username": account, "password": password},
            headers={"Origin": self.origin, "X-CSRF-Token": nonce.json()["csrf_token"]},
        )
        self.record(f"{account}:login", response.status_code == 200, status=response.status_code)
        if response.status_code != 200:
            return False
        actor = response.json()
        self.actors[account] = actor
        cookie = response.headers.get("set-cookie", "").lower()
        self.record(
            f"{account}:cookie_flags",
            "httponly" in cookie
            and "samesite=strict" in cookie
            and ("secure" in cookie if self.origin.startswith("https:") else True),
        )
        self.record(
            f"{account}:auth_contract",
            isinstance(actor.get("permissions"), list)
            and isinstance(actor.get("scope", {}).get("instance_ids"), list)
            and isinstance(actor.get("user"), dict)
            and bool(actor.get("csrf_token")),
        )
        return True

    def pages(self, account, path, **params):
        collected, total, seen = [], None, set()
        for _ in range(50):
            response, payload = self.request(account, "GET", path, params={**params, "limit": 100})
            if response.status_code != 200 or not isinstance(payload.get("items"), list):
                self.record(f"{account}:{path}:page", False, status=response.status_code)
                return [], None
            if total is None:
                total = payload.get("total")
            collected.extend(payload["items"])
            cursor = payload.get("next_cursor")
            if not cursor:
                break
            if cursor in seen:
                self.record(f"{account}:{path}:cursor_progress", False)
                break
            seen.add(cursor)
            params["cursor"] = cursor
        self.record(
            f"{account}:{path}:pagination_count",
            isinstance(total, int) and total == len(collected),
            total=total,
            returned=len(collected),
        )
        return collected, total

    def has_route(self, template):
        return "/api/v1" + template in self.schema

    def boundary_basics(self):
        self.clients["anonymous"] = self.client()
        self.expect("anonymous:authentication_required", "anonymous", "GET", "/auth/me", {401})
        response, body = self.request("anonymous", "GET", "/definitely-unknown-verification-path")
        self.record(
            "unknown_api_json_404",
            response.status_code == 404
            and "application/json" in response.headers.get("content-type", "")
            and body.get("error", {}).get("code") == "not_found",
        )
        marker = "verification-private-input-" + secrets.token_hex(16)
        self.passwords.append(marker)
        response, body = self.request(
            "anonymous", "POST", "/auth/login", body={"username": [], "password": marker}
        )
        self.record(
            "validation_does_not_echo_secret",
            response.status_code == 422
            and marker not in response.text
            and "input" not in json.dumps(body.get("error", {}).get("details", {})),
        )
        self.expect(
            "login:anonymous_nonce_required",
            "anonymous",
            "POST",
            "/auth/login",
            {403},
            body={"username": "verification_nonexistent_account", "password": marker},
        )
        response = self.clients["anonymous"].get(
            "/api/v1/health/live", headers={"Host": "untrusted.invalid"}
        )
        self.record(
            "untrusted_host_rejected", response.status_code == 400, status=response.status_code
        )
        response = self.clients["anonymous"].get("/api/v1/health/live")
        self.record(
            "security_headers",
            response.headers.get("x-content-type-options") == "nosniff"
            and response.headers.get("x-frame-options") == "DENY"
            and "frame-ancestors 'none'" in response.headers.get("content-security-policy", "")
            and response.headers.get("cache-control") == "no-store",
        )

    def scope_catalog(self):
        all_assets, _ = self.pages("admin", "/instances")
        self.assets = {r["id"]: r for r in all_assets}
        commerce = self.ids["systems"]["commerce"]
        ops = self.ids["departments"]["ops"]
        expected = {
            "dba": {i for i, r in self.assets.items() if r.get("department_id") == ops},
            "developer": {i for i, r in self.assets.items() if r["system_id"] == commerce},
            "viewer": {i for i, r in self.assets.items() if r["system_id"] == commerce},
        }
        self.expected = expected
        complete_assets = len(self.assets) >= 3 and all(
            any(r["system_id"] == system for r in all_assets)
            for system in self.ids["systems"].values()
        )
        self.record(
            "real_three_system_assets_present",
            True if complete_assets else None,
            reason=None
            if complete_assets
            else "Real research asset not provisioned yet; no synthetic substitute",
            actual_asset_count=len(self.assets),
            actual_system_count=len({r["system_id"] for r in all_assets}),
        )
        for role, allowed in expected.items():
            rows, total = self.pages(role, "/instances")
            actual = {r["id"] for r in rows}
            self.record(
                f"{role}:instance_scope",
                actual == allowed,
                expected_count=len(allowed),
                actual_count=len(actual),
            )
            actor_ids = set(self.actors[role].get("scope", {}).get("instance_ids", []))
            self.record(f"{role}:session_scope_summary", actor_ids == allowed)
            response, overview = self.request(role, "GET", "/overview")
            self.record(
                f"{role}:overview_scope_count",
                response.status_code == 200
                and overview.get("counts", {}).get("registered") == len(allowed),
            )
            for system_id in self.ids["systems"].values():
                subset, count = self.pages(role, "/instances", system_id=system_id)
                wanted = {i for i in allowed if self.assets[i]["system_id"] == system_id}
                self.record(
                    f"{role}:filtered_count:{system_id}",
                    count == len(wanted) and {r["id"] for r in subset} == wanted,
                )
        self.record(
            "admin:no_implicit_data_permission",
            "data.read" not in self.actors["admin"]["permissions"]
            and self.actors["admin"]["scope"].get("instance_count") == 0,
        )
        response, overview = self.request("admin", "GET", "/overview")
        self.record(
            "admin:overview_metadata_only",
            response.status_code == 200
            and overview.get("metadata_only") is True
            and overview.get("counts", {}).get("healthy") is None
            and not overview.get("source_health")
            and not overview.get("events"),
        )
        health_keys = {"health", "last_success_at", "last_attempt_at", "capabilities"}
        self.record(
            "admin:inventory_no_business_health",
            all(
                not any(r.get(k) not in (None, [], "unknown") for k in health_keys)
                for r in all_assets
            ),
        )
        if expected["dba"]:
            probe_body = {
                "instance_id": sorted(expected["dba"])[0],
                "title": "Must be rejected by CSRF",
            }
            self.expect(
                "write:authenticated_csrf_required",
                "dba",
                "POST",
                "/events",
                {403},
                body=probe_body,
                csrf=False,
            )
            self.expect(
                "write:cross_origin_rejected",
                "dba",
                "POST",
                "/events",
                {403},
                body=probe_body,
                headers={"Origin": "https://untrusted.invalid"},
            )
        now = datetime.now(timezone.utc)
        self.window = {"from": utc_iso(now - timedelta(hours=2)), "to": utc_iso(now), "step": 30}
        for role in ("admin", "dba", "developer", "viewer"):
            allowed = set() if role == "admin" else expected[role]
            for identifier in self.assets:
                forbidden = identifier not in allowed
                expected_status = {403} if role == "admin" else ({404} if forbidden else {200})
                for suffix in (
                    "overview",
                    "schemas",
                    "queries",
                    "locks",
                    "logs",
                    "replication",
                    "config",
                    "collection-runs",
                    "metrics",
                ):
                    # Metrics may be unavailable at its dependency; a 503 is NOT a scope pass.
                    statuses = expected_status | (
                        {503} if suffix == "metrics" and not forbidden else set()
                    )
                    response, payload = self.expect(
                        f"{role}:{identifier}:{suffix}",
                        role,
                        "GET",
                        f"/instances/{identifier}/{suffix}",
                        statuses,
                        params=self.window,
                    )
                    if role == "dba" and suffix == "metrics" and response.status_code == 200:
                        points = payload.get("points", [])
                        valid = sum(
                            p.get("value") is not None and p.get("validity") == "valid"
                            for p in points
                        )
                        self.record(
                            f"{role}:{identifier}:actual_prometheus_values",
                            payload.get("source") == "prometheus_resampled" if valid else None,
                            source=payload.get("source"),
                            point_count=len(points),
                            valid_point_count=valid,
                            null_point_count=sum(p.get("value") is None for p in points),
                        )
                if self.has_route("/instances/{identifier}/explain-options"):
                    explain_status = expected_status
                    if not forbidden:
                        permits = {"collection.trigger", "evidence.read_sensitive"} <= set(
                            self.actors[role]["permissions"]
                        )
                        explain_status = {200, 409} if permits else {403}
                    self.expect(
                        f"{role}:{identifier}:explain_options_scope",
                        role,
                        "GET",
                        f"/instances/{identifier}/explain-options",
                        explain_status,
                    )
                if role != "admin" and forbidden:
                    self.expect(
                        f"{role}:guessed_inventory_id:{identifier}",
                        role,
                        "GET",
                        f"/instances/{identifier}",
                        {404},
                    )
                    for resource in ("evidence", "events", "investigations"):
                        self.expect(
                            f"{role}:cross_scope_{resource}_filter:{identifier}",
                            role,
                            "GET",
                            f"/{resource}",
                            {404},
                            params={"instance_id": identifier, **self.window},
                        )
        for resource in ("evidence", "events", "investigations", "knowledge"):
            self.expect(
                f"admin:{resource}:no_business_read",
                "admin",
                "GET",
                f"/{resource}",
                {403},
                params=self.window,
            )

    def historical_objects(self):
        evidence, _ = self.pages("dba", "/evidence", **self.window)
        self.record("actual_live_evidence_available", bool(evidence), evidence_count=len(evidence))
        for role in ("developer", "viewer"):
            rows, total = self.pages(role, "/evidence", **self.window)
            wanted = [r for r in evidence if r.get("instance_id") in self.expected[role]]
            self.record(
                f"{role}:history_scope_count",
                total == len(wanted)
                and all(r.get("instance_id") in self.expected[role] for r in rows),
                total=total,
            )
        for item in evidence[:100]:
            identifier = item["id"]
            self.expect(
                f"admin:evidence_detail:{identifier}",
                "admin",
                "GET",
                f"/evidence/{identifier}",
                {403},
            )
            for role in ("developer", "viewer"):
                allowed = item.get("instance_id") in self.expected[role]
                self.expect(
                    f"{role}:evidence_detail:{identifier}",
                    role,
                    "GET",
                    f"/evidence/{identifier}",
                    {200} if allowed else {404},
                )
        if not evidence:
            self.record(
                "evidence_detail_actual_source_acceptance",
                None,
                reason="No real collected evidence in selected window",
            )
        for resource in ("events", "investigations", "knowledge"):
            rows, _ = self.pages("dba", f"/{resource}")
            for item in rows[:50]:
                source_ids = set(
                    item.get("instance_ids")
                    or ([item["instance_id"]] if item.get("instance_id") else [])
                )
                if not source_ids:
                    continue
                for role in ("developer", "viewer"):
                    status = {200} if source_ids <= self.expected[role] else {404}
                    author = bool(
                        {"knowledge.create", "knowledge.review"}
                        & set(self.actors[role]["permissions"])
                    )
                    if (
                        resource == "knowledge"
                        and not author
                        and item.get("approved_version") is None
                    ):
                        status = {404}  # Pure readers never see unpublished drafts.
                    self.expect(
                        f"{role}:{resource}_detail:{item['id']}",
                        role,
                        "GET",
                        f"/{resource}/{item['id']}",
                        status,
                    )
                _, detail = self.request("dba", "GET", f"/{resource}/{item['id']}")
                manifest_id = (
                    detail.get("manifest_id")
                    or (detail.get("manifest") or {}).get("id")
                    or (detail.get("content") or {}).get("manifest_id")
                )
                if manifest_id and self.has_route("/evidence-manifests/{identifier}"):
                    for role in ("dba", "developer", "viewer", "admin"):
                        allowed = (
                            role == "dba"
                            or role in self.expected
                            and source_ids <= self.expected[role]
                        )
                        author = bool(
                            {"knowledge.create", "knowledge.review"}
                            & set(self.actors[role]["permissions"])
                        )
                        if resource == "knowledge" and role != "dba" and not author:
                            latest = next(
                                (
                                    v
                                    for v in detail.get("versions", [])
                                    if (v.get("content") or {}).get("manifest_id") == manifest_id
                                ),
                                None,
                            )
                            allowed = (
                                allowed
                                and latest is not None
                                and latest.get("version") == detail.get("approved_version")
                            )
                        response, value = self.expect(
                            f"{role}:{resource}:manifest:{manifest_id}",
                            role,
                            "GET",
                            "/evidence-manifests/" + manifest_id,
                            {200} if allowed else ({403} if role == "admin" else {404}),
                            params={"limit": 1},
                        )
                        if response.status_code == 200:
                            self.record(
                                f"{role}:manifest_bounded_metadata:{manifest_id}",
                                isinstance(value.get("total"), int)
                                and len(value.get("items", [])) <= 1
                                and all("payload" not in r for r in value.get("items", [])),
                            )
        if self.has_route("/tasks"):
            tasks, _ = self.pages("dba", "/tasks")
            for role in ("developer", "viewer"):
                denied_tasks = [
                    t
                    for t in tasks
                    if t.get("instance_id") and t["instance_id"] not in self.expected[role]
                ]
                for item in denied_tasks[:10]:
                    self.expect(
                        f"{role}:cross_scope_task:{item['id']}",
                        role,
                        "GET",
                        "/tasks/" + item["id"],
                        {403, 404},
                    )
                for identifier in self.assets.keys() - self.expected[role]:
                    _, page = self.request(
                        role, "GET", "/tasks", params={"instance_id": identifier}
                    )
                    self.record(
                        f"{role}:cross_scope_task_count:{identifier}",
                        page.get("total") == 0 and page.get("items") == [],
                    )

    def wait_task(self, task_id):
        deadline = time.monotonic() + self.wait_seconds
        while True:
            response, payload = self.request("dba", "GET", "/tasks/" + task_id)
            if response.status_code != 200 or payload.get("status") in (
                "succeeded",
                "partial",
                "failed",
                "cancelled",
                "expired",
                "interrupted",
            ):
                return response, payload
            if time.monotonic() >= deadline:
                return response, payload
            time.sleep(min(1, max(0, deadline - time.monotonic())))

    def write_acceptance(self):
        if not self.write_probes:
            self.record(
                "mutable_scope_probes",
                None,
                reason="Enable --write-probes for labeled manual records and revocation account",
            )
            return
        commerce = sorted(self.expected["developer"])
        extra = sorted(self.expected["dba"] - self.expected["developer"])
        if not commerce or not extra:
            self.record(
                "mixed_scope_probes", None, reason="Need real commerce and second ops asset"
            )
            return
        first, other = commerce[0], extra[0]
        tag = "HTTP scope verification " + uuid4().hex[:12]
        event_ids = []
        for identifier in (first, other):
            response, event = self.expect(
                "manual_validation_event_create",
                "dba",
                "POST",
                "/events",
                {201},
                body={
                    "instance_id": identifier,
                    "title": tag,
                    "reason": "Manual authorization acceptance; no measured incident asserted",
                },
            )
            if response.status_code != 201:
                continue
            event_ids.append(event["id"])
            self.created.append({"type": "human_assertion_event", "id": event["id"]})
            for role in ("developer", "viewer"):
                status = {200} if identifier == first else {404}
                self.expect(
                    f"{role}:real_event_scope:{identifier}",
                    role,
                    "GET",
                    "/events/" + event["id"],
                    status,
                )
            self.expect(
                f"viewer:cannot_acknowledge:{identifier}",
                "viewer",
                "POST",
                "/events/" + event["id"] + "/acknowledge",
                {403} if identifier == first else {404},
                body={"version": event.get("version", 1), "reason": tag},
            )
        self.expect(
            "viewer:cannot_create_investigation",
            "viewer",
            "POST",
            "/investigations",
            {403},
            body={
                "instance_ids": [first],
                "from": self.window["from"],
                "to": self.window["to"],
                "question": tag,
            },
        )
        response, task = self.expect(
            "mixed_report_create",
            "dba",
            "POST",
            "/investigations",
            {202},
            body={
                "instance_ids": [first, other],
                "from": self.window["from"],
                "to": self.window["to"],
                "question": tag,
                "mode": "rules_only",
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        if response.status_code == 202:
            self.created.append({"type": "rules_only_investigation_task", "id": task["id"]})
            for role in ("developer", "viewer", "admin"):
                self.expect(
                    f"{role}:mixed_report_task_denied",
                    role,
                    "GET",
                    "/tasks/" + task["id"],
                    {403, 404},
                )
            _, state = self.wait_task(task["id"])
            ref = (state.get("result") or {}).get("investigation_id")
            if not ref and state.get("result_ref"):
                ref = str(state["result_ref"]).rstrip("/").rsplit("/", 1)[-1]
            self.record(
                "mixed_report_worker_completion",
                state.get("status") == "succeeded",
                status=state.get("status"),
            )
            if ref:
                for role in ("developer", "viewer", "admin"):
                    self.expect(
                        f"{role}:mixed_report_whole_scope",
                        role,
                        "GET",
                        "/investigations/" + ref,
                        {403, 404},
                    )
                self.created.append({"type": "rules_only_investigation", "id": ref})
        response, entry = self.expect(
            "mixed_knowledge_create",
            "dba",
            "POST",
            "/knowledge",
            {201},
            body={
                "instance_ids": [first, other],
                "title": tag,
                "content": {"summary": "Manual authorization acceptance draft"},
            },
        )
        if response.status_code == 201:
            self.created.append({"type": "human_draft_knowledge", "id": entry["id"]})
            for role in ("developer", "viewer", "admin"):
                self.expect(
                    f"{role}:mixed_knowledge_whole_scope",
                    role,
                    "GET",
                    "/knowledge/" + entry["id"],
                    {403, 404},
                )
        if event_ids:
            self.knowledge_reader_probe(first, event_ids[0], tag)
        self.explain_scope_probe(first)
        if self.has_route("/exports"):
            response, task = self.expect(
                "mixed_export_create",
                "dba",
                "POST",
                "/exports",
                {202},
                body={
                    "instance_ids": [first, other],
                    "resource": "evidence",
                    "format": "json",
                    "from": self.window["from"],
                    "to": self.window["to"],
                },
                headers={"Idempotency-Key": str(uuid4())},
            )
            if response.status_code == 202:
                self.created.append({"type": "private_export_task", "id": task["id"]})
                _, state = self.wait_task(task["id"])
                self.record(
                    "mixed_export_worker_completion",
                    state.get("status") == "succeeded",
                    status=state.get("status"),
                )
                for role in ("developer", "viewer", "admin"):
                    self.expect(
                        f"{role}:mixed_export_download_denied",
                        role,
                        "GET",
                        f"/exports/{task['id']}/download",
                        {403, 404},
                    )
                if state.get("status") == "succeeded":
                    response, _ = self.request("dba", "GET", f"/exports/{task['id']}/download")
                    self.record(
                        "dba:authorized_export_download",
                        response.status_code == 200,
                        status=response.status_code,
                        bytes=len(response.content),
                    )
        else:
            self.record("exports_actual_download_scope", None, reason="Export API not installed")
        self.revocation_probe(first)

    def knowledge_reader_probe(self, instance_id, event_id, tag):
        response, entry = self.expect(
            "reader_fixture:create",
            "dba",
            "POST",
            "/knowledge",
            {201},
            body={
                "instance_ids": [instance_id],
                "source_event_id": event_id,
                "title": tag + " published-reader",
                "tags": ["scope-validation-approved"],
                "content": {
                    "summary": "Automated authorization fixture, not a real user-approved operational case."
                },
            },
        )
        if response.status_code != 201:
            return
        identifier = entry["id"]
        self.created.append({"type": "automated_knowledge_reader_fixture", "id": identifier})
        self.expect(
            "viewer:unpublished_draft_hidden", "viewer", "GET", "/knowledge/" + identifier, {404}
        )
        self.expect(
            "developer:unpublished_draft_hidden",
            "developer",
            "GET",
            "/knowledge/" + identifier,
            {404},
        )
        _, page = self.request(
            "viewer", "GET", "/knowledge", params={"q": tag + " published-reader"}
        )
        self.record(
            "viewer:unpublished_draft_count_hidden",
            page.get("total") == 0 and page.get("items") == [],
        )
        _, entry = self.expect(
            "reader_fixture:submit",
            "dba",
            "POST",
            f"/knowledge/{identifier}/submit-review",
            {200},
            body={"version": 1},
        )
        _, entry = self.expect(
            "reader_fixture:automated_review",
            "dba",
            "POST",
            f"/knowledge/{identifier}/reviews",
            {200},
            body={
                "version": 1,
                "decision": "approved",
                "note": "Automated validation-account review of authorization behavior; not approval by an actual human DBA.",
            },
        )
        response, entry = self.expect(
            "reader_fixture:publish",
            "dba",
            "POST",
            f"/knowledge/{identifier}/publish",
            {200},
            body={"version": 1},
        )
        if response.status_code != 200:
            return
        approved = entry.get("content", {})
        response, draft = self.expect(
            "reader_fixture:new_draft",
            "dba",
            "POST",
            f"/knowledge/{identifier}/versions",
            {201},
            body={
                "version": 1,
                "tags": ["scope-validation-private-draft"],
                "content": {
                    "summary": "Private new draft must not replace the approved reader body."
                },
            },
        )
        if response.status_code != 201:
            return
        _, visible = self.expect(
            "viewer:approved_version_while_new_draft",
            "viewer",
            "GET",
            "/knowledge/" + identifier,
            {200},
        )
        self.record(
            "viewer:approved_body_and_tags_only",
            visible.get("version") == 1
            and visible.get("content") == approved
            and visible.get("tags") == ["scope-validation-approved"]
            and all(v.get("version") == 1 for v in visible.get("versions", [])),
        )
        _, page = self.request(
            "viewer",
            "GET",
            "/knowledge",
            params={"q": tag + " published-reader", "tag": "scope-validation-private-draft"},
        )
        self.record(
            "viewer:draft_tags_do_not_affect_count",
            page.get("total") == 0 and page.get("items") == [],
        )
        private_manifest = (draft.get("content") or {}).get("manifest_id")
        if private_manifest:
            self.expect(
                "viewer:new_draft_manifest_hidden",
                "viewer",
                "GET",
                "/evidence-manifests/" + private_manifest,
                {404},
            )
            self.expect(
                "developer:new_draft_manifest_hidden",
                "developer",
                "GET",
                "/evidence-manifests/" + private_manifest,
                {404},
            )
            self.expect(
                "dba:new_draft_manifest_authorized",
                "dba",
                "GET",
                "/evidence-manifests/" + private_manifest,
                {200},
                params={"limit": 1},
            )

    def explain_scope_probe(self, instance_id):
        if not self.has_route("/instances/{identifier}/explain-options"):
            return
        response, options = self.request("dba", "GET", f"/instances/{instance_id}/explain-options")
        if response.status_code != 200:
            self.record(
                "explain_actual_metadata",
                None,
                reason="No current actual column metadata; never fabricate it",
                status=response.status_code,
            )
            return
        item = next(
            (
                x
                for x in options.get("items", [])
                if any(c.get("predicate_allowed") for c in x.get("columns", []))
            ),
            None,
        )
        if not item:
            self.record(
                "explain_actual_metadata",
                None,
                reason="No allowed predicate in real selected-schema metadata",
            )
            return
        column = next(c for c in item["columns"] if c.get("predicate_allowed"))
        body = {
            "template": "single_table_lookup",
            "metadata_evidence_id": options["metadata_evidence_id"],
            "config_revision": options["config_revision"],
            "schema": item["schema"],
            "table": item["table"],
            "projection": [column["name"]],
            "predicates": [{"column": column["name"], "value": 1}],
            "limit": 1,
        }
        self.expect(
            "developer:explain_action_denied",
            "developer",
            "POST",
            f"/instances/{instance_id}/explain",
            {403},
            body=body,
        )
        self.expect(
            "viewer:explain_action_denied",
            "viewer",
            "POST",
            f"/instances/{instance_id}/explain",
            {403},
            body=body,
        )
        self.expect(
            "admin:explain_data_denied",
            "admin",
            "POST",
            f"/instances/{instance_id}/explain",
            {403},
            body=body,
        )
        for foreign in self.assets.keys() - self.expected["dba"]:
            self.expect(
                "dba:cross_scope_explain_denied:" + foreign,
                "dba",
                "POST",
                f"/instances/{foreign}/explain",
                {404},
                body=body,
            )
        response, _ = self.expect(
            "dba:raw_sql_explain_rejected",
            "dba",
            "POST",
            f"/instances/{instance_id}/explain",
            {422},
            body={"sql": "SELECT private_scope_marker"},
        )
        self.record(
            "explain_validation_no_raw_input_echo", "private_scope_marker" not in response.text
        )

    def revocation_probe(self, instance_id):
        _, options = self.request("admin", "GET", "/settings/access-options")
        role = next((r for r in options.get("delegable_roles", []) if r["name"] == "viewer"), None)
        if not role:
            self.record("immediate_revocation", None, reason="No explicitly delegable viewer role")
            return
        username, password = "verify_" + uuid4().hex[:16], secrets.token_urlsafe(32)
        self.passwords.append(password)
        response, user = self.expect(
            "ephemeral_revocation_user_create",
            "admin",
            "POST",
            "/users",
            {201},
            body={
                "username": username,
                "display_name": "HTTP scope verification (temporary)",
                "password": password,
                "grants": [
                    {"role_id": role["id"], "scope_type": "instance", "scope_id": instance_id}
                ],
            },
        )
        if response.status_code != 201:
            return
        self.created.append({"type": "disabled_revocation_user", "id": user["id"]})
        try:
            if self.login(username, password):
                self.expect(
                    "revocation:before",
                    username,
                    "GET",
                    f"/instances/{instance_id}/overview",
                    {200},
                )
                self.expect(
                    "revocation:remove_grant",
                    "admin",
                    "PATCH",
                    "/users/" + user["id"],
                    {200},
                    body={"grants": []},
                )
                self.expect(
                    "revocation:old_session_immediate_401",
                    username,
                    "GET",
                    f"/instances/{instance_id}/overview",
                    {401},
                )
                self.clients[username].close()
                if self.login(username, password):
                    self.expect(
                        "revocation:new_session_no_old_grant",
                        username,
                        "GET",
                        f"/instances/{instance_id}/overview",
                        {403},
                    )
        finally:
            self.expect(
                "revocation:ephemeral_user_disabled",
                "admin",
                "PATCH",
                "/users/" + user["id"],
                {200},
                body={"active": False},
            )

    def run(self):
        with self.client() as client:
            try:
                ready = client.get("/api/v1/health/ready")
            except httpx.HTTPError as exc:
                self.record("api_readiness", None, reason=type(exc).__name__)
                return
            self.record(
                "api_readiness",
                ready.status_code == 200 and ready.json().get("ready") is True,
                status=ready.status_code,
            )
            if ready.status_code != 200:
                return
            self.schema = client.get("/api/openapi.json").json().get("paths", {})
        self.boundary_basics()
        for user in self.plan["users"]:
            if user["username"] in ("admin", "dba", "developer", "viewer"):
                self.login(user["username"], user["password"])
        if not all(name in self.actors for name in ("admin", "dba", "developer", "viewer")):
            return
        self.scope_catalog()
        self.historical_objects()
        if self.has_route("/tasks/{identifier}") or any(
            p.startswith("/api/v1/tasks/") for p in self.schema
        ):
            self.write_acceptance()
        else:
            self.record("task_report_export_acceptance", None, reason="Task API not installed")
        # Logout has to revoke the actual server session, not merely clear cookies.
        client = self.clients["viewer"]
        cookie = client.cookies.get("dba_session")
        self.expect("logout:success", "viewer", "POST", "/auth/logout", {204})
        if cookie:
            client.cookies.set(
                "dba_session", cookie, domain=urlsplit(self.base).hostname, path="/api/v1"
            )
            self.expect("logout:replayed_cookie_rejected", "viewer", "GET", "/auth/me", {401})

    def report(self):
        counts = Counter(row["outcome"] for row in self.checks)
        return {
            "schema_version": 1,
            "started_at": self.started_at,
            "finished_at": utc_iso(),
            "source": "actual_http",
            "base_url": self.base,
            "write_probes": self.write_probes,
            "summary": dict(counts),
            "complete": bool(counts["pass"]) and not counts["fail"] and not counts["not_tested"],
            "created_validation_records": self.created,
            "checks": self.checks,
        }

    def close(self):
        for client in self.clients.values():
            client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--ids", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--write-probes", action="store_true")
    parser.add_argument(
        "--wait-seconds", type=int, default=30, choices=range(0, 61), metavar="0..60"
    )
    args = parser.parse_args(argv)
    repository = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if output.is_relative_to(repository):
        parser.error("Verification proof must be written outside the source repository")
    verifier = None
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        private = Path(config["private_dir"])
        plan = json.loads(
            (args.plan or private / "initialization-plan.json").read_text(encoding="utf-8-sig")
        )
        ids = json.loads(
            (args.ids or private / "initialization-ids.json").read_text(encoding="utf-8-sig")
        )
        base = args.base_url or config["public_origin"]
        # Avoid accidentally sending test credentials to a different origin.
        if urlsplit(base)[:2] != urlsplit(config["public_origin"])[:2]:
            parser.error("--base-url must match the configured public origin")
        verifier = Verification(
            base,
            config["public_origin"],
            plan,
            ids,
            write_probes=args.write_probes,
            wait_seconds=args.wait_seconds,
        )
        try:
            verifier.run()
        except Exception as exc:
            # HTTP errors may contain request bodies/URLs; persist only the class.
            verifier.record("verification_interrupted", False, error_type=type(exc).__name__)
        report = verifier.report()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {"summary": report["summary"], "complete": report["complete"]}, ensure_ascii=False
            )
        )
        return 0 if report["complete"] else (1 if report["summary"].get("fail") else 2)
    except (OSError, ValueError, KeyError) as exc:
        print(
            json.dumps(
                {
                    "error": "private_verification_configuration_unavailable",
                    "type": type(exc).__name__,
                }
            )
        )
        return 2
    finally:
        if verifier:
            verifier.close()


if __name__ == "__main__":
    sys.exit(main())
