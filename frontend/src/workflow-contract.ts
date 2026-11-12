import type { Row } from './api';

// Only advertise filters implemented by the corresponding canonical endpoint.
export const workflowFilters: Record<string, { search?: string; statuses: string[] }> = {
  '/events': {
    search: '搜索事件标题或事件 ID',
    statuses: [
      'open',
      'acknowledged',
      'investigating',
      'pending_verification',
      'resolved',
      'closed',
      'merged',
    ],
  },
  '/alerts': { search: '搜索告警 ID 或规则版本', statuses: ['firing', 'recovered', 'unknown'] },
  '/investigations': {
    search: '搜索调查问题或调查 ID',
    statuses: [
      'queued',
      'preparing',
      'analyzing',
      'completed',
      'partial',
      'failed',
      'cancelled',
      'expired',
    ],
  },
  '/knowledge': {
    search: '搜索已授权知识',
    statuses: ['draft', 'in_review', 'approved', 'published', 'rejected', 'superseded'],
  },
  '/tasks': {
    statuses: [
      'queued',
      'retry_wait',
      'running',
      'succeeded',
      'partial',
      'failed',
      'cancel_requested',
      'cancelled',
      'expired',
    ],
  },
};

export function manualEventPayload(values: Row): Row {
  const { description, ...rest } = values;
  return { ...rest, reason: description };
}

export const evidenceSources = [
  { value: 'mysql.identity', label: 'MySQL 身份' },
  { value: 'mysql.global_status', label: 'MySQL 全局状态' },
  { value: 'mysql.transactions', label: '事务' },
  { value: 'mysql.lock_waits', label: '锁等待' },
  { value: 'mysql.sessions', label: '会话' },
  { value: 'mysql.statement_digest', label: 'SQL 摘要' },
  { value: 'mysql.tables', label: '表结构' },
  { value: 'mysql.columns', label: '列元数据' },
  { value: 'mysql.indexes', label: '索引元数据' },
  { value: 'mysql.configuration', label: '实例配置' },
  { value: 'mysql.replication', label: '复制状态' },
  { value: 'mysql.error_log', label: '错误日志' },
  { value: 'mysql.slow_log.table', label: '慢日志 · TABLE' },
  { value: 'mysql.slow_log.file', label: '慢日志 · FILE' },
  { value: 'linux.host', label: 'Linux 主机' },
  { value: 'mysql.explain_plan', label: '只读执行计划' },
];
