import { useState } from 'react';
import { Alert, Card, Space } from 'antd';
import { useParams } from 'react-router-dom';
import type { Row } from './api';
import { historyPath } from './navigation';
import { useResource, useScope } from './hooks';
import { ContextLink, FieldGrid, LoadState, RecordCard, SourceCoverage, Status, Time, Title } from './ui';
import { Evidence } from './Workflow';
import { InvestigationReportView, ManifestButton, VerificationReportView, EventSummaryView } from './Presentation';
import { ExecutiveSummary } from './ExecutiveSummary';
import type { ObservationBoundsData } from './ObservationBounds';
import { ReportGrounding, type ReportGroundingData } from './ReportGrounding';
import { FrozenRuleFacts } from './FrozenRuleFacts';
import type { FrozenRuleFact } from './intelligenceModel';
import { FrozenMetricDrawer } from './Intelligence';

export function RetiredPage() {
  return <Card><Alert showIcon type="info" title="此历史页面已退役" description="此地址包含旧对象标识，不会跳转为当前数据。原始记录仍按服务端授权保留。" /><p><ContextLink to="/monitoring">打开 Zabbix 接入</ContextLink></p></Card>;
}

export function HistoricalInvestigationReport({ data, onMetricReference }: { data: Row; onMetricReference?: (reference: string) => void }) {
  const report = (data.report ?? data.report_json) as Row | undefined;
  return <>
    <div className="intel-summary-strip">
      <Space wrap><Status value={data.mode} /><Status value={data.status} /><span>冻结窗口 <Time value={data.from} /> — <Time value={data.to} /></span></Space>
    </div>
    <FrozenRuleFacts facts={data.frozen_facts as FrozenRuleFact[] | undefined} window={typeof data.from === 'string' && typeof data.to === 'string' ? { from: data.from, to: data.to } : null} onMetricReference={onMetricReference} />
    <ReportGrounding data={data.report_grounding as ReportGroundingData | undefined} onMetricReference={onMetricReference} />
    {!['completed', 'succeeded'].includes(String(data.status)) && <Alert type="warning" showIcon title="分析尚未成功完成" description="保留输出不能代表当前诊断已完成。" />}
    {!['completed', 'succeeded'].includes(String(data.status)) && !report?.executive_summary
      ? <Alert type="info" showIcon title="本轮未生成结论摘要" description="原始内容按原样保留；不根据事实列表补写模型结论。" />
      : <ExecutiveSummary report={report ?? null} mode={String(data.mode)} observationBounds={data.observation_bounds as ObservationBoundsData | undefined} onMetricReference={onMetricReference} />}
    <details className="intel-details"><summary>分析过程与全部原始引用</summary><InvestigationReportView data={report ?? data.summary} onMetricReference={onMetricReference} /></details>
    <details className="intel-details"><summary>完整问题、元数据与审计</summary>
      <section className="intel-text-items"><h3>完整分析问题</h3><p className="prose">{String(data.question ?? '原记录未提供分析问题。')}</p></section>
      <FieldGrid data={data} fields={[[ 'id', '历史 ID' ], ['model', '模型'], ['provider', '服务商'], ['created_at', '创建时间'], ['finished_at', '完成时间']]} />
      <SourceCoverage data={data} />
      <ManifestButton id={data.manifest_id ?? (data.manifest as Row | undefined)?.id} label="打开调查冻结清单" />
      <RecordCard title="历史调用与审计" data={{ model_calls: data.model_calls, tool_calls: data.tool_calls, reviews: data.reviews }} />
    </details>
  </>;
}

export function HistoricalPage({ kind }: { kind: string }) {
  const { id } = useParams();
  const { refresh } = useScope();
  const path = historyPath(kind, id);
  const resource = useResource<Row>(kind === 'evidence' ? null : path, refresh);
  const [reference, setReference] = useState<string>();
  if (!path) return <Alert type="error" title="历史标识无效" />;
  if (kind === 'evidence') return <Evidence />;
  const data = resource.data;
  return <div className="intel-page"><Title title={kind === 'investigations' ? '历史分析报告' : '历史记录'} description="只读查看授权的原始历史记录。" />
    <LoadState {...resource} retry={resource.reload}>{data && <>
      {kind === 'investigations' ? <>
        <HistoricalInvestigationReport data={data} onMetricReference={setReference} />
        <FrozenMetricDrawer reference={reference} investigationId={id!} window={data.from && data.to ? { from: String(data.from), to: String(data.to) } : null} close={() => setReference(undefined)} />
      </> : <>
        <Card><Space wrap><Status value={data.status} /><Status value={data.mode} /></Space><FieldGrid data={data} fields={[[ 'id', '历史 ID' ], ['from', '冻结窗口开始'], ['to', '冻结窗口结束'], ['created_at', '创建时间']]} /><SourceCoverage data={data} /></Card>
        {kind === 'verifications' ? <VerificationReportView data={data} /> : kind === 'events' ? <EventSummaryView data={data} /> : <RecordCard title="已授权的历史内容" data={data} />}
      </>}
    </>}</LoadState>
  </div>;
}
