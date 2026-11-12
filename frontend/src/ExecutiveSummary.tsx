import { Alert, Card, Tag } from 'antd';
import type { Row } from './api';
import { StatementRefs } from './Presentation';

export interface SummaryClaim {
  status: 'observed' | 'supported_hypothesis' | 'insufficient_evidence' | 'not_applicable' | 'refuted';
  statement: string;
  evidence_ids: string[];
  counterevidence_ids: string[];
  unknowns: string[];
}
export interface SummaryAction {
  kind: 'readonly_check' | 'manual_change' | 'observe';
  description: string;
  evidence_ids: string[];
  risk: 'low' | 'medium' | 'high' | 'unknown';
  requires_human_approval: boolean;
  prerequisites: string[];
  verification: string[];
}
export interface ExecutiveSummaryData {
  headline: string;
  phenomenon: SummaryClaim;
  surface_causes: SummaryClaim[];
  joint_root_cause: SummaryClaim;
  conclusion: SummaryClaim;
  recommended_actions: SummaryAction[];
}
const statuses = { observed: '已观察', supported_hypothesis: '有依据的候选解释', insufficient_evidence: '证据不足', not_applicable: '不适用', refuted: '已有反证' };
const risks = { low: '低风险', medium: '中风险', high: '高风险', unknown: '风险未知' };
function ClaimStatus({ claim }: { claim: SummaryClaim }) {
  return <Tag color={claim.status === 'observed' ? 'blue' : claim.status === 'supported_hypothesis' ? 'gold' : 'default'}>{statuses[claim.status] ?? '状态未知'}</Tag>;
}
function ActionBadges({ action }: { action: SummaryAction }) {
  return <div className="executive-action-badges"><Tag>{{ readonly_check: '只读检查', manual_change: '人工变更建议', observe: '继续观察' }[action.kind]}</Tag><Tag color={action.risk === 'high' ? 'red' : action.risk === 'medium' ? 'gold' : 'default'}>{risks[action.risk] ?? '风险未知'}</Tag>{(action.requires_human_approval || action.kind === 'manual_change') && <Tag color="orange">需要人工批准</Tag>}</div>;
}
function Claim({ title, claim, onMetricReference }: { title: string; claim: SummaryClaim; onMetricReference?: (ref: string) => void }) {
  return <section className="executive-claim"><h3>{title}</h3><ClaimStatus claim={claim} /><p>{claim.statement}</p>
    <StatementRefs data={claim.evidence_ids} onMetricReference={onMetricReference} />
    {!!claim.counterevidence_ids?.length && <div className="executive-counter"><small>反证引用</small><StatementRefs data={claim.counterevidence_ids} onMetricReference={onMetricReference} /></div>}
    {!!claim.unknowns?.length && <p className="intel-muted">尚未确认：{claim.unknowns.join('；')}</p>}
  </section>;
}
export function ExecutiveSummary({ report, onMetricReference, mode }: { report: Row | null; onMetricReference?: (ref: string) => void; mode?: string }) {
  const summary = report?.executive_summary as ExecutiveSummaryData | undefined;
  if (!summary || mode === 'rules_only') return <Alert showIcon type="info" title={mode === 'rules_only' ? '规则报告未生成模型执行摘要' : report ? '旧版未生成完整结论摘要' : '报告尚未生成执行摘要'} description="原始内容按原样保留；不根据事实列表补写模型结论。" />;
  return <Card className="executive-summary" title="AI分析">
    <p className="intel-muted">以下为 AI 对原因的解释与建议，不替代已确认事实；原始判断按原文保留。</p>
    <h2>{summary.headline}</h2>
    <div className="executive-verdict"><strong>AI判断</strong><ClaimStatus claim={summary.conclusion} /><p>{summary.conclusion.statement}</p></div>
    <div className="executive-questions">
      <section className="executive-column"><h3>发生了什么</h3><ClaimStatus claim={summary.phenomenon} /><p>{summary.phenomenon.statement}</p></section>
      <section className="executive-column"><h3>为什么</h3><h4>各层原因</h4>
        {summary.surface_causes.length ? <ul className="executive-cause-list">{summary.surface_causes.map((claim, i) => <li key={i}><ClaimStatus claim={claim} /><p>{claim.statement}</p></li>)}</ul> : <p className="intel-muted">未提出独立表层原因。</p>}
        <div className="executive-root-status"><strong>联合根因候选</strong><ClaimStatus claim={summary.joint_root_cause} /><p>{summary.joint_root_cause.statement}</p></div>
      </section>
      <section className="executive-column"><h3>现在怎么做</h3>
        {summary.recommended_actions.length ? <ol className="executive-action-list">{summary.recommended_actions.map((action, i) => <li key={i}><p>{action.description}</p><ActionBadges action={action} /></li>)}</ol> : <p className="intel-muted">未提出建议操作。</p>}
        <small>建议需人工核对；风险标签不代表操作授权。</small>
      </section>
    </div>
    <details className="executive-details"><summary>分层原因、操作前提与证据引用</summary>
      <Claim title="发生了什么" claim={summary.phenomenon} onMetricReference={onMetricReference} />
      {summary.surface_causes.map((claim, i) => <Claim key={i} title={`第 ${i + 1} 层原因`} claim={claim} onMetricReference={onMetricReference} />)}
      <Claim title="联合根因候选" claim={summary.joint_root_cause} onMetricReference={onMetricReference} />
      <Claim title="AI判断" claim={summary.conclusion} onMetricReference={onMetricReference} />
      <section className="executive-actions"><h3>建议操作与完整前提</h3><ol>{summary.recommended_actions.map((action, i) => <li key={i}><p>{action.description}</p><ActionBadges action={action} /><StatementRefs data={action.evidence_ids} onMetricReference={onMetricReference} />{!!action.prerequisites?.length && <p>前提条件：{action.prerequisites.join('；')}</p>}{!!action.verification?.length && <p>复验方法：{action.verification.join('；')}</p>}</li>)}</ol></section>
    </details>
  </Card>;
}
