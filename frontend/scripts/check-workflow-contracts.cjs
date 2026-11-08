// Isolated response-contract fixtures, never application data or API fallbacks.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const req = Module.createRequire(path.resolve(__dirname, '../package.json'));
const ts = req('typescript');
for (const extension of ['.ts', '.tsx'])
  Module._extensions[extension] = (module, filename) =>
    module._compile(
      ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
        compilerOptions: {
          module: ts.ModuleKind.CommonJS,
          target: ts.ScriptTarget.ES2022,
          jsx: ts.JsxEmit.ReactJSX,
          esModuleInterop: true,
        },
      }).outputText,
      filename,
    );
const React = req('react');
const { renderToStaticMarkup } = req('react-dom/server');
const { MemoryRouter } = req('react-router-dom');
const { ScopeContext } = req('./src/hooks.tsx');
const presentation = req('./src/Presentation.tsx');
const { workflowFilters, manualEventPayload, evidenceSources } = req('./src/workflow-contract.ts');
const { scopedDestination } = req('./src/navigation.ts');
const { ContextLink } = req('./src/ui.tsx');
const scope = { from: '2026-01-01T00:00:00Z', to: '2026-01-01T01:00:00Z', timezone: 'UTC' };
const render = (Component, props) =>
  renderToStaticMarkup(
    React.createElement(
      MemoryRouter,
      null,
      React.createElement(
        ScopeContext.Provider,
        {
          value: {
            scope,
            params: 'from=2026-01-01T00%3A00%3A00Z&to=2026-01-01T01%3A00%3A00Z',
            refresh: 0,
          },
        },
        React.createElement(Component, props),
      ),
    ),
  );
const id = '11111111-1111-4111-8111-111111111111';
const instance = '22222222-2222-4222-8222-222222222222';
const manifest = '33333333-3333-4333-8333-333333333333';
assert.equal(
  manualEventPayload({ title: '合成契约案例', description: '必须保留的观察' }).reason,
  '必须保留的观察',
);
assert(!('description' in manualEventPayload({ description: '观察' })));
assert.deepEqual(workflowFilters['/alerts'].statuses, ['firing', 'recovered', 'unknown']);
assert(workflowFilters['/investigations'].statuses.includes('completed'));
assert(!workflowFilters['/tasks'].search);
assert(!workflowFilters['/evidence']);
const event = render(presentation.EventListTable, {
  items: [
    {
      id,
      instance_id: instance,
      title: '合成事件',
      status: 'open',
      severity: 'medium',
      assigned_to: 'scoped-owner',
      created_at: scope.from,
    },
  ],
  instances: [{ id: instance, name: '契约实例' }],
  kind: 'events',
});
assert(event.includes('契约实例') && event.includes('scoped-owner'));
assert(!event.includes('最近复验') && !event.includes('规则版本'));
assert(event.includes('/assets/' + instance + '/overview?from='));
const alert = render(presentation.EventListTable, {
  items: [
    {
      id,
      instance_id: instance,
      status: 'firing',
      first_seen_at: scope.from,
      evidence: { rule_version: 'fixture-sql-v1' },
    },
  ],
  instances: [],
  kind: 'alerts',
});
assert(alert.includes('fixture-sql-v1') && alert.includes(instance.slice(0, 8)));
const report = {
  id,
  instance_id: instance,
  event_id: id,
  status: 'completed',
  template: 'locks_absent',
  template_version: 'v1',
  before_start: scope.from,
  before_end: scope.to,
  after_start: '2026-01-01T02:00:00Z',
  after_end: '2026-01-01T03:00:00Z',
  outcome: 'inconclusive',
  criteria: {
    minimum_observations: 3,
    minimum_executions: 10,
    coverage_revision: 4,
    load_context: { kind: 'human_assertion', description: '合成负载说明' },
  },
  result: {
    outcome: 'inconclusive',
    before_observations: 3,
    after_observations: 2,
    reasons: ['insufficient_observations'],
    conclusion_scope: '仅验证等待链',
    business_recovery_proven: false,
    causation_proven: false,
  },
  manifest_id: manifest,
};
const verification = render(presentation.VerificationReportView, { data: report });
assert(
  verification.includes('资料不足') &&
    verification.includes('前窗观测数') &&
    verification.includes('后窗观测数'),
);
assert(verification.includes('样本不足') && verification.includes('每个窗口至少 3 次观测'));
assert(verification.includes('打开复验冻结清单') && verification.includes('合成负载说明'));
assert(!verification.includes('[object Object]'));
const sql = render(presentation.VerificationReportView, {
  data: {
    ...report,
    template: 'sql_latency',
    outcome: 'improved',
    result: {
      outcome: 'improved',
      before_observations: 4,
      after_observations: 5,
      before_avg_seconds: 0.02,
      after_avg_seconds: 0.01,
      execution_rate_ratio: 1.1,
      latency_ratio: 0.5,
      reasons: [],
    },
  },
});
assert(sql.includes('20 ms') && sql.includes('10 ms') && sql.includes('调用率比'));
const configured = {
  departments: [
    {
      id,
      name: '配置部门',
      systems: [
        { id: manifest, name: '配置系统', instances: [{ id: instance, name: '契约实例' }] },
      ],
    },
  ],
  nodes: [
    { id, name: '部门' },
    { id: manifest, name: '系统' },
    { id: instance, name: '契约实例' },
  ],
  edges: [{ source: id, target: manifest, type: 'configured_ownership', source_mode: 'manual' }],
};
const topology = render(presentation.TopologyView, { data: configured });
assert(topology.includes('配置归属') && topology.includes('当前窗口没有已观察的复制关系'));
assert(!topology.includes('观测关系与来源证据'));
const observed = render(presentation.TopologyView, {
  data: {
    ...configured,
    edges: [
      ...configured.edges,
      {
        source: instance,
        target: id,
        type: 'observed_replication',
        source_mode: 'live',
        evidence_id: manifest,
        observed_at: scope.from,
      },
    ],
  },
});
assert(
  observed.includes('/evidence/' + manifest + '?from=') &&
    !observed.includes('当前窗口没有已观察的复制关系'),
);
assert(
  evidenceSources.some((source) => source.value === 'mysql.statement_digest') &&
    evidenceSources.some((source) => source.value === 'linux.host'),
);
assert(
  !evidenceSources.some(
    (source) => source.value === 'runtime' || source.value === 'statement_digest',
  ),
);
const eventSummary = render(presentation.EventSummaryView, {
  data: {
    id,
    source: 'rule',
    status: 'closed',
    closure_reason: '合成结案说明',
    manifest_id: manifest,
    episodes: [
      {
        id,
        status: 'recovered',
        severity: 'high',
        first_seen_at: scope.from,
        last_seen_at: scope.to,
        recovered_at: scope.to,
        evidence: {
          rule_version: 'fixture-lock-v1',
          trigger: { source_observed_at: scope.from, observed_count: 1, evidence_ids: [manifest] },
          latest: {
            source_observed_at: scope.to,
            observed_count: 0,
            recovery_observation: true,
            evidence_ids: [id],
          },
        },
      },
    ],
  },
});
assert(
  eventSummary.includes('首次触发观察（固定）') &&
    eventSummary.includes('最近规则观察（持续更新）') &&
    eventSummary.includes('合成结案说明'),
);
assert(
  eventSummary.includes('打开事件当前冻结清单') && eventSummary.includes('/evidence/' + manifest),
);
assert(!eventSummary.includes('当前记录尚无证据引用'));
const plan = render(presentation.RemediationPlanView, {
  data: {
    status: 'approved',
    version: 2,
    steps: ['由人工完成合成操作'],
    prerequisites: ['合成前提'],
    execution_records: [
      {
        id,
        executed_at: scope.to,
        executed_by: 'fixture-user',
        source: 'human_assertion',
        actual_result: { result: '合成人工结果' },
      },
    ],
  },
});
assert(
  plan.includes('由人工完成合成操作') && plan.includes('合成人工结果') && plan.includes('计划 v'),
);
const sourceHealth = render(presentation.SourceHealthSummary, {
  data: {
    source_health: [
      {
        collector: 'runtime',
        up: true,
        availability: 'available',
        interval_seconds: 30,
        last_success_at: scope.to,
        sample_age_seconds: 2,
      },
      {
        collector: 'replication',
        up: false,
        availability: 'not_applicable',
        reason_code: 'not_applicable',
        interval_seconds: 300,
      },
    ],
  },
});
assert(
  sourceHealth.includes('运行与锁事务') &&
    sourceHealth.includes('不适用') &&
    !sourceHealth.includes('来源新鲜度未知'),
);
const query = render(presentation.QueryWindowSummary, {
  data: {
    requested_from: scope.from,
    requested_to: scope.to,
    comparison_from: '2026-01-01T00:00:30Z',
    comparison_to: '2026-01-01T00:59:30Z',
    observations: 3,
    comparison_evidence_refs: [{ id: manifest, frame_observed_at: scope.from }],
  },
});
assert(
  query.includes('实际对照端点开始') &&
    query.includes('所采端点') &&
    query.includes('/evidence/' + manifest),
);
const navigation = scopedDestination(
  '/tasks/' + id + '?instance_id=target#result',
  'instance_id=old&from=new-start&to=new-end',
);
assert(
  navigation.includes('instance_id=target') &&
    !navigation.includes('instance_id=old') &&
    navigation.includes('from=new-start') &&
    navigation.endsWith('#result'),
);
const link = render(ContextLink, { to: '/events/' + id + '?from=explicit', children: '保留范围' });
assert(
  link.includes('from=explicit') &&
    link.includes('to=2026-01-01T01%3A00%3A00Z') &&
    !link.includes('from=2026'),
);
console.log('workflow contracts: 16 isolated checks passed');
