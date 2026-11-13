import { Alert } from 'antd';
import { StatementRefs } from './Presentation';
import { Time } from './ui';

interface Observation { evidence_id: string; observed_at: string }
export interface ObservationBoundsData {
  scope: string;
  complete: boolean;
  window: { from: string; to: string };
  groups: Array<{ instance_id: string; instance_name?: string; source: string; sample_count: number; nonempty_sample_count: number;
    identity?: { source_id: string; hostid: string; itemid: string } | null;
    first_observation: Observation; last_observation: Observation; last_nonempty: Observation | null;
    first_empty_after_last_nonempty: Observation | null; gap_to_next_empty_seconds: number | null }>;
}
const names: Record<string, string> = { 'mysql.lock_waits': '锁等待', 'mysql.transactions': '事务', 'mysql.sessions': '会话' };

export function ObservationBounds({ data, onMetricReference }: { data?: ObservationBoundsData | null; onMetricReference?: (ref: string) => void }) {
  if (!data || data.scope !== 'frozen_manifest_point_observations' || (!data.groups.length && data.complete)) return null;
  return <Alert showIcon type="info" title="证据时间边界" description={<>
    <p>历史采样，不代表当前状态。报告窗口：<Time value={data.window.from} /> — <Time value={data.window.to} />。</p>
    {!data.complete && <p>部分冻结观测无法核实或未展示，不据此认定健康或恢复。</p>}
    {data.groups.map((group, i) => <section key={`${group.instance_id}:${group.source}:${i}`}>
      <strong>对象：{group.instance_name ?? group.instance_id}</strong>
      {group.identity && <small> · 来源 {group.identity.source_id} · Zabbix 主机 {group.identity.hostid} / 监控项 {group.identity.itemid}</small>}
      <p>{names[group.source] ?? '时点'}快照：保留 {group.sample_count} 个时点样本，{group.nonempty_sample_count} 个非空。首末采样：<Time value={group.first_observation.observed_at} /> — <Time value={group.last_observation.observed_at} />。</p>
      {group.last_nonempty && <p>最后非空：<Time value={group.last_nonempty.observed_at} />。
        {group.first_empty_after_last_nonempty ? <>后续首个空样本：<Time value={group.first_empty_after_last_nonempty.observed_at} />；样本相隔 {Number(group.gap_to_next_empty_seconds?.toFixed(3))} 秒，不代表持续时长。</> : '未保留同来源的后续空样本，无法据此确定是否恢复。'}</p>}
      <StatementRefs data={[group.first_observation.evidence_id, group.last_nonempty?.evidence_id, group.first_empty_after_last_nonempty?.evidence_id, group.last_observation.evidence_id].filter((ref): ref is string => !!ref).filter((ref, index, all) => all.indexOf(ref) === index)} onMetricReference={onMetricReference} />
    </section>)}
    <p>这些是冻结清单中的观测，不等于完整连续过程，也不证明恢复方式。下方状态由 AI 标注，未经过独立语义验证。</p>
  </>} />;
}
