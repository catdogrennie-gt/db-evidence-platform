import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { createServer } from 'vite';
const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true, include: [] } });
after(() => server.close());
const { ExecutiveSummary } = await server.ssrLoadModule('/src/ExecutiveSummary.tsx');
const claim = { status: 'observed', statement: '原始模型判断', evidence_ids: ['a'], counterevidence_ids: [], unknowns: [] };
const report = { executive_summary: { headline: '原始模型标题保持不变', phenomenon: claim, surface_causes: [], joint_root_cause: claim, conclusion: claim, recommended_actions: [] } };
const ids = { a: '11111111-1111-4111-8111-111111111111', b: '22222222-2222-4222-8222-222222222222', c: '33333333-3333-4333-8333-333333333333' };
const ref = (id, time) => ({ evidence_id: ids[id], observed_at: time });
const bounds = { scope: 'frozen_manifest_point_observations', complete: false,
  window: { from: '2026-11-13T06:00:00Z', to: '2026-11-13T07:00:00Z' },
  groups: [{ instance_id: 'asset-a', source: 'mysql.lock_waits', sample_count: 3, nonempty_sample_count: 2,
    first_observation: ref('a', '2026-11-13T06:53:27Z'), last_observation: ref('c', '2026-11-13T06:58:27Z'),
    last_nonempty: ref('b', '2026-11-13T06:53:47Z'), first_empty_after_last_nonempty: ref('c', '2026-11-13T06:58:27Z'), gap_to_next_empty_seconds: 280 }] };
const render = value => renderToStaticMarkup(h(MemoryRouter, null, h(ExecutiveSummary, { report, observationBounds: value })));

test('frozen observation bounds precede the unchanged AI headline and qualify model status', () => {
  const html = render(bounds);
  assert.ok(html.indexOf('证据时间边界') >= 0 && html.indexOf('证据时间边界') < html.indexOf(report.executive_summary.headline));
  for (const text of ['AI 标注：已观察', '280', '不代表持续时长', '不代表当前状态', '部分冻结观测无法核实', `/evidence/${ids.b}`, `/evidence/${ids.c}`]) assert.ok(html.includes(text), text);
  assert.ok(html.includes(report.executive_summary.headline));
});

test('no later empty sample does not acquire a recovery transition', () => {
  const html = render({ ...bounds, groups: [{ ...bounds.groups[0], first_empty_after_last_nonempty: null, gap_to_next_empty_seconds: null }] });
  assert.match(html, /未保留同来源的后续空样本/);
  assert.doesNotMatch(html, /样本相隔/);
});

test('missing projection makes no observation claim and complete numeric-only reports have no snapshot panel', () => {
  assert.doesNotMatch(render(undefined), /证据时间边界/);
  assert.doesNotMatch(render({ ...bounds, groups: [], complete: true }), /证据时间边界/);
  assert.match(render({ ...bounds, groups: [] }), /冻结观测无法核实/);
});

test('same-source timelines keep their object and monitoring-source identities visible', () => {
  const a = { ...bounds.groups[0], instance_name: '订单业务', identity: { source_id: 'source-one', hostid: '101', itemid: '501' } };
  const b = { ...bounds.groups[0], instance_id: 'asset-b', instance_name: '支付业务', identity: { source_id: 'source-two', hostid: '102', itemid: '502' } };
  const html = render({ ...bounds, groups: [a, b] });
  for (const text of ['订单业务', '支付业务', 'source-one', 'source-two', '101', '102', '501', '502']) assert.ok(html.includes(text), text);
});
