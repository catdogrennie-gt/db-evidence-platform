# Whole-window analysis implementation plan

> **For agentic workers:** Use executing-plans inline. Request independent review before sealing and deployment.

**Goal:** Correct a contradictory latest-first analysis instruction without changing evidence, quotas or report admission.

**Architecture:** Keep the authorized frozen evidence pipeline and existing executive-summary schema. Replace the latest-centric instruction with a whole-window comparison rule near the start of the system prompt, and narrow when a normal inspection headline is appropriate. This is a prompt-policy hypothesis, not a semantic verifier.

**Tech Stack:** Existing Python/FastAPI investigation worker and pytest; no new dependency.

**Spec:** [Continuous evolution](../specs/2026-11-13-continuous-evolution-design.md), FR-023/024/025.

## Evidence and decision

POOL-01 keeps its original 2026-11-13 15:14 start. Actual AI32 request d0fc517316b71fe6f9964b4ac8bbc356336c497be13ac9004ed3e8e464a16804 delivered 23 numeric series, including early rolling request p95 about 5194ms, pool wait 600ms and error ratio 0.5669, followed by low end values. The report describes only the normal end. Its first retained rolling sample was observed after the fault was switched off; this does not establish fault duration or a pure healthy window.

The prompt already says to distinguish history/current, but also explicitly centers the latest observations. The latter is a contradictory priority, not a missing collector. Independent design review recommends replacing it rather than adding more numeric formats. A new citation-coverage validator is rejected for this round: the same series ID can support both endpoints, so citation presence cannot establish historical understanding. No machine-verification claim is added.

## Constraints and review focus

- No fault, manual model dispatch, quota change, larger context/output budget, new identity or policy.
- Preserve all facts, references, resource/source scopes, clocks, gaps and model-tool restrictions.
- Whole-window scope does not turn numerical variation into a fault, historical symptoms into current failure, or improvement into verified recovery.
- Counter epoch, unknown units, rolling statistics, missing timestamps and incomplete coverage remain limiting conditions.
- System prompt must not grow in UTF-8 size relative to sealed32, including source-qualified variants; preserve context budget for evidence.
- Only `platform_app/investigations.py`, a focused prompt-contract regression and this plan are product/source changes. Status/manual updates follow actual results.

## Tasks

- [ ] Reproduce the contradictory instruction with a failing prompt-policy regression across source-qualified first/final request variants. Static contract checks do not test model reasoning.
- [ ] Replace the obsolete priority, bring whole-window history/end/coverage comparison near the beginning, and tighten the normal-headline condition; bump prompt version. Keep schema and acceptance rules unchanged.
- [ ] Run focused prompt, summary, context, scope, budget and report-grounding regressions. Compare actual retained requests offline with only the system prompt replaced: exact user/tool evidence unchanged and input size no larger. Include a fully normal and an incomplete/unknown-epoch control without claiming model diagnosis.
- [ ] Independent exact-source review; seal immutable33 from32 with only the reviewed investigation module. Controlled AI-only idle handoff with one-minute notice and exact32 fallback if readiness permits. No live source overwrite.
- [ ] Audit the next original natural POOL request (currently due 09:38 UTC) for actual prompt version/input/report. Its window starts about08:38, so the old fault-tail samples should already have aged out: if it contains only normal evidence it is a normal counterexample, **not a retest of historical omission**. The latter remains WAIT until a separate same-condition bounded trial meets GO and is naturally analyzed. Do not force a request to finish the round. Public A/B checks and manual/scene status remain honest.

## Progress and stop conditions

Round104 starts 09:00 UTC; checkpoint 09:27 UTC. Its single deliverable is a reviewed, bounded whole-window prompt candidate, deployed only if the handoff is ready. If the new prompt would remove evidence or need broader schema/semantic changes, narrow or defer rather than add speculative work. Stop repeated prompt experimentation after the next natural comparison: use its specific failure or mark this path insufficient. Existing103 failure is never rewritten.
