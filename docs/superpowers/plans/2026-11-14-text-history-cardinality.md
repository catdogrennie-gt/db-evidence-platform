# Text history cardinality: bounded ROUND44 repair

The historical LOCK-03 recheck retained the correct adjacent snapshots but still inferred a short, self-resolving incident. That semantic failure remains open; no model retry is authorized for this cycle. A separate, existing natural normal report interpreted eight omitted text-history entries as eight lock rows. Correct this input ambiguity without changing source data, access, windows, quotas or service availability.

1. Add a regression for text/log/unknown Zabbix item projection: history-entry count is explicitly scoped to the retained, bounded projection; diagnostic `row_count` must be absent. Raw text remains unavailable. Empty history does not prove an empty diagnostic snapshot.
2. Preserve this cardinality contract through initial, final and tool packing. Keep MySQL diagnostic row counts unchanged. Run RED, implement the minimum allowlist change, then relevant regressions and backend checks.
3. Independently review the exact changes. Replay authorized, frozen normal inputs offline without model calls; preserve original reports and audit hashes. Do not claim improved model conclusions from offline packing checks.
4. Record actual input PASS versus semantic FAIL, manifest identity differences, and next output-contract decision. Check public A/B availability. Deploy only a sealed, verified candidate in a later bounded cycle; retain current services meanwhile.

Acceptance: no text contents forwarded; no omitted text-history count presented as diagnostic cardinality; contract survives supported packing paths. LOCK-03 temporal interpretation, public row-lock control safety, and actual-model verification remain separate unmet gates.
