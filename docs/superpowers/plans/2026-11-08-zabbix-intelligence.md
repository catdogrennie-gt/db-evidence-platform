# Zabbix Intelligence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend DB Evidence Platform into an automatic Zabbix-backed database problem analysis product with visual, evidence-based reports.

**Architecture:** Retain the current FastAPI/MySQL control plane, authorization, event and investigation entities and independent AI queue. Add a bounded external-monitoring provider and durable inspection dispatcher; improve the React/Ant Design/ECharts presentation rather than building a second analysis backend.

**Tech Stack:** Existing Python 3.12/FastAPI/SQLAlchemy/Alembic/httpx; React 19/TypeScript/Ant Design/ECharts; Zabbix JSON-RPC; existing pytest and frontend typecheck/build.

**Spec:** `docs/superpowers/specs/2026-11-08-zabbix-intelligence-design.md`

**Delivery checkpoint:** User requested immediate wrap-up at 2026-11-08 23:55 CST. Freeze completed implementation at `8fc5e8c` plus final documentation/screenshots; do not continue feature work. Final Python regression: 546 passed / 10 skipped / 6 subtests; frontend 18 passed, typecheck/build/contracts/dependencies passed. The source-window all-or-nothing budget issue and real SQL epoch validation remain unfinished. In-progress window-budget tests are retained privately, excluded from publication. Preserve unchecked combined tasks as partially completed rather than implying every acceptance goal is met.

## Global Constraints

- Never send email. Secrets, private materials and private exports never enter tracked/public artifacts.
- Old events, investigations, frozen reports, direct MySQL onboarding and explicit backend authorization remain compatible.
- Source host identity is not a MySQL UUID. No invented MetricFrame clocks or false monitoring capabilities.
- Missing samples remain null/unknown. Frozen reports do not silently update from live history.
- Periodic AI inspection runs without threshold breaches; urgent checks are a second path. Durable deduplication, scope, consent and run budgets apply.
- UI and README distinguish real measurements, deterministic rules, live AI, failed runs and simulated/replayed material.
- Preserve the previous public commit in a historical release. Online deployment is deferred.

## Review Focus

- Scope revocation or policy pause during queued/running work must prevent later external model calls and unauthorized reads.
- A source reusing host IDs or an object being deselected must not mix identities or delete historical reports.
- Stale/unsupported/truncated history and source failure must not render healthy or turn gaps into zeros.
- Concurrent due scans, restart and time gaps must not create duplicate or unlimited paid runs.
- A model's unreferenced explanation and coincident metric changes must not become proven causal facts or synthetic chart data.

## Delivery checkpoint

Checked items record completed implementation or bounded verification in the stated scope. Final unified regressions, actual report curves/screenshots and publication remain unchecked. The controlled periodic lock analysis has passed its stated semantic checks; its historical report lacks frozen curves. Final manifest/time-series retention, boundary handling and late-report/duplicate-result corrections still require completion and fresh verification. See [actual acceptance](../../acceptance-zabbix.md).

## Task 1: Source selection and existing-domain integration

**Files:** models.py, secrets.py, new monitoring.py and zabbix.py, routes/monitoring.py, api.py, incremental migration; tests/platform/test_monitoring.py.
**Consumes:** existing scope authorization, secret store and durable tasks. **Produces:** source/discovery/selection/object/metrics endpoints in the spec, stable external mapping and normalized real Evidence.

- [x] Write meaningful tests for token redaction, source/system authorization, UUID-free external registration, idempotent selection/deselection, unsupported capability and bounded history with missing samples; run and observe the missing behavior.
- [x] Implement source client, provider dispatch, durable selection and migration without opening direct MySQL transports for external objects.
- [x] Run targeted tests and the full backend suite; expected all tests pass with existing environment-dependent skips documented. Commit the source integration.

## Task 2: Source-neutral frozen windows and report data

**Files:** metrics.py, evidence.py, tools.py, investigations.py, routes/manifests.py and tests/platform/test_monitoring_reports.py.
**Consumes:** Task 1 provider and Evidence. **Produces:** authorized frozen external windows usable by existing tools, report/detail graph and chart projections; old report compatibility.

- [x] Test frozen external history is immutable after source change, references authorize correctly, gaps/units persist, and old Prometheus windows still work. Verify failure first.
- [ ] Add source-neutral time_series_window provider/projection and balanced compact evidence selection, preserving logical sources and reference validation.
- [ ] Run targeted and full backend checks. Commit the frozen-window integration.

## Task 3: Durable automatic inspection and result aggregation

**Files:** new intelligence.py, routes/intelligence.py, models.py, worker.py, migrations; tests/platform/test_intelligence.py.
**Consumes:** Tasks 1–2 and existing create_investigation/AI queue. **Produces:** policy, dispatcher, overview and issue report endpoints exactly as the spec.

- [x] Test recurring run without anomaly, urgent changed issue, duplicate/concurrent scan, missed-cycle no-catchup, permission/consent pause, daily cap and process restart. Verify failures before implementation.
- [x] Implement persisted policy/dispatch, row locks, current owner authorization, bounded resource grouping, queue limits and visible failure state. Reuse original Event/Investigation IDs rather than a second result system.
- [x] Add deterministic conditional risk only when sufficient actual history exists; test insufficient/gapped history produces no precise forecast.
- [ ] Run targeted and complete backend suite, inspect OpenAPI contract, and commit.

## Task 4: Approved product UI and real-data drill-down

**Files:** frontend/src/Intelligence.tsx, monitoring API types/helpers, App.tsx, styles.css, existing report/chart components as needed.
**Consumes:** frozen endpoint contract in the spec. **Produces:** results-first homepage, visual issue report, source/object selection, metric view and inspection settings; existing workflows/deep links preserved.

- [x] Implement typed response adapters and behavioral tests for null/gap preservation, pending/failed report states, scope resets and safe relationship labels; observe failures first for new behavior.
- [x] Build the approved skeleton as real pages using existing session/CSRF/useResource/scope helpers. No fake success action or hard-coded issue counts.
- [ ] Make graph nodes and chart/evidence links useful; distinguish observed/registered relationships and hypotheses. Preserve report frozen window; current metric view is separately labeled.
- [ ] Run frontend behavior checks, `npm run typecheck`, `npm run build`; validate root width/responsive layouts using supported UI controls. Commit.

## Task 5: Isolated local runtime and actual end-to-end verification

**Files:** private runtime scripts/acceptance proofs outside tracked source; public reproducibility scripts/docs without private values.
**Consumes:** Tasks 1–4, actual local Zabbix and independent SignalLens-Lab.

- [ ] Provision a separate control schema/config and app port; retain original runtime and baseline source unchanged. Apply explicit migration and verify old baseline remains recoverable.
- [ ] Connect real Zabbix, select actual objects, save policies and observe autonomous dispatches while the browser is closed.
- [ ] Exercise lab lock/SQL scenarios with bounded cleanup and real model calls under existing consent/budget. Verify findings against source evidence, freezing, restart, scope denial and failure behavior.
- [ ] Capture actual screenshots and reports. Run final regression/typecheck/build and independent review, then fix material findings with reproducible tests.

## Task 6: Public delivery and documentation

**Files:** README.md, CHANGELOG.md, docs/releases/*, docs/user-guide.md, docs/architecture.md, verified screenshots; private verification notes remain outside tracked artifacts.

- [x] Preserve exact previous public source commit with historical tag/release and release notes. Use available authenticated GitHub tools; if a tool lacks a release operation, pursue the supported UI/CLI path without stopping independent development.
- [ ] Follow official GitHub README guidance and suitable open-source product layouts. Show primary product/report screenshots directly before technical detail; fold only secondary evidence or verbose instructions.
- [ ] Write install, operations, source capability, model limitations, acceptance, demonstration and version migration docs based on actual verification. Check links and secret scans.
- [x] Review private project materials for factual accuracy; retain unrelated private content outside tracked/public artifacts.
- [ ] Publish verified current source and documentation to the original public repository without overwriting concurrent user commits. Fetch/read back the published commit and key images. Defer cloud/tunnel deployment.
