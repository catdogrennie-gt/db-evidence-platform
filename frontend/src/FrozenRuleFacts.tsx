import { Card } from 'antd';
import type { FrozenRuleFact } from './intelligenceModel';
import { StatementRefs } from './Presentation';
import { Time } from './ui';

export function FrozenRuleFacts({ facts, window, onMetricReference }: {
  facts?: FrozenRuleFact[];
  window: { from: string; to: string } | null;
  onMetricReference?: (reference: string) => void;
}) {
  const capacityFacts = Array.isArray(facts) ? facts.filter(fact => fact.kind === 'capacity_pressure') : [];
  if (!capacityFacts.length) return null;
  return <Card className="intel-frozen-rule-facts" title="报告窗口内已确认的风险">
    <p>冻结窗口 <Time value={window?.from} /> — <Time value={window?.to} />。以下为配置规则确认的观测事实；不代表当前状态，也不证明具体根因。</p>
    {capacityFacts.map((fact, index) => <section className="executive-claim" key={`${fact.instance_id}:${index}`}>
      <h3>{fact.title}</h3>
      <p>{fact.statement}</p>
      <p>观测时刻 <Time value={fact.observed_at} /></p>
      <StatementRefs data={fact.evidence_ids} onMetricReference={onMetricReference} />
      <details className="intel-details">
        <summary>配对观测 · {fact.calculation.sample_count} 组</summary>
        <p>配置关注水位 {fact.calculation.warning_percent}%；单位：{fact.calculation.unit || '未提供'}。</p>
        <ol>{fact.calculation.time_pairs.map((pair, pairIndex) => <li key={pairIndex}>
          <p>用量 {pair.used}，观测 <Time value={pair.used_at} />；容量 {pair.limit}，观测 <Time value={pair.limit_at} />。</p>
          <p>占用 {pair.utilization_percent}%，剩余 {pair.remaining} {fact.calculation.unit}。</p>
        </li>)}</ol>
      </details>
    </section>)}
  </Card>;
}
