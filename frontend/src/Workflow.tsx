import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Timeline,
  Typography,
} from 'antd';
import { useParams, useSearchParams } from 'react-router-dom';
import { api, awaitTask, pageItems, rows, terminal, text } from './api';
import type { Instance, Page, Row, TaskRef } from './api';
import {
  useCan,
  useCursor,
  useResource,
  useScope,
  useScopedPath,
  useScopedNavigate,
} from './hooks';
import {
  ContextLink,
  DataTable,
  ErrorNotice,
  FieldGrid,
  LoadState,
  RecordCard,
  SourceCoverage,
  Status,
  TaskProgress,
  Time,
  Title,
} from './ui';
import { Pagination } from './Inventory';
import {
  ExplainEvidenceView,
  InvestigationReportView,
  KnowledgeBodyView,
  KnowledgeVersions,
  ManifestButton,
  EventListTable,
  VerificationReportView,
  EventSummaryView,
  RemediationPlanView,
} from './Presentation';
import { evidenceSources, manualEventPayload, workflowFilters } from './workflow-contract';
type Column = { key: string; title: string; render?: (value: unknown, row: Row) => ReactNode };
function ExportAction({ resource }: { resource: string }) {
  const can = useCan();
  const { scope } = useScope();
  const navigate = useScopedNavigate();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [task, setTask] = useState<TaskRef>();
  async function submit(values: Row) {
    setBusy(true);
    setError(undefined);
    try {
      const ref = await api.post<TaskRef>(
        '/exports',
        { ...values, resource, format: 'json' },
        true,
      );
      const done = await awaitTask(ref, setTask);
      setOpen(false);
      navigate(`/tasks/${done.id}`);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  if (!can('export.create')) return null;
  return (
    <>
      <Button
        onClick={() => {
          if (!form.getFieldValue('from'))
            form.setFieldsValue({
              from: scope.from,
              to: scope.to,
              instance_ids: scope.instance_id ? [scope.instance_id] : [],
            });
          setOpen(true);
        }}
      >
        导出授权资料
      </Button>
      <Modal
        title="生成私密 JSON 资料快照"
        open={open}
        onCancel={() => setOpen(false)}
        confirmLoading={busy}
        onOk={() => form.submit()}
      >
        <ErrorNotice error={error} />
        <TaskProgress task={task} />
        <Alert
          type="info"
          showIcon
          title="生成与下载均重新核验全部实例权限"
          description="文件保留24小时，最多5000条和16MiB；截断与来源缺口会留存在文件中。"
          className="notice"
        />
        <Form form={form} layout="vertical" onFinish={submit}>
          <InstancePicker multiple />
          <DateField name="from" label="起始时间（UTC）" />
          <DateField name="to" label="结束时间（UTC）" />
        </Form>
      </Modal>
    </>
  );
}
export function ResourceList({
  title,
  description,
  endpoint,
  columns,
  actions,
  filters,
  extra = {},
  renderItems,
}: {
  title: string;
  description: string;
  endpoint: string;
  columns: Column[];
  actions?: ReactNode;
  filters?: ReactNode;
  extra?: Row;
  renderItems?: (items: Row[]) => ReactNode;
}) {
  const { refresh } = useScope();
  const [q, setQ] = useState('');
  const [cursor, setCursor] = useCursor();
  const [status, setStatus] = useState<string>();
  const contract = workflowFilters[endpoint];
  const filterKey = JSON.stringify(extra);
  useEffect(() => setCursor(undefined), [filterKey, setCursor]);
  const resource = useResource<Page>(
    useScopedPath(endpoint, {
      ...extra,
      ...(contract?.search ? { q } : {}),
      cursor,
      ...(contract?.statuses.length ? { status } : {}),
    }),
    refresh,
  );
  return (
    <>
      <Title
        title={title}
        description={description}
        actions={
          <>
            {actions}
            {['/evidence', '/events', '/investigations'].includes(endpoint) && (
              <ExportAction resource={endpoint.slice(1)} />
            )}
          </>
        }
      />
      <Card>
        <div className="table-toolbar">
          <Space wrap>
            {contract?.search && (
              <Input.Search
                allowClear
                onSearch={(value) => {
                  setQ(value);
                  setCursor(undefined);
                }}
                placeholder={contract.search}
                style={{ width: 250 }}
              />
            )}
            {!!contract?.statuses.length && (
              <Select
                allowClear
                placeholder="全部状态"
                style={{ width: 160 }}
                onChange={(value) => {
                  setStatus(value);
                  setCursor(undefined);
                }}
                options={contract.statuses.map((value) => ({
                  value,
                  label: <Status value={value} />,
                }))}
              />
            )}
            {filters}
          </Space>
          <Button onClick={resource.reload}>刷新列表</Button>
        </div>
        <LoadState {...resource} retry={resource.reload}>
          {renderItems ? (
            renderItems(pageItems(resource.data))
          ) : (
            <DataTable items={pageItems(resource.data)} columns={columns} />
          )}
          <Pagination data={resource.data} setCursor={setCursor} />
        </LoadState>
      </Card>
    </>
  );
}
export function Evidence() {
  const { id } = useParams();
  const { refresh } = useScope();
  const [source, setSource] = useState<string>();
  const detail = useResource<Row>(id ? `/evidence/${id}` : null, refresh);
  if (id)
    return (
      <>
        <Title
          title="历史证据"
          description="观察时间、入库时间和来源模式均来自原始证据封套。"
          actions={
            <ContextLink to="/evidence">
              <Button>返回查询</Button>
            </ContextLink>
          }
        />
        <LoadState {...detail} retry={detail.reload}>
          {detail.data && (
            <>
              <Card>
                <SourceCoverage data={detail.data} />
                <FieldGrid
                  data={detail.data}
                  fields={[
                    ['id', '证据 ID'],
                    ['instance_id', '来源实例'],
                    ['run_id', '采集批次'],
                    ['source', '来源'],
                    ['schema_version', '结构版本'],
                    ['sensitivity', '资料级别'],
                  ]}
                />
              </Card>
              <>
                {detail.data.source === 'mysql.explain_plan' ? (
                  <ExplainEvidenceView data={detail.data} />
                ) : (
                  <RecordCard
                    title="已授权的证据内容"
                    data={detail.data.payload ?? detail.data.data ?? detail.data.content}
                  />
                )}
              </>
            </>
          )}
        </LoadState>
      </>
    );
  return (
    <ResourceList
      title="证据查询"
      description="查询已采集的资料。时间窗按观察时间过滤，迟到与覆盖缺口保留。"
      endpoint="/evidence"
      extra={{ source }}
      filters={
        <Select
          allowClear
          placeholder="全部来源"
          onChange={setSource}
          style={{ width: 170 }}
          options={evidenceSources}
        />
      }
      columns={[
        {
          key: 'id',
          title: '证据',
          render: (v) => <ContextLink to={`/evidence/${v}`}>{text(v).slice(0, 12)}…</ContextLink>,
        },
        { key: 'instance_name', title: '实例' },
        { key: 'source', title: '来源' },
        { key: 'source_mode', title: '资料模式', render: (v) => <Status value={v} /> },
        { key: 'observed_at', title: '观察时间', render: (v) => <Time value={v} /> },
        { key: 'ingested_at', title: '入库时间', render: (v) => <Time value={v} /> },
        { key: 'sensitivity', title: '资料级别' },
      ]}
    />
  );
}
export function Events() {
  const can = useCan();
  const { refresh } = useScope();
  const instances = useResource<Page<Instance>>('/instances?limit=100', refresh);
  const [tab, setTab] = useState<'events' | 'alerts'>('events');
  const [create, setCreate] = useState(false);
  const [revision, setRevision] = useState(0);
  return (
    <>
      <Tabs
        activeKey={tab}
        onChange={(value) => setTab(value as 'events' | 'alerts')}
        items={[
          { key: 'events', label: '事件工作台' },
          { key: 'alerts', label: '原始告警 episodes' },
        ]}
      />
      <ResourceList
        key={tab + revision}
        title={tab === 'events' ? '事件中心' : '原始告警'}
        description={
          tab === 'events'
            ? '规则事实与人工处理状态分别保留；规则与复验详情在事件中核对。'
            : '告警是独立原始对象，人工关闭事件不会改写告警恢复事实。'
        }
        endpoint={tab === 'events' ? '/events' : '/alerts'}
        actions={
          tab === 'events' &&
          can('event.create') && (
            <Button type="primary" onClick={() => setCreate(true)}>
              ＋ 人工创建事件
            </Button>
          )
        }
        columns={[]}
        renderItems={(items) => (
          <EventListTable items={items} instances={pageItems(instances.data)} kind={tab} />
        )}
      />
      <CommandModal
        title="登记人工事件"
        open={create}
        onClose={() => setCreate(false)}
        endpoint="/events"
        transform={manualEventPayload}
        fields={
          <>
            <InstancePicker />
            <Required name="title" label="事件标题" />
            <Form.Item name="severity" label="严重度" initialValue="medium">
              <Select
                options={['critical', 'high', 'medium', 'low'].map((value) => ({
                  value,
                  label: <Status value={value} />,
                }))}
              />
            </Form.Item>
            <Required name="description" label="观察与影响说明" area />
            <Alert type="info" title="此记录标记为 human_assertion，不作为机器采集事实。" />
          </>
        }
        onSuccess={() => {
          setRevision((value) => value + 1);
          setCreate(false);
        }}
      />
    </>
  );
}
export function EventInbox() {
  const { refresh, scope } = useScope();
  const [cursor, setCursor] = useCursor();
  const resource = useResource<Row>(useScopedPath('/events/inbox', { limit: 50, cursor }), refresh);
  return (
    <>
      <Title
        title="未确认事件收件箱"
        description="按现在计算授权范围内尚未确认的事件；打开页面不会自动确认。维护期间保留事件与证据，提醒单独抑制。"
        actions={
          <ContextLink to="/events">
            <Button>全部事件</Button>
          </ContextLink>
        }
      />
      <LoadState {...resource} retry={resource.reload}>
        {resource.data && (
          <>
            <Card>
              <Space wrap>
                <Tag>{text(resource.data.total)} 个待确认</Tag>
                <Tag>{text(resource.data.suppressed_count)} 个维护中抑制</Tag>
                <Typography.Text type="secondary">
                  汇总时间 <Time value={resource.data.as_of} />
                </Typography.Text>
              </Space>
              <div className="section">
                <DataTable
                  items={rows(resource.data.items)}
                  columns={[
                    { key: 'severity', title: '级别', render: (value) => <Status value={value} /> },
                    {
                      key: 'title',
                      title: '事件',
                      render: (value, row) => (
                        <ContextLink to={`/events/${row.id}`}>{text(value)}</ContextLink>
                      ),
                    },
                    { key: 'instance_name', title: '实例' },
                    {
                      key: 'created_at',
                      title: '发生时间',
                      render: (value) => <Time value={value} />,
                    },
                    { key: 'version', title: '当前版本' },
                  ]}
                />
              </div>
              <Pagination data={resource.data as unknown as Page} setCursor={setCursor} />
            </Card>
            {Number(resource.data.suppressed_count) > 0 && (
              <Card title="维护抑制说明" className="section">
                <Alert
                  type="info"
                  title="当前维护窗口抑制提醒，到期后自动回到待确认列表。"
                  description="异常和采集事实继续保留；抑制不代表事件已恢复或已关闭。"
                />
                <div className="section">
                  <DataTable
                    items={rows(resource.data.suppressed)}
                    columns={[
                      {
                        key: 'title',
                        title: '事件',
                        render: (value, row) => (
                          <ContextLink to={`/events/${row.event_id}`}>{text(value)}</ContextLink>
                        ),
                      },
                      { key: 'reason', title: '原因', render: () => '当前维护窗口' },
                    ]}
                  />
                </div>
              </Card>
            )}
            <Typography.Paragraph type="secondary" className="section">
              此收件箱不发送邮件；需要拥有确认权限的操作者在事件详情中明确确认。
              {scope.instance_id ? '当前已按选定实例筛选。' : ''}
            </Typography.Paragraph>
          </>
        )}
      </LoadState>
    </>
  );
}
export function EventDetail() {
  const { id } = useParams();
  const { refresh } = useScope();
  const can = useCan();
  const resource = useResource<Row>(`/events/${id}`, refresh);
  const data = resource.data;
  const [command, setCommand] = useState('');
  const [plansVersion, setPlansVersion] = useState(0);
  const plans = useResource<Page>(
    id ? `/events/${id}/remediation-plans` : null,
    plansVersion + refresh,
  );
  const [plan, setPlan] = useState<Row>();
  const [error, setError] = useState<unknown>();
  const [task, setTask] = useState<TaskRef>();
  const [busy, setBusy] = useState(false);
  const [form] = Form.useForm();
  const [tab, setTab] = useState('summary');
  const { message } = App.useApp();
  const version = data?.version ?? data?.revision;
  const active = !!data && !['closed', 'merged'].includes(text(data.status));
  const mergeOptions = useResource<Page>(
    command === 'merge' && data?.instance_id
      ? `/events?instance_id=${data.instance_id}&limit=100`
      : null,
  );
  async function acknowledge() {
    setBusy(true);
    setError(undefined);
    try {
      await api.post(`/events/${id}/acknowledge`, {
        expected_version: version,
        reason: '已核对并认领，开始处理',
      });
      message.success('已认领事件');
      resource.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function submit(values: Row) {
    setBusy(true);
    setError(undefined);
    try {
      let endpoint = `/events/${id}/${command}`;
      let body: Row = { ...values, expected_version: version };
      if (command === 'execution-records')
        endpoint = `/remediation-plans/${plan?.id}/execution-records`;
      if (command === 'merge') {
        endpoint = '/events/merge';
        const sourceIds = Array.isArray(values.source_event_ids) ? values.source_event_ids : [];
        body = {
          target_event_id: id,
          source_event_ids: sourceIds,
          expected_versions: {
            [id!]: version,
            ...Object.fromEntries(
              pageItems(mergeOptions.data)
                .filter((e) => sourceIds.includes(e.id))
                .map((e) => [text(e.id), e.version]),
            ),
          },
          reason: values.reason,
        };
      }
      if (command === 'manual-changes') {
        endpoint = '/manual-changes';
        body = { ...body, instance_id: data?.instance_id, event_id: id };
      }
      if (command === 'review') {
        endpoint = `/remediation-plans/${plan?.id}/review`;
        body = { ...body, version: plan?.version, note: values.reason };
      }
      if (command === 'remediation-plans') {
        body = {
          ...body,
          prerequisites: [text(values.goal), text(values.success_criteria)],
          risks: [text(values.risk)],
          steps: text(values.steps).split('\n').filter(Boolean),
          rollback: text(values.rollback).split('\n').filter(Boolean),
        };
      }
      if (command === 'execution-records') {
        body = {
          ...body,
          actual_result: {
            executor: values.executor,
            actual_steps: values.actual_steps,
            result: values.result,
          },
        };
      }
      if (command === 'verifications') {
        body = {
          ...body,
          template: values.template ?? 'locks_absent',
          before: { from: values.before_from, to: values.before_to },
          after: { from: values.after_from, to: values.after_to },
          criteria: {
            load_context: {
              kind: values.load_kind ?? 'unknown',
              description: values.load_description ?? '',
            },
            digest: values.digest || undefined,
            schema: values.schema || undefined,
          },
        };
        delete body.before_from;
        delete body.before_to;
        delete body.after_from;
        delete body.after_to;
      }
      const result = await api.post<Row>(endpoint, body, command === 'verifications');
      if (result.type || result.poll_url) await awaitTask(result as TaskRef, setTask);
      message.success('记录已保存');
      form.resetFields();
      setCommand('');
      resource.reload();
      setPlansVersion((v) => v + 1);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  function open(name: string, p?: Row) {
    setCommand(name);
    setPlan(p);
    setError(undefined);
    form.resetFields();
  }
  return (
    <>
      <Title
        title={text(data?.title, '事件详情')}
        description="从事实与证据出发，记录人工处置，再发起确定性复验。"
        actions={
          <>
            {can('event.acknowledge') && data?.status === 'open' && (
              <Button loading={busy} onClick={acknowledge}>
                认领事件
              </Button>
            )}
            {can('event.assign') && data?.status !== 'merged' && (
              <Button onClick={() => open('assign')}>分派</Button>
            )}
            {can('investigation.create') && data && (
              <ContextLink to={`/investigations?instance_id=${data.instance_id}&event_id=${id}`}>
                <Button>建立调查</Button>
              </ContextLink>
            )}
            {can('verification.create') && active && (
              <Button type="primary" onClick={() => open('verifications')}>
                发起只读复验
              </Button>
            )}
            {can('event.close') && active && (
              <Button onClick={() => open('close')}>关闭并记录理由</Button>
            )}
            {can('event.merge') && active && (
              <Button onClick={() => open('merge')}>归并同实例事件</Button>
            )}
            {can('event.close') && data?.status === 'closed' && (
              <Button onClick={() => open('reopen')}>重开并记录原因</Button>
            )}
          </>
        }
      />
      <ErrorNotice error={error} />
      <TaskProgress task={task} />
      <LoadState {...resource} retry={resource.reload}>
        {data && (
          <>
            <Card className="instance-summary">
              <Space wrap>
                <Status value={data.severity} />
                <Status value={data.status} />
                <Status value={data.source_type ?? data.source ?? 'rule'} />
                <span>覆盖版本 {text(data.coverage_revision)}</span>
              </Space>
              <div className="section">
                <FieldGrid
                  data={data}
                  fields={[
                    ['instance_id', '实例 ID'],
                    ['assigned_to', '负责人 ID'],
                    ['created_at', '首次发生'],
                    ['updated_at', '最近变更'],
                    ['version', '事件版本'],
                    ['closure_type', '结案类型'],
                  ]}
                />
              </div>
              {data.closure_type === 'manual_exception' && (
                <Alert
                  type="warning"
                  showIcon
                  title="人工例外结案：未证明恢复"
                  description={text(data.closure_reason)}
                />
              )}
              <SourceCoverage data={data} />
            </Card>
            <Card className="section">
              <Tabs
                activeKey={tab}
                onChange={setTab}
                items={[
                  { key: 'summary', label: '概况与证据' },
                  { key: 'timeline', label: '时间线' },
                  { key: 'plans', label: '处置计划 / 人工记录' },
                  { key: 'verification', label: '只读复验' },
                  { key: 'discussion', label: '讨论与操作记录' },
                ]}
              />
              {tab === 'summary' ? (
                <>
                  <EventSummaryView data={data} />
                  {active && can('change.create', 'remediation.record_execution') && (
                    <Button className="section" onClick={() => open('manual-changes')}>
                      登记应用与人工变更
                    </Button>
                  )}
                </>
              ) : tab === 'timeline' ? (
                <Timeline
                  items={rows(data.timeline ?? data.transitions).map((t) => ({
                    children: (
                      <>
                        <Time value={t.occurred_at ?? t.created_at ?? t.observed_at} />
                        <Typography.Paragraph>
                          <Tag>{text(t.action)}</Tag>
                          {text(
                            (t.details as Row | undefined)?.reason ?? t.summary ?? t.reason,
                            '已保存状态转换',
                          )}
                          <span>
                            {' '}
                            · {text(t.from_state)} → {text(t.to_state)}
                          </span>
                        </Typography.Paragraph>
                      </>
                    ),
                  }))}
                />
              ) : tab === 'plans' ? (
                <>
                  <div className="table-toolbar">
                    <Typography.Text>计划只保存人工步骤，不执行数据库操作。</Typography.Text>
                    {active && can('remediation.create') && (
                      <Button type="primary" onClick={() => open('remediation-plans')}>
                        添加处置计划
                      </Button>
                    )}
                  </div>
                  <LoadState {...plans} retry={plans.reload}>
                    {pageItems(plans.data).length ? (
                      pageItems(plans.data).map((p) => (
                        <Card
                          key={text(p.id)}
                          title={text(p.title ?? p.goal, '处置计划')}
                          extra={
                            <Space>
                              {
                                <>
                                  {active &&
                                    can('remediation.review') &&
                                    ['draft', 'in_review'].includes(text(p.status)) && (
                                      <Button size="small" onClick={() => open('review', p)}>
                                        审核计划
                                      </Button>
                                    )}
                                  {active &&
                                    can('remediation.record_execution') &&
                                    p.status === 'approved' && (
                                      <Button
                                        size="small"
                                        onClick={() => open('execution-records', p)}
                                      >
                                        登记人工执行
                                      </Button>
                                    )}
                                </>
                              }
                            </Space>
                          }
                          className="section"
                        >
                          <RemediationPlanView data={p} />
                        </Card>
                      ))
                    ) : (
                      <Empty description="尚无处置计划" />
                    )}
                  </LoadState>
                </>
              ) : tab === 'verification' ? (
                <>
                  <Alert
                    type="info"
                    showIcon
                    title="复验说明"
                    description="改善不等于因果。无新资料、采集失败或负载不可比将保留资料不足；全部 episode 恢复门槛由服务端检查。"
                    className="notice"
                  />
                  <DataTable
                    items={rows(data.verifications)}
                    columns={[
                      {
                        key: 'id',
                        title: '复验',
                        render: (v) => (
                          <ContextLink to={`/verifications/${v}`}>{text(v)}</ContextLink>
                        ),
                      },
                      {
                        key: 'outcome',
                        title: '结论',
                        render: (v, r) => (v ? <Status value={v} /> : <Status value={r.status} />),
                      },
                      { key: 'created_at', title: '发起时间', render: (v) => <Time value={v} /> },
                    ]}
                  />
                </>
              ) : (
                <>
                  <div className="table-toolbar">
                    <span>讨论与操作记录追加留存</span>
                    {can('event.comment') && data.status !== 'merged' && (
                      <Button onClick={() => open('comments')}>添加备注</Button>
                    )}
                  </div>
                  <DataTable
                    items={rows(data.timeline).filter((item) => item.action === 'comment')}
                    columns={[
                      {
                        key: 'occurred_at',
                        title: '登记时间',
                        render: (value) => <Time value={value} />,
                      },
                      { key: 'actor_id', title: '登记账号' },
                      {
                        key: 'details',
                        title: '备注',
                        render: (value) => text((value as Row)?.reason),
                      },
                    ]}
                  />
                </>
              )}
            </Card>
          </>
        )}
      </LoadState>
      <Modal
        title={
          {
            assign: '分派事件',
            comments: '添加事件备注',
            'remediation-plans': '添加人工处置计划',
            'execution-records': '登记已由人工完成的执行',
            review: '审核处置计划',
            verifications: '发起只读复验',
            close: '关闭事件并记录依据',
            reopen: '重开事件并记录原因',
            merge: '归并同一实例事件',
            'manual-changes': '登记应用与人工变更',
          }[command] ?? ''
        }
        open={!!command}
        onCancel={() => setCommand('')}
        width={720}
        confirmLoading={busy}
        onOk={() => form.submit()}
        destroyOnHidden={false}
      >
        <ErrorNotice error={error} />
        <Form form={form} layout="vertical" onFinish={submit} requiredMark={false}>
          {command === 'merge' && (
            <>
              <Alert
                type="warning"
                showIcon
                title="归并保留原始事件与告警，已有复验将失效。只允许同一实例。"
                className="notice"
              />
              <ErrorNotice error={mergeOptions.error} />
              <Form.Item
                name="source_event_ids"
                label="同实例的来源事件"
                rules={[{ required: true }]}
              >
                <Select
                  mode="multiple"
                  loading={mergeOptions.loading}
                  options={pageItems(mergeOptions.data)
                    .filter((e) => e.id !== id && !['closed', 'merged'].includes(text(e.status)))
                    .map((e) => ({ value: text(e.id), label: text(e.title) }))}
                />
              </Form.Item>
              <Required name="reason" label="归并原因" area />
            </>
          )}
          {command === 'manual-changes' && (
            <>
              <DateField name="event_at" label="实际发生时间（UTC）" />
              <Form.Item name="category" label="变更类别" rules={[{ required: true }]}>
                <Select
                  options={[
                    { value: 'deployment', label: '应用发布' },
                    { value: 'ddl', label: 'DDL' },
                    { value: 'configuration', label: '配置' },
                    { value: 'job', label: '作业' },
                  ]}
                />
              </Form.Item>
              <Required name="description" label="脱敏变更说明" area />
              <Alert type="info" title="只登记资料，不执行提交的文本，也不代替机器观测。" />
            </>
          )}
          {command === 'assign' && (
            <>
              <Form.Item
                name="assignee_id"
                label="有此实例事件权限的责任人"
                rules={[{ required: true }]}
              >
                {rows(data?.eligible_assignees).length ? (
                  <Select
                    options={rows(data?.eligible_assignees).map((u) => ({
                      value: text(u.id),
                      label: text(u.display_name ?? u.username),
                    }))}
                  />
                ) : (
                  <Input placeholder="责任人用户 ID（由服务端核验范围）" />
                )}
              </Form.Item>
              <Required name="reason" label="分派说明" area />
              <Alert
                type="info"
                title="目标责任人必须具有此实例的事件读取权限，服务端会再次校验。"
              />
            </>
          )}
          {command === 'reopen' && <Required name="reason" label="重开原因与未解决问题" area />}
          {command === 'comments' && <Required name="body" label="脱敏备注" area />}
          {command === 'remediation-plans' && (
            <>
              <Required name="title" label="计划标题" />
              <Required name="goal" label="目标与适用条件" area />
              <Required name="steps" label="人工步骤" area />
              <Required name="risk" label="风险与影响" area />
              <Required name="rollback" label="回退方法" area />
              <Required name="success_criteria" label="拟验证条件" area />
              <Required name="owner_id" label="责任人 ID" />
            </>
          )}
          {command === 'execution-records' && (
            <>
              <Alert
                type="warning"
                showIcon
                title="仅登记已经由人工执行的操作。本平台不执行这些步骤。"
                className="notice"
              />
              <Required name="executor" label="实际执行者" />
              <DateField name="executed_at" label="实际执行时间（UTC）" />
              <Required name="actual_steps" label="实际执行步骤" area />
              <Required name="result" label="实际结果与偏差" area />
            </>
          )}
          {command === 'review' && (
            <>
              <Form.Item name="decision" label="审核结论" rules={[{ required: true }]}>
                <Select
                  options={[
                    { value: 'approved', label: '批准此计划记录' },
                    { value: 'rejected', label: '拒绝' },
                  ]}
                />
              </Form.Item>
              <Required name="reason" label="审核理由" area />
            </>
          )}
          {command === 'verifications' && (
            <>
              <Alert
                type="info"
                title="只读复验将创建持久采集任务，冻结前后绝对窗口与所选条件。"
                className="notice"
              />
              <Form.Item name="plan_id" label="关联处置计划">
                <Select
                  allowClear
                  options={pageItems(plans.data).map((p) => ({
                    value: text(p.id),
                    label: text(p.title ?? p.id),
                  }))}
                />
              </Form.Item>
              <Form.Item name="execution_record_id" label="人工执行记录 ID">
                <Input />
              </Form.Item>
              <DateField name="before_from" label="基准窗口开始（UTC）" />
              <DateField name="before_to" label="基准窗口结束（UTC）" />
              <DateField name="after_from" label="复验窗口开始（UTC）" />
              <DateField name="after_to" label="复验窗口结束（UTC）" />
              <Form.Item
                name="template"
                label="确定性检查模板"
                initialValue="locks_absent"
                rules={[{ required: true }]}
              >
                <Select
                  options={[
                    { value: 'locks_absent', label: '所观测锁等待链消失' },
                    { value: 'sql_latency', label: '相同 Digest 的可比窗口延迟' },
                  ]}
                />
              </Form.Item>
              <Form.Item name="digest" label="SQL Digest（SQL 比较必填）">
                <Input />
              </Form.Item>
              <Form.Item name="schema" label="Schema（SQL 比较适用）">
                <Input />
              </Form.Item>
              <Form.Item name="load_kind" label="负载上下文来源" initialValue="unknown">
                <Select
                  options={[
                    { value: 'unknown', label: '未知，不推断业务恢复' },
                    { value: 'human_assertion', label: '人工描述的负载条件' },
                    { value: 'measured', label: '已采集的可比较负载' },
                  ]}
                />
              </Form.Item>
              <Form.Item name="load_description" label="负载条件与比较限制">
                <Input.TextArea rows={3} />
              </Form.Item>
              <Required name="reason" label="复验目的" area />
            </>
          )}
          {command === 'close' && (
            <>
              <Form.Item
                name="closure_type"
                label="结案类型"
                initialValue="verified_recovery"
                rules={[{ required: true }]}
              >
                <Select
                  options={[
                    { value: 'verified_recovery', label: '已验证恢复（服务端核对全部告警覆盖）' },
                    { value: 'manual_exception', label: '人工例外（未证明恢复）' },
                  ]}
                />
              </Form.Item>
              <Form.Item name="verification_id" label="有效复验 ID">
                <Input />
              </Form.Item>
              <Required name="reason" label="结案依据" area />
              <Form.Item name="risks" label="人工例外接受的风险（例外结案必填）">
                <Input.TextArea rows={3} />
              </Form.Item>
              <Alert type="warning" title="人工例外不会将仍触发或未知的告警标记恢复。" />
            </>
          )}
        </Form>
      </Modal>
    </>
  );
}
export function Investigations() {
  const [search] = useSearchParams();
  const can = useCan();
  const { scope } = useScope();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const [task, setTask] = useState<TaskRef>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const navigate = useScopedNavigate();
  const [preview, setPreview] = useState<Row>();
  const [previewBusy, setPreviewBusy] = useState(false);
  const mode = Form.useWatch('mode', form);
  async function previewMaterials() {
    setPreviewBusy(true);
    setError(undefined);
    try {
      await form.validateFields(['question', 'instance_ids', 'from', 'to']);
      const result = await api.post('/investigations/preview', form.getFieldsValue(true));
      setPreview(result);
    } catch (e) {
      setError(e);
    } finally {
      setPreviewBusy(false);
    }
  }
  const profiles = useResource<Page>(can('model.external_use') ? '/model-profiles' : null);
  async function submit(values: Row) {
    setBusy(true);
    setError(undefined);
    try {
      if (values.mode === 'live_redacted' && !preview)
        throw new Error('请先预览当前范围的外发资料。');
      const ref = await api.post<TaskRef>(
        '/investigations',
        {
          ...values,
          event_id: search.get('event_id') ?? undefined,
          from: values.from ?? scope.from,
          to: values.to ?? scope.to,
        },
        true,
      );
      const done = await awaitTask(ref, setTask);
      const investigationId = done.result?.investigation_id;
      if (investigationId) {
        form.resetFields();
        setOpen(false);
        navigate(`/investigations/${investigationId}`);
      } else throw new Error('调查任务未返回报告引用，请在任务中心核查。');
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <ResourceList
        title="AI 调查"
        description="冻结授权实例与绝对时间窗，结论引用资料；规则与实时模型分别标识。"
        endpoint="/investigations"
        filters={<Tag>不自动重发计费调用</Tag>}
        actions={
          can('investigation.create') && (
            <Button
              type="primary"
              onClick={() => {
                form.setFieldsValue({
                  instance_ids: search.get('instance_id') ? [search.get('instance_id')] : [],
                  from: scope.from,
                  to: scope.to,
                  mode: 'rules_only',
                });
                setOpen(true);
              }}
            >
              ＋ 新建调查
            </Button>
          )
        }
        columns={[
          {
            key: 'question',
            title: '调查问题',
            render: (v, r) => (
              <ContextLink to={`/investigations/${r.id}`}>{text(v ?? r.title)}</ContextLink>
            ),
          },
          { key: 'status', title: '状态', render: (v) => <Status value={v} /> },
          { key: 'mode', title: '分析模式', render: (v) => <Status value={v} /> },
          { key: 'model', title: '模型' },
          { key: 'created_at', title: '创建时间', render: (v) => <Time value={v} /> },
          { key: 'from', title: '冻结起点', render: (v) => <Time value={v} /> },
          { key: 'to', title: '冻结终点', render: (v) => <Time value={v} /> },
        ]}
      />
      <Drawer
        title="新建有证据的调查"
        width={740}
        open={open}
        onClose={() => setOpen(false)}
        footer={
          <Space>
            <Button onClick={() => setOpen(false)}>保留输入并关闭</Button>
            <Button type="primary" loading={busy} onClick={() => form.submit()}>
              冻结窗口并开始调查
            </Button>
          </Space>
        }
      >
        <ErrorNotice error={error} />
        <TaskProgress task={task} />
        <Form
          form={form}
          layout="vertical"
          onFinish={submit}
          onValuesChange={(changed) => {
            if ('external_confirmed' in changed) return;
            setPreview(undefined);
            form.setFieldValue('external_confirmed', false);
          }}
          requiredMark={false}
        >
          <Typography.Title level={5}>1 · 问题与资料范围</Typography.Title>
          <Required name="question" label="需要调查的问题" area />
          <InstancePicker multiple />
          <DateField name="from" label="冻结窗口开始（UTC）" />
          <DateField name="to" label="冻结窗口结束（UTC）" />
          <Form.Item name="evidence_ids" label="已有证据 ID（可选）">
            <Select mode="tags" placeholder="添加有权读取的证据 ID" />
          </Form.Item>
          <Typography.Title level={5}>2 · 模式与预算</Typography.Title>
          <Form.Item name="mode" label="分析模式" initialValue="rules_only">
            <Select
              options={[
                { value: 'rules_only', label: '确定性资料与规则，不调用模型' },
                ...(can('model.external_use')
                  ? [{ value: 'live_redacted', label: '已批准资料的脱敏实时模型' }]
                  : []),
              ]}
            />
          </Form.Item>
          {mode === 'live_redacted' && (
            <>
              <Form.Item
                name="model_profile_id"
                label="服务端模型配置"
                rules={[{ required: true }]}
              >
                <Select
                  options={pageItems(profiles.data).map((p) => ({
                    value: text(p.id),
                    label: text(p.name ?? p.model),
                  }))}
                />
              </Form.Item>
              <ErrorNotice error={profiles.error} />
              <Form.Item name="max_tool_calls" label="工具调用上限" initialValue={6}>
                <InputNumber min={0} max={6} />
              </Form.Item>
              <Form.Item name="max_duration_seconds" label="运行时间上限（秒）" initialValue={120}>
                <InputNumber min={10} max={180} />
              </Form.Item>
              <Form.Item name="max_cost_usd" label="可选美元预算（仅已核实价格时可承诺）">
                <InputNumber min={0.01} max={5} step={0.05} />
              </Form.Item>
              <Typography.Title level={5}>3 · 脱敏材料与外发确认</Typography.Title>
              <Button onClick={previewMaterials} loading={previewBusy}>
                预览当前范围的外发资料
              </Button>
              {preview && (
                <div className="section">
                  <RecordCard title="服务端外发资料预览" data={preview} />
                </div>
              )}
              <Typography.Paragraph type="secondary" className="section">
                服务端限制工具次数、运行时间和 Token。未核实价格时，不显示已保证的美元费用上限。
              </Typography.Paragraph>
              <Alert
                type="warning"
                showIcon
                title="仅允许外发策略共同许可的资料"
                description="模型只能访问授权、脱敏的证据和白名单只读工具；SQL、日志与元数据脱敏后仍可能存在残留风险。策略拒绝不会自动切换其他模型。"
                className="notice"
              />
              <Form.Item
                name="external_confirmed"
                valuePropName="checked"
                rules={[
                  {
                    validator: (_, v) =>
                      v ? Promise.resolve() : Promise.reject(new Error('请确认资料外发范围与预算')),
                  },
                ]}
              >
                <Checkbox disabled={!preview}>
                  我已核对选定范围、资料类别、脱敏风险和本次预算。
                </Checkbox>
              </Form.Item>
            </>
          )}
          <Alert
            type="info"
            title="历史窗口不会自动加入结束时间之后的新采样；缺少当时快照将列为未知。"
          />
        </Form>
      </Drawer>
    </>
  );
}
export function InvestigationDetail() {
  const { id } = useParams();
  const { refresh } = useScope();
  const can = useCan();
  const resource = useResource<Row>(`/investigations/${id}`, refresh);
  const data = resource.data;
  const [review, setReview] = useState(false);
  const [tab, setTab] = useState('report');
  return (
    <>
      <Title
        title={text(data?.question, '调查详情')}
        description="保留冻结资料、有效引用和工具审计。模型建议经人工核对后使用。"
        actions={
          can('investigation.create') && <Button onClick={() => setReview(true)}>反馈结论</Button>
        }
      />
      <LoadState {...resource} retry={resource.reload}>
        {data && (
          <>
            <Card>
              <Space wrap>
                <Status value={data.status} />
                <Status value={data.mode} />
                {data.task_id ? (
                  <ContextLink to={`/tasks/${data.task_id}`}>查看关联任务</ContextLink>
                ) : null}
              </Space>
              <div className="section">
                <FieldGrid
                  data={data}
                  fields={[
                    ['from', '冻结窗口开始'],
                    ['to', '冻结窗口结束'],
                    ['model', '模型'],
                    ['provider', '服务商'],
                    ['created_at', '创建时间'],
                    ['finished_at', '完成时间'],
                  ]}
                />
              </div>
              <SourceCoverage data={data} />
            </Card>
            <Card className="section">
              <Tabs
                activeKey={tab}
                onChange={setTab}
                items={[
                  { key: 'report', label: '报告' },
                  { key: 'evidence', label: '证据关系与清单' },
                  { key: 'tools', label: '工具调用' },
                  { key: 'models', label: '模型调用' },
                  { key: 'reviews', label: '反馈与审计' },
                ]}
              />
              {tab === 'report' ? (
                <>
                  <div className="report-origin">
                    <Status
                      value={
                        data.conclusion_type ?? (data.mode === 'rules_only' ? 'rule' : 'live_model')
                      }
                    />
                    <span>依据冻结资料形成；未显示的指标不推断为零。</span>
                  </div>
                  <InvestigationReportView data={data.report ?? data.report_json ?? data.summary} />
                  {data.unknowns ? <RecordCard title="未知与反证" data={data.unknowns} /> : null}
                  <EvidenceLinks
                    data={investigationReferences(data)}
                    metricRefs={rows(data.metric_refs)}
                  />
                </>
              ) : tab === 'evidence' ? (
                <>
                  <RecordCard title="有来源的证据关系" data={data.evidence_graph ?? data.graph} />
                  <EvidenceLinks
                    data={investigationReferences(data)}
                    metricRefs={rows(data.metric_refs)}
                  />
                  <div className="section">
                    <ManifestButton
                      id={data.manifest_id ?? (data.manifest as Row | undefined)?.id}
                      label="打开调查冻结清单"
                    />
                  </div>
                </>
              ) : tab === 'tools' ? (
                <RecordCard title="有序工具调用（包含拒绝与失败）" data={data.tool_calls} />
              ) : tab === 'models' ? (
                <RecordCard title="模型调用与实际服务商用量" data={data.model_calls} />
              ) : (
                <RecordCard title="追加反馈与审计" data={data.reviews ?? data.audit} />
              )}
            </Card>
          </>
        )}
      </LoadState>
      <CommandModal
        title="反馈调查结论"
        open={review}
        onClose={() => setReview(false)}
        endpoint={`/investigations/${id}/reviews`}
        onSuccess={() => {
          setReview(false);
          resource.reload();
        }}
        fields={
          <>
            <Form.Item name="verdict" label="反馈" rules={[{ required: true }]}>
              <Select
                options={[
                  { value: 'useful', label: '有用' },
                  { value: 'incorrect', label: '结论有误' },
                  { value: 'inconclusive', label: '尚无法判断' },
                ]}
              />
            </Form.Item>
            <Required name="reason" label="实际原因与证据" area />
            <Form.Item name="evidence_ids" label="支持证据 ID">
              <Select mode="tags" />
            </Form.Item>
          </>
        }
      />
    </>
  );
}
export function Knowledge() {
  const can = useCan();
  const [create, setCreate] = useState(false);
  const [revision, setRevision] = useState(0);
  return (
    <>
      <ResourceList
        key={revision}
        title="知识库"
        description="经人工审核的结构化运行手册与案例。知识继承全部来源实例权限。"
        endpoint="/knowledge"
        filters={<Tag>关键词检索 · 版本留存</Tag>}
        actions={
          can('knowledge.create') && (
            <Button type="primary" onClick={() => setCreate(true)}>
              ＋ 新增草稿
            </Button>
          )
        }
        columns={[
          {
            key: 'title',
            title: '标题',
            render: (v, r) => <ContextLink to={`/knowledge/${r.id}`}>{text(v)}</ContextLink>,
          },
          { key: 'type', title: '数据库 / 类型', render: (v, r) => text(v ?? r.engine) },
          { key: 'status', title: '状态', render: (v) => <Status value={v} /> },
          { key: 'version', title: '版本' },
          {
            key: 'reviewed_at',
            title: '最近复核',
            render: (v, r) => (
              <Time
                value={
                  v ?? [...rows(r.versions)].reverse().find((item) => item.reviewed_at)?.reviewed_at
                }
              />
            ),
          },
        ]}
      />
      <CommandModal
        title="新增知识草稿"
        open={create}
        onClose={() => setCreate(false)}
        endpoint="/knowledge"
        transform={(v) => ({
          ...v,
          content: { body: v.content },
          source_event_id: Array.isArray(v.source_event_ids) ? v.source_event_ids[0] : undefined,
        })}
        onSuccess={() => {
          setCreate(false);
          setRevision((v) => v + 1);
        }}
        fields={
          <>
            <Required name="title" label="标题" />
            <Form.Item name="type" label="类型" initialValue="runbook">
              <Select
                options={[
                  { value: 'runbook', label: '运行手册' },
                  { value: 'verified_case', label: '已验证案例' },
                  { value: 'reference', label: '参考资料' },
                ]}
              />
            </Form.Item>
            <InstancePicker multiple field="instance_ids" />
            <Required name="content" label="适用条件、确定性检查、风险与回退" area />
            <Form.Item name="source_event_ids" label="来源事件 ID">
              <Select mode="tags" />
            </Form.Item>
            <Form.Item name="evidence_ids" label="来源证据 ID">
              <Select mode="tags" />
            </Form.Item>
            <Alert type="info" title="保存为草稿；发布需人工审核，不能扩大原始资料权限。" />
          </>
        }
      />
    </>
  );
}
export function KnowledgeDetail() {
  const { id } = useParams();
  const { refresh } = useScope();
  const can = useCan();
  const resource = useResource<Row>(`/knowledge/${id}`, refresh);
  const [command, setCommand] = useState('');
  const data = resource.data;
  const versions = rows(data?.versions);
  const current = versions.find((version) => version.version === data?.version) ?? versions.at(-1);
  const status = current?.status ?? data?.status;
  const lastReviewed = [...versions].reverse().find((version) => version.reviewed_at);
  const summary = {
    ...data,
    engine: data?.engine ?? current?.engine,
    reviewed_at: lastReviewed?.reviewed_at,
  };
  const content = (data?.content ?? {}) as Row;
  const evidenceRefs =
    data?.evidence_ids ?? data?.evidence_refs ?? content.evidence_ids ?? content.evidence_refs;
  const reviewOptions =
    status === 'published'
      ? [{ value: 'withdrawn', label: '撤回已发布版本' }]
      : [
          { value: 'published', label: '审核通过并发布' },
          { value: 'rejected', label: '拒绝并退回修改' },
        ];
  return (
    <>
      <Title
        title={text(data?.title, '知识详情')}
        description="正文与审核均版本化留存；来源实例撤权后整条知识不可读。"
        actions={
          <>
            {data && can('knowledge.create') && (
              <>
                <Button onClick={() => setCommand('versions')}>新增版本</Button>
                {['draft', 'rejected'].includes(text(status)) && (
                  <Button onClick={() => setCommand('submit-review')}>提交审核</Button>
                )}
              </>
            )}
            {data &&
              can('knowledge.review') &&
              ['in_review', 'published'].includes(text(status)) && (
                <Button
                  type={status === 'in_review' ? 'primary' : 'default'}
                  onClick={() => setCommand('reviews')}
                >
                  {status === 'published' ? '撤回发布' : '人工审核'}
                </Button>
              )}
            {data && can('knowledge.review') && status === 'approved' && (
              <Button type="primary" onClick={() => setCommand('publish')}>
                发布已审核版本
              </Button>
            )}
          </>
        }
      />
      <LoadState {...resource} retry={resource.reload}>
        {data && (
          <>
            <Card>
              <FieldGrid
                data={summary}
                fields={[
                  ['status', '状态'],
                  ['engine', '数据库'],
                  ['version', '当前版本'],
                  ['reviewed_at', '最近审核'],
                  ['created_at', '创建时间'],
                  ['updated_at', '最近修改'],
                ]}
              />
              {Array.isArray(data.engine_versions) && data.engine_versions.length > 0 && (
                <Typography.Paragraph type="secondary" className="section">
                  适用版本：{data.engine_versions.map((value) => text(value)).join('、')}
                </Typography.Paragraph>
              )}
            </Card>
            <KnowledgeBodyView data={data} />
            {Array.isArray(evidenceRefs) && evidenceRefs.length > 0 && (
              <EvidenceLinks data={evidenceRefs} />
            )}
            <KnowledgeVersions data={data} />
          </>
        )}
      </LoadState>
      <CommandModal
        title={
          command === 'reviews'
            ? status === 'published'
              ? '撤回知识发布'
              : '人工审核知识'
            : command === 'versions'
              ? '新增知识版本'
              : command === 'publish'
                ? '发布已审核版本'
                : '提交知识审核'
        }
        open={!!command}
        onClose={() => setCommand('')}
        endpoint={`/knowledge/${id}/${command}`}
        transform={(v) => ({
          ...v,
          version: data?.version ?? data?.current_version_number,
          note: v.reason,
          content: v.content ? { body: v.content } : undefined,
          ...(command === 'versions'
            ? { engine: data?.engine, engine_versions: data?.engine_versions, tags: data?.tags }
            : {}),
          ...(command === 'reviews' && status === 'published' ? { decision: 'withdrawn' } : {}),
        })}
        onSuccess={() => {
          setCommand('');
          resource.reload();
        }}
        fields={
          command === 'versions' ? (
            <>
              <Required name="content" label="新版本正文" area />
              <Required name="reason" label="修改原因" area />
            </>
          ) : command === 'reviews' ? (
            <>
              {status !== 'published' && (
                <Form.Item name="decision" label="决定" rules={[{ required: true }]}>
                  <Select options={reviewOptions} />
                </Form.Item>
              )}
              <Required name="reason" label="审核意见与脱敏核对" area />
            </>
          ) : (
            <Required name="reason" label={command === 'publish' ? '发布说明' : '提交说明'} area />
          )
        }
      />
    </>
  );
}
export function Tasks() {
  return (
    <ResourceList
      title="任务中心"
      description="排队、执行、取消请求与最终结果分别留存。任务离开页面后仍可查询。"
      endpoint="/tasks"
      columns={[
        {
          key: 'type',
          title: '任务',
          render: (v, r) => <ContextLink to={`/tasks/${r.id}`}>{text(v)}</ContextLink>,
        },
        { key: 'status', title: '状态', render: (v) => <Status value={v} /> },
        { key: 'instance_name', title: '实例' },
        { key: 'created_at', title: '排队时间', render: (v) => <Time value={v} /> },
        { key: 'started_at', title: '开始时间', render: (v) => <Time value={v} /> },
        { key: 'finished_at', title: '结束时间', render: (v) => <Time value={v} /> },
        { key: 'error_code', title: '错误码' },
      ]}
    />
  );
}
export function TaskDetail() {
  const { id } = useParams();
  const { refresh } = useScope();
  const can = useCan();
  const resource = useResource<TaskRef>(`/tasks/${id}`, refresh);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [retryOpen, setRetryOpen] = useState(false);
  const [costAck, setCostAck] = useState(false);
  const [externalAck, setExternalAck] = useState(false);
  const navigate = useScopedNavigate();
  const data = resource.data;
  useEffect(() => {
    if (!data || terminal(data.status)) return;
    const timer = setTimeout(resource.reload, 2000);
    return () => clearTimeout(timer);
  }, [data, resource.reload]);
  async function action(name: string) {
    setBusy(true);
    setError(undefined);
    try {
      const result = await api.post<TaskRef>(
        `/tasks/${id}/${name}`,
        name === 'retry'
          ? { acknowledge_unknown_cost: costAck, external_confirmed: externalAck }
          : {},
        name === 'retry',
      );
      if (name === 'retry' && result.id !== id) {
        setRetryOpen(false);
        navigate(`/tasks/${result.id}`);
      } else resource.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function download() {
    setBusy(true);
    setError(undefined);
    try {
      const response = await fetch(`/api/v1/exports/${encodeURIComponent(id!)}/download`, {
        credentials: 'same-origin',
      });
      if (!response.ok) {
        if (response.status === 401) window.dispatchEvent(new Event('session-expired'));
        const body = await response.json().catch(() => undefined);
        throw new Error(body?.error?.message ?? '导出不可下载，请核对当前权限和有效期。');
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `dba-export-${id}.json`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Title
        title="任务详情"
        description="显示服务端持久任务状态；取消请求不代表已经取消。"
        actions={
          <>
            {data && !terminal(data.status) && (
              <Button loading={busy} onClick={() => action('cancel')}>
                取消后续步骤
              </Button>
            )}
            {data &&
              can('task.manage') &&
              ['failed', 'expired', 'cancelled'].includes(data.status) &&
              ['collect_instance', 'activate_instance', 'investigation', 'export'].includes(
                data.type,
              ) && (
                <Button
                  onClick={() => {
                    setCostAck(false);
                    setExternalAck(false);
                    setRetryOpen(true);
                  }}
                >
                  受控重试
                </Button>
              )}
          </>
        }
      />
      <ErrorNotice error={error} />
      <LoadState {...resource} retry={resource.reload}>
        {data && (
          <>
            <TaskProgress task={data} />
            <Card>
              <FieldGrid
                data={data}
                fields={[
                  ['id', '任务 ID'],
                  ['type', '类型'],
                  ['status', '状态'],
                  ['created_at', '排队时间'],
                  ['started_at', '开始时间'],
                  ['finished_at', '完成时间'],
                  ['attempt', '尝试次数'],
                  ['config_revision', '配置版本'],
                ]}
              />
            </Card>
            {data.error_message && (
              <Alert
                type="error"
                showIcon
                title={data.error_code ?? '任务未完成'}
                description={data.error_message}
                className="notice"
              />
            )}
            <RecordCard title="真实阶段与执行记录" data={data.progress ?? data.steps} />
            <RecordCard title="授权结果引用" data={data.result} />
            {data.type === 'export' && data.status === 'succeeded' && can('export.create') && (
              <Button type="primary" className="section" loading={busy} onClick={download}>
                核验权限并下载 JSON
              </Button>
            )}
            {data.result_ref && safeResultLink(data.result_ref) ? (
              <div className="section">
                <ContextLink to={safeResultLink(data.result_ref)!}>
                  <Button type="primary">打开任务结果</Button>
                </ContextLink>
              </div>
            ) : null}
          </>
        )}
      </LoadState>
      <Modal
        title="核对当前范围后创建新任务"
        open={retryOpen}
        onCancel={() => setRetryOpen(false)}
        confirmLoading={busy}
        onOk={() => action('retry')}
        okButtonProps={{
          disabled:
            (!!data?.needs_external_confirmation && !externalAck) ||
            (!!data?.needs_unknown_cost_acknowledgement && !costAck),
        }}
      >
        <ErrorNotice error={error} />
        <Alert
          type="info"
          showIcon
          title="服务端将重新核验当前权限、配置和策略"
          description="旧结果与原始审计继续保留，新任务单独记录。"
          className="notice"
        />
        {data?.needs_unknown_cost_acknowledgement && (
          <Checkbox checked={costAck} onChange={(e) => setCostAck(e.target.checked)}>
            上一次请求结果未知，可能已经计费；我接受再次调用可能产生额外费用。
          </Checkbox>
        )}
        {data?.needs_external_confirmation && (
          <div className="section">
            <Checkbox checked={externalAck} onChange={(e) => setExternalAck(e.target.checked)}>
              我确认按当前策略再次外发已批准的脱敏资料。
            </Checkbox>
          </div>
        )}
      </Modal>
    </>
  );
}
function safeResultLink(value: string) {
  if (!value.startsWith('/') || value.startsWith('//')) return null;
  const path = value.replace(/^\/api\/v1/, '');
  if (path.startsWith('/instances/')) return path.replace('/instances/', '/assets/') + '/overview';
  return /^\/(investigations|events|evidence|knowledge|verifications)\/[a-zA-Z0-9-]+$/.test(path)
    ? path
    : null;
}
export function VerificationDetail() {
  const { id } = useParams();
  const { refresh } = useScope();
  const resource = useResource<Row>(`/verifications/${id}`, refresh);
  return (
    <>
      <Title title="只读复验报告" description="根据冻结资料比较所选窗口；资料不足不视为通过。" />
      <LoadState {...resource} retry={resource.reload}>
        {resource.data && <VerificationReportView data={resource.data} />}
      </LoadState>
    </>
  );
}
export function InstancePicker({
  multiple = false,
  field,
}: {
  multiple?: boolean;
  field?: string;
}) {
  const resource = useResource<Page<Instance>>('/instances');
  return (
    <Form.Item
      name={field ?? (multiple ? 'instance_ids' : 'instance_id')}
      label={multiple ? '授权实例（最多 5 个）' : '授权实例'}
      rules={[
        { required: true, message: '请选择授权实例' },
        ...(multiple
          ? [
              {
                validator: (_: unknown, v: string[]) =>
                  v?.length <= 5
                    ? Promise.resolve()
                    : Promise.reject(new Error('最多选择 5 个实例')),
              },
            ]
          : []),
      ]}
    >
      <Select
        mode={multiple ? 'multiple' : undefined}
        loading={resource.loading}
        options={pageItems(resource.data).map((i) => ({ value: i.id, label: i.name }))}
        placeholder={resource.error ? '实例选项读取失败，请重试' : '选择实例'}
      />
    </Form.Item>
  );
}
export function Required({
  name,
  label,
  area = false,
}: {
  name: string;
  label: string;
  area?: boolean;
}) {
  return (
    <Form.Item name={name} label={label} rules={[{ required: true, message: `请填写${label}` }]}>
      {area ? <Input.TextArea rows={4} maxLength={20000} /> : <Input maxLength={1000} />}
    </Form.Item>
  );
}
export function DateField({ name, label }: { name: string; label: string }) {
  return (
    <Form.Item
      name={name}
      label={label}
      rules={[
        { required: true, message: '请输入 ISO 8601 绝对时间' },
        {
          validator: (_, v) =>
            !v || (!isNaN(new Date(v).valueOf()) && /(Z|[+-]\d{2}:\d{2})$/.test(v))
              ? Promise.resolve()
              : Promise.reject(new Error('请使用含时区的 ISO 时间，例如 YYYY-MM-DDTHH:mm:ssZ')),
        },
      ]}
    >
      <Input placeholder="YYYY-MM-DDTHH:mm:ssZ" />
    </Form.Item>
  );
}
export function CommandModal({
  title,
  open,
  onClose,
  endpoint,
  fields,
  onSuccess,
  transform,
  method = 'POST',
  initialValues,
}: {
  title: string;
  open: boolean;
  onClose: () => void;
  endpoint: string;
  fields: ReactNode;
  onSuccess?: (result: Row) => void;
  transform?: (values: Row) => Row;
  method?: 'POST' | 'PATCH' | 'PUT';
  initialValues?: Row;
}) {
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const { message } = App.useApp();
  useEffect(() => {
    if (open) {
      setError(undefined);
      form.setFieldsValue(initialValues ?? {});
    }
  }, [open, initialValues, form]);
  async function submit(values: Row) {
    setBusy(true);
    setError(undefined);
    try {
      const body = transform ? transform(values) : values;
      const result =
        method === 'PATCH'
          ? await api.patch(endpoint, body)
          : method === 'PUT'
            ? await api.put(endpoint, body)
            : await api.post(endpoint, body);
      message.success('记录已保存');
      form.resetFields();
      onSuccess?.(result);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={title}
      open={open}
      onCancel={onClose}
      confirmLoading={busy}
      onOk={() => form.submit()}
      width={650}
    >
      <ErrorNotice error={error} />
      <Form
        form={form}
        layout="vertical"
        onFinish={submit}
        initialValues={initialValues}
        requiredMark={false}
      >
        {fields}
      </Form>
    </Modal>
  );
}
function investigationReferences(data: Row): unknown[] {
  const refs: unknown[] = [
    ...(Array.isArray(data.evidence_refs)
      ? data.evidence_refs
      : Array.isArray(data.evidence_ids)
        ? data.evidence_ids
        : []),
    ...rows(data.metric_refs),
  ];
  const report = (data.report ?? data.report_json) as Row | undefined;
  for (const fact of rows(report?.facts)) {
    if (Array.isArray(fact.evidence_ids)) refs.push(...fact.evidence_ids);
  }
  return refs;
}
function EvidenceLinks({ data, metricRefs = [] }: { data: unknown; metricRefs?: Row[] }) {
  const refs = Array.isArray(data) ? data : [];
  const [selected, setSelected] = useState<Row>();
  const path =
    typeof selected?.url === 'string' &&
    /^\/api\/v1\/investigations\/[0-9a-f-]{36}\/metric-windows\/[0-9a-f-]{36}$/i.test(selected.url)
      ? selected.url
      : null;
  const frozen = useResource<Row>(path);
  const unique = new Map<string, unknown>();
  for (const ref of refs) {
    const id =
      typeof ref === 'string' ? ref : text((ref as Row)?.id ?? (ref as Row)?.evidence_id, '');
    if (id) unique.set(id, ref);
  }
  return (
    <>
      <Card title="引用证据" className="section">
        {unique.size ? (
          <Space wrap>
            {[...unique].map(([id]) => {
              if (id.startsWith('manifest:')) {
                const metric = metricRefs.find((ref) => ref.id === id);
                return metric ? (
                  <Button key={id} size="small" onClick={() => setSelected(metric)}>
                    冻结指标 · {text(metric.template)} · {id.slice(9, 17)}
                  </Button>
                ) : (
                  <Tag key={id}>冻结指标引用 {id.slice(9, 17)}（详情未提供）</Tag>
                );
              }
              return (
                <ContextLink key={id} to={`/evidence/${encodeURIComponent(id)}`}>
                  <Tag>{id.slice(0, 12)}…</Tag>
                </ContextLink>
              );
            })}
          </Space>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前记录尚无证据引用" />
        )}
      </Card>
      <Drawer
        title="冻结时序证据"
        width={850}
        open={!!selected}
        onClose={() => setSelected(undefined)}
      >
        <Alert
          type="info"
          showIcon
          title="调查创建时固定的指标窗口"
          description="下列数值来自报告的保留清单，包含当时缺口；不会重新查询实时指标。"
          className="notice"
        />
        <LoadState {...frozen} retry={frozen.reload}>
          {frozen.data && (
            <>
              <FieldGrid
                data={frozen.data}
                fields={[
                  ['template', '指标模板'],
                  ['unit', '单位'],
                  ['source', '来源'],
                  ['payload_sha256', '内容校验'],
                ]}
              />
              <SourceCoverage data={frozen.data} />
              <Table<Row>
                size="small"
                className="section"
                rowKey={(row) => text(row.time)}
                dataSource={rows(frozen.data.points)}
                pagination={{
                  defaultPageSize: 50,
                  pageSizeOptions: [25, 50, 100],
                  showSizeChanger: true,
                  showTotal: (total) => `共 ${total} 个冻结采样`,
                }}
                scroll={{ x: 'max-content' }}
                columns={[
                  {
                    key: 'time',
                    dataIndex: 'time',
                    title: '观察时间',
                    render: (value) => <Time value={value} />,
                  },
                  {
                    key: 'value',
                    dataIndex: 'value',
                    title: `数值（${text(frozen.data.unit)}）`,
                    render: (value) => text(value, '未知'),
                  },
                  {
                    key: 'validity',
                    dataIndex: 'validity',
                    title: '质量',
                    render: (value) => <Status value={value} />,
                  },
                ]}
              />
            </>
          )}
        </LoadState>
      </Drawer>
    </>
  );
}
