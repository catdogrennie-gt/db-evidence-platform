# Implement report fact admission as one complete reporting change

Authority: [reviewed design](../specs/2026-11-14-report-fact-admission-design.md). This is not a natural-language semantic validator. Scope FR-022–024/AT-013; no new fault or live model call while preparing the contract. Preserve the approximately86-minute prior LOCK-03 clock.

## 1. Reproduce and pin the current gap

- Add `tests/platform/test_report_grounding_gap.py`: valid structure/citations still accept the preserved failure pattern; foreign citations remain rejected.
- Use real authorized detail/persistence fixtures for a legacy report lacking an actual delivery receipt. Assert raw report immutability and the desired unverified grounding sidecar. Mark only the unimplemented sidecar expectations strict XFAIL with the case identifier; `--runxfail` must fail at the missing contract, not fixture setup.
- Run these plus existing summary, snapshot-boundary and frozen-observation regressions. Record real RED versus expected failures; neither is an implemented fix.

## 2. Build the backend admission boundary

- First resolve the measured packing constraint: the preserved42-snapshot request is47549 bytes including reserve; the selected duplicate-layout simplified registry sample grows it to60643 against48000. The2-snapshot normal sample grows47118→47780 but excludes full metadata, so it is not a complete feasibility pass. These values apply only to that serialization layout, are not a theoretical minimum, and do not prove that other compact encodings exceed the limit. Probe compact references/shared schemas reusing existing projections; keep full provenance server-side and recheck exact required fields in the final request. Never append a registry after budget checks, increase limits or silently lose counterevidence.

- Factor a bounded source-qualified registry from actual delivered, authorized frozen projections, not all manifest entries or raw chronology alone. Bind payload/manifest identity, item/config identity, projection scope and delivery round.
- Recheck after packing; persist a server-generated receipt bound to the exact request producing that output before treating any fact as delivered. Unknown IO result stays unknown. Only necessary fact fields actually retained in that request qualify; earlier-call receipts cannot fill facts omitted from the final request. Tools and final-round compaction use the same receipt path. No budget increase.
- Add typed selection/facet validation; first adapter is point observations from qualified MySQL lock snapshots. No continuous-duration/count/release/current-state proof capability initially.
- Add server-only `report_grounding` to persisted result/read projection. Model payload cannot set its authority. Legacy missing receipts yield unverified; raw reports/audits remain unchanged. Reuse current per-request scope checks and enforce field sensitivity.
- Tests: source/config/object isolation, corrupted/expired/missing members, unavailable/partial snapshots, empty text history, numeric population, omitted facts, earlier-call-delivered/final-request-omitted denial, tools/multiple rounds, revoked access, receipt tampering, unsupported facets and context limits.

## 3. Complete the user-visible path before release

- One shared presentation policy for detail, overview/problem cards, report lists and exports. Verified primary facts/headlines are server templates over admitted values; free model prose stays unverified and cannot drive verified recovery/issue registration.
- Do not globally summarize mixed objects/sources from one lock snapshot. Keep provenance and time scope visible, preserve the raw AI section and explicit missing capabilities.
- Require both overclaiming and negated/free-form wording to remain unverified; no lexical detector. No isolated backend-only deployment declaring the defect closed.
- Run relevant backend/frontend regressions, build, independent review, exact public-parent capsule scans and sealed-candidate checks. Remove strict XFAIL only when its assertions pass; XPASS must block an unnoticed status change.

## 4. Controlled delivery and actual verification

- Preserve API/AI21 fallback, collector16/frontend unless the complete tested change requires replacement. Notify one minute before switch; idle handoff and duplicate-consumer fences remain mandatory.
- Verify public A/B before/after, new report and write denial, boot registration, and distinguish HTTP from visual QA.
- Only after actual deployment, use the original policy/legal budget and one explicitly labeled historical recheck if needed; no new fault solely to obtain a report, no retry luck. Compare delivered inputs, structured admission and free model narrative separately. Correct verified presentation does not retroactively make old model semantics correct.

## Round46 limit and next action

Deliver design plus a reproducible expected-failure contract this round, with no product runtime changes. Next implementation starts at registry/delivery/typed admission but may only release once the real reporting surfaces are coherent. If feasibility remains uncertain, retain WAIT with its exact reason rather than calling a new field semantic verification.
