# Quick showcase implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Implement disjoint tasks with integration and independent review.

**Goal:** Anonymous fault-and-result website A backed by the existing independently inspecting platform B, with reversible fast cadence and a read-only B account.

**Architecture:** A in SignalLens-Lab calls fixed-scope B service APIs. B keeps collection, durable inspection and AI ownership. Both observe one actual isolated MySQL through official Zabbix templates.

**Review focus:** no public general AI/SQL/config access; no overlapping fault sessions; OFF never blocked by quotas/monitoring; temporary cadence restores on failure without clobbering admin edits; fresh snapshot vs truncated history; recovery/reappearance and in-flight dedup; read-only scope; honest stale/failed results.

### 1. B autonomous inspection and fixed service contract

- [x] Add `platform_app/showcase.py`, `routes/showcase.py`, config field/router and worker cadence lease reconciliation; token only authorizes fixed snapshot/pacing.
- [x] Fix normal signal v2 completeness, recovery/reappearance and in-flight dedup; project policy state by visible target; persist actual observation interval.
- [x] Tests: token/scope/bad input, actual cadence activation/restoration/expiry/admin edit, no direct Investigation from pacing, snapshot gaps, real empty recovery followed by new abnormal episode, shared readonly policy status.

### 2. A controller and site backend

- [x] Add `lab/toggle_target.py`, `toggle_controller.py`, `toggle_web.py` and web dependencies/tests, preserving previous CLI.
- [x] Implement fixed guarded SSH/MySQL workload, short holder lease, exclusive controller, desired-state API, scope-limited B client and pacing renewal.
- [x] Tests: idempotent concurrent ON, rapid OFF/ON final state, no cooldown/count/recovery gate, rollback and lease expiry, safe input/errors/no AI endpoint calls.

### 3. A presentation and B read-only interface

- [x] Add `lab/web/` page/scripts/styles: switch, actual fault status, temporary cadence explanation, actual B discovery/AI status, summary, evidence and up to three curves; no fabricated percentages or report.
- [x] B returns to four ordinary navigation entries; readonly hides edit settings and shows real background inspection and sample timestamps with timely refresh.
- [x] Frontend build/render tests and both-site actual Chrome acceptance.

### 4. Runtime, actual integration, docs and publication

- [x] Back up runtime policy/cadence/source config; disable previous demo controller; provision fixed service capability and readonly public identity; no secret in source.
- [x] Set official Zabbix snapshot sampling to 5 seconds, respecting actual tags; start A, retain B URL and add separate A tunnel.
- [x] Exercise actual ON/OFF and autonomous B analysis; verify actual cadence restore, no workload overlap, semantic limits, screenshots and public endpoints.
- [x] Independent review; fresh relevant/full tests and public-file/link gates; document both sites and publish clean public ancestry to both repositories. Update parent CHECKPOINT and private handoff.

Actual integration, failure semantics and verification scope: [acceptance record](../../acceptance-quick-showcase.md). Publication commits and private handoff are recorded outside source and in public Git history.
