# Safe model transport diagnostics implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this single task inline, with requesting-code-review before delivery.

**Goal:** Preserve an allowlisted transport error category on an unsuccessful model call without changing its unknown-outcome, unknown-cost or no-retry semantics.

**Architecture:** The existing provider adapter maps an HTTPX exception to a fixed category. `ModelTransportError` accepts only that allowlist; the investigation stores the category in the existing private call metadata. No migration, endpoint, prompt, evidence, model request or retry policy changes.

**Tech Stack:** Python, HTTPX, SQLAlchemy, pytest.

**Spec:** `docs/superpowers/specs/2026-11-13-continuous-evolution-design.md` — instrument the earliest observed failure, keep unknown calls and costs unknown.

## Evidence and boundary

The natural 17:38 investigation failed with `model_call_outcome_unknown`; the adapter collapses timeout and transport errors into that single code. The existing response audit is created after a valid provider response, so it cannot recover the original failure type. Current non-billable connectivity succeeding does not identify the historical cause. This change improves future observability, not diagnosis accuracy or network reliability. POOL-01 retains its original cumulative clock; no new fault or model call is needed for this task.

## Review focus

- A raw exception can contain a credential, URL or sensitive provider text: persist only fixed categories.
- Read, write and connection failures do not establish whether a provider billed a request: preserve unknown outcome and cost.
- Unknown/custom subclasses and future HTTPX failures must have a fixed generic fallback.
- Existing model HTTP rejection, successful response, audit and request-delivery contracts must stay unchanged.
- Error categorization must neither retry nor alter request content or authorization.

## Task: one bounded observability improvement

- [ ] Add failing category, redaction, single-attempt and persistence regressions in `tests/platform/test_model_transport_diagnostics.py`.
- [ ] Run the regressions against unchanged production code and preserve the failure.
- [ ] Modify only `platform_app/investigations.py` to classify known transport exceptions, validate the category and persist it only when supplied.
- [ ] Run the focused regressions and existing AI failure/request-delivery tests; request independent review and address concrete findings.
- [ ] If review and checks pass, seal and verify an immutable candidate. Deploy only via the reviewed idle handoff with one-minute notice and exact fallback; otherwise retain AI33 and record the remaining gate.
- [ ] Record source/deployment/actual external failure verification separately. Do not manufacture a failing external request, replay the unknown call or claim the historical cause is known.

Checkpoint: 18:26 Asia/Shanghai. At that time record actual progress and any single remaining gate; do not reset the original case clock or repeat completed public route audits. The existing public routes and effective A scenarios remain unchanged.
