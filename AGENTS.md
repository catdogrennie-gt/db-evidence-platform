# Internal AI database operations platform

This repository is evolving from a fixed MySQL lab into an internal platform. The lab is a validation module. The authoritative platform design is `docs/platform/01-requirements.md`, `02-architecture.md`, `03-pages-and-workflows.md`, and `04-delivery-plan.md`. Historical `docs/01-requirements.txt` does not limit the platform to one user or fixed instances.

Read the parent workspace instructions. Follow the user's current instructions if they refine these documents; record material implementation decisions and synchronize the design instead of silently changing scope.

- Complete MySQL first; PostgreSQL is a later adapter. New assets must be onboarded without code edits or restarts.
- Deterministic collection and rules establish facts. AI reads authorized, redacted evidence and explains hypotheses; it has no arbitrary SQL, shell, database write, or email tool.
- Enforce actions, instance scopes, and field sensitivity in the backend, including counts, search, history, tasks, exports, reports, knowledge, and every AI tool call. An administrator role does not implicitly grant business evidence access.
- Preserve observed and ingested times, collector capabilities, source modes, gaps, counter epochs, and per-source freshness. Missing data is not zero or healthy. Historical investigations must not silently include new observations outside their frozen window.
- Keep identities stable. UUID changes quarantine an old asset; a genuinely new UUID creates a new asset and new history. P0 has one owning system and one effective endpoint per instance.
- Keep raw alert episodes, event transitions, human execution records, verification manifests, and reviewed knowledge distinct. P0 event merging is limited to one instance. All included episodes must have fresh recovery evidence before verified event recovery.
- Use the independent control database and Prometheus specified by the design. Do not label SQLite JSON history as the platform TSDB or claim optional Grafana/exporter integration without evidence.
- Private source materials, personal information, API keys, passwords, master keys, connection files, and private exports remain outside tracked files. Never send email or publish externally without the user's explicit instruction.
- Keep the old lab's operator and recovery credentials isolated from general onboarding. Only explicitly registered lab assets may use controlled experiments.
- Track implementation status against FR/AT identifiers. Preserve failures and distinguish implemented-and-verified, implemented-awaiting-verification, unimplemented, and externally-unverifiable.
- Run relevant fresh checks before completion claims and commits. Record reproducible commands and the next action in the parent workspace `CHECKPOINT.txt` during extended work.
- Use separate file ownership for concurrent contributors. The coordinator owns shared models, migrations, dependencies, and API contracts unless explicitly delegated.

Professional execution brief: build a sustained internal operations workflow, not isolated test buttons. Follow the reviewed design from asset onboarding through evidence, events, investigation, human remediation, verification, and knowledge. Make routine reversible engineering choices, record them, and continue. Diagnose tool failures and use the documented alternatives; do not stop at the first environment issue or after a short experiment.
