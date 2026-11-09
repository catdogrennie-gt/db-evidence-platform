import { useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Collapse,
  Descriptions,
  Drawer,
  Empty,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd';
import { rows, text } from './api';
import type { Row } from './api';
import { useResource } from './hooks';
import {
  ContextLink,
  DataTable,
  FieldGrid,
  LoadState,
  RecordBlock,
  RelationshipGraph,
  SourceCoverage,
  Status,
  Time,
} from './ui';

const object = (value: unknown): Row =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Row) : {};
const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
type ResourceId = string & { readonly __resourceId: unique symbol };
const uuid = (value: unknown): value is ResourceId =>
  typeof value === 'string' &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
const numeric = (value: unknown, suffix = '') =>
  typeof value === 'number' && Number.isFinite(value)
    ? `${new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 3 }).format(value)}${suffix}`
    : '来源未返回';
const joined = (value: unknown) => strings(value).join('、') || '来源未返回';

function RawDetails({ data, label = '查看原始脱敏资料' }: { data: unknown; label?: string }) {
  return (
    <Collapse
      className="section"
      items={[{ key: 'raw', label, children: <RecordBlock data={data} /> }]}
    />
  );
}
function Prose({ value }: { value: unknown }) {
  if (typeof value !== 'string') return <RecordBlock data={value} />;
  return (
    <div className="article-body">
      {value
        .split(/\n\s*\n/)
        .filter(Boolean)
        .map((paragraph, index) => {
          const heading = paragraph.match(/^#{1,4}\s+([^\n]+)$/);
          return heading ? (
            <Typography.Title key={index} level={5}>
              {heading[1]}
            </Typography.Title>
          ) : (
            <Typography.Paragraph key={index} className="prose">
              {paragraph}
            </Typography.Paragraph>
          );
        })}
    </div>
  );
}

const accessNames: Record<string, string> = {
  system: '系统表常量',
  const: '常量定位',
  eq_ref: '唯一键关联',
  ref: '非唯一键定位',
  ref_or_null: '键定位（含空值）',
  index_merge: '索引合并',
  range: '索引范围扫描',
  index: '全索引扫描',
  ALL: '全表扫描',
};
function findTable(value: Row): Row | undefined {
  if (value.table && typeof value.table === 'object') return object(value.table);
  for (const key of [
    'query_block',
    'ordering_operation',
    'grouping_operation',
    'duplicates_removal',
  ]) {
    if (!value[key] || typeof value[key] !== 'object') continue;
    const found = findTable(object(value[key]));
    if (found) return found;
  }
  return undefined;
}
export function ExplainEvidenceView({ data }: { data: Row }) {
  const payload = object(data.payload ?? data.data ?? data.content);
  const plan = object(payload.plan);
  const table = findTable(plan) ?? {};
  const access = text(table.access_type, '');
  const coverage = object(data.coverage);
  const hasPlan = Object.keys(plan).length > 0;
  return (
    <Card title="只读执行计划摘要" className="section">
      <Alert
        type="info"
        showIcon
        title="优化器估计，业务查询未执行"
        description="此结果来自固定单表模板的 EXPLAIN。估算行数和成本不能证明真实耗时。索引名仅展示已采元数据允许的名称；未展示不能认定没有索引。"
        className="notice"
      />
      {hasPlan ? (
        <>
          <div className="plan-metrics">
            <div>
              <span>访问方式</span>
              <strong>{accessNames[access] ?? (access || '来源未返回')}</strong>
              {access && <code>{access}</code>}
            </div>
            <div>
              <span>每次扫描估算行数</span>
              <strong>{numeric(table.rows_examined_per_scan)}</strong>
            </div>
            <div>
              <span>使用索引</span>
              <strong>{text(table.key, '索引名称未展示')}</strong>
            </div>
            <div>
              <span>估算输出行数</span>
              <strong>{numeric(table.rows_produced_per_join)}</strong>
            </div>
          </div>
          {['ALL', 'index'].includes(access) && (
            <Alert
              type="warning"
              showIcon
              title={access === 'ALL' ? '计划采用全表扫描' : '计划采用全索引扫描'}
              description="是否需要优化应结合表规模、实际负载与目标窗口判断；本次计划只提供估计。"
              className="notice"
            />
          )}
          <Descriptions
            size="small"
            column={{ xs: 1, sm: 2 }}
            className="section"
            items={[
              {
                key: 'scope',
                label: '固定对象',
                children:
                  [payload.schema, payload.table]
                    .filter(Boolean)
                    .map((value) => text(value))
                    .join(' / ') || '来源未返回',
              },
              { key: 'keyparts', label: '使用键列', children: joined(table.used_key_parts) },
              {
                key: 'possible',
                label: '已采元数据内的候选索引',
                children: joined(table.possible_keys),
              },
              { key: 'filtered', label: '估算过滤比例', children: numeric(table.filtered, '%') },
              { key: 'columns', label: '投影列', children: joined(payload.projection) },
              {
                key: 'predicates',
                label: '等值条件列',
                children: joined(payload.predicate_columns),
              },
              { key: 'limit', label: '模板行数上限', children: numeric(payload.limit) },
              {
                key: 'time',
                label: 'SQL 阶段耗时',
                children: numeric(coverage.sql_duration_ms, ' ms'),
              },
              {
                key: 'metadata',
                label: '元数据依据',
                children: uuid(payload.metadata_evidence_id) ? (
                  <ContextLink to={`/evidence/${payload.metadata_evidence_id}`}>
                    查看当时列元数据
                  </ContextLink>
                ) : (
                  '来源未返回'
                ),
              },
              {
                key: 'protection',
                label: '参数保护',
                children: strings(payload.omitted_fields).includes('bound_constants')
                  ? '常量绑定执行，结果已移除常量与 SQL 正文'
                  : '请核对原始投影说明',
              },
            ]}
          />
        </>
      ) : (
        <Empty description="当前授权响应未提供执行计划数值" />
      )}
      <RawDetails data={payload} />
    </Card>
  );
}

export function StatementRefs({
  data,
  onMetricReference,
}: {
  data: unknown;
  onMetricReference?: (reference: string) => void;
}) {
  const refs = strings(data);
  return refs.length ? (
    <Space wrap size={[4, 4]} className="statement-refs">
      {refs.map((ref) =>
        uuid(ref) ? (
          <ContextLink key={ref} to={`/evidence/${ref}`}>
            <Tag>证据 {ref.slice(0, 8)}</Tag>
          </ContextLink>
        ) : ref.startsWith('manifest:') && onMetricReference ? (
          <Button key={ref} type="link" size="small" onClick={() => onMetricReference(ref)}>
            查看冻结时序证据 ↗
          </Button>
        ) : (
          <Tag key={ref}>
            {ref.startsWith('manifest:') ? '冻结指标 · ' + ref.slice(9, 17) : ref}
          </Tag>
        ),
      )}
    </Space>
  ) : null;
}
function Statements({
  title,
  data,
  empty,
  kind,
  onMetricReference,
}: {
  title: string;
  data: unknown;
  empty: string;
  kind?: string;
  onMetricReference?: (reference: string) => void;
}) {
  const entries = Array.isArray(data) ? data : typeof data === 'string' ? [data] : [];
  return (
    <section className="report-section">
      <Typography.Title level={5}>{title}</Typography.Title>
      {entries.length ? (
        <ol className="report-statements">
          {entries.map((value, index) => {
            const item = object(value);
            return (
              <li key={index}>
                {kind === 'finding' && (
                  <Tag>
                    {(
                      {
                        locks: '锁与事务',
                        sql: 'SQL',
                        host: '主机',
                        network: '网络',
                        correlation: '跨证据关联',
                      } as Record<string, string>
                    )[text(item.category)] ?? '未分类发现'}
                  </Tag>
                )}
                <Typography.Paragraph className="prose">
                  {typeof value === 'string'
                    ? value
                    : text(item.statement ?? item.description ?? item.summary)}
                </Typography.Paragraph>
                <StatementRefs data={item.evidence_ids} onMetricReference={onMetricReference} />
                {strings(item.refutations).length > 0 && (
                  <Typography.Paragraph type="secondary">
                    反证与限制：{strings(item.refutations).join('；')}
                  </Typography.Paragraph>
                )}
                {kind === 'hypothesis' && (
                  <Typography.Text type="secondary">候选解释，需人工核验。</Typography.Text>
                )}
                {kind === 'finding' && (
                  <Typography.Text type="secondary">
                    待人工核验，尚未证实；不代表根因或业务影响已确认。
                  </Typography.Text>
                )}
              </li>
            );
          })}
        </ol>
      ) : (
        <Typography.Paragraph type="secondary">{empty}</Typography.Paragraph>
      )}
    </section>
  );
}
export function InvestigationReportView({
  data,
  onMetricReference,
}: {
  data: unknown;
  onMetricReference?: (reference: string) => void;
}) {
  const report = object(data);
  if (!Object.keys(report).length)
    return (
      <Card title="调查报告">
        <Prose value={data ?? '报告尚未生成。'} />
      </Card>
    );
  return (
    <Card title="调查结论与依据">
      <Statements
        title="已观察事实"
        data={report.facts}
        empty="报告未返回已观察事实。"
        onMetricReference={onMetricReference}
      />
      {Array.isArray(report.findings) && report.findings.length > 0 && (
        <Statements
          title="待核验发现"
          data={report.findings}
          kind="finding"
          empty="报告未提出待核验发现。"
          onMetricReference={onMetricReference}
        />
      )}
      <Statements
        title="候选原因"
        data={report.hypotheses}
        kind="hypothesis"
        onMetricReference={onMetricReference}
        empty={
          Array.isArray(report.findings) && report.findings.length > 0
            ? '未另列候选原因，请结合上方待核验发现核对。'
            : '报告未提出候选原因。'
        }
      />
      <Statements
        title="未知项与资料限制"
        data={report.unknowns}
        onMetricReference={onMetricReference}
        empty="报告未单独列出未知项；仍需核对来源覆盖。"
      />
      {Array.isArray(report.refutations) && report.refutations.length > 0 && (
        <Statements
          title="反证"
          data={report.refutations}
          empty=""
          onMetricReference={onMetricReference}
        />
      )}
      <Statements
        title="建议的下一步只读检查"
        data={report.next_readonly_checks}
        onMetricReference={onMetricReference}
        empty="报告未返回下一步检查。"
      />
      <Typography.Paragraph type="secondary">
        {onMetricReference
          ? '点击段落中的冻结时序引用查看原始窗口；缺失采样保留断点。'
          : '冻结指标引用可在下方“引用证据”打开；报告不会将缺失采样补为零。'}
      </Typography.Paragraph>
      <RawDetails data={data} label="查看报告结构与版本资料" />
    </Card>
  );
}

export function ManifestButton({ id, label = '查看冻结清单' }: { id: unknown; label?: string }) {
  const [history, setHistory] = useState<string[]>([]);
  const [cursor, setCursor] = useState<string>();
  const selected = history.at(-1);
  const resource = useResource<Row>(
    selected
      ? `/evidence-manifests/${selected}?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`
      : null,
  );
  if (!uuid(id)) return null;
  function open(identifier: string) {
    setCursor(undefined);
    setHistory((items) => [...items, identifier]);
  }
  return (
    <>
      <Button
        size="small"
        onClick={() => {
          setCursor(undefined);
          setHistory([id]);
        }}
      >
        {label}
      </Button>
      <Drawer title="冻结证据清单" width={920} open={!!selected} onClose={() => setHistory([])}>
        <Alert
          type="info"
          showIcon
          title="保留创建时的窗口、覆盖与引用"
          description="每次打开都由服务端重新核验全部来源及所属对象权限。清单本身只显示安全元数据，不返回原始内容或敏感值。"
          className="notice"
        />
        {history.length > 1 && (
          <Button
            className="notice"
            onClick={() => {
              setCursor(undefined);
              setHistory((items) => items.slice(0, -1));
            }}
          >
            返回上一层清单
          </Button>
        )}
        <LoadState {...resource} retry={resource.reload}>
          {resource.data && (
            <>
              <FieldGrid
                data={resource.data}
                fields={[
                  ['id', '清单 ID'],
                  ['owner_type', '所属对象'],
                  ['version', '版本'],
                  ['manifest_sha256', '内容校验'],
                  ['retain_until', '保护截止'],
                ]}
              />
              <Descriptions
                size="small"
                className="section"
                items={[
                  {
                    key: 'window',
                    label: '冻结时间窗',
                    children: (
                      <>
                        <Time value={resource.data.window_start} /> —{' '}
                        <Time value={resource.data.window_end} />
                      </>
                    ),
                  },
                ]}
              />
              <SourceCoverage
                data={{
                  coverage: {
                    ...object(resource.data.coverage),
                    complete: object(resource.data.coverage).coverage_complete,
                  },
                }}
              />
              <Table<Row>
                size="small"
                className="section"
                rowKey={(item) => text(item.id)}
                dataSource={rows(resource.data.items)}
                pagination={false}
                scroll={{ x: 'max-content' }}
                columns={[
                  { key: 'kind', title: '资料类型', dataIndex: 'item_type' },
                  {
                    key: 'source',
                    title: '来源',
                    dataIndex: 'source',
                    render: (value) => text(value),
                  },
                  {
                    key: 'time',
                    title: '观察时间',
                    dataIndex: 'observed_at',
                    render: (value) => <Time value={value} />,
                  },
                  {
                    key: 'ref',
                    title: '引用',
                    render: (_, item) =>
                      uuid(item.evidence_id) ? (
                        <ContextLink to={`/evidence/${item.evidence_id}`}>
                          打开证据 {item.evidence_id.slice(0, 8)}
                        </ContextLink>
                      ) : uuid(item.reference_manifest_id) ? (
                        <Button
                          size="small"
                          onClick={() => open(item.reference_manifest_id as string)}
                        >
                          查看来源清单
                        </Button>
                      ) : (
                        <Typography.Text type="secondary">
                          {item.metric_run_id ? '冻结指标快照' : '安全元数据'}
                        </Typography.Text>
                      ),
                  },
                  {
                    key: 'hash',
                    title: '内容校验',
                    dataIndex: 'payload_sha256',
                    render: (value) => <span title={text(value)}>{text(value).slice(0, 16)}…</span>,
                  },
                ]}
              />
              <div className="pagination">
                <span>共 {text(resource.data.total)} 项</span>
                <Space>
                  {cursor && <Button onClick={() => setCursor(undefined)}>首页</Button>}
                  <Button
                    disabled={!resource.data.next_cursor}
                    onClick={() => setCursor(text(resource.data?.next_cursor, ''))}
                  >
                    下一页
                  </Button>
                </Space>
              </div>
            </>
          )}
        </LoadState>
      </Drawer>
    </>
  );
}

export function KnowledgeBodyView({ data }: { data: Row }) {
  const content = object(data.content ?? data.current_version);
  const refs = object(content.source_refs);
  const investigations = strings(refs.source_investigation_ids);
  const manifests = [
    ...new Set([
      ...strings(content.source_manifest_ids),
      ...(uuid(content.manifest_id) ? [content.manifest_id] : []),
    ]),
  ];
  const hasSources =
    uuid(refs.source_event_id) ||
    uuid(refs.verification_id) ||
    investigations.some(uuid) ||
    manifests.some(uuid);
  return (
    <>
      <Card title="知识正文" className="section">
        {content.acceptance_case === true && content.user_human_reviewed === false && (
          <Alert
            type="warning"
            showIcon
            title="受控验收案例 · 自动化验收账号审核，待用户人工审阅"
            className="notice"
          />
        )}
        <Prose value={content.body ?? content.summary ?? data.content ?? data.current_version} />
        {strings(data.tags).length > 0 && (
          <Space wrap className="section">
            {strings(data.tags).map((tag) => (
              <Tag key={tag}>{tag}</Tag>
            ))}
          </Space>
        )}
        <RawDetails data={content} label="查看当前版本的结构化记录" />
      </Card>
      <Card title="来源与适用范围" className="section">
        {hasSources ? (
          <Space orientation="vertical" size={14} style={{ width: '100%' }}>
            {uuid(refs.source_event_id) && (
              <div>
                <Typography.Text type="secondary">原始事件　</Typography.Text>
                <ContextLink to={`/events/${refs.source_event_id}`}>
                  查看事件及其处理时间线
                </ContextLink>
              </div>
            )}
            {uuid(refs.verification_id) && (
              <div>
                <Typography.Text type="secondary">确定性复验　</Typography.Text>
                <ContextLink to={`/verifications/${refs.verification_id}`}>
                  查看前后窗口、条件与结果
                </ContextLink>
              </div>
            )}
            {investigations.filter(uuid).map((identifier) => (
              <div key={identifier}>
                <Typography.Text type="secondary">关联调查　</Typography.Text>
                <ContextLink to={`/investigations/${identifier}`}>
                  查看报告与冻结依据 · {identifier.slice(0, 8)}
                </ContextLink>
              </div>
            ))}
            {investigations.some(uuid) && (
              <Alert
                type="info"
                showIcon
                title="关联调查有独立的冻结时间窗"
                description="关联可用于同类问题参考。请核对调查起止时间与本事件窗口，不能仅凭关联认定同窗证据或因果关系。"
              />
            )}
            {manifests.some(uuid) && (
              <div>
                <Typography.Text type="secondary">保留清单　</Typography.Text>
                <Space wrap>
                  {manifests.map((identifier, index) => (
                    <ManifestButton
                      key={identifier}
                      id={identifier}
                      label={`清单 ${index + 1} · ${identifier.slice(0, 8)}`}
                    />
                  ))}
                </Space>
              </div>
            )}
          </Space>
        ) : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="当前版本未登记事件、复验或调查来源"
          />
        )}
        <Typography.Paragraph type="secondary" className="section">
          链接保留当前页面的时间与范围上下文；目标对象仍按自身冻结窗口解释。知识继承全部来源实例的访问限制。
        </Typography.Paragraph>
      </Card>
    </>
  );
}
export function KnowledgeVersions({ data }: { data: Row }) {
  return (
    <Card title="版本与审核记录" className="section">
      <Table<Row>
        size="small"
        rowKey={(item) => text(item.version)}
        dataSource={[...rows(data.versions)].reverse()}
        pagination={false}
        scroll={{ x: 'max-content' }}
        expandable={{
          expandedRowRender: (item) => (
            <>
              <Descriptions
                size="small"
                items={[
                  {
                    key: 'note',
                    label: '审核意见',
                    children: text(item.review_note, '尚无审核意见'),
                  },
                ]}
                className="notice"
              />
              <Prose value={object(item.content).body ?? item.content} />
              <RawDetails data={item} label="查看此版本完整脱敏记录" />
            </>
          ),
        }}
        columns={[
          {
            key: 'version',
            title: '版本',
            dataIndex: 'version',
            render: (value) => `v${text(value)}`,
          },
          {
            key: 'status',
            title: '状态',
            dataIndex: 'status',
            render: (value) => <Status value={value} />,
          },
          { key: 'engine', title: '数据库', dataIndex: 'engine', render: (value) => text(value) },
          {
            key: 'created',
            title: '创建时间',
            dataIndex: 'created_at',
            render: (value) => <Time value={value} />,
          },
          {
            key: 'reviewed',
            title: '审核时间',
            dataIndex: 'reviewed_at',
            render: (value) => <Time value={value} />,
          },
          {
            key: 'reviewer',
            title: '审核者',
            dataIndex: 'reviewer_id',
            render: (value) => text(value, '尚未审核'),
          },
        ]}
      />
    </Card>
  );
}

export function InstanceReference({ id, instances = [] }: { id: unknown; instances?: Row[] }) {
  const identifier = typeof id === 'string' ? id : '';
  const instance = instances.find((item) => item.id === identifier);
  return identifier ? (
    <ContextLink to={`/assets/${encodeURIComponent(identifier)}/overview`}>
      <span title={identifier}>{text(instance?.name, identifier.slice(0, 8))}</span>
    </ContextLink>
  ) : (
    <span>来源未返回</span>
  );
}

export function EventListTable({
  items,
  instances = [],
  kind,
}: {
  items: Row[];
  instances?: Row[];
  kind: 'events' | 'alerts';
}) {
  const alerts = kind === 'alerts';
  return (
    <DataTable
      items={items}
      columns={[
        { key: 'severity', title: '级别', render: (value) => <Status value={value} /> },
        {
          key: alerts ? 'id' : 'title',
          title: alerts ? '原始告警' : '事件',
          render: (value, row) =>
            alerts ? (
              <span title={text(row.id)}>{text(row.id).slice(0, 12)}…</span>
            ) : (
              <ContextLink to={`/events/${row.id}`}>{text(value)}</ContextLink>
            ),
        },
        {
          key: 'instance_id',
          title: '实例',
          render: (value) => <InstanceReference id={value} instances={instances} />,
        },
        { key: 'status', title: '状态', render: (value) => <Status value={value} /> },
        ...(alerts
          ? [
              {
                key: 'evidence',
                title: '规则版本',
                render: (value: unknown) => text(object(value).rule_version, '来源未返回'),
              },
              {
                key: 'first_seen_at',
                title: '首次触发',
                render: (value: unknown) => <Time value={value} />,
              },
              {
                key: 'last_seen_at',
                title: '最近观察',
                render: (value: unknown) => <Time value={value} />,
              },
              {
                key: 'recovered_at',
                title: '恢复观察',
                render: (value: unknown) => (value ? <Time value={value} /> : '尚无恢复观察'),
              },
            ]
          : [
              {
                key: 'assigned_to',
                title: '负责人',
                render: (value: unknown) =>
                  typeof value === 'string' ? <span title={value}>{value}</span> : '尚未分派',
              },
              {
                key: 'created_at',
                title: '发生时间',
                render: (value: unknown) => <Time value={value} />,
              },
              { key: 'version', title: '事件版本' },
            ]),
      ]}
    />
  );
}

const verificationReasons: Record<string, string> = {
  insufficient_observations: '样本不足：前后窗口各需达到最少观测次数',
  source_gap_or_truncated: '来源缺口或采集截断',
  identity_changed: '实例身份发生变化',
  coverage_gap: '冻结资料覆盖不完整',
  manifest_truncated: '冻结清单被截断',
  event_coverage_changed: '事件覆盖版本已变化',
  frozen_manifest_missing: '缺少冻结清单',
  baseline_wait_chain_missing: '前窗没有可验证的等待链基线',
  incomparable_execution_rate: '前后调用率不具可比性',
  insufficient_executions: 'SQL 执行次数不足',
  invalid_window: '窗口无效',
  schema_changed: 'Schema 不一致',
  digest_changed: 'SQL 摘要不一致',
  counter_epoch_changed: '累计计数周期发生变化',
  server_uuid_changed: '实例身份不一致',
  zero_baseline: '基线为零，无法计算改善比例',
  invalid_latency_tolerance: '耗时阈值无效',
  digest_missing_or_evicted: 'SQL 摘要缺失或已被淘汰',
  counter_decreased: '累计计数下降，不能直接比较',
};
export function VerificationReportView({ data }: { data: Row }) {
  const result = object(data.result);
  const criteria = object(data.criteria);
  const load = object(result.load_context ?? criteria.load_context);
  const outcome = data.outcome ?? result.outcome;
  const reasons = strings(result.reasons);
  const template = text(data.template, '');
  const sql = template === 'sql_latency';
  const minimum =
    typeof criteria.minimum_observations === 'number' ? criteria.minimum_observations : undefined;
  const state =
    typeof outcome === 'string' && ['improved', 'not_improved', 'inconclusive'].includes(outcome)
      ? outcome
      : undefined;
  return (
    <>
      <Card>
        <Space wrap>
          <Status value={data.status} />
          {state ? <Status value={state} /> : <Tag>结论尚未生成</Tag>}
          {uuid(data.event_id) && (
            <ContextLink to={`/events/${data.event_id}`}>打开关联事件</ContextLink>
          )}
        </Space>
        <div className="section">
          <FieldGrid
            data={data}
            fields={[
              ['id', '复验 ID'],
              ['created_at', '发起时间'],
              ['template_version', '模板版本'],
              ['event_version', '发起时事件版本'],
            ]}
          />
        </div>
        <Typography.Paragraph className="section">
          检查范围：
          {text(
            result.conclusion_scope,
            sql ? '同一 SQL 摘要在可比调用率窗口中的平均耗时变化。' : '所观测的等待链是否消失。',
          )}
        </Typography.Paragraph>
        <Alert
          type={state === 'inconclusive' ? 'warning' : 'info'}
          showIcon
          title="改善不证明业务恢复或处置因果"
          description="结论仅适用于本次模板、固定窗口和所选资料；来源缺失、样本不足或负载不可比会保留资料不足。"
        />
      </Card>
      <Card title={sql ? 'SQL 摘要前后比较' : '等待链前后比较'} className="section">
        <div className="plan-metrics">
          <div>
            <span>前窗观测数</span>
            <strong>{numeric(result.before_observations)}</strong>
          </div>
          <div>
            <span>后窗观测数</span>
            <strong>{numeric(result.after_observations)}</strong>
          </div>
          {sql ? (
            <>
              <div>
                <span>前窗平均耗时</span>
                <strong>
                  {typeof result.before_avg_seconds === 'number'
                    ? numeric(result.before_avg_seconds * 1000, ' ms')
                    : '来源未返回'}
                </strong>
              </div>
              <div>
                <span>后窗平均耗时</span>
                <strong>
                  {typeof result.after_avg_seconds === 'number'
                    ? numeric(result.after_avg_seconds * 1000, ' ms')
                    : '来源未返回'}
                </strong>
              </div>
            </>
          ) : (
            <div>
              <span>选定等待链数</span>
              <strong>{numeric(result.selected_chain_count)}</strong>
            </div>
          )}
        </div>
        {minimum !== undefined && (
          <Typography.Paragraph className="section">
            每个窗口至少 {minimum} 次观测
            {sql
              ? `；每个窗口至少 ${text(criteria.minimum_executions, '来源未返回')} 次执行。`
              : '。'}
          </Typography.Paragraph>
        )}
        {sql && (
          <Descriptions
            size="small"
            className="section"
            items={[
              {
                key: 'rate',
                label: '调用率比（后 / 前）',
                children: numeric(result.execution_rate_ratio),
              },
              {
                key: 'latency',
                label: '平均耗时比（后 / 前）',
                children: numeric(result.latency_ratio),
              },
              { key: 'schema', label: 'Schema', children: text(criteria.schema, '未限定') },
              {
                key: 'digest',
                label: '固定 SQL 摘要',
                children: text(criteria.digest, '来源未返回'),
              },
            ]}
          />
        )}
        <div className="section">
          <FieldGrid
            data={data}
            fields={[
              ['before_start', '前窗开始'],
              ['before_end', '前窗结束'],
              ['after_start', '后窗开始'],
              ['after_end', '后窗结束'],
            ]}
          />
        </div>
      </Card>
      <Card title="判定依据与限制" className="section">
        {reasons.length ? (
          <ul>
            {reasons.map((reason, index) => (
              <li key={`${reason}-${index}`}>{verificationReasons[reason] ?? reason}</li>
            ))}
          </ul>
        ) : (
          <Typography.Paragraph>
            {state ? '本次判定未记录额外拒绝原因。' : '复验尚未产生最终判定，请稍后刷新。'}
          </Typography.Paragraph>
        )}
        <Descriptions
          size="small"
          items={[
            {
              key: 'coverage',
              label: '发起时告警覆盖版本',
              children: text(criteria.coverage_revision ?? result.coverage_revision, '来源未返回'),
            },
            {
              key: 'load',
              label: '负载可比依据',
              children:
                load.kind === 'measured'
                  ? '机器观测'
                  : load.kind === 'human_assertion'
                    ? '人工负载说明（未经机器独立验证）'
                    : '未知',
            },
            { key: 'description', label: '负载说明', children: text(load.description, '未提供') },
          ]}
        />
        {strings(result.limitations).map((item, index) => (
          <Typography.Paragraph type="secondary" key={index}>
            {item}
          </Typography.Paragraph>
        ))}
        {data.manifest_id ? (
          <div className="section">
            <ManifestButton id={data.manifest_id} label="打开复验冻结清单" />
          </div>
        ) : (
          <Typography.Paragraph type="secondary" className="section">
            当前复验未返回冻结清单引用。
          </Typography.Paragraph>
        )}
        <RawDetails
          data={{ criteria, result, plan_id: data.plan_id, manifest_id: data.manifest_id }}
          label="查看判定条件与完整脱敏结果"
        />
      </Card>
    </>
  );
}

export function TopologyView({ data }: { data: Row }) {
  const nodes = rows(data.nodes);
  const edges = rows(data.edges);
  const observed = edges.filter(
    (edge) => edge.type === 'observed_replication' && edge.source_mode === 'live',
  );
  const manual = edges.filter((edge) => edge.type === 'verified_host_association');
  const observedIds = new Set(observed.flatMap((edge) => [text(edge.source), text(edge.target)]));
  const names = new Map(nodes.map((node) => [text(node.id), text(node.name ?? node.id)]));
  return (
    <>
      <Card title="配置归属 · 部门 / 系统 / 实例">
        <Typography.Paragraph type="secondary">
          以下来自登记配置，不作为数据库连接、复制或健康观测证据。
        </Typography.Paragraph>
        <div className="topology-tree">
          {rows(data.departments).map((department) => (
            <div className="topology-department" key={text(department.id)}>
              <strong>{text(department.name)}</strong>
              {rows(department.systems).map((system) => (
                <div className="topology-system" key={text(system.id)}>
                  <Tag>配置归属</Tag>
                  <strong>{text(system.name)}</strong>
                  <div className="topology-instances">
                    {rows(system.instances).map((instance) => (
                      <ContextLink key={text(instance.id)} to={`/assets/${instance.id}/overview`}>
                        <div className="topology-node">
                          <span className="node-icon">▤</span>
                          <strong>{text(instance.name)}</strong>
                          <Status value={instance.health} />
                        </div>
                      </ContextLink>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          ))}
        </div>
        {!rows(data.departments).length && <Empty description="当前授权范围内没有配置归属资料" />}
      </Card>
      {manual.length > 0 && (
        <Card title="已核验的主机关联 · 人工配置" className="section">
          <DataTable
            items={manual}
            columns={[
              {
                key: 'source',
                title: '主机',
                render: (value) => names.get(text(value)) ?? text(value),
              },
              {
                key: 'target',
                title: '实例',
                render: (value) => <InstanceReference id={value} instances={nodes} />,
              },
              { key: 'source_mode', title: '依据', render: () => '人工核验登记' },
            ]}
          />
        </Card>
      )}
      <Card title="实际观测 · 复制关系" className="section">
        {observed.length ? (
          <>
            <Alert
              type="info"
              showIcon
              title="仅依据当前窗口的副本侧观测"
              description="上游可能未纳管或无权查看；这些关系不能证明拓扑完整或主机归属。"
              className="notice"
            />
            <RelationshipGraph
              nodes={nodes.filter((node) => observedIds.has(text(node.id)))}
              edges={observed}
            />
            <DataTable
              items={observed}
              columns={[
                {
                  key: 'source',
                  title: '观察实例',
                  render: (value) => <InstanceReference id={value} instances={nodes} />,
                },
                {
                  key: 'target',
                  title: '上游',
                  render: (value) => names.get(text(value)) ?? '未纳管或无权查看的上游',
                },
                {
                  key: 'observed_at',
                  title: '观察时间',
                  render: (value) => <Time value={value} />,
                },
                {
                  key: 'evidence_id',
                  title: '来源证据',
                  render: (value) =>
                    uuid(value) ? (
                      <ContextLink to={`/evidence/${value}`}>打开复制观测证据</ContextLink>
                    ) : (
                      '来源未返回'
                    ),
                },
              ]}
            />
          </>
        ) : (
          <Alert
            type="info"
            showIcon
            title="当前窗口没有已观察的复制关系"
            description="关系保持未知。未配置、未纳管、无权限或资料缺失均不能推断不存在复制关系。"
          />
        )}
      </Card>
    </>
  );
}

function EvidenceReferences({ values }: { values: unknown[] }) {
  const ids = [...new Set(values.flatMap((value) => strings(value)).filter(uuid))];
  return ids.length ? (
    <Space wrap>
      {ids.map((id) => (
        <ContextLink key={id} to={`/evidence/${id}`}>
          <Tag>证据 {id.slice(0, 8)}</Tag>
        </ContextLink>
      ))}
    </Space>
  ) : (
    <Typography.Text type="secondary">本次观察未返回直接证据引用</Typography.Text>
  );
}
function ObservationView({ data }: { data: Row }) {
  return (
    <Space orientation="vertical" size={2}>
      {data.source_observed_at ? (
        <Time value={data.source_observed_at} />
      ) : (
        <Typography.Text type="secondary">观察时间未返回</Typography.Text>
      )}
      {typeof data.observed_count === 'number' && (
        <span>等待链 {numeric(data.observed_count)} 条</span>
      )}
      {typeof data.count === 'number' && <span>窗口执行 {numeric(data.count)} 次</span>}
      {typeof data.avg_seconds === 'number' && (
        <span>平均耗时 {numeric(data.avg_seconds * 1000, ' ms')}</span>
      )}
      {typeof data.age_seconds === 'number' && (
        <span>距成功观察 {numeric(data.age_seconds, ' 秒')}</span>
      )}
      {data.recovery_observation === true && <Tag color="success">本次观察满足恢复条件</Tag>}
    </Space>
  );
}
function EventManifest({ id }: { id: unknown }) {
  const resource = useResource<Row>(uuid(id) ? `/evidence-manifests/${id}?limit=1` : null);
  return (
    <>
      <ManifestButton id={id} label="打开事件当前冻结清单" />
      <div className="section">
        <LoadState {...resource} retry={resource.reload}>
          {resource.data && (
            <>
              <FieldGrid
                data={resource.data}
                fields={[
                  ['window_start', '冻结窗口开始'],
                  ['window_end', '冻结窗口结束'],
                  ['version', '清单版本'],
                  ['total', '保留引用数'],
                ]}
              />
              <Typography.Paragraph type="secondary" className="section">
                清单固定该次事件版本的引用；告警最近观察可继续变化，不能以当前值覆盖历史触发事实。
              </Typography.Paragraph>
            </>
          )}
        </LoadState>
      </div>
    </>
  );
}
export function EventSummaryView({ data }: { data: Row }) {
  const episodes = rows(data.episodes);
  const manual = rows(data.timeline).find((item) => item.action === 'manual_create');
  const reason = object(manual?.details).reason;
  return (
    <>
      <Card title="事件事实与业务影响" className="section">
        <Descriptions
          size="small"
          items={[
            { key: 'source', label: '记录来源', children: <Status value={data.source} /> },
            {
              key: 'impact',
              label: '业务影响',
              children: text(data.impact, '未记录业务影响；告警事实不能直接证明业务损失。'),
            },
            ...(reason ? [{ key: 'reason', label: '人工观察说明', children: text(reason) }] : []),
            ...(data.closure_reason
              ? [{ key: 'closure', label: '结案说明', children: text(data.closure_reason) }]
              : []),
          ]}
        />
        {data.status === 'merged' && uuid(data.merged_into_event_id) && (
          <div className="section">
            <ContextLink to={`/events/${data.merged_into_event_id}`}>
              打开归并后的目标事件
            </ContextLink>
          </div>
        )}
      </Card>
      <Card title={`原始告警 · ${episodes.length} 个 episode`} className="section">
        <Alert
          type="info"
          showIcon
          title="首次触发是历史事实，最近观察会随持续采集更新"
          description="规则恢复、事件人工处理和复验结论分别留存；事件关闭不会改写原始告警状态。"
          className="notice"
        />
        <Table<Row>
          size="small"
          rowKey={(row) => text(row.id)}
          dataSource={episodes}
          pagination={false}
          scroll={{ x: 'max-content' }}
          columns={[
            {
              key: 'status',
              dataIndex: 'status',
              title: '原始状态',
              render: (value) => <Status value={value} />,
            },
            {
              key: 'severity',
              dataIndex: 'severity',
              title: '级别',
              render: (value) => <Status value={value} />,
            },
            {
              key: 'rule',
              title: '规则版本',
              render: (_, row) => text(object(row.evidence).rule_version, '来源未返回'),
            },
            {
              key: 'trigger',
              title: '首次触发观察（固定）',
              render: (_, row) => <ObservationView data={object(object(row.evidence).trigger)} />,
            },
            {
              key: 'latest',
              title: '最近规则观察（持续更新）',
              render: (_, row) => <ObservationView data={object(object(row.evidence).latest)} />,
            },
            {
              key: 'first_seen_at',
              dataIndex: 'first_seen_at',
              title: '首次触发时间',
              render: (value) => <Time value={value} />,
            },
            {
              key: 'last_seen_at',
              dataIndex: 'last_seen_at',
              title: '最近有效条件观察',
              render: (value) => <Time value={value} />,
            },
            {
              key: 'recovered_at',
              dataIndex: 'recovered_at',
              title: '恢复观察时间',
              render: (value) => (value ? <Time value={value} /> : '尚无恢复观察'),
            },
          ]}
          expandable={{
            expandedRowRender: (row) => {
              const evidence = object(row.evidence);
              return (
                <>
                  <EvidenceReferences
                    values={[
                      object(evidence.trigger).evidence_ids,
                      object(evidence.latest).evidence_ids,
                      evidence.recovery_evidence_ids,
                    ]}
                  />
                  <RawDetails data={row} label="查看此告警的完整脱敏记录" />
                </>
              );
            },
          }}
        />
        {episodes.map((episode) => {
          const evidence = object(episode.evidence);
          return (
            <div className="section" key={text(episode.id)}>
              <Typography.Text type="secondary">
                告警 {text(episode.id).slice(0, 8)} 的证据　
              </Typography.Text>
              <EvidenceReferences
                values={[
                  object(evidence.trigger).evidence_ids,
                  object(evidence.latest).evidence_ids,
                  evidence.recovery_evidence_ids,
                ]}
              />
            </div>
          );
        })}
      </Card>
      <Card title="事件版本的冻结依据" className="section">
        {uuid(data.manifest_id) ? (
          <EventManifest id={data.manifest_id} />
        ) : (
          <Typography.Paragraph type="secondary">
            此事件当前版本未返回冻结清单引用。
          </Typography.Paragraph>
        )}
      </Card>
      <Card title="应用与人工变更 · human_assertion" className="section">
        <DataTable
          items={rows(data.manual_changes)}
          columns={[
            { key: 'event_at', title: '实际发生时间', render: (value) => <Time value={value} /> },
            { key: 'description', title: '脱敏说明' },
            { key: 'source', title: '来源', render: (value) => <Status value={value} /> },
          ]}
        />
      </Card>
    </>
  );
}

export function RemediationPlanView({ data }: { data: Row }) {
  return (
    <>
      <Space wrap>
        <Status value={data.status} />
        <Tag>计划 v{text(data.version)}</Tag>
        <Typography.Text type="secondary">仅保存人工步骤；平台不执行这些操作。</Typography.Text>
      </Space>
      <Typography.Title level={5}>人工步骤</Typography.Title>
      {strings(data.steps).length ? (
        <ol>
          {strings(data.steps).map((step, index) => (
            <li key={index}>{step}</li>
          ))}
        </ol>
      ) : (
        <Typography.Paragraph type="secondary">此版本未提供步骤。</Typography.Paragraph>
      )}
      <Collapse
        className="section"
        items={[
          {
            key: 'conditions',
            label: '适用条件、风险与回退',
            children: (
              <>
                {[
                  ['prerequisites', '适用条件'],
                  ['risks', '风险'],
                  ['rollback', '回退方法'],
                ].map(([key, label]) => (
                  <div key={key}>
                    <Typography.Title level={5}>{label}</Typography.Title>
                    {strings(data[key]).length ? (
                      <ul>
                        {strings(data[key]).map((item, index) => (
                          <li key={index}>{item}</li>
                        ))}
                      </ul>
                    ) : (
                      <Typography.Text type="secondary">未记录</Typography.Text>
                    )}
                  </div>
                ))}
              </>
            ),
          },
        ]}
      />
      <Typography.Title level={5}>已登记的人工执行</Typography.Title>
      <DataTable
        items={rows(data.execution_records)}
        columns={[
          { key: 'executed_at', title: '实际执行时间', render: (value) => <Time value={value} /> },
          { key: 'executed_by', title: '登记账号' },
          {
            key: 'actual_result',
            title: '人工结果说明',
            render: (value) =>
              text(
                object(value).result ?? object(value).description ?? object(value).action,
                '已登记结构化记录，详情见下方脱敏记录',
              ),
          },
          { key: 'source', title: '来源', render: (value) => <Status value={value} /> },
        ]}
      />
      <RawDetails data={data} label="查看此版本完整脱敏计划与执行记录" />
    </>
  );
}

const collectorNames: Record<string, string> = {
  identity_status: '身份与状态',
  runtime: '运行与锁事务',
  statement_digest: 'SQL 摘要',
  schema_config: '结构与配置',
  replication: '复制基础',
  error_log: '错误日志',
  slow_log: '慢日志',
  host: '主机',
};
export function SourceHealthSummary({ data }: { data: Row }) {
  const sources = rows(data.source_health);
  const state = (source: Row) =>
    source.reason_code === 'not_applicable'
      ? 'not_applicable'
      : source.up === true
        ? 'available'
        : (source.availability ?? 'unknown');
  return (
    <div className="section">
      <Typography.Title level={5}>各来源当前新鲜度</Typography.Title>
      {sources.length ? (
        <>
          <Space wrap>
            {sources.map((source) => (
              <span key={text(source.collector)}>
                <Typography.Text>
                  {collectorNames[text(source.collector)] ?? text(source.collector)}　
                </Typography.Text>
                <Status value={state(source)} />
              </span>
            ))}
          </Space>
          <Collapse
            className="section"
            items={[
              {
                key: 'sources',
                label: '查看来源周期、最近成功与当前尝试',
                children: (
                  <DataTable
                    items={sources}
                    columns={[
                      {
                        key: 'collector',
                        title: '来源',
                        render: (value) => collectorNames[text(value)] ?? text(value),
                      },
                      {
                        key: 'availability',
                        title: '当前资料',
                        render: (_, row) => <Status value={state(row)} />,
                      },
                      {
                        key: 'interval_seconds',
                        title: '配置周期（秒）',
                        render: (value) => numeric(value),
                      },
                      {
                        key: 'last_success_at',
                        title: '最近成功观察',
                        render: (value) => (value ? <Time value={value} /> : '尚无成功观察'),
                      },
                      {
                        key: 'sample_age_seconds',
                        title: '距成功观察（秒）',
                        render: (value) => numeric(value),
                      },
                      {
                        key: 'freshness_limit_seconds',
                        title: '新鲜度上限（秒）',
                        render: (value) => numeric(value),
                      },
                      {
                        key: 'latest_attempt_status',
                        title: '最近尝试',
                        render: (value, row) => (
                          <Space>
                            <Status value={value} />
                            {row.attempt_in_progress === true && <Tag>正在采集</Tag>}
                          </Space>
                        ),
                      },
                      {
                        key: 'reason_code',
                        title: '资料说明',
                        render: (value) => text(value, '无额外缺口'),
                      },
                    ]}
                  />
                ),
              },
            ]}
          />
        </>
      ) : (
        <Alert
          type="info"
          title="当前实例尚无来源新鲜度记录"
          description="没有记录不能推断为健康，请检查已配置采集类别与真实任务。"
        />
      )}
    </div>
  );
}

const windowReasonNames: Record<string, string> = {
  insufficient_observations: '观察次数不足',
  source_gap: '来源覆盖缺口',
  coverage_gap: '采集间隔存在缺口',
  source_gap_or_truncated: '来源缺口或截断',
  counter_decreased: '累计计数下降',
  counter_epoch_changed: '计数周期发生变化',
  identity_changed: '实例身份发生变化',
  digest_missing: 'SQL 摘要缺失',
  digest_missing_or_evicted: 'SQL 摘要缺失或淘汰',
  row_missing: '中间快照缺少摘要',
  truncated: '来源被截断',
  snapshot_limit: '超过快照预算',
  invalid_window: '窗口无效',
};
export function QueryWindowSummary({ data }: { data: Row }) {
  return (
    <div className="section">
      <Alert
        type="info"
        showIcon
        title="区间差分使用所采端点，不等同于精确请求边界"
        description="仅累加请求窗口内可靠相邻快照；重置、身份变化、缺帧或资料不足时，区间次数与耗时保持未知。累计扫描行数是摘要累计值。"
        className="notice"
      />
      {data.comparison_from && data.comparison_to ? (
        <FieldGrid
          data={data}
          fields={[
            ['requested_from', '请求窗口开始'],
            ['requested_to', '请求窗口结束'],
            ['comparison_from', '实际对照端点开始'],
            ['comparison_to', '实际对照端点结束'],
            ['observations', '参与观察数'],
          ]}
        />
      ) : (
        <Typography.Paragraph type="secondary">
          服务端尚未返回可对照的采样端点；请核对当前资料覆盖。
        </Typography.Paragraph>
      )}
      {rows(data.comparison_evidence_refs).length > 0 && (
        <div className="section">
          <Typography.Text type="secondary">对照端点证据　</Typography.Text>
          <Space wrap>
            {rows(data.comparison_evidence_refs).map((ref, index) =>
              uuid(ref.id) ? (
                <ContextLink key={`${ref.id}-${index}`} to={`/evidence/${ref.id}`}>
                  端点 {index + 1} · <Time value={ref.frame_observed_at ?? ref.observed_at} />
                </ContextLink>
              ) : null,
            )}
          </Space>
        </div>
      )}
    </div>
  );
}
export function QueryValidity({ validity, reasons }: { validity: unknown; reasons: unknown }) {
  return (
    <Space orientation="vertical" size={2}>
      <Tag color={validity === 'valid' ? 'success' : 'warning'}>
        {validity === 'valid' ? '区间可比较' : '区间未知'}
      </Tag>
      {strings(reasons).map((reason) => (
        <Typography.Text type="secondary" key={reason}>
          {windowReasonNames[reason] ?? reason}
        </Typography.Text>
      ))}
    </Space>
  );
}

const connectionGuidance: Record<string, string> = {
  authentication_failed:
    '只读账号认证未通过。请核对账号、密码及该账号允许的连接来源，然后重新预检。',
  tls_required: 'TLS 配置不完整。请选用服务端已配置 CA 的 TLS 配置；不能通过忽略证书验证继续。',
  invalid_tls_mode: 'TLS 模式无效。请选择平台支持的身份验证模式或已明确允许的隔离模式。',
  tls_verification_failed: 'TLS 证书验证未通过。请管理员核对 CA、证书身份与服务端 TLS 配置。',
  permission_denied: '只读账号缺少所需权限。请核对预检步骤和最小授权要求，补齐后重新预检。',
  connection_refused: '目标拒绝连接。请核对目标端口和数据库监听状态。',
  connection_timeout: '连接超时。请核对目标可达性及已允许的网络入口。',
};
export function ConnectionTestView({ data }: { data: Row }) {
  const code = text(data.error_code, '');
  const steps = rows(data.steps);
  const passed = data.status === 'succeeded';
  return (
    <div className="section">
      <FieldGrid
        data={data}
        fields={[
          ['status', '预检结果'],
          ['server_uuid', '数据库身份'],
          ['server_version', '版本'],
          ['expires_at', '测试到期'],
        ]}
      />
      {!passed && (
        <Alert
          type="error"
          showIcon
          title={code ? `预检未通过 · ${code}` : '预检未通过'}
          description={
            connectionGuidance[code] ??
            '请按下列安全错误码核对失败步骤，修正配置后重新预检。当前结果不能用于登记或重验。'
          }
          className="notice"
        />
      )}
      {steps.length > 0 && (
        <div className="section">
          <DataTable
            items={steps}
            columns={[
              {
                key: 'step',
                title: '预检步骤',
                render: (value) =>
                  (
                    ({
                      database_identity: '数据库身份与认证',
                      identity: '数据库身份',
                      network: '网络连接',
                      dns_tcp_ssh: 'DNS / TCP / SSH',
                    }) as Record<string, string>
                  )[text(value)] ??
                  collectorNames[text(value)] ??
                  text(value),
              },
              { key: 'status', title: '实际结果', render: (value) => <Status value={value} /> },
              { key: 'error_code', title: '安全错误码', render: (value) => text(value, '无') },
            ]}
          />
        </div>
      )}
    </div>
  );
}
