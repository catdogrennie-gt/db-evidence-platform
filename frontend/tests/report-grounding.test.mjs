import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { createServer } from 'vite';
const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true, include: [] } });
after(() => server.close());
const { ReportEvidence } = await server.ssrLoadModule('/src/Intelligence.tsx');
const { HistoricalInvestigationReport } = await server.ssrLoadModule('/src/Historical.tsx');
const { ReportGrounding } = await server.ssrLoadModule('/src/ReportGrounding.tsx');
const render = (component, props) => renderToStaticMarkup(h(MemoryRouter, null, h(component, props)));
const id = '11111111-1111-4111-8111-111111111111';
const claim = { status: 'observed', statement: '模型认为已经自行解除', evidence_ids: [id], counterevidence_ids: [], unknowns: [] };
const report = { executive_summary: { headline: '模型原始标题', phenomenon: claim, conclusion: claim, surface_causes: [], joint_root_cause: claim, recommended_actions: [] } };
const grounding = { version: 'report-grounding-v1', status: 'point_facts_only', verified_headline: '历史采样观察到 MySQL 锁等待（仅时点事实）', reason_codes: [], model_narrative_verification: 'unverified', not_established: ['continuous_duration', 'incident_count', 'release_mechanism', 'current_state'], admitted_facts: [{ kind: 'lock_snapshot_point', fact_id: id, instance_id: 'asset-a', source: 'mysql.lock_waits', observed_at: '2026-11-13T06:53:47Z', row_count: 2, count_scope: 'at_least_projected_rows', source_identity: { source_id: 'source-a', hostid: '101', itemid: '501', mapping_version: 1, config_revision: 2 } }] };
for (const [name, component, props] of [
  ['issue', ReportEvidence, { data: { report, analysis_mode: 'live_redacted', report_status: 'completed', status: 'completed', charts: [], nodes: [], report_grounding: grounding } }],
  ['historical', HistoricalInvestigationReport, { data: { report, mode: 'live_redacted', status: 'completed', report_grounding: grounding } }],
]) {
  test(`${name}: admitted point observations precede unchanged model prose with source, scope and evidence`, () => {
    const before = JSON.stringify(props), html = render(component, props);
    assert.ok(html.includes('已核实的历史观测'));
    assert.ok(html.indexOf(grounding.verified_headline) < html.indexOf(report.executive_summary.headline));
    for (const text of ['至少 2 条锁等待记录', 'asset-a', '101', '501', `/evidence/${id}`, '不能据此确认持续时长、发生次数、解除原因或当前状态', 'AI 解释尚未核实', claim.statement]) assert.ok(html.includes(text), text);
    assert.equal(JSON.stringify(props), before);
  });
  test(`${name}: missing delivery proof does not turn model observed status into a verified headline`, () => {
    const html = render(component, { data: { ...props.data, report_grounding: { ...grounding, status: 'unverified', admitted_facts: [], verified_headline: null, reason_codes: ['actual_delivery_receipt_missing'] } } });
    assert.match(html, /未取得可核实的时点事实/);
    assert.match(html, /旧报告缺少本次分析的实际输入凭据/);
    assert.doesNotMatch(html, /已核实的历史观测|至少 2 条/);
    assert.ok(html.includes(report.executive_summary.headline));
  });
  test(`${name}: rule-only reports retain their own presentation without an AI verification failure`, () => {
    const html = render(component, { data: { ...props.data, mode: 'rules_only', analysis_mode: 'rules_only', report_grounding: { ...grounding, status: 'unverified', admitted_facts: [], verified_headline: null, reason_codes: ['no_completed_live_model_report'] } } });
    assert.doesNotMatch(html, /未取得可核实的时点事实|AI 解释尚未核实|已核实的历史观测/);
    assert.match(html, /规则报告未生成模型执行摘要/);
  });
}
test('a rule-only result does not acquire an irrelevant failed AI verification panel', () => {
  const html = render(ReportGrounding, { data: { ...grounding, status: 'unverified', admitted_facts: [], verified_headline: null, reason_codes: ['no_completed_live_model_report'] } });
  assert.equal(html, '');
});
test('unsupported versions cannot grant authority, and mixed identities have no invented whole-report headline', () => {
  const unsupported = render(ReportGrounding, { data: { ...grounding, version: 'future-schema' } });
  assert.doesNotMatch(unsupported, /已核实的历史观测|至少 2 条/);
  const mixed = render(ReportGrounding, { data: { ...grounding, verified_headline: null } });
  assert.match(mixed, /已核实的历史观测/);
  assert.doesNotMatch(mixed, /<h2>/);
  assert.match(mixed, /至少 2 条/);
});
