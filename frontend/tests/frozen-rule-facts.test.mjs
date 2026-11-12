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
const render = (component, props) => renderToStaticMarkup(h(MemoryRouter, null, h(component, props)));
const usedRef = '11111111-1111-4111-8111-111111111111';
const limitRef = '22222222-2222-4222-8222-222222222222';
const window = { from: '2026-11-10T01:00:00Z', to: '2026-11-10T02:00:00Z' };
const pairs = [10, 15, 20].map(second => ({ used_at: `2026-11-10T01:30:${second}Z`, limit_at: `2026-11-10T01:30:${second + 1}Z`, used: 33, limit: 40, remaining: 7, utilization_percent: 82.5 }));
const fact = { kind: 'capacity_pressure', instance_id: 'database', title: '连接容量余量不足', statement: '用量 33 / 40，占用 82.5%，剩余 7；超过配置的 80% 关注水位。', observed_at: pairs[2].used_at, evidence_ids: [usedRef, limitRef], calculation: { used: 33, limit: 40, utilization_percent: 82.5, remaining: 7, warning_percent: 80, unit: '连接', sample_count: 3, time_pairs: pairs } };
const claim = statement => ({ status: 'insufficient_evidence', statement, evidence_ids: [usedRef], counterevidence_ids: [], unknowns: ['具体连接来源未知'] });
const report = { executive_summary: { headline: '模型原始标题', phenomenon: claim('模型原始现象'), conclusion: claim('模型原始判断'), surface_causes: [], joint_root_cause: claim('原因待查'), recommended_actions: [] } };
const base = { window, report, report_status: 'completed', analysis_mode: 'live_redacted', nodes: [], edges: [], charts: [], kind: 'risk', title: '容量问题', finding_status: 'observed', presentation_kind: 'history', observation_state: 'recovered', summary: '后续实际观测已恢复', report_freshness: { event_updated_at: '2026-11-10T03:00:00Z' }, frozen_facts: [fact] };

test('frozen facts precede later recovery and the unchanged AI interpretation with exact window and operands', () => {
  const before = JSON.stringify(base);
  const html = render(ReportEvidence, { data: base });
  assert.match(html, /报告窗口内已确认的风险/);
  assert.ok(html.indexOf(fact.statement) < html.indexOf(base.summary));
  assert.ok(html.indexOf(base.summary) < html.indexOf('模型原始判断'));
  for (const value of [fact.title, window.from, window.to, fact.observed_at, `/evidence/${usedRef}`, `/evidence/${limitRef}`, 'AI分析', 'AI判断', '不替代已确认事实']) assert.ok(html.includes(value), value);
  assert.match(html, /模型原始标题/);
  assert.equal(JSON.stringify(base), before, 'rendering must not rewrite report or frozen evidence');
});

test('all supplied paired observations remain available behind a folded disclosure', () => {
  const html = render(ReportEvidence, { data: base });
  const disclosure = html.match(/<details[^>]*><summary>[^<]*配对观测[^<]*<\/summary>[\s\S]*?<\/details>/)?.[0];
  assert.ok(disclosure, 'paired evidence belongs in an explicit disclosure');
  assert.doesNotMatch(disclosure, /<details[^>]*\bopen/);
  for (const pair of pairs) for (const time of [pair.used_at, pair.limit_at]) assert.ok(disclosure.includes(time), time);
  for (const value of ['33', '40', '82.5', '7', '连接']) assert.ok(disclosure.includes(value), value);
});

test('a failed historical AI report still exposes only its own frozen rule facts', () => {
  const html = render(HistoricalInvestigationReport, { data: { status: 'failed', mode: 'live_redacted', ...window, from: window.from, to: window.to, report: null, frozen_facts: [fact] } });
  assert.match(html, /报告窗口内已确认的风险/);
  assert.match(html, /分析尚未成功完成/);
  assert.match(html, /本轮未生成结论摘要/);
  assert.ok(html.indexOf(fact.statement) < html.indexOf('分析尚未成功完成'));
  assert.doesNotMatch(html, /后续实际观测已恢复|2026-11-10T03:00:00Z/);
});

test('legacy and empty frozen facts do not infer risk, recovery or overall health from AI text', () => {
  for (const frozen_facts of [undefined, []]) {
    const html = render(ReportEvidence, { data: { ...base, finding_status: undefined, frozen_facts } });
    assert.doesNotMatch(html, /报告窗口内已确认的风险|整体健康|配置的 80%/);
    assert.match(html, /模型原始判断/);
  }
});
