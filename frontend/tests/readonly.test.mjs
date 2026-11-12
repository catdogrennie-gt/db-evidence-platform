import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true, include: [] } });
after(() => server.close());
const { ReadonlySettingsView } = await server.ssrLoadModule('/src/Admin.tsx');
const { ReadonlySourceList } = await server.ssrLoadModule('/src/Monitoring.tsx');
const { menuDefinitions } = await server.ssrLoadModule('/src/App.tsx');
const snapshot = {
  sources: [{ id: 's', name: 'Demo Zabbix', status: 'ready', host_count: 4, item_count: 16, group_count: 2 }],
  systems: [{ id: 'sys', name: 'Orders', environment: 'lab', model_policy: 'live_redacted' }],
  objects: [{ id: 'o', name: 'Orders analysis', enabled: true }],
  policies: [{ id: 'p', instance_ids: ['o'], enabled: true, status: 'scheduled', mode: 'live_redacted', signal_interval_seconds: 30, ai_interval_seconds: 300, max_daily_runs: 24 }],
  rules: [{ id: 'r', name: 'Latency rule', template: 'sql_latency', enabled: true, parameters: { threshold: 2 } }],
  model: { provider: 'DeepSeek', model: 'example', configured: true },
  tasks: [{ id: 't', type: 'investigation', status: 'running', queue: 'ai' }],
  platform: { control_database: 'available', workers: [{ queue: 'ai', status: 'available' }] },
  access: { read_only: true, object_count: 1, system_count: 1, permissions: ['data.read','configuration.read'], roles: ['demo_viewer'] },
};
test('read-only guests can find configuration without a management permission', () => {
  assert.ok(menuDefinitions.find(m => m.key === '/admin').permissions.includes('configuration.read'));
});
test('safe configuration view shows actual policies and runtime information without mutation controls', () => {
  assert.equal(typeof ReadonlySettingsView, 'function');
  const html = renderToStaticMarkup(createElement(ReadonlySettingsView, { data: snapshot }));
  for (const value of ['只读', 'Demo Zabbix', 'Orders', 'DeepSeek', '巡检', '30', '300', 'Latency rule', '执行中']) assert.ok(html.includes(value), value);
  assert.doesNotMatch(html, /<input|<form|<button|立即运行|轮换凭据/);
});
test('source safety summary is visible without an editor or credential fields', () => {
  assert.equal(typeof ReadonlySourceList, 'function');
  const html = renderToStaticMarkup(createElement(ReadonlySourceList, { sources: snapshot.sources }));
  assert.match(html, /Demo Zabbix/);
  assert.match(html, /16/);
  assert.doesNotMatch(html, /<input|<form|<button|token|api_key|配置 \/ 轮换/);
});
