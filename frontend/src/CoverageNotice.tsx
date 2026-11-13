import { Alert, List, Typography } from 'antd';
import { ContextLink, Time } from './ui';

export interface CoverageGap {
  instance_id: string;
  object_name: string;
  mapping_id: string;
  host_name: string;
  itemid: string;
  item_name: string;
  reason: string;
  last_sample_at?: string | null;
}
export interface CoverageSummary {
  status: string;
  missing_count: number;
  items?: CoverageGap[];
  truncated?: boolean;
  checked_at?: string;
}
const reasons: Record<string, [string, string]> = {
  item_missing: ['监控项已不可见', '请维护者检查 Zabbix 监控项是否已删除或移出授权范围，再更新对象选择。'],
  item_disabled: ['监控项已停用', '请维护者核对是否有意停用；需要分析此项时，在 Zabbix 恢复采集。'],
  item_unsupported: ['监控项读取失败', '请维护者在 Zabbix 最新数据中检查该项错误、Agent 连通性与采集权限。'],
  no_samples: ['尚无有效样本', '先检查上游是否启用并产生真实数据，再等待首个采样；平台不能补出未采集的信息。'],
  sample_stale: ['样本已过期', '请维护者检查上游采样与平台取数是否仍运行；恢复后等待新样本，不用旧值当成正常。'],
  sample_time_in_future: ['采样时间异常', '请维护者核对 Zabbix 与采集主机时钟，修正后重新核对样本时间。'],
  sampling_interval_unverified: ['采样间隔尚未确认', '请维护者核对采样周期；按变化保留历史的项目需配置可验证的心跳，不能用空窗判断健康。'],
};

export function CoverageNotice({ coverage }: { coverage: CoverageSummary }) {
  if (!coverage.items?.length) return null;
  return <Alert className="intel-gap-banner" type="warning" showIcon
    title={`当前采集有 ${coverage.missing_count} 项需要核对`}
    description={<>
      <p>以下是当前采集状态，不是所选历史窗口的完整性结论；相关信息暂不能作为可靠诊断依据。</p>
      <details className="intel-details">
        <summary>查看缺失明细</summary>
        <List dataSource={coverage.items} pagination={coverage.items.length > 10 ? { pageSize: 10, showSizeChanger: false } : false}
          renderItem={gap => {
            const [label, remedy] = reasons[gap.reason] ?? ['采集状态待核对', '请维护者检查此监控项的采样状态与权限。'];
            return <List.Item key={`${gap.instance_id}:${gap.itemid}`}>
              <div>
                <Typography.Text strong>{gap.object_name} · {gap.item_name}</Typography.Text>
                <p>{gap.host_name} · {label} · 最近样本 <Time value={gap.last_sample_at} /></p>
                <p>建议：{remedy}</p>
                <ContextLink to={`/metrics?instance_id=${encodeURIComponent(gap.instance_id)}`}>查看该对象的数据 →</ContextLink>
              </div>
            </List.Item>;
          }} />
        {coverage.truncated && <p>当前展示前 {coverage.items.length} 项；请按业务或对象筛选后继续核对。</p>}
      </details>
    </>} />;
}
