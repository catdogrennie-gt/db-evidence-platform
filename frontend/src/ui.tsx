import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from 'antd';
import * as echarts from 'echarts/core';
import { LineChart, GraphChart } from 'echarts/charts';
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsOption } from 'echarts';
import { Link, useLocation } from 'react-router-dom';
import type { Row, TaskRef } from './api';
import { text } from './api';
import { errorMessage, useScope } from './hooks';
import { scopedDestination } from './navigation';
echarts.use([
  LineChart,
  GraphChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  CanvasRenderer,
]);
const labels: Record<string, string> = {
  completed: '已完成',
  preparing: '准备资料',
  analyzing: '分析中',
  merged: '已归并',
  warning: '提示',
  active: '启用',
  ready: '待首采',
  paused: '已暂停',
  archived: '已退役',
  activating: '首采中',
  revalidation_required: '需要重验',
  healthy: '健康',
  degraded: '降级',
  unknown: '未知',
  unreachable: '不可达',
  stale: '已过期',
  partial: '部分完成',
  succeeded: '成功',
  failed: '失败',
  queued: '排队',
  retry_wait: '等待重试',
  running: '执行中',
  cancel_requested: '请求取消',
  cancelled: '已取消',
  interrupted: '已中断',
  expired: '已过期',
  available: '可用',
  unsupported: '不支持',
  permission_denied: '权限不足',
  not_configured: '未配置',
  not_applicable: '不适用',
  open: '待处理',
  acknowledged: '已认领',
  investigating: '调查中',
  pending_verification: '待复验',
  resolved: '已满足条件',
  closed: '已关闭',
  firing: '触发中',
  recovered: '已恢复',
  improved: '达到所选条件',
  not_improved: '未达到条件',
  inconclusive: '资料不足',
  live: '实时采集',
  replay: '回放',
  imported: '导入',
  rule: '确定性规则',
  live_model: '实时模型',
  human_assertion: '人工登记',
  rules_only: '仅规则',
  live_redacted: '脱敏模型',
  disabled: '停用',
  draft: '草稿',
  published: '已发布',
  pending_review: '待审核',
  in_review: '待审核',
  approved: '已审核',
  rejected: '已退回',
  superseded: '已取代 / 撤回',
  high: '高',
  medium: '中',
  low: '低',
  critical: '严重',
};
export function Status({ value }: { value: unknown }) {
  const key = text(value, 'unknown');
  const color = [
    'healthy',
    'succeeded',
    'available',
    'recovered',
    'improved',
    'published',
    'active',
  ].includes(key)
    ? 'success'
    : [
          'failed',
          'unreachable',
          'firing',
          'critical',
          'high',
          'revalidation_required',
          'not_improved',
        ].includes(key)
      ? 'error'
      : ['partial', 'degraded', 'stale', 'unknown', 'inconclusive', 'pending_review'].includes(key)
        ? 'warning'
        : ['running', 'queued', 'activating', 'investigating'].includes(key)
          ? 'processing'
          : 'default';
  return <Tag color={color}>{labels[key] ?? key}</Tag>;
}
export function Time({ value }: { value: unknown }) {
  const raw = text(value, '');
  const d = new Date(raw);
  return (
    <span title={raw}>
      {raw && !isNaN(d.valueOf())
        ? new Intl.DateTimeFormat('zh-CN', {
            dateStyle: 'short',
            timeStyle: 'medium',
            hour12: false,
          }).format(d)
        : '—'}
    </span>
  );
}
export function Title({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        <Typography.Title level={2}>{title}</Typography.Title>
        {description && <Typography.Text type="secondary">{description}</Typography.Text>}
      </div>
      <Space wrap>{actions}</Space>
    </div>
  );
}
export function LoadState({
  loading,
  error,
  retry,
  children,
  empty = false,
  refreshing,
  updatedAt,
  refreshError,
}: {
  loading?: boolean;
  error?: Error;
  retry?: () => void;
  children?: ReactNode;
  empty?: boolean;
  refreshing?: boolean;
  updatedAt?: string;
  refreshError?: Error;
}) {
  if (loading)
    return (
      <div className="loading-state">
        <Spin />
        <span>正在读取授权范围内资料…</span>
      </div>
    );
  if (error)
    return (
      <Alert
        type="error"
        showIcon
        title="读取未完成"
        description={
          <>
            <div>{errorMessage(error)}</div>
            {retry && (
              <Button onClick={retry} className="retry">
                重新加载
              </Button>
            )}
          </>
        }
      />
    );
  return <>
    <ResourceRefreshStatus refreshing={refreshing} updatedAt={updatedAt} refreshError={refreshError} />
    {empty ? <Empty description="当前范围内暂无资料" /> : children}
  </>;
}
export function ResourceRefreshStatus({ refreshing, updatedAt, refreshError }: {
  refreshing?: boolean; updatedAt?: string; refreshError?: Error;
}) {
  if (!updatedAt && !refreshing && !refreshError) return null;
  return <div role="status" aria-live="polite" style={{ marginBottom: 12 }}>
    <Typography.Text type={refreshError ? 'warning' : 'secondary'}>
      {refreshError ? '刷新未完成，显示上次成功读取的内容。' : refreshing ? '刷新中，正在显示上次成功读取的内容。' : '上次成功读取：'}
      {(refreshError || refreshing) && updatedAt ? ' 上次成功读取：' : ''}
      <Time value={updatedAt} />
      {refreshError && <span> · {errorMessage(refreshError)}</span>}
    </Typography.Text>
  </div>;
}
export function ErrorNotice({ error }: { error: unknown }) {
  return error ? (
    <Alert type="error" showIcon title={errorMessage(error)} className="notice" />
  ) : null;
}
export function TaskProgress({ task }: { task?: TaskRef }) {
  return task ? (
    <Alert
      type={task.status === 'failed' ? 'error' : 'info'}
      showIcon
      title={
        <Space>
          任务 <Status value={task.status} />
          <ContextLink to={`/tasks/${task.id}`}>查看持久任务</ContextLink>
        </Space>
      }
      description={
        <Space wrap>
          <span>{text(task.type)}</span>
          {task.progress && <span>{text(task.progress.stage ?? task.progress.phase)}</span>}
          <span>任务 ID：{task.id}</span>
          {task.error_code && <Tag color="error">{task.error_code}</Tag>}
          {task.error_message && <span>{task.error_message}</span>}
        </Space>
      }
      className="notice"
    />
  ) : null;
}
export function ContextLink({ to, children }: { to: string; children: ReactNode }) {
  const location = useLocation();
  const { params } = useScope();
  return <Link to={scopedDestination(to, params || location.search)}>{children}</Link>;
}
export function FieldGrid({ data, fields }: { data: Row; fields: [string, string][] }) {
  return (
    <Descriptions
      size="small"
      column={{ xs: 1, sm: 2, lg: 3 }}
      items={fields.map(([key, label]) => ({
        key,
        label,
        children: key.endsWith('_at') ? (
          <Time value={data[key]} />
        ) : ['status', 'health', 'lifecycle', 'source_mode', 'mode', 'availability'].includes(
            key,
          ) ? (
          <Status value={data[key]} />
        ) : (
          <span className="break-word">{text(data[key])}</span>
        ),
      }))}
    />
  );
}
const coverageReasonLabels: Record<string, string> = {
  missing_samples: '缺失采样', no_samples: '当前窗口没有历史样本',
  invalid_value: '包含无效数值', invalid_history_row: '包含无效历史记录',
  item_unavailable: '监控项当前不可用', unsupported_value_type: '暂不支持此值类型',
  sample_interval_unverified: '采样周期待核对', history_truncated: '达到历史条数上限',
  history_byte_budget: '达到历史大小上限', value_truncated: '部分文本值已截断',
  sparse_history_no_value_change_or_missing: '按变化或心跳记录；无新记录不等于采集失败',
  point_budget_exceeded: '超出样本数量预算', byte_budget_exceeded: '超出数据大小预算',
  unknown_gap: '缺口原因未说明',
};
function coverageGapReason(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object') {
    const gap = value as Row;
    if (typeof gap.reason === 'string') return gap.reason;
    if (typeof gap.code === 'string') return gap.code;
  }
  return 'unknown_gap';
}
function gapEntries(value: unknown): unknown[] {
  return Array.isArray(value) ? value : value == null ? [] : [value];
}
export function SourceCoverage({ data }: { data?: Row }) {
  if (!data)
    return (
      <Alert
        type="warning"
        showIcon
        title="来源新鲜度未知"
        description="服务端未返回来源覆盖资料，无法据此判断健康。"
      />
    );
  const coverage = (data.coverage ?? {}) as Row;
  const rawGaps = [gapEntries(data.gaps), gapEntries(coverage.gaps)].find(items => items.length) ?? [];
  const existingReasons = new Set(rawGaps.map(coverageGapReason));
  const gaps = [...rawGaps, ...gapEntries(coverage.reason_codes).filter(value => !existingReasons.has(coverageGapReason(value)))]
    .filter(value => { const reason = coverageGapReason(value); return reason !== 'not_applicable' && !reason.endsWith(':not_applicable'); });
  const reasons = new Map<string, { count: number; from?: unknown; to?: unknown }>();
  for (const value of gaps) {
    const reason = coverageGapReason(value);
    const previous = reasons.get(reason);
    if (previous) previous.count += 1;
    else {
      const gap = value && typeof value === 'object' ? value as Row : {};
      reasons.set(reason, { count: 1, from: gap.from, to: gap.to });
    }
  }
  const summaries = [...reasons.entries()];
  const complete = coverage.complete === true && !coverage.truncated && gaps.length === 0;
  return (
    <div className="source-line">
      <Space wrap>
        {data.source_mode || data.source ? (
          <Status value={data.source_mode ?? data.source} />
        ) : (
          <Tag>当前范围资料</Tag>
        )}
        {data.last_observed_at || data.observed_at ? (
          <span>
            观察 <Time value={data.last_observed_at ?? data.observed_at} />
          </span>
        ) : null}
        {data.ingested_at ? (
          <span>
            入库 <Time value={data.ingested_at} />
          </span>
        ) : null}
        {data.freshness || data.quality ? <Status value={data.freshness ?? data.quality} /> : null}
        {coverage.complete !== undefined || coverage.truncated || gaps.length > 0 ? (
          <Tag color={complete ? 'success' : 'warning'}>
            {complete ? '覆盖完整' : '覆盖不完整'}
          </Tag>
        ) : null}
        {summaries.length > 0 ? (
          <Typography.Text type="warning">
            缺口：{summaries.slice(0, 3).map(([reason, gap], index) => <span key={reason}>
              {index > 0 ? '；' : ''}{coverageReasonLabels[reason] ?? `其他缺口（${reason.slice(0, 80)}）`}
              {gap.count > 1 ? `（${gap.count} 处）` : gap.from || gap.to ? <>（<Time value={gap.from} /> — <Time value={gap.to} />）</> : null}
            </span>)}{summaries.length > 3 ? `；另 ${summaries.length - 3} 类缺口，展开明细查看` : ''}
          </Typography.Text>
        ) : null}
      </Space>
    </div>
  );
}
export function DataTable({
  items,
  columns,
  loading,
  onRowClick,
}: {
  items: Row[];
  columns: { key: string; title: string; render?: (value: unknown, row: Row) => ReactNode }[];
  loading?: boolean;
  onRowClick?: (row: Row) => void;
}) {
  return (
    <Table<Row>
      size="middle"
      rowKey={(row) => text(row.id ?? row.digest ?? row.name ?? JSON.stringify(row))}
      dataSource={items}
      loading={loading}
      pagination={false}
      scroll={{ x: 'max-content' }}
      columns={columns.map((c) => ({
        key: c.key,
        dataIndex: c.key,
        title: c.title,
        render: (value: unknown, row: Row) => (c.render ? c.render(value, row) : text(value)),
      }))}
      onRow={(row) => ({
        onClick: () => onRowClick?.(row),
        className: onRowClick ? 'clickable-row' : '',
      })}
      locale={{
        emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前范围内暂无记录" />,
      }}
    />
  );
}
export function RecordCard({ title, data }: { title: string; data: unknown }) {
  if (data === undefined || data === null)
    return (
      <Card title={title}>
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无资料" />
      </Card>
    );
  if (Array.isArray(data))
    return (
      <Card title={title}>
        <Space orientation="vertical" style={{ width: '100%' }}>
          {data.length ? (
            data.map((v, i) => <RecordBlock key={i} data={v} />)
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无记录" />
          )}
        </Space>
      </Card>
    );
  return (
    <Card title={title}>
      <RecordBlock data={data} />
    </Card>
  );
}
const fieldLabels: Record<string, string> = {
  id: 'ID',
  name: '名称',
  title: '标题',
  source: '来源',
  source_mode: '资料模式',
  observed_at: '观察时间',
  ingested_at: '入库时间',
  created_at: '创建时间',
  updated_at: '更新时间',
  status: '状态',
  reason: '原因',
  reason_code: '原因码',
  message: '说明',
  summary: '摘要',
  description: '说明',
  question: '问题',
  facts: '已观察事实',
  hypotheses: '候选原因',
  unknowns: '未知项',
  recommendations: '建议检查',
  evidence_ids: '证据引用',
  statement: '说明',
  content: '正文',
  availability: '可用性',
  collector: '采集器',
  checked_at: '探测时间',
  duration_ms: '耗时 ms',
  error_code: '错误码',
  error_message: '公开错误说明',
  result: '结果',
  conditions: '确定性条件',
  coverage: '覆盖范围',
  rule_version: '规则版本',
  version: '版本',
  source_instance_ids: '来源实例',
  role: '角色',
  risk: '风险',
  rollback: '回退方法',
  steps: '人工步骤',
  goal: '目标',
  permission_version: '授权版本',
  instance_id: '实例',
  from: '起始时间',
  to: '结束时间',
  code: '编码',
  enabled: '启用',
  username: '用户名',
  display_name: '显示名',
  permissions: '动作权限',
  scope: '资源范围',
  scope_type: '范围类型',
  scope_id: '范围 ID',
  action: '动作',
  actor_id: '操作者',
  result_ref: '结果引用',
  complete: '完整覆盖',
  gaps: '缺口',
  truncated: '截断',
  count: '记录数',
  items: '记录',
  data: '资料',
  metadata: '元数据',
  report: '报告',
  report_json: '报告',
  tool_calls: '工具调用',
  model_calls: '模型调用',
  manifest: '证据清单',
  condition_state: '规则状态',
  human_state: '人工流程',
  severity: '严重度',
  owner_id: '负责人',
};
export function RecordBlock({ data, depth = 0 }: { data: unknown; depth?: number }) {
  if (typeof data !== 'object' || data === null) return <span className="prose">{text(data)}</span>;
  if (Array.isArray(data))
    return (
      <div className="record-list">
        {data.length
          ? data.map((v, i) => (
              <div key={i}>
                <RecordBlock data={v} depth={depth + 1} />
              </div>
            ))
          : '无记录'}
      </div>
    );
  return (
    <dl className="record-fields">
      {Object.entries(data).map(([key, v]) => (
        <div key={key}>
          <dt>{fieldLabels[key] ?? key}</dt>
          <dd>
            {['status', 'availability', 'source_mode', 'condition_state'].includes(key) ? (
              <Status value={v} />
            ) : key.endsWith('_at') ? (
              <Time value={v} />
            ) : key === 'evidence_id' && typeof v === 'string' && !v.startsWith('manifest:') ? (
              <ContextLink to={`/evidence/${v}`}>{v}</ContextLink>
            ) : typeof v === 'object' && v !== null && depth < 4 ? (
              <RecordBlock data={v} depth={depth + 1} />
            ) : (
              <span className="prose">{text(v)}</span>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}
function optionRows(value: unknown): Row[] {
  return (Array.isArray(value) ? value : value && typeof value === 'object' ? [value] : []) as Row[];
}
function previousComponent(rows: Row[], next: Row, index: number): Row | undefined {
  if (next.id !== undefined) return rows.find(row => row.id === next.id);
  if (next.name !== undefined) return rows.find(row => row.name === next.name);
  return rows[index];
}
// setOption merges data into the mounted instance. Preserve the reader's interaction
// state explicitly because incoming options often contain default zoom/legend values.
export function applyChartUpdate(chart: Pick<ReturnType<typeof echarts.init>, 'getOption' | 'setOption'>, option: EChartsOption) {
  const current = chart.getOption() ?? {};
  const next = { ...option };
  if (option.dataZoom) next.dataZoom = optionRows(option.dataZoom).map((zoom, index) => {
    const previous = previousComponent(optionRows(current.dataZoom), zoom, index);
    return { ...zoom, ...(typeof previous?.start === 'number' ? { start: previous.start } : {}), ...(typeof previous?.end === 'number' ? { end: previous.end } : {}) };
  }) as EChartsOption['dataZoom'];
  if (option.legend) next.legend = optionRows(option.legend).map((legend, index) => {
    const previous = previousComponent(optionRows(current.legend), legend, index);
    return { ...legend, ...(previous?.selected ? { selected: { ...(legend.selected as Row ?? {}), ...(previous.selected as Row) } } : {}) };
  }) as EChartsOption['legend'];
  if (option.series) next.series = optionRows(option.series).map((series, index) => {
    if (series.type !== 'graph') return series;
    const previous = previousComponent(optionRows(current.series), series, index);
    return previous?.type === 'graph' ? { ...series, ...(typeof previous.zoom === 'number' ? { zoom: previous.zoom } : {}), ...(Array.isArray(previous.center) ? { center: previous.center } : {}) } : series;
  }) as EChartsOption['series'];
  chart.setOption(next, { replaceMerge: ['series', 'dataZoom', 'legend'] });
}
export function Chart({
  option,
  height = 300,
  onClick,
  label = '基于服务端资料绘制的图表',
}: {
  option: EChartsOption;
  height?: number;
  onClick?: (params: Row) => void;
  label?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const instance = useRef<ReturnType<typeof echarts.init> | null>(null);
  const click = useRef(onClick);
  click.current = onClick;
  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current);
    instance.current = chart;
    chart.on('click', (p) => click.current?.(p as unknown as Row));
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => {
      observer.disconnect();
      instance.current = null;
      chart.dispose();
    };
  }, []);
  useEffect(() => {
    if (instance.current) applyChartUpdate(instance.current, option);
  }, [option]);
  return <div ref={ref} style={{ height, width: '100%' }} role="img" aria-label={label} />;
}
export function RelationshipGraph({ nodes, edges }: { nodes: Row[]; edges: Row[] }) {
  const ids = new Set(nodes.map((n) => text(n.id)));
  const links = edges.filter(
    (e) => ids.has(text(e.source ?? e.source_id)) && ids.has(text(e.target ?? e.target_id)),
  );
  const option: EChartsOption = {
    tooltip: {
      formatter: (params) => {
        const p = params as unknown as { data: Row };
        return text(p.data.name ?? p.data.id);
      },
    },
    series: [
      {
        type: 'graph',
        layout: 'force',
        roam: true,
        force: { repulsion: 450, edgeLength: 150 },
        symbolSize: 48,
        label: { show: true, position: 'bottom', fontSize: 11, color: '#48645e' },
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: 7,
        lineStyle: { color: '#92b9ad', width: 1.5 },
        itemStyle: { color: '#3e8b76' },
        data: nodes.map((n) => ({ id: text(n.id), name: text(n.name ?? n.id), symbolSize: 42 })),
        links: links.map((e) => ({
          source: text(e.source ?? e.source_id),
          target: text(e.target ?? e.target_id),
          name: text(e.type ?? e.relation_type, '关系'),
        })),
      },
    ],
  };
  return nodes.length ? (
    <Chart option={option} height={350} />
  ) : (
    <Empty description="当前范围内尚无已登记关系" />
  );
}
