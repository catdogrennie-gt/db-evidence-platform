import test from 'node:test';
import assert from 'node:assert/strict';
import {
  chartData,
  reportState,
  visibleGraph,
  selectionForScope,
  scopeKey,
  sourcePayload,
  frozenMetricPath,
  findingLabel,
  initialSidebarCollapsed,
  reportMetricTargets,
  prioritizeIssueCharts,
  prioritizeIssueNodes,
  compactIssueNodes,
} from '../src/intelligenceModel.ts';

test('typed issue category selects lock evidence before unrelated charts without guessing from titles', () => {
  const charts = [
    { id: 'probe', metric_key: 'probe_availability', title: '锁等待（误导性标题）' },
    { id: 'qps', metric_key: 'questions_per_second', title: '流量' },
    { id: 'locks-other', metric_key: 'lock_waits', instance_id: 'other', title: '另一对象' },
    { id: 'locks', metric_key: 'lock_waits', instance_id: 'db', title: '订单库' },
    { id: 'threads', metric_key: 'threads_running', title: '活跃线程' },
  ];
  assert.deepEqual(
    prioritizeIssueCharts(charts, 'locks', 'db')
      .slice(0, 2)
      .map((c) => c.id),
    ['locks', 'locks-other'],
  );
  assert.deepEqual(
    prioritizeIssueCharts(charts, 'sql')
      .slice(0, 2)
      .map((c) => c.id),
    ['threads', 'qps'],
  );
  assert.deepEqual(
    prioritizeIssueCharts(charts, 'network').map((c) => c.id),
    ['probe', 'qps', 'locks-other', 'locks', 'threads'],
  );
  assert.deepEqual(prioritizeIssueCharts(charts, 'unknown'), charts);
  assert.deepEqual(
    charts.map((c) => c.id),
    ['probe', 'qps', 'locks-other', 'locks', 'threads'],
    'priority must not mutate frozen data',
  );
});

test('host reports put CPU and disk evidence first using canonical metric keys', () => {
  const charts = [
    { id: 'probe', metric_key: 'probe_latency_ms' },
    { id: 'qps', metric_key: 'questions_per_second' },
    { id: 'disk', metric_key: 'host_disk_read_bytes_per_second' },
    { id: 'cpu', metric_key: 'host_cpu_percent' },
  ];
  assert.deepEqual(
    prioritizeIssueCharts(charts, 'host')
      .slice(0, 2)
      .map((c) => c.id),
    ['cpu', 'disk'],
  );
});

test('the compact graph keeps the observed lock, its raw snapshot, ownership and linked explanation ahead of unrelated hypotheses', () => {
  const unrelated = Array.from({ length: 12 }, (_, i) => ({
    id: `hyp-${i}`,
    label: '候选解释',
    kind: 'hypothesis',
    evidence_ids: [],
  }));
  const nodes = [
    ...unrelated,
    {
      id: 'probe',
      label: '锁等待（误导性标题）',
      kind: 'observed',
      metric_key: 'probe_availability',
      evidence_ids: ['probe'],
    },
    { id: 'db', label: '订单库', kind: 'registered', evidence_ids: [] },
    {
      id: 'lock',
      label: '实际等待',
      kind: 'observed',
      metric_key: 'lock_waits',
      instance_id: 'db',
      evidence_ids: ['lock'],
    },
    {
      id: 'snapshot',
      label: '运行快照',
      kind: 'observed',
      evidence_category: 'locks',
      instance_id: 'db',
      evidence_ids: ['snapshot'],
    },
    { id: 'linked', label: '等待的候选解释', kind: 'hypothesis', evidence_ids: ['snapshot'] },
  ];
  const edges = [
    { from: 'db', to: 'lock', kind: 'registered' },
    { from: 'snapshot', to: 'linked', kind: 'hypothesis' },
  ];
  const prioritized = prioritizeIssueNodes(nodes, edges, 'locks', 'db');
  assert.deepEqual(
    prioritized.slice(0, 4).map((n) => n.id),
    ['lock', 'snapshot', 'db', 'linked'],
  );
  assert.equal(prioritized[0].kind, 'observed');
  assert.ok(prioritized.slice(0, 9).some((n) => n.id === 'snapshot'));
  assert.equal(prioritized.length, nodes.length, 'folding must retain every evidence node');
});

test('nine graph slots retain a representative evidence chain instead of seven snapshots from the same source', () => {
  const snapshots = Array.from({ length: 7 }, (_, i) => ({
    id: `raw-${i}`,
    kind: 'observed',
    source: 'mysql.lock_waits',
    instance_id: 'db',
    evidence_category: 'locks',
    evidence_ids: [`raw-${i}`],
  }));
  const nodes = [
    ...snapshots,
    { id: 'db', kind: 'registered', evidence_ids: [] },
    {
      id: 'lock',
      kind: 'observed',
      metric_key: 'lock_waits',
      instance_id: 'db',
      evidence_ids: ['lock'],
    },
    { id: 'fact', kind: 'observed', evidence_ids: snapshots.map((n) => n.id) },
    { id: 'candidate', kind: 'hypothesis', evidence_ids: ['raw-0'] },
    { id: 'forecast', kind: 'conditional_prediction', evidence_ids: ['cpu'] },
    {
      id: 'cpu',
      kind: 'observed',
      metric_key: 'host_cpu_percent',
      instance_id: 'host',
      evidence_ids: ['cpu'],
    },
    {
      id: 'probe',
      kind: 'observed',
      metric_key: 'probe_availability',
      instance_id: 'network',
      evidence_ids: ['probe'],
    },
    { id: 'host', kind: 'registered', evidence_ids: [] },
    { id: 'network', kind: 'registered', evidence_ids: [] },
  ];
  const edges = [
    ...snapshots.map((node) => ({ from: node.id, to: 'fact', kind: 'observed' })),
    { from: 'raw-0', to: 'candidate', kind: 'hypothesis' },
    { from: 'db', to: 'lock', kind: 'registered' },
    { from: 'host', to: 'cpu', kind: 'registered' },
    { from: 'network', to: 'probe', kind: 'registered' },
  ];
  const compact = compactIssueNodes(nodes, edges, 'locks', 'db');
  assert.ok(compact.length <= 9);
  assert.equal(compact.filter((node) => node.source === 'mysql.lock_waits').length, 1);
  const selected = new Set(compact.map((node) => node.id));
  for (const id of ['db', 'lock', 'raw-0', 'fact', 'candidate', 'forecast', 'cpu', 'probe'])
    assert.ok(selected.has(id), `compact graph must retain ${id}`);
  const expanded = visibleGraph(prioritizeIssueNodes(nodes, edges, 'locks', 'db'), edges);
  assert.deepEqual(
    new Set(expanded.nodes.map((node) => node.id)),
    new Set(nodes.map((node) => node.id)),
    'full graph must retain every original snapshot and semantic type',
  );
  assert.equal(expanded.edges.length, edges.length);
});

test('snapshot representatives use direct references and keep identical sources on different objects distinct', () => {
  const nodes = [
    {
      id: 'unreferenced',
      kind: 'observed',
      source: 'mysql.lock_waits',
      instance_id: 'db',
      evidence_category: 'locks',
      evidence_ids: ['unreferenced'],
    },
    {
      id: 'referenced',
      kind: 'observed',
      source: 'mysql.lock_waits',
      instance_id: 'db',
      evidence_category: 'locks',
      evidence_ids: ['referenced'],
    },
    {
      id: 'other-db',
      kind: 'observed',
      source: 'mysql.lock_waits',
      instance_id: 'another',
      evidence_category: 'locks',
      evidence_ids: ['other-db'],
    },
    { id: 'candidate', kind: 'hypothesis', evidence_ids: ['referenced'] },
  ];
  const edges = [{ from: 'referenced', to: 'candidate', kind: 'hypothesis' }];
  const compact = compactIssueNodes(nodes, edges, 'locks', 'db');
  assert.deepEqual(
    new Set(compact.map((node) => node.id)),
    new Set(['referenced', 'other-db', 'candidate']),
  );
  assert.equal(nodes.length, 4, 'folding must not delete raw evidence');
});

test('a missing, invalid or non-finite sample stays a gap, never zero or a connected value', () => {
  assert.deepEqual(
    chartData([
      { time: '10:00', value: 8, validity: 'valid' },
      { time: '10:01', value: null },
      { time: '10:02', value: 99, validity: 'stale' },
      { time: '10:03', value: Infinity },
      { time: '10:04', value: 0 },
    ]),
    [
      ['10:00', 8],
      ['10:01', null],
      ['10:02', null],
      ['10:03', null],
      ['10:04', 0],
    ],
  );
});

test('the first narrow load collapses navigation before a resize event occurs', () => {
  assert.equal(initialSidebarCollapsed(789), true);
  assert.equal(initialSidebarCollapsed(991), true);
  assert.equal(initialSidebarCollapsed(992), false);
  assert.equal(initialSidebarCollapsed(1440), false);
});

test('metric drill-down retains host and probe in the analysis scope instead of only the event subject', () => {
  assert.deepEqual(
    reportMetricTargets({
      instance_ids: ['db'],
      object_names: ['订单库'],
      analysis_objects: [
        { id: 'db', name: '订单库', engine: 'mysql' },
        { id: 'host', name: '订单主机', engine: 'host' },
        { id: 'probe', name: '连接探针', engine: 'network' },
      ],
    }),
    [
      { id: 'db', name: '订单库' },
      { id: 'host', name: '订单主机' },
      { id: 'probe', name: '连接探针' },
    ],
  );
  assert.deepEqual(reportMetricTargets({ instance_ids: ['db'], object_names: ['旧报告订单库'] }), [
    { id: 'db', name: '旧报告订单库' },
  ]);
});

test('an AI correlation hypothesis is never labelled as a measured trend forecast', () => {
  assert.equal(findingLabel({ kind: 'risk', finding_status: 'hypothesis' }), '候选解释 · 待核验');
  assert.equal(
    findingLabel({ kind: 'risk', finding_status: 'conditional_prediction' }),
    '条件预测',
  );
  assert.equal(findingLabel({ kind: 'current', finding_status: 'observed' }), '当前问题');
});

test('pending and failed analyses cannot render as a completed diagnosis', () => {
  assert.equal(
    reportState({ report: null, status: 'analyzing', investigation_id: 'a' }),
    'pending',
  );
  assert.equal(reportState({ report: null, status: 'failed', investigation_id: 'a' }), 'failed');
  assert.equal(
    reportState({ report: null, status: 'open', investigation_id: null }),
    'unavailable',
  );
  assert.equal(
    reportState({ report: { facts: [] }, status: 'open', investigation_id: 'a' }),
    'ready',
  );
  assert.equal(
    reportState({ report: { facts: ['older result'] }, status: 'failed', investigation_id: 'a' }),
    'failed',
  );
  assert.equal(
    reportState({
      report: { facts: ['older result'] },
      status: 'analyzing',
      investigation_id: 'a',
    }),
    'pending',
  );
});

test('a frozen reference targets the exact item and rejects malformed navigation paths', () => {
  const inv = '11111111-1111-4111-8111-111111111111',
    item = '22222222-2222-4222-8222-222222222222';
  assert.equal(
    frozenMetricPath(inv, `manifest:${item}`),
    `/investigations/${inv}/metric-windows/${item}`,
  );
  assert.equal(frozenMetricPath(inv, 'manifest:../../users'), null);
  assert.equal(frozenMetricPath(null, `manifest:${item}`), null);
});

test('rotating a source without a replacement token or group restriction preserves saved settings', () => {
  assert.deepEqual(
    sourcePayload(
      {
        name: '监控',
        url: 'https://monitor.internal',
        system_id: 'sys',
        api_token: '',
        groups: '',
      },
      true,
    ),
    {
      name: '监控',
      url: 'https://monitor.internal',
      system_id: 'sys',
    },
  );
  assert.deepEqual(
    sourcePayload(
      {
        name: '监控',
        url: 'https://monitor.internal',
        system_id: 'sys',
        api_token: 'replacement',
        groups: '12, 24',
      },
      true,
    ),
    {
      name: '监控',
      url: 'https://monitor.internal',
      system_id: 'sys',
      api_token: 'replacement',
      allowed_host_group_ids: ['12', '24'],
    },
  );
});

test('a graph retains typed relationships but rejects dangling or untyped causal assertions', () => {
  const nodes = [
    { id: 'a', label: '主机', kind: 'registered', summary: '', evidence_ids: [] },
    { id: 'b', label: '等待', kind: 'observed', summary: '', evidence_ids: ['ev'] },
    { id: 'c', label: '业务影响', kind: 'hypothesis', summary: '', evidence_ids: ['ev'] },
  ];
  const edges = [
    {
      from: 'a',
      to: 'b',
      relationship: '登记关联',
      kind: 'registered',
      evidence_ids: [],
      provenance: 'inventory',
    },
    { from: 'b', to: 'c', relationship: '待核对的影响', kind: 'hypothesis', evidence_ids: ['ev'] },
    { from: 'b', to: 'x', relationship: '导致', kind: 'observed', evidence_ids: ['ev'] },
    { from: 'a', to: 'c', relationship: '导致', evidence_ids: [] },
  ];
  const result = visibleGraph(nodes, edges);
  assert.deepEqual(result.edges, edges.slice(0, 2));
  assert.equal(result.nodes.find((n) => n.id === 'c').kind, 'hypothesis');
});

test('a changed business scope clears stale object selections but changing time does not', () => {
  const old = scopeKey({ system_id: 'a', department_id: 'd' });
  const same = scopeKey({ system_id: 'a', department_id: 'd', from: 'new' });
  const changed = scopeKey({ system_id: 'b', department_id: 'd' });
  assert.deepEqual(selectionForScope(old, same, ['object-a']), ['object-a']);
  assert.deepEqual(selectionForScope(old, changed, ['object-a']), []);
});
