import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { createServer } from 'vite';
import { readFileSync } from 'node:fs';
const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true, include: [] } });
after(() => server.close());
const ui = await server.ssrLoadModule('/src/Intelligence.tsx');
const app = await server.ssrLoadModule('/src/App.tsx');
const nav = await server.ssrLoadModule('/src/navigation.ts');
const id = '11111111-1111-4111-8111-111111111111';
const claim = (statement, status = 'observed') => ({ statement, status, evidence_ids: [id], counterevidence_ids: [`manifest:${id}`], unknowns: ['尚缺并发对照'] });
const summary = { headline: '等待与延迟同时出现', phenomenon: claim('请求变慢'), surface_causes: [claim('锁等待升高')], joint_root_cause: claim('不能确认联合原因', 'insufficient_evidence'), conclusion: claim('当前只能确认观测重叠'), recommended_actions: [{ kind: 'manual_change', description: '评估调整', evidence_ids: [id], risk: 'high', requires_human_approval: true, prerequisites: ['确认负载'], verification: ['复查等待'] }] };
const render = (Component, props) => renderToStaticMarkup(h(MemoryRouter, null, h(Component, props)));
const base = { report: { executive_summary: summary }, nodes: [], edges: [], charts: [], checks: [], next_checks: [], unknowns: [], refutations: [], instance_ids: [], object_names: [], history: [], status: 'open', investigation_id: id };
test('real summary exposes nested references, limitations and manual action controls', () => {
  assert.equal(typeof ui.ExecutiveSummary, 'function');
  const html = render(ui.ExecutiveSummary, { report: base.report });
  for (const value of ['发生了什么', '各层原因', '联合根因候选', '问题结论', '建议操作', '请求变慢', '证据不足', '尚缺并发对照', '高风险', '需要人工批准', '确认负载', '复查等待', `/evidence/${id}`, '反证引用']) assert.ok(html.includes(value), value);
});
test('normal inspection accepts no applicable root cause and no action', () => {
  assert.equal(typeof ui.ExecutiveSummary, 'function');
  const html = render(ui.ExecutiveSummary, { report: { executive_summary: { ...summary, surface_causes: [], joint_root_cause: claim('本窗口未见异常，不适用根因分析', 'not_applicable'), recommended_actions: [] } } });
  assert.match(html, /不适用/); assert.match(html, /未提出建议操作/); assert.doesNotMatch(html, /已确认根因/);
});
test('legacy prose stays original and empty visualization panels disappear', () => {
  assert.equal(typeof ui.ReportEvidence, 'function');
  const html = render(ui.ReportEvidence, { data: { ...base, report: { facts: [{ statement: '原始事实正文', evidence_ids: [id] }] } } });
  assert.match(html, /旧版未生成完整结论摘要/); assert.match(html, /原始事实正文/); assert.doesNotMatch(html, /证据关系|重点冻结曲线|报告曲线/);
});
test('summary precedes optional folded graph and curves', () => {
  assert.equal(typeof ui.ReportEvidence, 'function');
  const html = render(ui.ReportEvidence, { data: { ...base, nodes: [{ id, label: '来源', kind: 'observed', evidence_ids: [id] }], charts: [{ id, title: 'CPU', points: [], gaps: [], coverage: {}, window: {from:'a',to:'b'} }] } });
  assert.ok(html.indexOf('请求变慢') < html.indexOf('证据关系'));
  assert.match(html, /<details[^>]*><summary>证据关系/); assert.doesNotMatch(html, /<details[^>]*open/);
});
test('failed and pending reports label retained output as historical', () => {
  assert.equal(typeof ui.ReportEvidence, 'function');
  for (const status of ['failed', 'analyzing']) assert.match(render(ui.ReportEvidence, { data: { ...base, status } }), /保留的历史输出/);
});
test('only four navigation entries and group selections remain', () => {
  assert.deepEqual(app.menuDefinitions?.map(x => x.label), ['问题分析', 'Zabbix 接入', '数据查看', '配置']);
  assert.doesNotMatch(readFileSync('src/Monitoring.tsx', 'utf8'), /HostSelection|singleObjects|按单主机接入|\/monitoring\/objects/);
  assert.doesNotMatch(readFileSync('src/Admin.tsx', 'utf8'), /function Collection\(|<Collection|采集模板/);
});
test('history preserves exact IDs and denies unsupported types or traversal', () => {
  assert.equal(typeof nav.historyPath, 'function');
  assert.equal(nav.historyPath('investigations', id), `/investigations/${id}`);
  assert.equal(nav.historyPath('evidence', '../../users'), null);
  assert.equal(nav.historyPath('users', id), null);
});

test('empty or wholly invalid frozen charts do not reserve report panels', () => {
  const html = render(ui.ReportEvidence, { data: { ...base, charts: [{ id, title: '无数据', points: [{ time: 'a', value: null }], gaps: [], window: { from: 'a', to: 'b' } }] } });
  assert.doesNotMatch(html, /组冻结指标/);
});


test('recent analyses expose exact report links, normal summary and honest legacy/failure states, capped at three', () => {
  assert.equal(typeof ui.RecentAnalyses, 'function');
  const html = render(ui.RecentAnalyses, { reports: [
    { id, mode: 'live_redacted', status: 'completed', report: { executive_summary: summary } },
    { id: '22222222-2222-4222-8222-222222222222', mode: 'live_redacted', status: 'completed', report: { facts: [{ statement: '不能拼成摘要' }] } },
    { id: '33333333-3333-4333-8333-333333333333', mode: 'live_redacted', status: 'failed', report: { executive_summary: { ...summary, headline: '不可显示的失败结论' } } },
    { id: '44444444-4444-4444-8444-444444444444', status: 'completed', report: { executive_summary: summary } },
  ] });
  assert.match(html, /最近分析/); assert.match(html, /等待与延迟同时出现/); assert.match(html, /当前只能确认观测重叠/);
  assert.match(html, /旧版未生成完整结论摘要/); assert.match(html, /分析未成功完成/);
  assert.match(html, new RegExp(`/investigations/${id}`));
  assert.doesNotMatch(html, /不可显示的失败结论|不能拼成摘要|44444444-4444-4444-8444-444444444444/);
});

const history = await server.ssrLoadModule('/src/Historical.tsx');
test('historical report leads with compact status/window and summary before collapsed full metadata', () => {
  assert.equal(typeof history.HistoricalInvestigationReport, 'function');
  const question = '完整问题内容'.repeat(300);
  const html = render(history.HistoricalInvestigationReport, { data: { id, question, status: 'completed', mode: 'live_redacted', from: '2026-11-09T00:00:00Z', to: '2026-11-09T01:00:00Z', model: '审计模型名称', report: base.report, model_calls: [{ status: 'succeeded' }] } });
  const heading = html.indexOf('等待与延迟同时出现');
  assert.ok(heading > html.indexOf('2026-11-09T00:00:00Z'));
  assert.ok(heading < html.indexOf(question));
  const metadata = html.match(/<details\b[^>]*><summary>完整问题、元数据与审计<\/summary>[\s\S]*?<\/details>/)?.[0];
  assert.ok(metadata, 'full question and audit must be available behind a collapsed disclosure');
  assert.match(metadata, new RegExp(question));
  assert.match(metadata, /审计模型名称/);
  assert.doesNotMatch(metadata, /<details[^>]*\bopen/);
  for (const status of ['failed', 'running']) {
    const result = render(history.HistoricalInvestigationReport, { data: { id, status, mode: 'live_redacted', report: base.report } });
    assert.ok(result.indexOf('分析尚未成功完成') < result.indexOf('等待与延迟同时出现'));
  }
});
test('inspection history footer links to the actual problem-analysis entry', () => {
  const source = readFileSync('src/Monitoring.tsx', 'utf8');
  assert.doesNotMatch(source, /查看全部调查记录|to="\/investigations"/);
  assert.match(source, /to="\/">返回问题分析/);
});

test('first-view summary exposes the three questions and every action before folded evidence detail', () => {
  const actions = Array.from({ length: 3 }, (_, i) => ({ ...summary.recommended_actions[0], description: `完整操作${i + 1}：` + '在已授权窗口内进行人工核对，保留原始陈述。'.repeat(3) }));
  const html = render(ui.ExecutiveSummary, { report: { executive_summary: { ...summary, recommended_actions: actions } } });
  const split = html.indexOf('<details class="executive-details"');
  assert.ok(split > 0, 'full evidence belongs in a default-closed summary disclosure');
  const firstView = html.slice(0, split), detail = html.slice(split);
  for (const value of ['发生了什么', '为什么', '现在怎么做', summary.conclusion.statement, summary.phenomenon.statement, ...summary.surface_causes.map(c => c.statement), ...actions.map(a => a.description), '证据不足', '高风险', '需要人工批准']) assert.ok(firstView.includes(value), value);
  for (const value of ['反证引用', '尚缺并发对照', '确认负载', '复查等待', summary.joint_root_cause.statement]) {
    assert.ok(!firstView.includes(value), 'technical detail must not push actions below the fold: ' + value);
    assert.ok(detail.includes(value), 'full detail must remain available: ' + value);
  }
  assert.match(detail, /<summary>分层原因、操作前提与证据引用<\/summary>/);
  assert.doesNotMatch(detail, /<details[^>]*\bopen/);
  assert.ok(firstView.indexOf(summary.conclusion.statement) < firstView.indexOf('发生了什么'));
  assert.match(firstView, /class="executive-questions"/);
});
