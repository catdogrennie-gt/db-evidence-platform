import { Alert, Card } from 'antd';
import { StatementRefs } from './Presentation';
import { Time } from './ui';

export interface ReportGroundingData {
  version: string;
  status: string;
  verified_headline: string | null;
  reason_codes: string[];
  model_narrative_verification: string;
  not_established: string[];
  admitted_facts: Array<{
    kind: string; fact_id: string; instance_id: string; observed_at: string;
    row_count: number; count_scope: string;
    source_identity: { source_id: string; hostid: string; itemid: string; mapping_version: number; config_revision: number };
  }>;
}

export function ReportGrounding({ data, onMetricReference }: { data?: ReportGroundingData | null; onMetricReference?: (ref: string) => void }) {
  if (!data) return null;
  const supported = data.version === 'report-grounding-v1';
  if (supported && data.reason_codes.length === 1 && data.reason_codes[0] === 'no_completed_live_model_report') return null;
  const facts = supported && data.status === 'point_facts_only' ? data.admitted_facts : [];
  if (!facts.length) return <Alert showIcon type="info" title="未取得可核实的时点事实" description={<>
    <p>{supported && data.reason_codes.includes('actual_delivery_receipt_missing') ? '旧报告缺少本次分析的实际输入凭据。' : '当前报告尚未满足来源、冻结证据与实际分析输入的核对条件。'}这不表示数据库正常，也不表示 AI 提出的候选问题已被否定。</p>
    <p>AI 解释尚未核实，仍保留在下方供人工核对。</p>
  </>} />;
  return <Card title="已核实的历史观测" className="report-grounding">
    {data.verified_headline && <h2>{data.verified_headline}</h2>}
    <p>以下 {facts.length} 个时点已核对来源、冻结证据和本次 AI 实际收到的数据。仅证明这些采样时点的记录；不能据此确认持续时长、发生次数、解除原因或当前状态。</p>
    <details className="intel-details"><summary>查看时点、来源与证据</summary>
      {facts.map(fact => <section key={fact.fact_id}>
        <p><Time value={fact.observed_at} /> · 对象 {fact.instance_id} · {fact.count_scope === 'at_least_projected_rows' ? '至少 ' : ''}{fact.row_count} 条锁等待记录</p>
        <small>来源 {fact.source_identity.source_id} · Zabbix 主机 {fact.source_identity.hostid} / 监控项 {fact.source_identity.itemid} · 配置版本 {fact.source_identity.config_revision}</small>
        <StatementRefs data={[fact.fact_id]} onMetricReference={onMetricReference} />
      </section>)}
    </details>
    <p className="intel-muted">AI 解释尚未核实。下方原因判断和建议保留模型原文，需结合证据人工核对。</p>
  </Card>;
}
