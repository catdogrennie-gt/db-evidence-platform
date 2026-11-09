import { ExecutiveSummary } from './ExecutiveSummary';
export { ExecutiveSummary } from './ExecutiveSummary';
import { useEffect, useId, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Drawer,
  Empty,
  List,
  Space,
  Statistic,
  Tag,
  Timeline,
  Typography,
} from 'antd';
import { useParams } from 'react-router-dom';
import type { Row } from './api';
import type { ExecutiveSummaryData } from './ExecutiveSummary';
import type { EChartsOption } from 'echarts';
import { query, text } from './api';
import { useCan, useResource, useScope, useScopedPath } from './hooks';
import { Chart, ContextLink, LoadState, SourceCoverage, Status, Time, Title } from './ui';
import { InvestigationReportView } from './Presentation';
import {
  chartData,
  findingLabel,
  frozenMetricPath,
  graphKinds,
  reportState,
  reportMetricTargets,
  prioritizeIssueCharts,
  prioritizeIssueNodes,
  compactIssueNodes,
  metricSamplingSummary,
  visibleGraph,
} from './intelligenceModel';
import type {
  Capability,
  EvidenceEdge,
  EvidenceNode,
  IntelligenceOverview,
  Issue,
  IssueDetail,
  MetricChartData,
  IssueCategory,
  ReportFreshness,
} from './intelligenceModel';

export function AccessNotice({
  detail = '当前身份尚未获得此范围的数据访问权限。',
}: {
  detail?: string;
}) {
  return <Alert type="warning" showIcon title="需要授权" description={detail} />;
}

const inspectionLabels: Record<string, string> = {
  completed: '最近巡检已完成',
  succeeded: '最近巡检已完成',
  idle: '等待下一轮',
  active: '巡检已启用',
  ready: '巡检就绪',
  scheduled: '已安排后台巡检',
  analysis_failed: '最近分析失败',
  object_paused: '巡检对象已暂停',
  queue_full: '调查队列繁忙',
  queued: '巡检排队中',
  running: '正在巡检',
  analyzing: '正在分析',
  preparing: '正在准备证据',
  disabled: '巡检已停用',
  paused: '巡检已暂停',
  not_configured: '尚未配置巡检',
  no_policy: '尚未配置巡检',
  pending: '等待首轮巡检',
  waiting: '等待巡检',
  never_run: '等待首轮巡检',
  failed: '最近巡检失败',
  source_offline: '监控来源离线',
  offline: '监控来源离线',
  budget_exhausted: '当日预算已用尽',
  permission_denied: '巡检权限已失效',
  consent_required: '等待模型外发授权',
  model_failed: '模型分析失败',
};
export function InspectionStatus({ status, reason }: { status?: string; reason?: string | null }) {
  const failed = [
    'failed',
    'model_failed',
    'analysis_failed',
    'source_offline',
    'offline',
    'permission_denied',
  ].includes(status ?? '');
  return (
    <div className={`intel-inspection ${failed ? 'is-error' : ''}`}>
      <span className="intel-pulse" aria-hidden />
      <span>
        {inspectionLabels[status ?? ''] ?? (status ? `巡检状态：${status}` : '巡检状态未知')}
        {reason && <small>{reason}</small>}
      </span>
    </div>
  );
}

export function IntelligenceHome() {
  const can = useCan();
  const { refresh } = useScope();
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setTick((v) => v + 1), 30000);
    return () => clearInterval(timer);
  }, []);
  const path = useScopedPath('/intelligence/overview');
  const resource = useResource<IntelligenceOverview>(
    can('data.read') && can('event.read') ? path : null,
    refresh + tick,
  );
  const recentPath = useScopedPath('/investigations', { limit: 3 });
  const recent = useResource<{ items: Row[] }>(can('data.read') ? recentPath : null, refresh + tick);
  const data = resource.data;
  return (
    <div className="intel-page">
      <Title
        title="问题分析"
        description="自动发现，持续核对。从结果进入调查。"
        actions={
          <ContextLink to="/admin/inspection">
            <Button>巡检设置</Button>
          </ContextLink>
        }
      />
      {!can('data.read') || !can('event.read') ? (
        <AccessNotice detail="查看问题分析需要数据读取和事件读取权限。可在“联合分析对象”查看已授权资源。" />
      ) : (
        <LoadState {...resource} retry={resource.reload}>
          {data && (
            <>
              <div className="intel-summary-strip">
                <InspectionStatus status={data.inspection.status} reason={data.inspection.reason} />
                <div>
                  最近完成 <Time value={data.inspection.last_completed_at} />{' '}
                  <span className="intel-dot">·</span> 下次计划{' '}
                  <Time value={data.inspection.next_due_at} />
                </div>
                <Status value={data.inspection.mode ?? 'unknown'} />
              </div>
              <div className="intel-stat-grid">
                {[
                  ['current', '当前需要关注', '已合并同一问题，持续跟踪'],
                  ['risk', '潜在风险', '候选解释与有条件的趋势风险'],
                  ['objects', '巡检范围内对象', '当前授权业务范围'],
                ].map(([key, label, note]) => (
                  <Card key={key} className={`intel-stat intel-stat-${key}`}>
                    <Statistic
                      title={label}
                      value={data.stats[key as keyof typeof data.stats] ?? '未知'}
                    />
                    <span>{note}</span>
                  </Card>
                ))}
              </div>
              {data.coverage.status !== 'complete' && data.coverage.status !== 'available' && (
                <Alert
                  className="intel-gap-banner"
                  type="warning"
                  showIcon
                  title="监控资料存在覆盖缺口"
                  description={`${data.coverage.missing_count ?? '未知'} 项来源或能力缺口；没有发现问题不代表这些对象已确认健康。`}
                  action={<ContextLink to="/monitoring">查看覆盖</ContextLink>}
                />
              )}
              {data.stats.objects === 0 ? (
                <Card className="intel-first-run">
                  <Empty description="当前范围尚未纳入联合分析对象" />
                  <p>选择已有 Zabbix 对象后，后台按巡检策略收集授权证据并形成结果。</p>
                  <ContextLink to="/monitoring">
                    <Button type="primary">选择联合分析对象</Button>
                  </ContextLink>
                </Card>
              ) : (
                <>
                  <IssueSection
                    title="当前问题"
                    description="优先处理正在发生的异常"
                    issues={data.issues.filter((i) => i.kind === 'current')}
                    empty={
                      data.inspection.last_completed_at
                        ? '最近已完成巡检未返回当前问题。仍请关注来源覆盖和未核对项。'
                        : '等待首轮有效巡检。尚无结果不代表健康。'
                    }
                  />
                  <IssueSection
                    title="潜在风险"
                    description="尚未确认为故障，候选解释与条件预测分别标明"
                    issues={data.issues.filter((i) => i.kind === 'risk')}
                    empty="当前没有足够证据支持的潜在风险或待核验假设。数据不足时不生成精确预测。"
                  />
                </>
              )}
              <LoadState {...recent} retry={recent.reload}>{recent.data && <RecentAnalyses reports={recent.data.items} />}</LoadState>
              <div className="intel-bottom-note">
                <span>结果来自后台巡检；本页每 30 秒刷新状态。</span>

              </div>
            </>
          )}
        </LoadState>
      )}
    </div>
  );
}

function IssueSection({
  title,
  description,
  issues,
  empty,
}: {
  title: string;
  description: string;
  issues: Issue[];
  empty: string;
}) {
  return (
    <section className="intel-section">
      <div className="intel-section-head">
        <h2>{title}</h2>
        <span>{description}</span>
      </div>
      {issues.length ? (
        <div className="intel-issues">
          {issues.map((issue, index) => (
            <Card
              key={issue.id}
              className={`intel-issue ${index === 0 && issue.kind === 'current' ? 'is-featured' : ''}`}
            >
              <div className="intel-issue-top">
                <Space wrap>
                  <Tag color={issue.kind === 'risk' ? 'gold' : 'blue'}>{findingLabel(issue)}</Tag>
                  <Status value={issue.severity} />
                  <Status value={issue.status} />
                  <ReportFreshnessTag freshness={issue.report_freshness} />
                </Space>
                <span className="intel-muted">
                  更新 <Time value={issue.updated_at} />
                </span>
              </div>
              <ContextLink to={`/issues/${issue.id}`}>
                <h3>{issue.title}</h3>
              </ContextLink>
              <p>{issue.summary || '分析摘要尚未生成；可进入查看当前证据与处理进展。'}</p>
              <div className="intel-issue-foot">
                <span>
                  {issue.object_names.join(' / ') || '对象名称不可用'}{' '}
                  <span className="intel-dot">·</span> <Status value={issue.analysis_mode} />
                  <ReportCoverageTag status={issue.coverage_status} />
                </span>
                <ContextLink to={`/issues/${issue.id}`}>查看问题报告 →</ContextLink>
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <div className="intel-empty-inline">{empty}</div>
      )}
    </section>
  );
}

export function CapabilityList({
  capabilities,
  compact = false,
}: {
  capabilities: Capability[];
  compact?: boolean;
}) {
  if (!capabilities.length)
    return <Typography.Text type="secondary">能力尚未识别，不能据此判断覆盖。</Typography.Text>;
  return (
    <div className={compact ? 'intel-capabilities compact' : 'intel-capabilities'}>
      {capabilities.map((cap, i) => (
        <div className="intel-capability" key={`${cap.name}-${i}`}>
          <span>
            <strong>{capabilityNames[cap.name] ?? cap.name}</strong>
            {cap.reason && !compact && <small>{cap.reason}</small>}
          </span>
          <Tag
            color={
              cap.status === 'available' ? 'success' : cap.status === 'failed' ? 'error' : 'warning'
            }
          >
            {{
              available: '可用',
              missing: '未采集',
              unsupported: '不支持',
              stale: '已过期',
              failed: '读取失败',
            }[cap.status] ?? cap.status}
          </Tag>
          {compact && cap.reason && <span className="intel-cap-reason">{cap.reason}</span>}
        </div>
      ))}
    </div>
  );
}

const sourceLabels: Record<string, string> = {
  zabbix: 'Zabbix 上游历史',
  zabbix_history: 'Zabbix 上游历史',
  prometheus_resampled: 'Prometheus 重采样',
  replay: '回放数据',
  live: '实时采集',
  time_series_window: '冻结时序窗口',
  prometheus_window: '冻结 Prometheus 窗口',
};
const capabilityNames: Record<string, string> = {
  metrics: '数值指标',
  runtime: '运行与锁等待',
  statement_digest: 'SQL 摘要',
  host: '主机指标',
  error_log: '错误日志',
  slow_log: '慢日志',
  replication: '复制资料',
  schema_config: '结构与配置',
};
const metricNames: Record<string, string> = {
  connections: '数据库连接数',
  threads_running: '运行线程',
  questions_per_second: '语句吞吐',
  lock_waits: '锁等待数量',
  buffer_pool_hit_ratio: 'Buffer Pool 命中率',
  host_cpu_percent: '主机 CPU 使用率',
  host_disk_read_bytes_per_second: '磁盘读取速率',
  host_disk_write_bytes_per_second: '磁盘写入速率',
  probe_latency_ms: '连接探针延迟',
  probe_availability: '连接探针可用性',
};
export function MetricPanel({ data, frozen = false }: { data: MetricChartData; frozen?: boolean }) {
  const points = chartData(data.points ?? []);
  const sampling = metricSamplingSummary(data.coverage);
  const option = useMemo<EChartsOption>(
    () => ({
      color: ['#316dd3', '#a47d30'],
      animation: false,
      tooltip: { trigger: 'axis', confine: true },
      legend: {
        top: 0,
        data: [
          '观测值',
          ...(data.baseline?.length ? ['对照基线'] : []),
          ...(data.threshold !== undefined ? ['风险水位'] : []),
        ],
      },
      grid: { left: 55, right: 24, top: 42, bottom: 46 },
      xAxis: {
        type: 'time',
        min: data.window?.from,
        max: data.window?.to,
        axisLabel: { hideOverlap: true },
      },
      yAxis: { type: 'value', name: data.unit, splitLine: { lineStyle: { color: '#edf1f6' } } },
      dataZoom: [{ type: 'inside' }],
      series: [
        {
          name: '观测值',
          type: 'line',
          showSymbol: false,
          connectNulls: false,
          lineStyle: { width: 2.5 },
          areaStyle: { opacity: 0.04 },
          data: points,
        },
        ...(data.baseline?.length
          ? [
              {
                name: '对照基线',
                type: 'line' as const,
                showSymbol: false,
                connectNulls: false,
                lineStyle: { type: 'dashed' as const, width: 1.5 },
                data: chartData(data.baseline),
              },
            ]
          : []),
        ...(data.threshold !== undefined
          ? [
              {
                name: '风险水位',
                type: 'line' as const,
                showSymbol: false,
                lineStyle: { type: 'dashed' as const, color: '#ae7924' },
                data: [
                  [data.window.from, data.threshold],
                  [data.window.to, data.threshold],
                ],
              },
            ]
          : []),
      ],
    }),
    [data, points],
  );
  return (
    <Card
      title={metricNames[data.title] ?? data.title}
      className="intel-metric"
      extra={<Tag>{frozen ? '报告冻结数据' : '当前历史查询'}</Tag>}
    >
      <div className="intel-chart-meta">
        <span>
          {data.object_name && <strong>{data.object_name} · </strong>}
          {sourceLabels[data.source] ?? data.source}
        </span>
        <span>
          <Time value={data.window?.from} /> — <Time value={data.window?.to} />
        </span>
      </div>
      {points.some(([, value]) => value !== null) ? (
        <Chart
          option={option}
          height={260}
          label={`${data.title}，单位 ${data.unit}，${frozen ? '报告冻结证据' : '当前历史查询'}，缺失采样保留断点`}
        />
      ) : (
        <Empty description="此窗口没有有效采样；不补零、不推断健康" />
      )}
      <SourceCoverage data={{ source: data.source, coverage: data.coverage, gaps: data.gaps }} />
      {sampling && <Alert type="info" showIcon title="曲线已抽样" description={sampling} className="intel-gap-banner" />}
      <details className="intel-details">
        <summary>查看样本明细与缺口</summary>
        <div className="intel-sample-table">
          <table>
            <thead>
              <tr>
                <th>观察时间</th>
                <th>数值（{data.unit}）</th>
                <th>有效性</th>
              </tr>
            </thead>
            <tbody>
              {data.points.slice(0, 200).map((point, i) => (
                <tr key={`${point.time}-${i}`}>
                  <td>
                    <Time value={point.time} />
                  </td>
                  <td>{chartData([point])[0][1] ?? '缺失'}</td>
                  <td>{point.validity ?? '上游观测'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {data.points.length > 200 && <p>此处显示前 200 个样本，曲线包含服务端返回的全部样本。</p>}
        <p>
          缺口：
          {data.gaps.length
            ? JSON.stringify(data.gaps)
            : '服务端未标记额外缺口；仍以样本有效性为准。'}
        </p>
      </details>
    </Card>
  );
}

function EvidenceReferences({
  ids,
  investigationId,
  onMetricReference,
}: {
  ids: string[];
  investigationId: string | null;
  onMetricReference: (reference: string) => void;
}) {
  return (
    <Space wrap>
      {ids.map((id, index) =>
        id.startsWith('manifest:') ? (
          investigationId && (
            <Button key={id} size="small" type="link" onClick={() => onMetricReference(id)}>
              冻结证据 {index + 1} ↗
            </Button>
          )
        ) : (
          <ContextLink key={id} to={`/evidence/${encodeURIComponent(id)}`}>
            证据 {index + 1} ↗
          </ContextLink>
        ),
      )}
    </Space>
  );
}

function EvidenceGraph({
  nodes,
  edges,
  investigationId,
  onMetricReference,
  category,
  subjectId,
}: {
  nodes: EvidenceNode[];
  edges: EvidenceEdge[];
  investigationId: string | null;
  onMetricReference: (reference: string) => void;
  category?: IssueCategory;
  subjectId?: string;
}) {
  const fullGraph = visibleGraph(nodes, edges);
  const [expanded, setExpanded] = useState(false);
  const prioritized = prioritizeIssueNodes(fullGraph.nodes, fullGraph.edges, category, subjectId);
  const compact = compactIssueNodes(fullGraph.nodes, fullGraph.edges, category, subjectId);
  const graph = visibleGraph(expanded ? prioritized : compact, fullGraph.edges);
  const [selected, setSelected] = useState<string>();
  const markerId = useId().replaceAll(':', '');
  const current = graph.nodes.find((n) => n.id === selected) ?? graph.nodes[0];
  const positions = new Map(
    graph.nodes.map((node, i) => [
      node.id,
      { x: 100 + (i % 3) * 230, y: 48 + Math.floor(i / 3) * 100 },
    ]),
  );
  if (!graph.nodes.length)
    return <Empty description="尚无可展示的有来源关系。不会根据标题或同期曲线补造因果关系。" />;
  return (
    <>
      <div className="intel-graph-legend">
        {Object.entries(graphKinds).map(([key, kind]) => (
          <span key={key}>
            <i style={{ borderColor: kind.color, borderStyle: kind.dashed ? 'dashed' : 'solid' }} />
            {kind.label}
          </span>
        ))}
      </div>
      {fullGraph.nodes.length > compact.length && (
        <Button type="link" size="small" onClick={() => setExpanded((value) => !value)}>
          {expanded ? '收起关系全图' : `展开全部 ${fullGraph.nodes.length} 个证据节点`}（当前展示{' '}
          {graph.nodes.length} 个）
        </Button>
      )}
      <div className="intel-graph-scroll">
        <svg
          className="intel-graph"
          viewBox={`0 0 660 ${Math.ceil(graph.nodes.length / 3) * 100}`}
          role="group"
          aria-label="证据关系图。连线含义见下方文字，不表示自动确认因果。"
        >
          <defs>
            <marker id={markerId} markerWidth="6" markerHeight="6" refX="6" refY="3" orient="auto">
              <path d="M0,0 L6,3 L0,6" fill="#91a2b6" />
            </marker>
          </defs>
          {graph.edges.map((edge, i) => {
            const a = positions.get(edge.from)!;
            const b = positions.get(edge.to)!;
            return (
              <line
                key={i}
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                stroke={graphKinds[edge.kind].color}
                strokeWidth="1.5"
                strokeDasharray={graphKinds[edge.kind].dashed ? '5 4' : undefined}
                markerEnd={`url(#${markerId})`}
              />
            );
          })}
          {graph.nodes.map((node) => {
            const p = positions.get(node.id)!;
            const kind = graphKinds[node.kind];
            return (
              <g
                key={node.id}
                role="button"
                tabIndex={0}
                aria-label={`${kind.label}：${node.label}`}
                aria-pressed={node.id === current?.id}
                onClick={() => setSelected(node.id)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    setSelected(node.id);
                  }
                }}
              >
                <rect
                  x={p.x - 89}
                  y={p.y - 27}
                  width="178"
                  height="56"
                  rx="8"
                  fill={node.id === current?.id ? '#ecf3ff' : '#fff'}
                  stroke={kind.color}
                  strokeWidth={node.id === current?.id ? 2 : 1}
                  strokeDasharray={kind.dashed ? '5 3' : undefined}
                />
                <text x={p.x} y={p.y - 7} textAnchor="middle" fill={kind.color} fontSize="10">
                  {kind.label}
                </text>
                <text x={p.x} y={p.y + 13} textAnchor="middle" fill="#243247" fontSize="12">
                  {node.label.length > 16 ? node.label.slice(0, 15) + '…' : node.label}
                </text>
                <title>{node.label}</title>
              </g>
            );
          })}
        </svg>
      </div>
      {current && (
        <div className="intel-selected-evidence">
          <Tag color={graphKinds[current.kind].color}>{graphKinds[current.kind].label}</Tag>
          <strong>{current.label}</strong>
          <p>{current.summary || '此节点尚无单独说明。'}</p>
          {current.object_name && <p>所属对象：{current.object_name}</p>}
          <EvidenceReferences
            ids={current.evidence_ids}
            investigationId={investigationId}
            onMetricReference={onMetricReference}
          />
          {!current.evidence_ids.length && <small>未提供直接证据引用；请结合关系来源核对。</small>}
        </div>
      )}
      <details className="intel-details">
        <summary>查看全部关系与来源（{graph.edges.length}）</summary>
        <ul>
          {graph.edges.map((edge: EvidenceEdge, i) => (
            <li key={i}>
              <Tag>{graphKinds[edge.kind].label}</Tag>
              {graph.nodes.find((n) => n.id === edge.from)?.label} →{' '}
              {graph.nodes.find((n) => n.id === edge.to)?.label}：{edge.relationship}
              {edge.provenance && <small> 来源：{edge.provenance}</small>}{' '}
              <EvidenceReferences
                ids={edge.evidence_ids}
                investigationId={investigationId}
                onMetricReference={onMetricReference}
              />
            </li>
          ))}
        </ul>
      </details>
      <p className="intel-muted">
        连线只表达标注的关系；候选解释和条件预测仍需核验，同期变化不会自动认定为因果。
      </p>
    </>
  );
}

function ReportCoverageTag({ status }: { status: string }) {
  return (
    <Tag color={status === 'partial' ? 'warning' : undefined}>
      {status === 'complete' ? '覆盖完整' : status === 'partial' ? '部分覆盖' : '覆盖未知'}
    </Tag>
  );
}

function ReportFreshnessTag({ freshness }: { freshness?: ReportFreshness }) {
  if (!freshness) return <Tag>报告时效待核对</Tag>;
  const labels = {
    current: '报告覆盖当前观察',
    outdated: '旧报告未覆盖新观测',
    pending: '最新观测分析中',
    unavailable: '最新观测待分析',
  };
  return (
    <Tag
      color={
        freshness.status === 'outdated'
          ? 'warning'
          : freshness.status === 'pending'
            ? 'processing'
            : undefined
      }
    >
      {labels[freshness.status]}
    </Tag>
  );
}

export function ReportFreshnessNotice({ freshness }: { freshness?: ReportFreshness } = {}) {
  const status = freshness?.status;
  const title =
    status === 'outdated'
      ? '旧报告尚未覆盖最新观测'
      : status === 'pending'
        ? '最新观测正在分析'
        : status === 'unavailable'
          ? '最新观测尚无可用报告'
          : status === 'current'
            ? '报告覆盖当前问题观察'
            : '报告时效尚未确认';
  const description =
    status === 'outdated'
      ? '下方是历史冻结报告，不代表最新观测已完成分析；请等待覆盖最新观察的新版本。'
      : status === 'pending'
        ? '新一轮分析尚未完成；下方已保留的资料不代表最新观测已完成分析。'
        : status === 'current'
          ? '结论仅适用于此报告的冻结窗口；后续观察可能需要新的报告。'
          : '现有资料尚不能确认已覆盖最新观测；不能据此宣称当前问题已完成分析。';
  return (
    <Alert
      className="intel-gap-banner"
      showIcon
      type={status === 'outdated' || status === 'unavailable' ? 'warning' : 'info'}
      title={title}
      description={
        <>
          <p>{description}</p>
          {freshness && (
            <Space wrap>
              <span>
                最新问题观察 <Time value={freshness.event_updated_at} />
              </span>
              <span>
                报告窗口截止 <Time value={freshness.report_window_to} />
              </span>
            </Space>
          )}
        </>
      }
    />
  );
}

export function IssueReport() {
  const { id } = useParams();
  const { refresh } = useScope();
  const can = useCan();
  const [tick, setTick] = useState(0);
  const [selectedReference, setSelectedReference] = useState<string>();
  const resource = useResource<IssueDetail & { report_status?: string; report_error?: string }>(
    can('data.read') && can('event.read') ? `/intelligence/issues/${id}` : null,
    refresh + tick,
  );
  const data = resource.data;
  const state = data
    ? reportState({ ...data, status: data.report_status ?? data.status })
    : 'unavailable';

  useEffect(() => setSelectedReference(undefined), [id]);
  useEffect(() => {
    if (
      state !== 'pending' &&
      !['pending', 'outdated'].includes(data?.report_freshness?.status ?? '')
    )
      return;
    const timer = setInterval(() => setTick((value) => value + 1), 15000);
    return () => clearInterval(timer);
  }, [state, id, data?.report_freshness?.status]);
  return (
    <div className="intel-page">
      <div className="intel-breadcrumb">
        <ContextLink to="/">问题分析</ContextLink>
        <span> / 问题报告</span>
      </div>
      <Title
        title={data?.title ?? '问题报告'}
        description="基于报告保留的证据窗口。新一轮巡检形成新的报告版本。"
        actions={
          data && (
            <Space wrap>
              <ContextLink to={`/events/${data.event_id}`}>
                <Button>查看事件历史</Button>
              </ContextLink>
              {data.investigation_id && (
                <ContextLink to={`/investigations/${data.investigation_id}`}>
                  <Button>历史报告与审计</Button>
                </ContextLink>
              )}
            </Space>
          )
        }
      />
      {!can('data.read') || !can('event.read') ? (
        <AccessNotice />
      ) : (
        <LoadState {...resource} retry={resource.reload}>
          {data && (
            <>
              <Card className="intel-report-summary">
                <Space wrap>
                  <Tag color={data.kind === 'risk' ? 'gold' : 'blue'}>{findingLabel(data)}</Tag>
                  <Status value={data.status} />
                  <Status value={data.analysis_mode} />
                  <ReportCoverageTag status={data.coverage_status} />
                  <ReportFreshnessTag freshness={data.report_freshness} />
                </Space>

                <div className="intel-report-meta">
                  <strong>
                    问题主体：
                    {data.event_subject?.name ??
                      (data.object_names.join(' / ') || '对象名称不可用')}
                  </strong>
                  <span>
                    首次发现 <Time value={data.first_seen_at} />
                  </span>
                  <span>
                    最近更新 <Time value={data.updated_at} />
                  </span>
                </div>
                <div className="intel-analysis-scope">
                  <span>报告分析范围</span>
                  {data.analysis_objects?.length ? (
                    data.analysis_objects.map((object) => (
                      <Tag key={object.id}>
                        {object.name} ·{' '}
                        {{ mysql: '数据库', host: '主机', network: '网络探针', analysis: '联合分析对象' }[object.engine] ??
                          object.engine}
                      </Tag>
                    ))
                  ) : (
                    <small>此报告未提供独立分析范围；问题主体不代表全部分析对象。</small>
                  )}
                </div>
                {data.window && (
                  <div className="intel-window">
                    报告观察窗口 <Time value={data.window.from} /> — <Time value={data.window.to} />{' '}
                    <Tag>固定历史依据</Tag>
                  </div>
                )}
              </Card>
              <ReportFreshnessNotice freshness={data.report_freshness} />
              {state !== 'ready' &&
                !(state === 'pending' && data.report_freshness?.status === 'pending') && (
                  <Alert
                    className="intel-gap-banner"
                    showIcon
                    type={state === 'failed' ? 'error' : 'info'}
                    title={
                      state === 'failed'
                        ? '本次分析未成功完成'
                        : state === 'pending'
                          ? '调查进行中，报告尚未就绪'
                          : '此问题尚无已完成的调查报告'
                    }
                    description={
                      data.report_error ??
                      '下方仅展示服务端已保留的资料；当前不能据此宣称已定位根因。'
                    }
                  />
                )}
              <div className="intel-report-layout">
                <div className="intel-report-main">
                  <ReportEvidence data={data} onMetricReference={setSelectedReference} />
                </div>
                <aside className="intel-report-aside">
                  <Card title="已核对与覆盖">
                    <CapabilityList capabilities={data.checks ?? []} />
                    <ContextLink to="/monitoring">查看对象与能力 →</ContextLink>
                  </Card>
                  <Card title="下一步只读检查">
                    {data.next_checks?.length ? (
                      <ol className="intel-next">
                        {data.next_checks.map((s, i) => (
                          <li key={i}>{s}</li>
                        ))}
                      </ol>
                    ) : (
                      <p className="intel-muted">尚无有依据的下一步建议。</p>
                    )}
                    {reportMetricTargets(data).map((object) => (
                      <ContextLink
                        key={object.id}
                        to={`/metrics?${query({ instance_id: object.id, report: data.id, ...(data.window ?? {}) })}`}
                      >
                        <Button block className="intel-current-metric">
                          查看{object.name}当前指标 ↗
                        </Button>
                      </ContextLink>
                    ))}
                    <small>当前指标是新查询，与报告冻结依据分开显示。</small>
                  </Card>
                  <Card title="未知与反证">
                    <TextItems
                      title="尚未确认"
                      items={data.unknowns}
                      empty="报告未单列未知项，仍需核对覆盖能力。"
                    />
                    <TextItems
                      title="反证与限制"
                      items={data.refutations}
                      empty="报告未单列反证；不表示其他解释已排除。"
                    />
                  </Card>
                  <Card title="报告历史">
                    {data.history?.length ? (
                      <Timeline
                        items={data.history.map((h) => ({
                          children: (
                            <div>
                              <ContextLink to={`/investigations/${h.id}`}>
                                <Time value={h.created_at} /> 报告 ↗
                              </ContextLink>
                              <div>
                                <Status value={h.status} />
                                <Status value={h.mode} />
                              </div>
                            </div>
                          ),
                        }))}
                      />
                    ) : (
                      <p className="intel-muted">暂无关联的报告版本。</p>
                    )}
                  </Card>
                </aside>
              </div>
              <FrozenMetricDrawer
                reference={selectedReference}
                investigationId={data.investigation_id}
                window={data.window}
                chartMetadata={data.charts.find((chart) => chart.id === selectedReference)}
                close={() => setSelectedReference(undefined)}
              />
            </>
          )}
        </LoadState>
      )}
    </div>
  );
}

export function FrozenMetricDrawer({
  reference,
  investigationId,
  window,
  close,
  chartMetadata,
}: {
  reference?: string;
  investigationId: string | null;
  window: IssueDetail['window'];
  close: () => void;
  chartMetadata?: MetricChartData;
}) {
  const path = reference ? frozenMetricPath(investigationId, reference) : null;
  const resource = useResource<MetricChartData & { template?: string }>(path);
  const sample = resource.data;
  return (
    <Drawer title="报告引用的冻结时序证据" open={!!reference} onClose={close} width={900}>
      <Alert
        type="info"
        showIcon
        title="固定历史依据"
        description="读取这份调查保存的具体证据条目，不重新查询上游实时指标。"
      />
      {reference && !path ? (
        <Alert type="warning" title="此引用未提供可读取的固定时序条目标识" />
      ) : (
        <LoadState {...resource} retry={resource.reload}>
          {sample &&
            (window ? (
              <MetricPanel
                data={{
                  ...sample,
                  id: reference!,
                  title: chartMetadata?.title ?? sample.title ?? sample.template ?? '冻结指标',
                  instance_id: chartMetadata?.instance_id ?? sample.instance_id,
                  object_name: chartMetadata?.object_name ?? sample.object_name,
                  gaps:
                    sample.gaps ??
                    (Array.isArray(sample.coverage?.gaps)
                      ? sample.coverage.gaps.map((reason: unknown) =>
                          typeof reason === 'object' && reason !== null
                            ? reason
                            : { reason: String(reason) },
                        )
                      : []),
                  window,
                }}
                frozen
              />
            ) : (
              <SourceCoverage data={{ source: sample.source, coverage: sample.coverage }} />
            ))}
        </LoadState>
      )}
    </Drawer>
  );
}

function MoreFrozenCharts({ charts }: { charts: MetricChartData[] }) {
  const [open, setOpen] = useState(false);
  return (
    <details className="intel-details" onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>查看 {charts.length} 组冻结指标</summary>
      {open && (
        <div className="intel-report-main">
          {charts.map((chart) => (
            <MetricPanel key={chart.id} data={chart} frozen />
          ))}
        </div>
      )}
    </details>
  );
}

function TextItems({ title, items, empty }: { title: string; items?: string[]; empty: string }) {
  return (
    <section className="intel-text-items">
      <strong>{title}</strong>
      {items?.length ? (
        <List
          size="small"
          dataSource={items}
          renderItem={(item) => <List.Item>{text(item)}</List.Item>}
        />
      ) : (
        <p className="intel-muted">{empty}</p>
      )}
    </section>
  );
}

export function ReportEvidence({ data, onMetricReference }: { data: IssueDetail; onMetricReference?: (ref: string) => void }) {
  const state = reportState({ ...data, status: String(data.report_status ?? data.status) });
  const charts = prioritizeIssueCharts((data.charts ?? []).filter(chart => chartData(chart.points ?? []).some(([, value]) => value !== null)), data.issue_category, data.event_subject?.id ?? data.instance_ids?.[0]);
  return <>
    {data.report && state !== 'ready' && <Alert type="warning" showIcon title="保留的历史输出" description="本次分析尚未成功完成，以下已保留内容不能代表当前问题已完成诊断。" />}
    <ExecutiveSummary report={data.report} mode={data.analysis_mode} onMetricReference={onMetricReference} />
    {data.report && <details className="intel-details intel-report-prose"><summary>分析过程与原始报告</summary><InvestigationReportView data={data.report} onMetricReference={onMetricReference} /></details>}
    {!!data.nodes?.length && <details className="intel-details"><summary>证据关系 · {data.nodes.length} 个节点</summary><EvidenceGraph nodes={data.nodes} edges={data.edges ?? []} investigationId={data.investigation_id} onMetricReference={onMetricReference ?? (() => {})} category={data.issue_category} subjectId={data.event_subject?.id ?? data.instance_ids?.[0]} /></details>}
    {!!charts.length && <MoreFrozenCharts charts={charts} />}
  </>;
}

export function RecentAnalyses({ reports }: { reports: Row[] }) {
  return <section className="intel-section"><div className="intel-section-head"><h2>最近分析</h2><span>包含正常巡检 · 按当前授权范围</span></div>
    {reports.length ? <div className="intel-recent-reports">{reports.slice(0, 3).map(report => {
      const summary = (report.report as Row | undefined)?.executive_summary as ExecutiveSummaryData | undefined;
      const complete = ['completed', 'succeeded'].includes(String(report.status));
      const pending = ['queued', 'running', 'preparing', 'analyzing', 'pending', 'collecting_evidence'].includes(String(report.status));
      const title = !complete ? pending ? '分析尚未完成' : '分析未成功完成' : report.mode === 'rules_only' ? '规则巡检结果' : summary?.headline ?? '旧版未生成完整结论摘要';
      return <Card key={String(report.id)} size="small"><Space wrap><Status value={report.status} /><Status value={report.mode} /><Time value={report.created_at} /></Space><ContextLink to={`/investigations/${report.id}`}><h3>{title}</h3></ContextLink>
        {complete && report.mode !== 'rules_only' && summary && <p>{summary.conclusion.statement}</p>}
        <ContextLink to={`/investigations/${report.id}`}>查看冻结报告 →</ContextLink></Card>;
    })}</div> : <div className="intel-empty-inline">当前范围与时间内尚无分析报告。</div>}
  </section>;
}
