# DB Evidence Platform: automatic inspection and visual reports

Status: approved direction and interactive page skeleton, 2026-11-08. The user explicitly requested implementation, verification, documentation, GitHub publication, a historical release, and a truthful resume update. Deployment to an internet server is deferred by the user. This document records engineering decisions within that approval; it does not introduce another approval gate.

## Intent and product boundary

An internal database operations analysis product: existing monitoring does the collection; this platform automatically identifies current problems and conditional risks, correlates authorized evidence, and presents useful, visual explanations. Reuse the original platform's permissions, events, investigation worker, frozen evidence, human reviews, verification and knowledge. Do not reduce its analysis to fit a Grafana plugin.

The approved daily flow is **problem/risk results → report summary → evidence relationships and charts → evidence/current-metric drill-down → next checks and existing human workflow**. First-time setup is **administrator connects a read-only Zabbix source → select existing monitored objects → inspect capabilities → enable inspection policy**. No routine manual selection of historical windows is necessary to receive results.

## Approaches and decisions

1. Extend the existing independent platform (chosen): retain domain depth and improve source adapters, automation, and presentation. Existing React/Ant Design/ECharts and FastAPI/MySQL control database are sufficient.
2. Continue a Grafana app as the primary product: technically possible, but it would require rebuilding the original investigation domain. Keep Grafana as an optional future linked dashboard; do not make it a prerequisite.
3. Build another independent analysis backend: rejected because it duplicates authorization, events and evidence and previously weakened the result.

## Source and identity

- Add a persistent Zabbix source and source-host mapping to the existing control database. Use the existing encrypted secret store with an explicit Zabbix purpose; API tokens never return to the browser, audit or reports.
- A mapped object is an existing platform Instance with `connection_mode=zabbix` and an explicit external identity mapping. Its MySQL UUID is nullable and remains unknown unless actually observed. Direct MySQL onboarding retains its UUID validation. Host/network objects do not impersonate MySQL servers.
- Store source ID, upstream host ID, normalized item mappings, capability status, parent relationship provenance and ownership system. Stable identity is the source/host pair, not an inferred database UUID.
- A selected Zabbix host represents one logical monitored object in this version. Detect and reject ambiguous multi-database hosts instead of combining their evidence. Once a source has mappings, its upstream URL cannot be repointed; register a new source for another upstream. Token rotation does not change source identity.
- Zabbix remains the original time-series store. Query bounded history on the server; preserve upstream timestamps, units, item availability, gaps, pagination/truncation and freshness. Do not fabricate MetricFrame boot IDs, uptime or monotonic clocks.
- Recognize supported official keys and explicit SignalLens item annotations already used by the independent lab. Unknown items remain unmapped. A discovered template alone never proves locks, SQL, logs or execution plans are present.
- Normalize acquired diagnostic snapshots into existing CollectionRun/Evidence logical categories. Preserve transport/source mode separately. All ingestion uses existing task/lease/fence behavior or an equivalent bounded queue handler; it must not open a MySQL transport for a Zabbix object.

## Analysis and frozen evidence

- Existing Event/Investigation/Task/Manifest entities remain authoritative. Do not create another analyses database or authentication service.
- A source-neutral metric provider supports the existing permitted metric templates plus supported external metrics. Current queries and frozen investigation windows use the same provider. New frozen `time_series_window` items coexist with old `prometheus_window` items.
- Whitelisted tools read the frozen windows and snapshots. Actual numeric/log/SQL evidence is available within explicit budgets; ordinary strings must not all be erased, nor may arbitrary SQL, shell, network tools or unrestricted live retrieval be added.
- Initial evidence selection preserves diagnostic layers, anomaly-relevant facts and baseline context within the bounded bundle. More recent repeated points must not evict all SQL/host/counterevidence.
- Report facts cite real evidence or manifest IDs. Facts, registered relationships, observed relationships, hypotheses and conditional predictions are distinct. No confidence percentages or invented causal edges.
- A report's graph and curves always use its frozen data. A new cycle creates another immutable report attached to the same issue. Current metrics are a separate, clearly labeled drill-down.

## Automatic inspection

- Add persistent policy and dispatch records, with an actual active policy owner, authorized scope, enabled state, mode, external-data consent/version, signal and AI intervals, cooldown, daily run budget and due/status timestamps.
- Worker performs lightweight signal checks (default 300 seconds), periodic comprehensive AI inspection (default 1800 seconds, even without a threshold breach), and an urgent path for new/changed anomalies. Keep the AI queue independent from collection.
- Dispatch uses the existing create_investigation API/domain function; preserve the maximum of three in-flight investigations per user and five resources per frozen investigation. Partition larger scopes into bounded groups.
- Durable unique dispatch keys, row locking, cooldown and budget reservations prevent duplicate concurrent work and catch-up storms after sleep/restart. Recheck owner and current grants before dispatch and each model/tool call. Disabled policy, revoked permission, missing consent, offline source and exhausted budget are visible states.
- Repeated findings update the same issue and link report versions. Normal periodic results remain inspection history and do not manufacture an alert. Emerging risks must provide their observation window, deterministic calculation, threshold and assumptions; insufficient trend data means no forecast.
- Normalize finding identity from the mapped object, a bounded finding category, and a verified dimension such as SQL digest or filesystem; never deduplicate using a model-generated title. Existing events remain the issue identity; a small inspection-finding link may preserve fingerprint and latest report. AI-proposed findings require valid evidence references, remain hypotheses, and do not bypass this normalization.
- Notifications are in-app. Never send email. No automated database changes are introduced.

## UI and API contract

Keep old deep links and workflows. New primary navigation: **问题与风险**, **监控对象**, **指标看板**. Inspection settings are secondary. Scope controls remain and the backend owns permissions and counts.

All new paths below are relative to `/api/v1`. Responses are JSON; timestamps are UTC ISO-8601. Errors use the existing API envelope. Empty lists are not evidence of health. An optional unavailable field is null/absent with a readable reason, never fabricated.

### Shared display types

`Capability = {name:string,status:'available'|'missing'|'unsupported'|'stale'|'failed',reason?:string}`.

`Chart = {id:string,title:string,unit:string,source:string,points:Array<{time:string,value:number|null,validity?:string}>,gaps:Array<object>,baseline?:Array<{time:string,value:number|null}>,threshold?:number,window:{from:string,to:string},coverage?:object}`.

`Issue = {id:string,title:string,kind:'current'|'risk',status:string,severity:string,summary:string,object_names:string[],instance_ids:string[],first_seen_at:string,updated_at:string,investigation_id:string|null,analysis_mode:string,coverage_status:string}`. IDs remain stable platform IDs. Source/host IDs only appear in source management/details.

### New integration endpoints

- `GET /monitoring/sources` → `{items:[{id,name,url,system_id,status,last_success_at,error_code,has_token}],total}`; source management permission required. No token in response.
- `POST /monitoring/sources` accepts `{name,url,system_id,api_token,allowed_host_group_ids?:string[]}`; validates/test read-only connection. `PUT /monitoring/sources/{id}` supports saved configuration/credential rotation, without requiring a browser read of the previous token.
- `GET /monitoring/sources/{id}/hosts` → `{items:[{hostid,name,layer,capabilities:Capability[],selected:boolean,instance_id:string|null,item_count:number}],total,truncated:boolean}`. Discovery is bounded and belongs to the source's authorized system.
- `POST /monitoring/sources/{id}/selection` accepts `{hostids:string[]}` and idempotently enables selected mappings; deselection pauses future inspection, preserving history.
- `GET /monitoring/objects` → `{items:[{id,name,engine,system_id,system_name,source_id,source_name,enabled,capabilities:Capability[],last_observed_at,status}],total}`. Return only visible business resources; control-plane administrator alone does not acquire data access.
- `GET /monitoring/objects/{id}/metrics?from=&to=` → `{object:{id,name},charts:Chart[],capabilities:Capability[],window:{from,to}}` for bounded authorized existing history.
- `GET /intelligence/overview` → `{stats:{current:number,risk:number,objects:number},issues:Issue[],inspection:{status,last_completed_at,next_due_at,mode,reason},coverage:{status,missing_count}}`; support existing system/department/environment/instance scope query parameters.
- `GET /intelligence/issues/{id}` → `{...Issue,window:{from,to}|null,report:object|null,nodes:[{id,label,kind,summary,evidence_ids:string[]}],edges:[{from,to,relationship}],charts:Chart[],checks:Capability[],next_checks:string[],unknowns:string[],refutations:string[],history:[{id,created_at,status,mode}],event_id:string}`. Include only evidence authorized on every read. Report pending/failed is distinct from a completed diagnosis.
- Graph `kind` is `observed`, `registered`, `hypothesis` or `conditional_prediction`; each edge includes `kind`, `evidence_ids` and optional `provenance` in addition to its human-readable relationship. The frontend must not infer a causal relationship from edge order, a title or simultaneous curves.
- `GET /intelligence/policy` → `{policy:Policy|null,status:object}`. `PUT /intelligence/policy` saves the current authorized owner's policy and scope: `{enabled,instance_ids,signal_interval_seconds,ai_interval_seconds,urgent_enabled,cooldown_seconds,max_daily_runs,mode,external_confirmed}`. Existing privileged model policy settings still apply. Returned Policy includes id/version/last/next/status, but no secrets.
- `POST /intelligence/policy/run` requests an authorized bounded run and returns existing task references; this is a secondary maintenance/testing action, not the daily main flow.

Frontend may accept additive optional fields; shared contract changes require coordinator notification. Existing `/events`, `/investigations`, `/evidence`, `/assets` and their APIs stay usable.

## Acceptance and delivery

1. Connect to the actual local Zabbix; select and deselect objects, inspect truthful capabilities and query original history without target database credentials.
2. Produce real automatic dispatches through the original AI worker, verify recurring inspection without an anomaly, urgent anomaly behavior, deduplication, restart persistence, budget and permission revocation.
3. Use the independent lab for lock/SQL cases, gather real evidence, compare model statements with evidence. Preserve failures and distinguish live measurements, rules and live model output. Model completion is not proof of root cause.
4. Verify frozen graph/curve values remain unchanged after upstream history changes; gaps remain null and old reports remain readable.
5. Verify user-visible navigation, empty/error states, scope changes and report drill-down. Use actual screenshots and independently checked charts for documentation; never present the simulated skeleton as a running product.
6. Before publishing, scan tracked content and screenshots for credentials, personal documents and private environment details. Preserve the exact previous public commit as a named historical release/tag; do not rewrite old commit history. README begins with the product and visible key screenshots, then workflow, features, quick start, architecture, evidence and limits; secondary details may fold.
7. Update the user's resume privately using only demonstrated personal-project claims, real technology and measured evidence. Do not change unrelated employment dates or imply production adoption. Internet deployment is explicitly deferred.
