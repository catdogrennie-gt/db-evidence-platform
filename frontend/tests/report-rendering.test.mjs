import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { createServer } from 'vite';

const server = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true, include: [] },
});
after(() => server.close());
const { InvestigationReportView } = await server.ssrLoadModule('/src/Presentation.tsx');
const { ReportFreshnessNotice } = await server.ssrLoadModule('/src/Intelligence.tsx');
const evidenceId = '11111111-1111-4111-8111-111111111111';
const itemId = '22222222-2222-4222-8222-222222222222';
const instanceId = '33333333-3333-4333-8333-333333333333';
const emptyReport = {
  facts: [],
  hypotheses: [],
  unknowns: [],
  refutations: [],
  next_readonly_checks: [],
};

function render(report, onMetricReference) {
  return renderToStaticMarkup(
    createElement(
      MemoryRouter,
      null,
      createElement(InvestigationReportView, { data: report, onMetricReference }),
    ),
  );
}

function findingSection(markup) {
  const sections = markup.match(/<section\b[^>]*>[\s\S]*?<\/section>/g) ?? [];
  const section = sections.find((value) => value.includes('待核验发现'));
  assert.ok(
    section,
    'findings-only report must have a visible, dedicated pending-findings section',
  );
  return section;
}

test('a completed historical report visibly warns that its window does not cover the newer anomaly', () => {
  const markup = renderToStaticMarkup(
    createElement(ReportFreshnessNotice, {
      freshness: {
        status: 'outdated',
        reason: 'latest_observation_outside_report_window',
        event_updated_at: '2026-11-08T09:54:36Z',
        report_window_to: '2026-11-08T09:54:27Z',
      },
    }),
  );
  assert.match(markup, /旧报告尚未覆盖最新观测/);
  assert.match(markup, /不代表最新观测已完成分析/);
  assert.match(markup, /2026-11-08T09:54:36Z/);
  assert.match(markup, /2026-11-08T09:54:27Z/);
  assert.doesNotMatch(markup, /报告覆盖当前问题观察/);
});

test('a pending or missing freshness contract never claims that the latest anomaly is analyzed', () => {
  const pending = renderToStaticMarkup(
    createElement(ReportFreshnessNotice, {
      freshness: {
        status: 'pending',
        reason: null,
        event_updated_at: '2026-11-08T09:54:36Z',
        report_window_to: null,
      },
    }),
  );
  assert.match(pending, /最新观测正在分析/);
  const legacy = renderToStaticMarkup(createElement(ReportFreshnessNotice));
  assert.match(legacy, /报告时效尚未确认/);
  assert.doesNotMatch(pending + legacy, /报告覆盖当前问题观察/);
});

test('a findings-only report visibly presents the unverified claim, its category and evidence link', () => {
  const markup = render({
    ...emptyReport,
    findings: [
      {
        intent: 'review_issue',
        category: 'locks',
        instance_id: instanceId,
        dimension: { kind: 'instance', value: instanceId },
        statement: '锁等待与请求延迟可能有关，需要核验',
        evidence_ids: [evidenceId],
      },
    ],
  });
  const section = findingSection(markup);
  assert.match(section, /锁等待与请求延迟可能有关，需要核验/);
  assert.match(section, /锁与事务/);
  assert.match(section, /尚未证实/);
  assert.match(section, /href="\/evidence\/11111111-1111-4111-8111-111111111111"/);
});

test('a structured SQL finding reuses the clickable frozen-reference control when supported', () => {
  const section = findingSection(
    render(
      {
        ...emptyReport,
        findings: [
          {
            intent: 'review_issue',
            category: 'sql',
            instance_id: instanceId,
            dimension: { kind: 'sql_digest', value: 'abcdef0123456789abcdef0123456789' },
            statement: 'SQL 耗时变化需要进一步核验',
            evidence_ids: [`manifest:${itemId}`],
          },
        ],
      },
      () => {},
    ),
  );
  assert.match(section, />SQL<\/span>/);
  assert.match(section, /<button[^>]*>[\s\S]*?查看冻结时序证据/);
});
