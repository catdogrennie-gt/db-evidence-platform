import type { Row } from './api';

export interface Capability {
  name: string;
  status: 'available' | 'missing' | 'unsupported' | 'stale' | 'failed';
  reason?: string;
}
export interface SeriesPoint {
  time: string;
  value: number | null;
  validity?: string;
}
export interface MetricChartData {
  id: string;
  title: string;
  unit: string;
  source: string;
  points: SeriesPoint[];
  gaps: Row[];
  baseline?: SeriesPoint[];
  threshold?: number;
  window: { from: string; to: string };
  coverage?: Row;
  instance_id?: string;
  object_name?: string;
  metric_key?: string;
}
export type IssueCategory = 'locks' | 'sql' | 'host' | 'network' | 'correlation' | 'unknown';
export interface ReportFreshness {
  status: 'current' | 'outdated' | 'pending' | 'unavailable';
  reason: string | null;
  event_updated_at: string | null;
  report_window_to: string | null;
}
export interface Issue {
  id: string;
  title: string;
  kind: 'current' | 'risk';
  presentation_kind?: 'issue' | 'signal' | 'history';
  status: string;
  severity: string;
  summary: string;
  object_names: string[];
  instance_ids: string[];
  first_seen_at: string;
  updated_at: string;
  investigation_id: string | null;
  analysis_mode: string;
  coverage_status: string;
  finding_status?: 'observed' | 'hypothesis' | 'conditional_prediction';
  issue_category?: IssueCategory;
  report_freshness?: ReportFreshness;
  observation_state?: string;
}
export type GraphKind = 'observed' | 'registered' | 'hypothesis' | 'conditional_prediction';
export interface EvidenceNode {
  id: string;
  label: string;
  kind: GraphKind;
  summary: string;
  evidence_ids: string[];
  instance_id?: string;
  object_name?: string;
  metric_key?: string;
  evidence_category?: IssueCategory;
  source?: string;
}
export interface EvidenceEdge {
  from: string;
  to: string;
  relationship: string;
  kind: GraphKind;
  evidence_ids: string[];
  provenance?: string;
}
export interface FrozenRuleFact {
  kind: 'capacity_pressure';
  instance_id: string;
  title: string;
  statement: string;
  observed_at: string;
  evidence_ids: string[];
  calculation: {
    used: number;
    limit: number;
    utilization_percent: number;
    remaining: number;
    warning_percent: number;
    unit: string;
    sample_count: number;
    time_pairs: Array<{
      used_at: string;
      limit_at: string;
      used: number;
      limit: number;
      remaining: number;
      utilization_percent: number;
    }>;
  };
}
export interface IssueDetail extends Issue {
  frozen_facts?: FrozenRuleFact[];
  report_status?: string;
  window: { from: string; to: string } | null;
  report: Row | null;
  nodes: EvidenceNode[];
  edges: EvidenceEdge[];
  charts: MetricChartData[];
  checks: Capability[];
  next_checks: string[];
  unknowns: string[];
  refutations: string[];
  history: { id: string; created_at: string; status: string; mode: string }[];
  event_id: string;
  event_subject?: { id: string; name: string };
  analysis_instance_ids?: string[];
  analysis_objects?: Array<{ id: string; name: string; engine: string }>;
}
export interface IntelligenceOverview {
  stats: { current: number; risk: number; objects: number };
  issues: Issue[];
  inspection: {
    status: string;
    last_completed_at?: string | null;
    last_checked_at?: string | null;
    next_check_at?: string | null;
    last_observed_at?: string | null;
    collection_seconds?: number | null;
    signal_seconds?: number | null;
    pacing?: { active: boolean; expires_at?: string | null };
    task?: { id?: string; status: string; created_at?: string | null; started_at?: string | null; error_code?: string | null } | null;
    next_due_at?: string | null;
    mode?: string;
    reason?: string;
  };
  coverage: import('./CoverageNotice').CoverageSummary;
}
export interface MonitoringSource {
  id: string;
  name: string;
  url: string;
  system_id: string;
  status: string;
  last_success_at?: string;
  error_code?: string;
  has_token: boolean;
}
export interface ZabbixTag { tag: string; value: string }
export interface ZabbixItem {
  hostid: string;
  host_name: string;
  host_tags?: ZabbixTag[];
  itemid: string;
  name: string;
  key: string;
  units: string;
  value_type: string | number;
  description?: string;
  tags: ZabbixTag[];
  status: string | number;
  state: string | number;
  error?: string;
  lastclock?: string | number | null;
  value_kind: 'numeric' | 'text' | 'log' | 'unsupported';
  selectable?: boolean;
  enhancement?: string | null;
  diagnostic_query?: string;
  history_semantics?: string;
  heartbeat_seconds?: number | null;
  poll_interval?: number | null;
}
export interface AnalysisRelation {
  from_hostid: string;
  to_hostid: string;
  kind: 'runs_on' | 'depends_on' | 'connects_to' | 'related_to';
  label?: string;
}
export interface AnalysisGroup {
  id: string;
  instance_id: string;
  name: string;
  description: string;
  source_id: string;
  source_name: string;
  system_id: string;
  enabled: boolean;
  config_revision: number;
  version: number;
  status: string;
  last_observed_at?: string | null;
  item_count: number;
  host_count: number;
  value_kind_counts: Partial<Record<ZabbixItem['value_kind'], number>>;
  items: ZabbixItem[];
  hosts: Array<{ hostid: string; name: string; tags: ZabbixTag[] }>;
  relations: AnalysisRelation[];
  interval_seconds: number;
}
export interface AnalysisGroupInput {
  name: string;
  source_id: string;
  description: string;
  items: Array<{ hostid: string; itemid: string }>;
  relations: AnalysisRelation[];
  enabled: boolean;
  interval_seconds: number;
  config_revision?: number;
}
export function analysisSelectionKey(item: Pick<ZabbixItem, 'hostid' | 'itemid'>): string {
  return `${item.hostid}:${item.itemid}`;
}
export function updateAnalysisSelection(previous: ZabbixItem[], page: ZabbixItem[], keys: string[]): ZabbixItem[] {
  const unique = [...new Set(keys)];
  if (unique.length > 32) throw new Error('每个联合分析对象最多选择 32 个监控项');
  const known = new Map([...previous, ...page].map(item => [analysisSelectionKey(item), item]));
  if (unique.some(key => !known.has(key))) throw new Error('监控项已变化，请重新加载后选择');
  return unique.map(key => known.get(key)!);
}
export function analysisGroupPayload(values: Row, selected: ZabbixItem[], relations: AnalysisRelation[]): AnalysisGroupInput {
  if (!selected.length || selected.length > 32) throw new Error('请选择 1 到 32 个监控项');
  if (new Set(selected.map(analysisSelectionKey)).size !== selected.length) throw new Error('请移除重复监控项');
  const hosts = new Set(selected.map(item => item.hostid));
  if (relations.some(relation => !hosts.has(relation.from_hostid) || !hosts.has(relation.to_hostid)))
    throw new Error('关系两端必须属于已选监控项的主机');
  if (!String(values.name ?? '').trim() || !values.source_id) throw new Error('请填写对象名称并选择来源');
  return {
    name: String(values.name).trim(), source_id: String(values.source_id),
    description: String(values.description ?? '').trim(),
    items: selected.map(({ hostid, itemid }) => ({ hostid, itemid })),
    relations: relations.map(({ from_hostid, to_hostid, kind, label }) => ({ from_hostid, to_hostid, kind, label: label?.trim() ?? '' })),
    enabled: values.enabled !== false,
    interval_seconds: Number(values.interval_seconds ?? 60),
    ...(typeof values.config_revision === 'number' ? { config_revision: values.config_revision } : {}),
  };
}
export function analysisItemStatus(item: ZabbixItem, nowSeconds = Date.now() / 1000): { label: string; color: string } {
  if (String(item.status) === '1') return { label: '上游已停用', color: 'default' };
  if (String(item.state) === '1') return { label: '采集不支持', color: 'error' };
  if (item.value_kind === 'unsupported') return { label: '不参与数值分析', color: 'default' };
  const clock = Number(item.lastclock);
  if (!Number.isFinite(clock) || clock <= 0) return { label: '尚无采样', color: 'warning' };
  if (nowSeconds - clock > 900) return { label: '采样较早', color: 'warning' };
  return { label: '已采样', color: 'blue' };
}
export function analysisGroupReadyLabel(lastObservedAt: string | null | undefined): string {
  return lastObservedAt ? '已获得有效观察' : '等待有效观察';
}
export function analysisHistoryCoverage(coverage: Row): { label: string; reasons: string[] } {
  const labels: Record<string, string> = {
    history_truncated: '达到历史条数上限', history_byte_budget: '达到历史大小上限',
    value_truncated: '部分文本值已截断', item_unavailable: '监控项当前不可用',
    no_samples: '当前窗口没有历史样本', unsupported_value_type: '暂不支持此值类型',
    invalid_value: '包含无效数值', invalid_history_row: '包含无效历史记录',
    sample_interval_unverified: '采样周期待核对',
    sparse_history_no_value_change_or_missing: '仅变化 / 心跳记录；无新历史不等于采集失败，也不能据此补写当前值',
  };
  const reasons = Array.isArray(coverage.reason_codes) ? coverage.reason_codes.map(String).map(reason => labels[reason] ?? reason) : [];
  return {
    label: coverage.complete === true && !coverage.truncated ? '覆盖完整' : coverage.complete === false || coverage.truncated ? '覆盖不完整' : '覆盖未知',
    reasons,
  };
}
export function metricSamplingSummary(coverage?: Row): string | null {
  const sampling = coverage?.sampling;
  if (!sampling || typeof sampling !== 'object') return null;
  const data = sampling as Row;
  const original = Number(data.original_point_count);
  const retained = Number(data.retained_point_count);
  if (!Number.isSafeInteger(original) || !Number.isSafeInteger(retained) || original < 1 || retained < 0 || retained > original) return null;
  const method = data.method === 'original_point_bucket_minmax' ? '桶极值抽样' : '有界抽样';
  return `保留 ${retained} / ${original} 个原始点（${method}）；曲线未展示全部原始采样。`;
}
export interface DiscoveredHost {
  hostid: string;
  name: string;
  layer: string;
  capabilities: Capability[];
  selected: boolean;
  instance_id: string | null;
  item_count: number;
  binding_error?: string | null;
}
export interface MonitoringObject {
  id: string;
  name: string;
  engine: string;
  system_id: string;
  system_name: string;
  source_id: string;
  source_name: string;
  enabled: boolean;
  capabilities: Capability[];
  last_observed_at?: string;
  status: string;
}
export interface InspectionPolicy {
  id?: string;
  version?: number;
  enabled: boolean;
  instance_ids: string[];
  signal_interval_seconds: number;
  ai_interval_seconds: number;
  urgent_enabled: boolean;
  cooldown_seconds: number;
  max_daily_runs: number;
  mode: string;
  external_confirmed: boolean;
  status?: string;
  last_completed_at?: string;
  next_due_at?: string;
}
export interface ObjectMetrics {
  object: { id: string; name: string };
  charts: MetricChartData[];
  capabilities: Capability[];
  window: { from: string; to: string };
}
export interface ResourceScope {
  department_id?: string;
  system_id?: string;
  environment?: string;
  instance_id?: string;
}

export const graphKinds: Record<GraphKind, { label: string; color: string; dashed: boolean }> = {
  observed: { label: '已观测', color: '#2768cc', dashed: false },
  registered: { label: '登记关系', color: '#53776a', dashed: false },
  hypothesis: { label: '候选解释', color: '#aa761b', dashed: true },
  conditional_prediction: { label: '条件预测', color: '#9a5db2', dashed: true },
};

export function chartData(points: SeriesPoint[]): Array<[string, number | null]> {
  return points.map((p) => [
    p.time,
    typeof p.value === 'number' &&
    Number.isFinite(p.value) &&
    (!p.validity || ['valid', 'available', 'observed'].includes(p.validity))
      ? p.value
      : null,
  ]);
}
export function reportState(
  data: Pick<IssueDetail, 'report' | 'status' | 'investigation_id'>,
): 'ready' | 'failed' | 'pending' | 'unavailable' {
  if (['failed', 'interrupted', 'cancelled', 'expired', 'model_failed'].includes(data.status))
    return 'failed';
  if (
    ['queued', 'preparing', 'analyzing', 'running', 'collecting_evidence', 'pending'].includes(
      data.status,
    )
  )
    return 'pending';
  if (data.report && Object.keys(data.report).length) return 'ready';
  if (
    data.investigation_id ||
    ['queued', 'preparing', 'analyzing', 'running', 'collecting_evidence', 'pending'].includes(
      data.status,
    )
  )
    return 'pending';
  return 'unavailable';
}
export function visibleGraph(
  nodes: EvidenceNode[],
  edges: EvidenceEdge[],
): { nodes: EvidenceNode[]; edges: EvidenceEdge[] } {
  const safeNodes = nodes.filter((n) => Object.hasOwn(graphKinds, n.kind));
  const ids = new Set(safeNodes.map((n) => n.id));
  return {
    nodes: safeNodes,
    edges: edges.filter(
      (e) => ids.has(e.from) && ids.has(e.to) && Object.hasOwn(graphKinds, e.kind),
    ),
  };
}
export function selectionForScope(previous: string, current: string, selected: string[]): string[] {
  return previous === current ? selected : [];
}
export function scopeKey(scope: ResourceScope): string {
  return JSON.stringify([
    scope.department_id ?? '',
    scope.system_id ?? '',
    scope.environment ?? '',
    scope.instance_id ?? '',
  ]);
}
export function sourcePayload(values: Row, editing: boolean): Row {
  const groups =
    typeof values.groups === 'string'
      ? values.groups
          .split(',')
          .map((v) => v.trim())
          .filter(Boolean)
      : [];
  return {
    name: values.name,
    url: values.url,
    system_id: values.system_id,
    ...(values.api_token ? { api_token: values.api_token } : {}),
    ...(!editing || groups.length ? { allowed_host_group_ids: groups } : {}),
  };
}
export function frozenMetricPath(investigationId: string | null, reference: string): string | null {
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const item = reference.startsWith('manifest:') ? reference.slice(9) : '';
  return investigationId && uuid.test(investigationId) && uuid.test(item)
    ? `/investigations/${investigationId}/metric-windows/${item}`
    : null;
}
export function isHistoricalObservation(issue: Pick<Issue, 'observation_state' | 'presentation_kind'>): boolean {
  return issue.presentation_kind === 'history' || ['returned_to_baseline','recovered','configuration_changed'].includes(issue.observation_state ?? '');
}
export function findingLabel(issue: Pick<Issue, 'kind' | 'finding_status' | 'observation_state' | 'presentation_kind'>): string {
  if (issue.observation_state === 'returned_to_baseline') return '指标已回基线';
  if (issue.observation_state === 'configuration_changed') return '配置已变化 · 历史观测';
  if (issue.observation_state === 'recovered') return '后续观测已恢复';
  if (issue.presentation_kind === 'history') return issue.finding_status === 'hypothesis' ? '历史候选解释 · 待复核' : '历史观测';
  if (issue.presentation_kind === 'signal') return '变化线索 · 尚未判定为问题';
  if (issue.finding_status === 'hypothesis') return '候选解释 · 待核验';
  if (issue.finding_status === 'conditional_prediction') return '条件预测';
  return issue.kind === 'current' ? '当前问题' : '潜在风险';
}
export function initialSidebarCollapsed(width: number): boolean {
  return width < 992;
}
export function reportMetricTargets(
  data: Pick<IssueDetail, 'analysis_objects' | 'instance_ids' | 'object_names'>,
): Array<{ id: string; name: string }> {
  return data.analysis_objects?.length
    ? data.analysis_objects.map(({ id, name }) => ({ id, name }))
    : data.instance_ids.map((id, i) => ({ id, name: data.object_names[i] ?? '对象' }));
}

// Only the API's typed category and canonical template key affect relevance.
// Display titles, prose and array order are never evidence of issue category.
const categoryMetrics: Record<IssueCategory, string[]> = {
  locks: ['lock_waits', 'threads_running', 'transactions', 'questions_per_second', 'connections'],
  sql: ['threads_running', 'questions_per_second', 'connections', 'transactions', 'lock_waits'],
  host: [
    'host_cpu_percent',
    'host_cpu_busy_ratio',
    'host_disk_read_bytes_per_second',
    'host_disk_write_bytes_per_second',
    'host_load1',
    'host_memory_available_bytes',
  ],
  network: [
    'probe_availability',
    'probe_latency_ms',
    'bytes_received_per_second',
    'bytes_sent_per_second',
  ],
  correlation: [],
  unknown: [],
};

function metricPriority(key: string | undefined, category: IssueCategory): number {
  const rank = key ? categoryMetrics[category].indexOf(key) : -1;
  return rank < 0 ? 100 : rank;
}

export function prioritizeIssueCharts(
  charts: MetricChartData[],
  category: IssueCategory = 'unknown',
  subjectId?: string,
): MetricChartData[] {
  return [...charts].sort((a, b) => {
    const relevance =
      metricPriority(a.metric_key, category) - metricPriority(b.metric_key, category);
    if (relevance) return relevance;
    // Match the event subject only within an equally relevant metric; never let
    // an unrelated subject metric displace a relevant cross-object observation.
    if (metricPriority(a.metric_key, category) < 100 && subjectId)
      return Number(b.instance_id === subjectId) - Number(a.instance_id === subjectId);
    return 0;
  });
}

export function prioritizeIssueNodes(
  nodes: EvidenceNode[],
  edges: EvidenceEdge[],
  category: IssueCategory = 'unknown',
  subjectId?: string,
): EvidenceNode[] {
  const relevant = new Set(
    nodes
      .filter(
        (node) =>
          metricPriority(node.metric_key, category) < 100 ||
          (category !== 'unknown' && node.evidence_category === category),
      )
      .map((node) => node.id),
  );
  const neighbors = new Set(
    edges
      .filter((edge) => relevant.has(edge.from) || relevant.has(edge.to))
      .flatMap((edge) => [edge.from, edge.to]),
  );
  function priority(node: EvidenceNode): number {
    if (node.kind === 'observed' && relevant.has(node.id)) {
      const metric = metricPriority(node.metric_key, category);
      return metric < 100 ? metric / 100 : 0.5;
    }
    if (node.kind === 'registered' && (node.id === subjectId || neighbors.has(node.id))) return 1;
    if (neighbors.has(node.id)) return node.kind === 'observed' ? 1.5 : 2;
    return node.kind === 'observed' ? 3 : node.kind === 'registered' ? 4 : 5;
  }
  return [...nodes].sort((a, b) => priority(a) - priority(b));
}

export function compactIssueNodes(
  nodes: EvidenceNode[],
  edges: EvidenceEdge[],
  category: IssueCategory = 'unknown',
  subjectId?: string,
  limit = 9,
): EvidenceNode[] {
  const ordered = prioritizeIssueNodes(nodes, edges, category, subjectId);
  const ids = new Set(nodes.map((node) => node.id));
  const isRaw = (node: EvidenceNode) =>
    node.kind === 'observed' && !!node.source && !node.metric_key;
  const isFact = (node: EvidenceNode) =>
    node.kind === 'observed' && !node.source && !node.metric_key;
  const statements = nodes.filter(
    (node) => isFact(node) || ['hypothesis', 'conditional_prediction'].includes(node.kind),
  );
  // Choose a directly referenced representative per source and object. The API
  // does not expose typed snapshot time/content here, so this makes no claim
  // about recency or whether a snapshot proves an abnormal value.
  const rawKey = (node: EvidenceNode) => JSON.stringify([node.instance_id ?? '', node.source]);
  const referenceCount = (node: EvidenceNode) =>
    statements.filter(
      (statement) =>
        statement.evidence_ids.includes(node.id) ||
        edges.some(
          (edge) =>
            (edge.from === node.id && edge.to === statement.id) ||
            (edge.to === node.id && edge.from === statement.id),
        ),
    ).length;
  const representatives = new Map<string, EvidenceNode>();
  for (const node of ordered.filter(isRaw)) {
    const previous = representatives.get(rawKey(node));
    if (!previous || referenceCount(node) > referenceCount(previous))
      representatives.set(rawKey(node), node);
  }
  const available = ordered.filter(
    (node) => !isRaw(node) || representatives.get(rawKey(node))?.id === node.id,
  );
  const selected: EvidenceNode[] = [];
  const selectedIds = new Set<string>();
  function pick(predicate: (node: EvidenceNode) => boolean) {
    const node = available.find(
      (candidate) => !selectedIds.has(candidate.id) && predicate(candidate),
    );
    if (node && selected.length < limit) {
      selected.push(node);
      selectedIds.add(node.id);
    }
  }
  const relevant = (node: EvidenceNode) =>
    metricPriority(node.metric_key, category) < 100 ||
    (category !== 'unknown' && node.evidence_category === category);
  const linked = (node: EvidenceNode) =>
    node.evidence_ids.some((ref) => selectedIds.has(ref)) ||
    edges.some(
      (edge) =>
        (edge.from === node.id && selectedIds.has(edge.to)) ||
        (edge.to === node.id && selectedIds.has(edge.from)),
    );
  const supported = (node: EvidenceNode) => node.evidence_ids.some((ref) => ids.has(ref));
  pick((node) => node.kind === 'observed' && !!node.metric_key && relevant(node));
  pick((node) => isRaw(node) && relevant(node));
  pick((node) => node.kind === 'registered' && node.id === subjectId);
  pick((node) => isFact(node) && linked(node));
  if (!selected.some(isFact)) pick((node) => isFact(node) && supported(node));
  for (const kind of ['hypothesis', 'conditional_prediction']) {
    pick((node) => node.kind === kind && linked(node));
    if (!selected.some((node) => node.kind === kind))
      pick((node) => node.kind === kind && supported(node));
  }
  const layer = (node: EvidenceNode) => {
    if (node.kind !== 'observed') return undefined;
    if (
      categoryMetrics.network.includes(node.metric_key ?? '') ||
      node.evidence_category === 'network'
    )
      return 'network';
    if (categoryMetrics.host.includes(node.metric_key ?? '') || node.evidence_category === 'host')
      return 'host';
    if (
      categoryMetrics.locks.includes(node.metric_key ?? '') ||
      categoryMetrics.sql.includes(node.metric_key ?? '') ||
      ['locks', 'sql'].includes(node.evidence_category ?? '')
    )
      return 'database';
    return undefined;
  };
  // Reserve space for cross-layer observations before filling with more facts
  // or explanations of the same layer. These are context, not causal claims.
  for (const target of ['database', 'host', 'network']) {
    if (!selected.some((node) => layer(node) === target)) pick((node) => layer(node) === target);
  }
  const directRefs = new Set(selected.flatMap((node) => node.evidence_ids));
  pick((node) => directRefs.has(node.id));
  const owners = new Set(selected.map((node) => node.instance_id).filter(Boolean));
  for (const node of available)
    if (node.kind === 'registered' && owners.has(node.id))
      pick((candidate) => candidate.id === node.id);
  for (const node of available) pick((candidate) => candidate.id === node.id);
  return selected;
}

export function initialRollingWindow(_pathname: string, _search: string): boolean {
  return false;
}
