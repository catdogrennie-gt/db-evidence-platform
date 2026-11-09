import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Space, Switch, Tag } from 'antd';
import { api, ApiError } from './api';
import type { Row } from './api';
import { useCan } from './hooks';
import { HistoricalInvestigationReport } from './Historical';
import { FrozenMetricDrawer, MetricPanel } from './Intelligence';
import { chartData, frozenMetricPath } from './intelligenceModel';
import type { MetricChartData } from './intelligenceModel';
import { LoadState, Time, Title } from './ui';

export interface DemoRun {
  id: string; status: string; phase: string; fault_state: string;
  started_at?: string | null; expires_at?: string | null; stopped_at?: string | null;
  stop_requested?: boolean; evidence_id?: string | null; observed_at?: string | null;
  investigation_id?: string | null; analysis_status?: string; error_code?: string | null;
  cleanup?: Row | null; steps?: { key: string; label: string; status: string; at?: string | null; detail?: string | null }[];
}
export interface DemoSnapshot {
  enabled: boolean; scenario: string; target: { display_name?: string; instance_id?: string; environment?: string } | null;
  can_control: boolean; server_time: string;
  limits: { max_duration_seconds: number; cooldown_seconds: number; max_daily_runs: number; max_daily_ai_runs: number };
  budget: { used_runs: number; remaining_runs: number; used_ai_runs: number; remaining_ai_runs: number };
  cooldown_until?: string | null; run: DemoRun | null; report: Row | null; history: Row[];
}
const labels: Record<string, string> = {
  requested: '等待执行', injecting: '准备注入', recovering: '回滚与直接核验中', cleanup: '回滚与直接核验中', monitoring_recovery: '等待 Zabbix 恢复采样', cleanup_unknown: '清理状态未知', awaiting_recovery: '等待 Zabbix 恢复采样', recovery_unconfirmed: '监控恢复未确认', cleared: '事务已解除', not_started: '尚未开始', sessions_closed: '本轮连接已关闭', locks_cleared: '直接锁核验已清空', dataset_unchanged: '实验行数据未变',
  idle: '空闲', pending: '等待中', queued: '排队中', starting: '准备实验', preparing: '核对实验身份', running: '实验运行中',
  active: '故障生效', waiting_evidence: '等待 Zabbix 证据', analyzing: '模型分析中', stopping: '正在解除事务',
  stopped: '已停止', completed: '已完成', succeeded: '已完成', failed: '失败', unknown: '未知',
  skipped: '未执行', budget_exhausted: '模型额度不足', unavailable: '不可用', verified: '已核验',
};
const label = (value?: unknown) => labels[String(value)] ?? String(value ?? '未提供');
const errorLabels: Record<string, string> = {
  demo_cleanup_unconfirmed: '清理结果尚未确认，请停止本轮并等待核验',
  demo_monitoring_recovery_unconfirmed: '尚未取得 Zabbix 恢复证据',
  demo_controller_failed: '实验控制未完成，清理状态待核验',
  demo_fault_inactive: '锁等待已不再生效，等待清理确认',
  demo_analysis_unavailable: '本轮模型分析未成功，请查看证据与历史报告',
  MODEL_BUDGET_EXHAUSTED: '今日模型额度已用完',
  demo_stale_run: '此轮已结束，请刷新状态后再操作',
  demo_cooldown: '共享实验正在冷却', demo_daily_budget: '今日演示额度已用完',
  demo_disabled: '演示已暂停', demo_mapping_invalid: '固定实验监控映射不可用',
};
const errorLabel = (code: string) => `${errorLabels[code] ?? '本轮未完成，请核对实验与采集状态'}（${code}）`;
const secondsUntil = (date: string | null | undefined, now: number) => date && Number.isFinite(now) && Number.isFinite(Date.parse(date)) ? Math.max(0, Math.ceil((Date.parse(date) - now) / 1000)) : null;
export function DemoView({ data, error, actionError, receivedAt, elapsedSeconds = 0, busy = false, onToggle, onRefresh }: {
  data?: DemoSnapshot; error?: unknown; actionError?: unknown; receivedAt?: string; elapsedSeconds?: number; busy?: boolean;
  onToggle?: (start: boolean) => void; onRefresh?: () => void;
}) {
  const [reference, setReference] = useState<string>();
  const run = data?.run;
  const serverNow = data ? Date.parse(data.server_time) + elapsedSeconds * 1000 : NaN;
  const remaining = secondsUntil(run?.expires_at, serverNow);
  const cooldown = secondsUntil(data?.cooldown_until, serverNow);
  const terminal = !run || ['completed', 'stopped', 'failed', 'cancelled'].includes(run.status);
  // A terminal failed run with unknown cleanup is blocked, but its active transaction can still be stopped.
  const active = !!run && (run.fault_state === 'active' || ['requested', 'injecting', 'active', 'running', 'recovering', 'stopping', 'cleanup_unknown'].includes(run.status));
  const cleanupVerified = !!run?.cleanup && ['sessions_closed', 'locks_cleared', 'dataset_unchanged'].every(key => run.cleanup?.[key] === true);
  const unsafe = !!run && (['unknown', 'unconfirmed'].includes(run.fault_state) || ['cleanup_unknown', 'awaiting_recovery', 'recovery_unconfirmed'].includes(run.status) || (terminal && !cleanupVerified));
  const blocked = !data?.enabled || unsafe || !!cooldown || !data?.budget.remaining_runs || !terminal;
  const disabled = busy || (!active && (blocked || !!error));
  const currentReport = !!data?.report && !!run?.investigation_id && String(data.report.id) === run.investigation_id;
  return <div className="intel-page demo-page">
    <Title title="真实锁等待演示" description="真实隔离实验 · 合成业务负载 · 实际 Zabbix 观测" />
    <Alert type="info" showIcon title="全站共享同一轮实验" description="所有访客看到同一个固定 Lab；有控制权限的访客均可停止当前轮次。实验仅制造专用测试行的事务等待，服务端到期自动回滚。模型解释只使用授权的冻结证据。" />
    {!!error && <Alert type="warning" showIcon title={data ? '状态已过期：保留上次快照' : '暂时无法读取演示状态'} description={<><p>{error instanceof ApiError ? errorLabel(error.code) : '连接失败，请刷新重试。'}</p><p>上次成功读取：{receivedAt ?? '尚无成功快照'}；倒计时不代表解除已获确认。</p></>} action={<Button onClick={onRefresh}>重试</Button>} />}
    {!!actionError && <Alert type="error" showIcon title="操作未成功" description={actionError instanceof ApiError ? errorLabel(actionError.code) : '请求未完成，请核对当前状态后重试。'} />}
    {!data ? <LoadState loading={!error} /> : <>
      {!data.enabled && <Alert type="warning" showIcon title="演示未启用" description="此部署尚未配置固定实验；不能从浏览器选择目标或启用实验。" />}
      <div className="demo-grid">
        <Card title="固定实验对象">
          <h2>{data.target?.display_name ?? '实验对象未配置'}</h2>
          <Space wrap><Tag>Lab 隔离环境</Tag><Tag>MySQL 单行事务锁等待</Tag></Space>
          <p>业务负载为合成实验，锁等待、采样时间与分析结果来自实际运行；不代表生产环境。</p>
          <div className="demo-control"><div><strong>{active ? '停止本轮锁等待' : '开启固定锁等待'}</strong><p>{run?.status === 'completed' ? '本轮已结束，事务已解除并完成监控复验。' : run?.stop_requested ? '停止请求已提交，等待清理确认。' : active ? '关闭开关请求回滚本轮事务。' : '开启后等待实际证据和固定问题分析。'}</p></div>
            {data.can_control ? <Switch aria-label={active ? '停止本轮锁等待' : '开启固定锁等待'} checked={active} disabled={disabled} loading={busy} onChange={onToggle} checkedChildren="运行" unCheckedChildren="关闭" /> : <Tag>只读访问</Tag>}
          </div>
          {unsafe && <Alert type="warning" title="恢复尚未确认，禁止开始新一轮" />}
          {!active && !!cooldown && <p role="status">冷却中：约 {cooldown} 秒</p>}
          {!active && data.budget.remaining_runs === 0 && <p>UTC 当日实验额度已用尽。</p>}
          {data.budget.remaining_ai_runs === 0 && <p>UTC 当日模型额度已用尽；不会生成新的模型结论。</p>}
        </Card>
        <Card title="服务端限制与额度">
          <dl className="demo-facts"><dt>单轮故障最长</dt><dd>{data.limits.max_duration_seconds} 秒</dd><dt>结束后冷却</dt><dd>{data.limits.cooldown_seconds} 秒</dd><dt>UTC 当日实验</dt><dd>已用 {data.budget.used_runs} / {data.limits.max_daily_runs}，剩余 {data.budget.remaining_runs}</dd><dt>UTC 当日真实模型分析</dt><dd>已用 {data.budget.used_ai_runs} / {data.limits.max_daily_ai_runs}，剩余 {data.budget.remaining_ai_runs}</dd><dt>状态采样时间</dt><dd><Time value={data.server_time} /></dd></dl>
          <p>额度按全站共享计算。停止始终优先于冷却与额度；浏览器关闭不会延长故障期限。</p>
        </Card>
      </div>
      <Card title="本轮实际进度" className="demo-progress">
        {!run ? <p>尚未开始。开启实验后，此处只显示服务端已记录的里程碑。</p> : <>
          <Space wrap><Tag>{label(run.status)}</Tag><span>当前阶段：{run.phase === 'analyzing' && run.analysis_status === 'completed' ? '报告已生成，等待解除与复验' : run.phase === 'analyzing' && run.analysis_status === 'failed' ? '报告失败，等待解除与复验' : label(run.phase)}</span><span>故障：{label(run.fault_state)}</span></Space>
          <dl className="demo-facts"><dt>轮次</dt><dd>{run.id}</dd><dt>开始</dt><dd><Time value={run.started_at} /></dd><dt>自动解除期限</dt><dd><Time value={run.expires_at} />{active && remaining !== null && <span>（约 {remaining} 秒，等待服务端确认）</span>}</dd><dt>Zabbix 证据时间</dt><dd><Time value={run.observed_at} /></dd><dt>分析状态</dt><dd>{label(run.analysis_status)}</dd></dl>
          {run.error_code && <Alert type="warning" title={run.status === 'completed' && run.error_code === 'demo_fault_inactive' ? '锁等待已提前解除；本轮清理及监控复验已通过（demo_fault_inactive）' : errorLabel(run.error_code)} />}
          <ol className="demo-steps">{(run.steps ?? []).map(step => <li key={step.key}><strong>{step.label}</strong><Tag>{label(step.status)}</Tag><Time value={step.at} />{step.detail && <p>{step.detail}</p>}</li>)}</ol>
          {!run.steps?.length && <p>尚无服务端里程碑。</p>}
          <details className="intel-details"><summary>解除与复验记录</summary><p>直接清理核验与 Zabbix 恢复采样分别记录；缺失记录表示未知。</p>{run.cleanup && Object.keys(run.cleanup).length ? <dl className="demo-facts">{Object.entries(run.cleanup).map(([key, value]) => <div key={key}><dt>{label(key)}</dt><dd>{typeof value === 'boolean' ? value ? '是' : '否' : typeof value === 'string' || typeof value === 'number' ? String(value) : '见服务端核验记录'}</dd></div>)}</dl> : <p>尚未提供清理核验结果。</p>}</details>
        </>}
      </Card>
      <Card title={currentReport ? '本轮冻结报告' : data.report ? '历史报告' : '分析结论'}>
        {!currentReport && data.report && <Alert type="info" title="保留的历史报告，不代表本轮分析结果" description="本轮进度独立显示。下方结论仅属于报告标注的冻结窗口。" />}
        {data.report ? <><HistoricalInvestigationReport key={String(data.report.id)} data={data.report} onMetricReference={setReference} /><DemoReportCurves key={`${data.report.id}:${data.report.status}`} report={data.report} historical={!currentReport} /><FrozenMetricDrawer reference={reference} investigationId={String(data.report.id)} window={data.report.from && data.report.to ? { from: String(data.report.from), to: String(data.report.to) } : null} close={() => setReference(undefined)} /></> : <p>尚无可读取的模型报告。未取得新鲜非空锁快照、模型额度不足或分析失败时，不生成替代结论。</p>}
      </Card>
    </>}
  </div>;
}
export function Demo() {
  const can = useCan();
  const allowed = can('data.read');
  const [data, setData] = useState<DemoSnapshot>();
  const [error, setError] = useState<unknown>();
  const [actionError, setActionError] = useState<unknown>();
  const [receivedAt, setReceivedAt] = useState<string>();
  const [elapsedSeconds, setElapsed] = useState(0);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const anchor = useRef(0);
  const mounted = useRef(false);
  useEffect(() => {
    if (!allowed) return;
    mounted.current = true;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    async function poll() {
      try {
        const value = await api.get<DemoSnapshot>('/demo', controller.signal);
        if (stopped) return;
        setData(value); setError(undefined); setReceivedAt(new Date().toISOString()); anchor.current = Date.now(); setElapsed(0);
      } catch (e) { if (!stopped) setError(e); }
      finally { if (!stopped) timer = setTimeout(poll, 3000); }
    }
    void poll();
    const clock = setInterval(() => setElapsed(anchor.current ? Math.max(0, (Date.now() - anchor.current) / 1000) : 0), 1000);
    return () => { stopped = true; mounted.current = false; controller.abort(); clearTimeout(timer); clearInterval(clock); };
  }, [allowed, refresh]);
  async function toggle(start: boolean) {
    setBusy(true); setActionError(undefined);
    try { await api.post(start ? '/demo/start' : '/demo/stop', start ? {} : { run_id: data?.run?.id }); if (mounted.current) setRefresh(v => v + 1); }
    catch (e) { if (mounted.current) { setActionError(e); setRefresh(v => v + 1); } }
    finally { if (mounted.current) setBusy(false); }
  }
  if (!allowed) return <Alert type="warning" title="无演示读取权限" />;
  return <DemoView data={data} error={error} actionError={actionError} receivedAt={receivedAt} elapsedSeconds={elapsedSeconds} busy={busy} onToggle={toggle} onRefresh={() => setRefresh(v => v + 1)} />;
}





export function demoMetricPaths(report: Row): string[] {
  if (!Array.isArray(report.metric_refs)) return [];
  return [...new Set(report.metric_refs.slice(0, 18).flatMap((ref: Row) => {
    const path = frozenMetricPath(String(report.id), String(ref.id ?? `manifest:${ref.manifest_item_id}`));
    return path ? [path] : [];
  }))];
}
export function selectDemoCharts(charts: MetricChartData[]): MetricChartData[] {
  const available = charts.filter(chart => chartData(chart.points ?? []).some(([, value]) => value !== null));
  const selected: MetricChartData[] = [];
  const matchers = [/lock_waits|lock.*wait|锁等待/i, /host_cpu_percent|cpu|处理器/i, /questions_per_second|queries_per_second|queries|questions|qps|查询速率|每秒查询/i];
  for (const pattern of matchers) {
    const chart = available.find(item => !selected.includes(item) && pattern.test(`${item.metric_key ?? ''} ${item.title}`));
    if (chart) selected.push(chart);
  }
  return [...selected, ...available.filter(item => !selected.includes(item))].slice(0, 3);
}
export function DemoFrozenCharts({ charts, historical, loading = false, errors = 0 }: {
  charts: MetricChartData[]; historical: boolean; loading?: boolean; errors?: number;
}) {
  const selected = selectDemoCharts(charts);
  return <section className="demo-frozen-charts">
    <h3>{historical ? '历史报告冻结曲线' : '本轮报告冻结曲线'}</h3>
    <p>优先展示实际锁等待、CPU 与查询速率；缺少对应指标时展示其他可用曲线。并列观测不表示因果，曲线仅属于上方报告的冻结窗口。</p><p>结构快照与数值曲线来自不同监控项，采样时刻可能不同；曲线未包含故障点不代表快照中的锁等待不存在。</p>
    {loading && <p role="status">正在读取报告冻结指标…</p>}
    {!!errors && <Alert showIcon type="warning" title={`${errors} 项冻结指标读取失败或缺少有效窗口；已保留可读取的观测。`} />}
    {!loading && selected.length === 0 && <p>缺少有效观测，无法绘制冻结曲线；缺失数据不补零。</p>}
    {selected.length > 0 && <div className="demo-curves-grid">{selected.map(chart => <MetricPanel key={chart.id} data={chart} frozen />)}</div>}
    {!loading && selected.length > 0 && selected.length < 3 && <p>仅有 {selected.length} 条可用曲线，其余指标缺失或没有有效采样。</p>}
  </section>;
}
function DemoReportCurves({ report, historical }: { report: Row; historical: boolean }) {
  const [state, setState] = useState<{ charts: MetricChartData[]; errors: number; loading: boolean }>({ charts: [], errors: 0, loading: false });
  // The parent key contains ID + completion status. Three-second snapshots retain this mount.
  useEffect(() => {
    if (!['completed', 'succeeded'].includes(String(report.status))) return;
    const controller = new AbortController();
    const paths = demoMetricPaths(report);
    setState({ charts: [], errors: 0, loading: paths.length > 0 });
    void Promise.allSettled(paths.map(path => api.get<MetricChartData & { template?: string }>(path, controller.signal))).then(results => {
      if (controller.signal.aborted) return;
      const charts: MetricChartData[] = [];
      let errors = 0;
      for (const [index, result] of results.entries()) {
        if (result.status === 'rejected') { errors++; continue; }
        const sample = result.value;
        const window = sample.window ?? (report.from && report.to ? { from: String(report.from), to: String(report.to) } : null);
        if (!window?.from || !window?.to || !Array.isArray(sample.points)) { errors++; continue; }
        charts.push({ ...sample, id: paths[index], title: sample.title ?? sample.template ?? '未命名冻结指标', window,
          gaps: sample.gaps ?? (Array.isArray(sample.coverage?.gaps) ? sample.coverage.gaps.map((gap: unknown) => typeof gap === 'object' && gap !== null ? gap as Row : { reason: String(gap) }) : []),
        });
      }
      setState({ charts, errors, loading: false });
    });
    return () => controller.abort();
  }, []);
  if (!['completed', 'succeeded'].includes(String(report.status))) return <p>报告尚未完成，冻结曲线暂不可用。</p>;
  return <DemoFrozenCharts {...state} historical={historical} />;
}


