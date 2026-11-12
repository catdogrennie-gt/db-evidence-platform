# Health Analysis Evolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Tasks are independently reviewed; implementation remains isolated from the running public checkout.

**Goal:** Validate and improve the platform against multiple real monitoring gaps while keeping the current public demo available.

**Architecture:** Independent staging control schema, bounded multi-service Lab targets and versioned release directories. A only controls approved workloads and reads fixed B projections; B discovers and analyzes through its own Zabbix pipeline. A local Caddy gateway enables checked API version switching and rollback.

**Tech Stack:** Existing Python/FastAPI/MySQL, React/TypeScript/ECharts, Zabbix; isolated Docker Lab services; Caddy on Windows.

**Spec:** ../specs/2026-11-12-health-analysis-evolution-design.md

## Global Constraints

- Never stop existing A/B, overwrite their build artifacts, use monitoring storage as a fault target, or publish secrets/PII.
- No AI SQL/Shell/write execution; no scenario labels or expected answers in AI evidence.
- Preserve real samples, unknown data, rules, model hypotheses and historical reports distinctly.
- No concurrency amplification: one controlled scenario owner and bounded connections/workload; no artificial cooldown or run-count UI barriers.
- Keep manuals and validation-scenarios.md accurate as actual results arrive.

## Review Focus

- Repeated ON, rapid OFF/ON, controller loss and switching active scenarios must not accumulate workload.
- Missing/stale/sparse/reset/counter data must not become a confident anomaly or recovery.
- Read-only API access must not leak secrets, cross business scope or schedule tasks, even with valid CSRF.
- Old assets and sessions must remain usable while gateway switches version; workers must not compete unsafely.
- AI-only findings must be distinguishable by stable evidence dimensions and remain reviewable hypotheses.

## Task 1: Protect running services and prepare release switching

Files: scripts/platform/release_gateway.py, tests/platform/test_release_gateway.py, docs/deployment-releases.md; private runtime outside repository.

- [ ] Capture current health/PIDs/addresses and isolate staging schema/config/ports.
- [ ] Add tested Caddy config generation, readiness preflight, atomic apply and rollback support; loopback-only admin, no secrets in source.
- [ ] Verify failed target cannot replace active route; validate continuous requests during successful switch and rollback.
- [ ] Prepare immutable release artifacts; leave old public addresses serving until replacement has passed acceptance.

## Task 2: Full read-only product experience

Files: platform_app/access.py, new readonly projection domain/router, relevant frontend navigation/config/source pages and tests. Root integrates router in api.py.

- [ ] Add safe scoped read projections for source/config/inspection/platform state and test write/cross-scope denials.
- [ ] Keep old A bridge reader minimal; new public demo and internal bridge identities are separate.
- [ ] Make read-visible versus writable explicit in UI; no management permission to unlock read-only pages.
- [ ] Validate actual guest navigation and API responses, including read-only POST catalog queries.

## Task 3: Normal-pipeline generic evidence changes and multiple findings

Files: platform_app/health_signals.py, intelligence.py, investigations.py, zabbix_items.py if needed; tests/platform/test_health_signals.py and adjacent tests.

- [ ] Write failing cases for fresh baseline changes, constant values, counters, missing samples and restarts.
- [ ] Add generic numeric baseline change evidence and normal signal scheduling without embedding scenario answers.
- [ ] Give model findings stable metric/resource dimensions so unrelated hypotheses do not overwrite one another.
- [ ] Preserve authorization, bounded evidence and existing automatic collection/AI queues.

## Task 4: Multi-scenario controlled Lab and fixed B projection

Files: separate SignalLens-Lab worktree lab/scenario_* and business_app modules, bounded compose extension, tests/web-v2; root owns platform_app/showcase.py and routes/showcase.py.

- [ ] Prepare multiple business MySQL/app services with official Zabbix base templates and narrowly scoped extensions.
- [ ] Implement first four scenarios, then long transaction and replica if resource/collection gates pass; no target mutation on old public scenario.
- [ ] Add fixed target catalog to B bridge, generic observations/reports/charts; retain old single-lock endpoint compatibility.
- [ ] Add A scenario selector, one ON/OFF control and measured progress/read-only results; show unimplemented cases truthfully.
- [ ] Verify workload bounds, controller exclusivity, lease cleanup and rapid toggling.

## Task 5: Real validation, iteration and operating manual

Files: docs/validation-scenarios.md, docs/operator-manual.md, docs/acceptance-health-analysis.md; private measurements and reproduction scripts outside public repository.

- [ ] For each case collect baseline, inject, observe actual Zabbix alerts/samples, wait for autonomous B, evaluate report, stop and observe recovery.
- [ ] Record failures and fix demonstrated pipeline gaps; repeat only affected verification.
- [ ] Document startup, monitoring selection, analysis reading, scenario operation, release switching, rollback and troubleshooting.
- [ ] Run relevant tests/builds/security source gate; independent code and factual review before publication.
- [ ] Switch verified release behind gateway with continuous probes; update public links/docs after both new sites pass. Keep previous release and old entrypoints available.
