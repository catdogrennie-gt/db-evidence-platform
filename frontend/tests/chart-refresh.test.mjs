import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import * as echarts from 'echarts';

const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true, include: [] } });
after(() => server.close());
const { applyChartUpdate, ResourceRefreshStatus } = await server.ssrLoadModule('/src/ui.tsx');

test('chart data refresh preserves a user-selected zoom range and legend selection', () => {
  const chart = echarts.init(null, null, { renderer: 'svg', ssr: true, width: 600, height: 300 });
  try {
    const option = { animation: false, legend: {}, xAxis: { type: 'category', data: ['one', 'two', 'three', 'four'] }, yAxis: {}, dataZoom: [{ type: 'inside', start: 0, end: 100 }], series: [{ name: 'latency', type: 'line', data: [1, 2, 3, 4] }] };
    chart.setOption(option);
    chart.dispatchAction({ type: 'dataZoom', start: 25, end: 75 });
    chart.dispatchAction({ type: 'legendUnSelect', name: 'latency' });
    assert.equal(chart.getOption().legend[0].selected.latency, false);
    assert.equal(typeof applyChartUpdate, 'function');
    applyChartUpdate(chart, { ...option, series: [{ name: 'latency', type: 'line', data: [2, 3, 4, 5] }] });
    const current = chart.getOption();
    assert.equal(current.dataZoom[0].start, 25);
    assert.equal(current.dataZoom[0].end, 75);
    assert.equal(current.legend[0].selected.latency, false);
    assert.deepEqual(current.series[0].data, [2, 3, 4, 5]);
    assert.notEqual(chart.isDisposed(), true);
  } finally { chart.dispose(); }
});

test('the first chart update accepts an initialized instance without previous options', () => {
  const chart = echarts.init(null, null, { renderer: 'svg', ssr: true, width: 600, height: 300 });
  try {
    applyChartUpdate(chart, { animation: false, xAxis: {}, yAxis: {}, dataZoom: [{ type: 'inside' }], legend: {}, series: [{ type: 'line', name: 'observed', data: [1, 2] }] });
    assert.equal(chart.getOption().series[0].data.length, 2);
  } finally { chart.dispose(); }
});

test('graph updates preserve pan and zoom while removing obsolete data series', () => {
  const chart = echarts.init(null, null, { renderer: 'svg', ssr: true, width: 600, height: 300 });
  try {
    const graph = { id: 'evidence', type: 'graph', roam: true, layout: 'none', data: [{ id: 'a', name: 'a', x: 0, y: 0 }, { id: 'b', name: 'b', x: 10, y: 10 }], links: [{ source: 'a', target: 'b' }] };
    chart.setOption({ animation: false, series: [{ ...graph, zoom: 2, center: [4, 6] }, { ...graph, id: 'obsolete' }] });
    assert.equal(typeof applyChartUpdate, 'function');
    applyChartUpdate(chart, { animation: false, series: [{ ...graph, data: [...graph.data, { id: 'c', name: 'c', x: 30, y: 30 }] }] });
    const current = chart.getOption();
    assert.equal(current.series.length, 1);
    assert.equal(current.series[0].zoom, 2);
    assert.deepEqual(current.series[0].center, [4, 6]);
    assert.equal(current.series[0].data.length, 3);
  } finally { chart.dispose(); }
});

test('refresh labels disclose in-flight and failed revalidation with the last successful read time', () => {
  assert.equal(typeof ResourceRefreshStatus, 'function');
  const updatedAt = '2026-11-12T02:00:00Z';
  const pending = renderToStaticMarkup(createElement(ResourceRefreshStatus, { updatedAt, refreshing: true }));
  assert.match(pending, /刷新中/);
  assert.match(pending, /上次成功读取/);
  assert.match(pending, /2026/);
  const failed = renderToStaticMarkup(createElement(ResourceRefreshStatus, { updatedAt, refreshError: new Error('network down') }));
  assert.match(failed, /刷新未完成/);
  assert.match(failed, /上次成功/);
  assert.doesNotMatch(failed, /最新数据/);
});
