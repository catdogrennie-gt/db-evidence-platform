# Official monitoring and analysis groups implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development or executing-plans to implement this plan task-by-task. Update the runtime ledger with actual results.

**Goal:** Official MySQL/Linux measurements can be selected by host and item into a persistent, authorized joint analysis object, with charts and real bounded AI analysis.

**Architecture:** Reuse MonitoringSource, MonitoringMapping, Instance, collection leases and inspection policies. Synthetic group mappings contain exact selected item identities and metadata, while a single `engine=analysis` Instance supplies authorization and dispatch identity. Generic typed data is independent of optional MySQL semantic enhancement.

**Tech Stack:** Existing FastAPI/SQLAlchemy/MySQL/React/TypeScript/ECharts and Zabbix 7.0 API/Agent 2.

**Spec:** `docs/superpowers/specs/2026-11-09-analysis-groups-design.md`

## Global constraints

- No email, music, public deployment or private-data publication.
- Official templates remain unmodified; diagnostic extensions are separate and identified.
- Preserve existing history, original host mappings and frozen reports.
- One source and one business system per group; exact item selection, explicit relations, max 32 items and 32 relations.
- No arbitrary SQL, shell or target writes by AI. Consequential remediation is outside this change.
- Numerical data, text/log snapshots and unsupported binary have different representations. Unknown is not zero.
- Calls and evidence are bounded; text/log payloads and unsafe metadata do not pass directly into external model input.

## Review focus

- Selecting an itemid under the wrong host or outside allowed source hostgroups must fail without leaking it.
- Same-named metrics on different hosts retain independent stable identities, units and source provenance.
- A lost or unsupported selected item remains a visible gap instead of silently altering the group definition.
- Editing a group must reject stale revisions and prevent a former lease from overwriting the new configuration; historical evidence stays unchanged.
- Arbitrary metric labels/log text must not become instructions or disclose secrets to the model; numeric unknown keys still have useful bounded analysis.

### Task 1: Official real collection (infrastructure owner)

**Private files:** new scripts and proof in the existing private deployment/infra directory; no passwords or raw configuration in source.

- [x] Read exact-version official MySQL and Linux templates and Agent 2 instructions.
- [x] Confirm baseline host/item/error/freshness statistics before edits.
- [x] Add official Linux and MySQL hosts, scoped credentials and real Agent 2 collection; retain old hosts.
- [x] Add a separate lock/SQL diagnostic extension with explicit unknown counter epoch.
- [x] Verify two successive actual Zabbix history samples, including units and timestamps; record remaining unsupported items.

### Task 2: Typed catalog and persistent joint objects (backend owner)

**Files:** `platform_app/monitoring.py`, `platform_app/routes/monitoring.py`, new focused tests under `tests/platform/`.

**Interfaces:**

- `POST /api/v1/monitoring/sources/{source_id}/items/query`: hostids, host/item tag predicates, search, offset, limit <= 200; returns metadata, counts and pagination. Tags are lists and may repeat names.
- `GET /api/v1/monitoring/groups`, `GET /groups/{id}`, `POST /groups`, `PUT /groups/{id}`.
- Group input: name, source_id, description, items[{hostid,itemid}], relations[{from_hostid,to_hostid,kind,label}], enabled, interval_seconds(30..3600), config_revision(required on PUT).
- Group output: mapping id, instance_id, name, description, source/system identity, enabled/revisions/status/last observation, counts, full selected items/hosts, relations and interval.
- `GET /groups/{id}/metrics` and `GET /groups/{id}/items/{itemid}/history` preserve numeric and typed nonnumeric representations.

- [x] RED: catalog unknown-key/tag/repeated-tag tests; group exact scope, unauthorized source/host/item, duplicates, missing items and 32-item boundary.
- [x] Implement generic metadata and group binding without schema migration; preserve old selection APIs.
- [x] GREEN: create/update/pause, optimistic revision, no-op saves, authorization and old mapping compatibility.

### Task 3: Collection, frozen charts and AI (same backend owner)

**Files:** `platform_app/metrics.py`, `tools.py`, `intelligence.py`, `investigations.py`, `evidence.py`, `worker.py` as required; focused tests.

- [x] RED: selected multi-host numeric/text/log collection and source provenance; same-name independent curves; selected-item-only scope.
- [x] Carry safe numeric metadata into immutable chart windows and model projections; keep raw text restricted.
- [x] Include group composition and explicit relations in frozen evidence, preserving history across edits.
- [x] Address total-window budget overflow using explicit bounded per-series representation instead of whole-group omission.
- [x] Verify optional `dbops.mysql.*` snapshots, timer units and unknown digest reset epoch handling.
- [x] GREEN: model references, budget/coverage, old reports and authorization regression.

### Task 4: Guided UI (frontend owner)

**Files:** `frontend/src/AnalysisGroups.tsx`, related model/tests, `Monitoring.tsx`, `Intelligence.tsx`, `styles.css`, navigation if needed.

- [x] Show the joint-object list as the primary monitoring workflow, keeping original independent objects accessible.
- [x] Four steps: source/name → host/item selection with search/tags/paging → optional relations → review and save.
- [x] Edit/pause with config_revision; show actual sampling state, value kinds, host/item counts and limits.
- [x] Numeric charts and typed history viewer; link group.instance_id to existing policy configuration and reports.
- [x] Run meaningful selection/model tests, typecheck and production build.

### Task 5: Integration and actual platform analysis (coordinator)

**Private files:** `intelligence-runtime/upgrade-20261109/`, bounded acceptance driver in `local-state/`.

- [x] Preserve baseline control schema in private SQL backup; record running service ownership.
- [x] Run backend/frontend regression gates, restart only owned application services with the existing private config.
- [x] Via ordinary authenticated HTTP, discover official data, create a Linux+MySQL group and prove selected metadata/series/history match Zabbix.
- [x] Check owner/viewer/other/admin scope behavior; keep old report unchanged.
- [x] Run one bounded real automatic/model cycle with authorization and configured external-use budget. Record normal/no-issue result honestly if no anomaly exists. Do not fabricate measurements or label a controlled Lab fault as a production incident.
- [x] Inspect actual UI through a supported route; if native browser automation is unavailable, run independent rendered/browser tests and state the limitation.

### Task 6: Review, documentation and reproducibility (coordinator + independent review)

- [x] Independent whole-change review, then address actionable defects and rerun relevant checks.
- [x] Update user guide, requirements/architecture notes, README and acceptance results only with verified functionality.
- [x] Save progress, exact commands, source commits, current entry and remaining boundaries in parent CHECKPOINT.txt and this plan's ledger.
- [x] Local commit after review. Public publishing follows existing authorization only after private-data checks; no need to create another repository.

## Execution record

Implemented and reviewed in the existing isolated feature worktree. Whole platform regression: 571 passed, 10 skipped, 6 subtests; final display-only follow-up: 53 passed. Frontend: 27 tests and production build/typecheck passed. Ordinary HTTP: 11 checks; isolated browser: 5 group-flow and 5 report checks. Original 5,750 manifest item hashes were unchanged at the final comparison.

Actual normal and deliberately controlled Lab lock periodic model cycles completed, with per-call coverage and omissions documented in `docs/acceptance-analysis-groups.md`. The lock report retained an incorrect CPU range statement; original output and exact correction are disclosed, without pretending it was rewritten or approved by the user. No production adoption, automatic repair, or general accuracy claim.
