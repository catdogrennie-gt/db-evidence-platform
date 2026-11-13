# Snapshot inference boundaries implementation plan

> Use superpowers:executing-plans in the existing isolated worktree. Do not delegate implementation. Request one focused independent review before publication.

**Goal:** Distinguish discrete snapshot observations from release mechanism, continuous duration and numeric populations in actual bounded model input.

**Architecture:** Extend platform-generated snapshot chronology with a small fixed interpretation contract. Reuse existing projection, authorization, source grouping and fail-closed budget paths; no output rewriting or new evidence.

**Tech stack:** Python/pytest and existing AI request assembly.

**Spec:** [Design](../specs/2026-11-13-snapshot-inference-boundaries-design.md).

## Task: Typed snapshot interpretation boundary

- [x] Add failing tests for snapshot-only count scope, active/empty normal cases, final/tool compaction and an actual authorized request in `tests/platform/test_snapshot_inference_boundaries.py`.
- [x] Add only fixed semantic metadata in `refresh_snapshot_chronology`; preserve factual values, identity and references. Bump input revision and its explicit revision test.
- [ ] Run relevant fresh tests, authorized request assembly with a stub provider, and offline metadata projection of a recorded real request without provider calls; then independent review and clean-public backend checks.
- [ ] Publish only the reviewed exact source/docs overlay; record actual deployment separately. Do not overwrite current workers.
- [ ] Record LOCK-03 prerequisite result, remaining control/resource GO checks, public availability and next action in status/manual/scenario list/CHECKPOINT. Keep model interpretation unverified until a separate real natural test.

## Constraints

No extra prompt budget, response filtering, hand-written diagnosis, manual AI dispatch, new fault or unapproved target. Empty samples never prove release method. Counts of retained snapshots are distinct from numeric series sample counts and complete window incidence. Optional chronology may be explicitly omitted for a small tool budget; underlying evidence takes priority.
