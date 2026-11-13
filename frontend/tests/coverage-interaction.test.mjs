import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { createServer } from 'vite';

const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true, include: [] } });
after(() => server.close());
const { CoverageNotice } = await server.ssrLoadModule('/src/CoverageNotice.tsx');
const render = coverage => renderToStaticMarkup(h(MemoryRouter, null, h(CoverageNotice, { coverage })));

test('no concrete details means no generic gap count or misleading navigation', () => {
  assert.equal(render({ status:'unknown', missing_count:7 }), '');
  assert.equal(render({ status:'unknown', missing_count:0, items:[] }), '');
});

test('gap details open in place and explain the exact object, missing data and remedy', () => {
  const html=render({ status:'partial',missing_count:1,items:[{instance_id:'orders',object_name:'订单库',mapping_id:'group',host_name:'Linux 主机',itemid:'12',item_name:'CPU 使用率',reason:'sample_stale',last_sample_at:'2026-11-13T01:00:00Z'}] });
  for(const text of ['查看缺失明细','订单库','Linux 主机','CPU 使用率','样本已过期','建议','当前采集状态'])assert.ok(html.includes(text),text);
  assert.match(html,/<details/);
  assert.ok(html.includes('instance_id=orders'));
  assert.doesNotMatch(html,/href="\/monitoring[?\"]/);
});
