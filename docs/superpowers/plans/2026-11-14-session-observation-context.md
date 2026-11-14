# Session observation context repair

> **For agentic workers:** Use superpowers:executing-plans to implement this single repair, without concurrent controllers.

**Goal:** Stop losing safe session command/state evidence before AI analysis, without exposing SQL or inventing causal conclusions.

**Architecture:** Extend the existing allowlisted model projection for `mysql.sessions`. Preserve finite command/state values; add fixed interpretation limits only for the verified diagnostics producer. Retain these limits through tool compaction and disclose the additional external fields in the preview. Do not change collection, scopes, frozen windows, prompt budgets, scheduling, or SQL.

**Tech Stack:** Python, existing FastAPI investigation pipeline, pytest.

**Spec:** [Continuous evolution](../specs/2026-11-13-continuous-evolution-design.md); same TXN-HISTORY01 normal counterexample, not a new scenario.

## Evidence and boundary

ROUND42's bounded normal autocommit workload completed 238 commits with transaction age zero and a fluctuating history list. The natural report treated long-duration background sessions as requiring review. Raw session evidence contained `Daemon` and `Waiting on empty queue` / `Suspending`, but the actual model request retained only IDs and duration. The loss is in `project_evidence`, not upstream collection. This repair supplies observations; it must not manufacture a healthy verdict or specific root cause.

## Review focus

- Reject arbitrary state text, SQL, credentials, conflicting field spellings and non-string enums.
- Do not assign verified producer semantics to a lookalike custom item or another source.
- Preserve raw evidence, evidence identity, timestamps, frozen scope and incomplete coverage.
- Preserve retained session context under initial/final/tool packing; keep existing hard bounds.
- Local projection tests and offline replay do not prove a new live model diagnosis.

## Single task

- [x] Add regression tests for the actual data-loss boundary, safe/unknown/conflicting enums, source qualification, packing and preview disclosure; observe meaningful failures (8 fail / 12 pass before repair).
- [x] Add the minimal allowlist and fixed source-qualified interpretation limits in `platform_app/tools.py`; keep limits in `platform_app/model_context.py`; update preview disclosure in `platform_app/investigations.py`.
- [x] Run focused and backend checks, replay actual frozen evidence locally without model calls, and request independent review (84 focused; 1380 backend pass, 25 skip, 6 subtests; two actual frozen observations replayed; no important review findings).
- [x] Record normal-control outcome and verification in case/status/manual/CHECKPOINT. Publish the exact reviewed overlay, deploy sealed API/AI20 with rollback and unchanged collector16/frontend, and verify actual frozen session input in one naturally dispatched periodic report. Full TXN scenario and visual acceptance remain incomplete.
