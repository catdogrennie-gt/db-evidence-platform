# Named Capacity Context Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans in the existing isolated worktree. Do not delegate implementation.

**Goal:** Preserve named deterministic capacity facts in actual bounded model requests.

**Architecture:** Exempt capacity calculations from the shared positional table in `model_context.py`. Keep all original facts and the existing authorization, citation and fail-closed budget path; bump the auditable input revision without expanding the prompt.

**Tech Stack:** Python, pytest, existing FastAPI/MySQL worker.

**Spec:** [Input design](../specs/2026-11-13-named-capacity-context-design.md).

## Constraints and review

No SQL/shell/model writes, no new evidence, no scenario-specific answers, no current data in frozen history. Active and recovered capacities must remain distinct; mixed baseline signals must continue decoding; final compaction cannot lose threshold, operand references or timestamps. Oversized protected evidence must fail closed. Public source publication uses clean public ancestry; runtime handoff only when the old worker is idle.

## Task 1: Lossless named capacity calculations

- [x] Add failing active/recovered, mixed-signal, final-budget and actual-request tests in `tests/platform/test_named_capacity_context.py`.
- [x] Observe intended failures, then minimally change `_encode_numeric_context` in `platform_app/model_context.py`.
- [x] Bump `PROMPT_VERSION` in `platform_app/investigations.py` and its explicit revision check.
- [x] Run new and related request/compaction/authorization tests, then clean public candidate checks; request focused review.
- [x] If verified, seal an AI-only candidate and hand off when idle; public API and collector remain unchanged. Recheck evidence, budget, leases and normal baseline before one natural retest.
- [x] Record actual semantic result, cleanup, public availability and next action; update status/manual without calling program tests a diagnosed scenario.

Result: four intended representation failures before the fix; 114 related tests and 1210 clean-public backend tests passed (25 skipped, 6 subtests). AI-only release 14 was handed off while idle. CAPACITY-07 independently reached 33/40 and naturally produced the explicit 82.5% versus configured 80% risk explanation. Both operand references, timestamps and selection scope were retained. OFF, fresh recovery samples and cadence restoration were read back. Public B report, issue association and charts were visually checked in isolated Chrome. This is a bounded L1 capacity-risk pass, not a root-cause or full platform acceptance. Public A remains gated pending a curated single-scene release.
