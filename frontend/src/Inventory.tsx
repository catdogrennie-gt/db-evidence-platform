import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Row as GridRow,
  Select,
  Space,
  Steps,
  Tabs,
  Tag,
  Typography,
} from 'antd';
import { useParams } from 'react-router-dom';
import type { EChartsOption } from 'echarts';
import {
  api,
  awaitTask,
  connectionTestId,
  connectionTestPassed,
  pageItems,
  query,
  rows,
  text,
} from './api';
import type { Capability, Instance, MetricSeries, Page, Row, TaskRef } from './api';
import {
  useCan,
  useCursor,
  useResource,
  useScope,
  useScopedPath,
  useScopedNavigate,
} from './hooks';
import {
  Chart,
  ContextLink,
  DataTable,
  ErrorNotice,
  FieldGrid,
  LoadState,
  RecordBlock,
  RecordCard,
  RelationshipGraph,
  SourceCoverage,
  Status,
  TaskProgress,
  Time,
  Title,
} from './ui';
import {
  ConnectionTestView,
  SourceHealthSummary,
  QueryWindowSummary,
  QueryValidity,
} from './Presentation';
export function AssetList() {
  const can = useCan();
  const { refresh } = useScope();
  const [q, setQ] = useState('');
  const [health, setHealth] = useState<string>();
  const [cursor, setCursor] = useCursor();
  const path = useScopedPath('/instances', { q, health, cursor });
  const resource = useResource<Page<Instance>>(path, refresh);
  return (
    <>
      <Title
        title="资产目录"
        description="配置状态按现在显示。登记、启用与健康分别计数。"
        actions={
          can('asset.onboard') && (
            <ContextLink to="/assets/new">
              <Button type="primary">＋ 接入 MySQL</Button>
            </ContextLink>
          )
        }
      />
      <Card>
        <div className="table-toolbar">
          <Space wrap>
            <Input.Search
              placeholder="搜索授权实例"
              allowClear
              onSearch={(v) => {
                setQ(v);
                setCursor(undefined);
              }}
              style={{ width: 270 }}
            />
            <Select
              aria-label="资料状态"
              placeholder="全部资料状态"
              allowClear
              onChange={(v) => {
                setHealth(v);
                setCursor(undefined);
              }}
              style={{ width: 160 }}
              options={['healthy', 'degraded', 'stale', 'unreachable', 'unknown'].map((value) => ({
                value,
                label: <Status value={value} />,
              }))}
            />
          </Space>
          <span className="muted">
            {resource.data ? `授权范围内 ${resource.data.total} 个实例` : '范围计数待查询'}
          </span>
        </div>
        <LoadState {...resource} retry={resource.reload}>
          <DataTable
            items={pageItems(resource.data)}
            columns={[
              {
                key: 'name',
                title: '实例名称',
                render: (_, r) => (
                  <ContextLink to={`/assets/${r.id}/overview`}>
                    <strong>{text(r.name)}</strong>
                  </ContextLink>
                ),
              },
              { key: 'system_name', title: '业务系统' },
              { key: 'environment', title: '环境', render: (v) => <Tag>{text(v)}</Tag> },
              {
                key: 'server_version',
                title: 'MySQL 版本',
                render: (v, r) => (r.data_access === false ? '无观测权限' : text(v)),
              },
              { key: 'lifecycle', title: '生命周期', render: (v) => <Status value={v} /> },
              {
                key: 'health',
                title: '资料状态',
                render: (v, r) =>
                  r.data_access === false ? <Tag>未授予观测资料权限</Tag> : <Status value={v} />,
              },
              {
                key: 'last_success_at',
                title: '最近成功采集',
                render: (v, r) => (r.data_access === false ? '无观测权限' : <Time value={v} />),
              },
            ]}
          />
          <Pagination data={resource.data} setCursor={setCursor} />
        </LoadState>
      </Card>
      <Drafts />
    </>
  );
}
function Drafts() {
  const can = useCan();
  const resource = useResource<Page>(can('asset.onboard') ? '/connection-drafts' : null);
  const items = pageItems(resource.data);
  return items.length ? (
    <Card title="未完成的接入草稿" className="section">
      <DataTable
        items={items}
        columns={[
          {
            key: 'name',
            title: '名称',
            render: (_, r) => (
              <ContextLink to={`/assets/onboarding/${r.id}`}>
                {text(r.name, '未命名草稿')}
              </ContextLink>
            ),
          },
          { key: 'updated_at', title: '保存时间', render: (v) => <Time value={v} /> },
          { key: 'expires_at', title: '到期时间', render: (v) => <Time value={v} /> },
        ]}
      />
    </Card>
  ) : null;
}
export function Pagination({
  data,
  setCursor,
}: {
  data?: Page<unknown>;
  setCursor: (v: string | undefined) => void;
}) {
  return data ? (
    <div className="pagination">
      <Typography.Text type="secondary">当前授权查询共 {data.total} 条</Typography.Text>
      <Space>
        <Button size="small" onClick={() => setCursor(undefined)}>
          返回首批
        </Button>
        <Button
          size="small"
          disabled={!data.next_cursor}
          onClick={() => setCursor(data.next_cursor ?? undefined)}
        >
          下一批
        </Button>
      </Space>
    </div>
  ) : null;
}
const collectors = [
  ['identity_status', '身份与基础状态'],
  ['runtime', '运行与锁事务'],
  ['statement_digest', 'SQL 摘要'],
  ['schema_config', '结构与配置'],
  ['replication', '复制基础'],
  ['error_log', '错误日志'],
  ['slow_log', '慢日志'],
  ['host', '主机资料'],
];
export function Onboarding() {
  const can = useCan();
  const { draftId } = useParams();
  const navigate = useScopedNavigate();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [step, setStep] = useState(0);
  const [id, setId] = useState(draftId);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [task, setTask] = useState<TaskRef>();
  const [test, setTest] = useState<Row>();
  const [created, setCreated] = useState<Instance>();
  const [credentialRef, setCredentialRef] = useState<string>();
  const [draftRevision, setDraftRevision] = useState<number>();
  const bindingRevision = useRef(0);
  const systems = useResource<Page>('/systems');
  const profiles = useResource<Page>('/collection-profiles');
  const options = useResource<Row>('/settings/connection-options');
  const draft = useResource<Row>(draftId ? `/connection-drafts/${draftId}` : null);
  const mode = Form.useWatch(['connection', 'mode'], form);
  const selectedSystem = Form.useWatch('system_id', form);
  useEffect(() => {
    if (draft.data) {
      const data = (draft.data.config ?? draft.data) as Row;
      form.setFieldsValue(data);
      setCredentialRef(text(draft.data.credential_ref ?? data.credential_ref, ''));
      setDraftRevision(Number(draft.data.revision));
    }
  }, [draft.data, form]);
  useEffect(() => {
    const system = pageItems(systems.data).find((s) => s.id === selectedSystem);
    if (system?.environment) form.setFieldValue('environment', system.environment);
  }, [selectedSystem, systems.data, form]);
  const fields = [
    ['name', 'system_id', 'environment'],
    [
      ['connection', 'mode'],
      ['connection', 'db_host'],
      ['connection', 'db_port'],
    ],
    [],
    [],
    ['collection_profile'],
    [],
  ];
  function config() {
    const values = form.getFieldsValue(true);
    const credentials = values.credentials as Row | undefined;
    const payload = {
      ...values,
      engine: 'mysql',
      model_policy: 'rules_only',
      connection: {
        ...(values.connection ?? {}),
        tls_profile_ref: values.connection?.tls_profile_ref ?? null,
        ssh_profile_ref: values.connection?.ssh_profile_ref ?? null,
      },
      credential_ref: credentialRef,
      revision: draftRevision,
    };
    if (!credentials?.password) delete payload.credentials;
    return payload;
  }
  async function saveDraft() {
    setBusy(true);
    setError(undefined);
    try {
      const data = id
        ? await api.patch<Row>(`/connection-drafts/${id}`, config())
        : await api.post<Row>('/connection-drafts', config());
      setId(text(data.id));
      setDraftRevision(Number(data.revision));
      setCredentialRef(text(data.credential_ref, credentialRef));
      form.resetFields(['credentials']);
      message.success('草稿已保存，凭据字段已清除');
      if (!draftId) navigate(`/assets/onboarding/${data.id}`, { replace: true });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function precheck() {
    setBusy(true);
    setError(undefined);
    setTest(undefined);
    const startedRevision = bindingRevision.current;
    try {
      await form.validateFields([
        ['connection', 'db_host'],
        ['connection', 'db_port'],
      ]);
      const payload = config();
      if (!payload.credentials && !credentialRef)
        throw new Error('请提供只读数据库凭据，或恢复已保存的凭据引用。');
      const ref = await api.post<TaskRef>('/connections/test', payload, true);
      const done = await awaitTask(ref, setTask, undefined, { readConnectionTestFailure: true });
      const testId = connectionTestId(done);
      if (!testId) throw new Error('任务未返回有效预检引用，请在任务中心核查。');
      const result = await api.get<Row>(`/connections/tests/${testId}`);
      if (startedRevision !== bindingRevision.current)
        throw new Error('预检期间配置已修改，请按当前配置重新预检。');
      setTest(result);
      form.setFieldValue('accept_capability_gaps', false);
      if (connectionTestPassed(result)) {
        if (result.credential_ref) {
          setCredentialRef(text(result.credential_ref));
          form.resetFields(['credentials']);
        }
        message.success('身份预检通过，请核对能力与缺口');
      } else message.warning('预检未通过，请核对失败步骤与修正指引');
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function next() {
    try {
      await form.validateFields(fields[step]);
      if (
        step === 2 &&
        rows(test?.capabilities).some(
          (c) => !['available', 'not_applicable'].includes(text(c.availability)),
        ) &&
        !form.getFieldValue('accept_capability_gaps')
      )
        throw new Error('请逐项核对并明确接受当前资料缺口。');
      if (step === 2 && !test) throw new Error('请先执行连接与能力预检。');
      if (step === 2 && !connectionTestPassed(test))
        throw new Error('当前预检尚未通过，请核对失败项。');
      setError(undefined);
      setStep((v) => v + 1);
    } catch (e) {
      if (e instanceof Error) setError(e);
    }
  }
  async function activate() {
    setBusy(true);
    setError(undefined);
    try {
      if (!created && !connectionTestPassed(test))
        throw new Error('预检未通过或已到期，请重新预检。');
      if (!test) throw new Error('预检结果已失效，请重新预检。');
      const instance = created
        ? await api.get<Instance>(`/instances/${created.id}`)
        : await api.post<Instance>('/instances', {
            ...config(),
            credentials: undefined,
            test_id: test.test_id ?? test.id,
          });
      setCreated(instance);
      if (!can('asset.manage')) {
        message.success('资产已登记，等待有权人员启用采集');
        return;
      }
      const active = instance.lifecycle === 'active';
      const ref = await api.post<TaskRef>(
        `/instances/${instance.id}/${active ? 'collect' : 'activate'}`,
        active
          ? { collectors: form.getFieldValue('requested_collectors') }
          : { config_revision: instance.config_revision },
        true,
      );
      const done = await awaitTask(ref, setTask);
      setCreated(await api.get<Instance>(`/instances/${instance.id}`));
      const incomplete =
        done.status === 'partial' ||
        Object.values((done.result?.collectors ?? {}) as Row).some(
          (v) => !['succeeded', 'not_applicable'].includes(text(v)),
        );
      if (incomplete) message.warning('核心采集已完成，仍有来源缺口；可核对配置后继续补采。');
      else message.success('实例已登记，选定资料首采已完成');
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  const values = form.getFieldsValue(true);
  const capabilities = rows(test?.capabilities);
  const collectorGaps = Object.values((task?.result?.collectors ?? {}) as Row).some(
    (v) => !['succeeded', 'not_applicable'].includes(text(v)),
  );
  const firstCollectionComplete =
    !!task &&
    ['activate_instance', 'collect_instance'].includes(task.type) &&
    task.status === 'succeeded' &&
    !collectorGaps;
  return (
    <>
      <Title
        title="接入 MySQL"
        description="通过只读预检确认身份与能力，再登记资产并启动首采。"
        actions={
          <Button onClick={saveDraft} loading={busy}>
            保存草稿
          </Button>
        }
      />
      <LoadState loading={draft.loading} error={draft.error} retry={draft.reload}>
        <Card className="wizard">
          <Steps
            current={step}
            items={['基本信息', '连接方式', '连接预检', '资料范围', '采集策略', '确认启用'].map(
              (title) => ({ title }),
            )}
          />
          <ErrorNotice error={error} />
          <TaskProgress task={task} />
          <Form
            form={form}
            layout="vertical"
            initialValues={{
              engine: 'mysql',
              connection: { mode: 'direct', db_port: 3306 },
              requested_collectors: [
                'identity_status',
                'runtime',
                'statement_digest',
                'schema_config',
                'replication',
                'error_log',
              ],
              selected_schemas: [],
              model_policy: 'rules_only',
            }}
            onValuesChange={(changed) => {
              if (changed.host_profile_ref || changed.log_profile_ref) {
                const selected = form.getFieldValue('requested_collectors') ?? [];
                form.setFieldValue('requested_collectors', [
                  ...new Set([
                    ...selected,
                    ...(changed.host_profile_ref ? ['host'] : []),
                    ...(changed.log_profile_ref ? ['slow_log'] : []),
                  ]),
                ]);
              }
              if (
                changed.connection ||
                changed.credentials ||
                changed.system_id ||
                'host_profile_ref' in changed ||
                'log_profile_ref' in changed
              ) {
                bindingRevision.current += 1;
                setTest(undefined);
                setTask(undefined);
              }
            }}
            requiredMark={false}
          >
            <div className="wizard-body">
              <div hidden={step !== 0}>
                <Typography.Title level={4}>资产归属与名称</Typography.Title>
                <Typography.Paragraph type="secondary">
                  每个实例只归属一个业务系统。归属关系不会自动授予业务资料访问权。
                </Typography.Paragraph>
                <Form.Item
                  name="name"
                  label="实例名称"
                  rules={[{ required: true, message: '请输入实例名称' }]}
                >
                  <Input maxLength={120} placeholder="填写便于团队识别的名称" />
                </Form.Item>
                <Form.Item
                  name="system_id"
                  label="业务系统"
                  rules={[{ required: true, message: '请选择有权接入的业务系统' }]}
                >
                  <Select
                    loading={systems.loading}
                    options={pageItems(systems.data).map((s) => ({
                      value: text(s.id),
                      label: text(s.name),
                    }))}
                    placeholder="选择授权系统"
                  />
                </Form.Item>
                <Form.Item name="environment" label="环境" rules={[{ required: true }]}>
                  <Input readOnly placeholder="由业务系统归属决定" />
                </Form.Item>
                <Form.Item name="description" label="用途说明">
                  <Input.TextArea rows={3} maxLength={1000} />
                </Form.Item>
                <ErrorNotice error={systems.error} />
              </div>
              <div hidden={step !== 1}>
                <Typography.Title level={4}>只读连接配置</Typography.Title>
                <Form.Item name={['connection', 'mode']} label="连接方式">
                  <Select
                    options={[
                      { value: 'direct', label: '数据库直连' },
                      { value: 'ssh', label: '已配置的 SSH 受限通道' },
                    ]}
                  />
                </Form.Item>
                <GridRow gutter={16}>
                  <Col span={17}>
                    <Form.Item
                      name={['connection', 'db_host']}
                      label={mode === 'ssh' ? '数据库地址（相对于 SSH 主机）' : '数据库地址'}
                      rules={[{ required: true, message: '请输入数据库地址' }]}
                    >
                      <Input placeholder="主机名或 IP" />
                    </Form.Item>
                  </Col>
                  <Col span={7}>
                    <Form.Item
                      name={['connection', 'db_port']}
                      label="端口"
                      rules={[{ required: true }]}
                    >
                      <InputNumber min={1} max={65535} style={{ width: '100%' }} />
                    </Form.Item>
                  </Col>
                </GridRow>
                <Form.Item
                  name={['connection', 'tls_mode']}
                  label="TLS 验证模式"
                  initialValue="verify_identity"
                >
                  <Select
                    options={[
                      { value: 'verify_identity', label: '校验 CA 与服务端身份' },
                      { value: 'disabled', label: '隔离回环或 SSH 隧道内不额外启用 TLS' },
                    ]}
                  />
                </Form.Item>
                <Form.Item name={['connection', 'tls_profile_ref']} label="TLS 配置">
                  <Select
                    allowClear
                    placeholder="未选择 TLS 配置"
                    options={rows(options.data?.tls_profiles).map((v) => ({
                      value: text(v.id),
                      label: text(v.name),
                    }))}
                  />
                </Form.Item>
                {mode === 'ssh' && (
                  <Form.Item
                    name={['connection', 'ssh_profile_ref']}
                    label="SSH 通道引用"
                    rules={[{ required: true, message: '请选择已配置的 SSH 通道' }]}
                  >
                    <Select
                      placeholder="由管理员配置、固定主机身份的通道"
                      options={rows(options.data?.ssh_profiles).map((v) => ({
                        value: text(v.id),
                        label: text(v.name),
                      }))}
                    />
                  </Form.Item>
                )}
                <Alert
                  type="info"
                  showIcon
                  title={
                    credentialRef
                      ? '已有已保存的凭据引用；留空可继续使用。'
                      : '输入专用只读账号，凭据仅写入服务端。'
                  }
                  className="notice"
                />
                <Form.Item name={['credentials', 'username']} label="只读数据库用户名">
                  <Input autoComplete="off" />
                </Form.Item>
                <Form.Item name={['credentials', 'password']} label="数据库密码">
                  <Input.Password autoComplete="new-password" />
                </Form.Item>
                <Form.Item name="host_profile_ref" label="可选主机来源（受限配置引用）">
                  <Select
                    allowClear
                    placeholder="仅数据库视角 / 未绑定主机"
                    options={rows(options.data?.host_profiles).map((p) => ({
                      value: text(p.id),
                      label: text(p.name),
                    }))}
                  />
                </Form.Item>
                <Form.Item name="log_profile_ref" label="可选文件日志来源（白名单配置引用）">
                  <Select
                    allowClear
                    placeholder="仅数据库可读日志接口"
                    options={rows(options.data?.log_profiles).map((p) => ({
                      value: text(p.id),
                      label: text(p.name),
                    }))}
                  />
                </Form.Item>
                <ErrorNotice error={options.error} />
              </div>
              <div hidden={step !== 2}>
                <Typography.Title level={4}>连接、身份与能力预检</Typography.Title>
                <Typography.Paragraph type="secondary">
                  测试仅执行白名单只读探测，不授予数据库权限，不修改配置。
                </Typography.Paragraph>
                <Button type="primary" onClick={precheck} loading={busy}>
                  开始只读预检
                </Button>
                {test && (
                  <>
                    <ConnectionTestView data={test} />
                    <Capabilities capabilities={capabilities as Capability[]} />
                    {capabilities.some(
                      (c) => !['available', 'not_applicable'].includes(text(c.availability)),
                    ) && (
                      <Form.Item
                        name="accept_capability_gaps"
                        valuePropName="checked"
                        className="section"
                      >
                        <Checkbox disabled={!connectionTestPassed(test)}>
                          我已逐项核对能力限制，并接受当前缺失或降级来源；这些来源不会被显示为健康。
                        </Checkbox>
                      </Form.Item>
                    )}
                  </>
                )}
              </div>
              <div hidden={step !== 3}>
                <Typography.Title level={4}>选择需要收集的资料</Typography.Title>
                <Form.Item name="selected_schemas" label="Schema 白名单">
                  <Select mode="tags" placeholder="添加需采集元数据的 schema；留空遵循服务端策略" />
                </Form.Item>
                <Alert
                  type="warning"
                  showIcon
                  title="Schema 范围控制采集，不构成共享实例的安全隔离边界。"
                  className="notice"
                />
                <Form.Item name="requested_collectors" label="资料类型">
                  <Checkbox.Group
                    options={collectors.map(([value, label]) => ({ value, label }))}
                  />
                </Form.Item>
                <Alert
                  type="info"
                  showIcon
                  title="主机资料仅使用明确绑定的受限采集器。未配置来源保留缺口。"
                />
                <div className="section">
                  <Capabilities capabilities={capabilities as Capability[]} />
                </div>
              </div>
              <div hidden={step !== 4}>
                <Typography.Title level={4}>采集与资料策略</Typography.Title>
                <Form.Item
                  name="collection_profile"
                  label="采集模板"
                  rules={[{ required: true, message: '请选择实际已发布的采集模板' }]}
                >
                  <Select
                    loading={profiles.loading}
                    placeholder="选择服务端已发布模板"
                    options={pageItems(profiles.data).map((p) => ({
                      value: text(p.id ?? p.name),
                      label: text(p.name),
                    }))}
                  />
                </Form.Item>
                <ErrorNotice error={profiles.error} />
                <DataTable
                  items={pageItems(profiles.data).flatMap((p) =>
                    Object.entries((p.intervals ?? {}) as Row).map(([collector, interval]) => ({
                      id: text(p.id) + collector,
                      profile: p.name,
                      collector,
                      interval,
                    })),
                  )}
                  columns={[
                    { key: 'profile', title: '采集模板' },
                    {
                      key: 'collector',
                      title: '资料类型',
                      render: (v) => collectors.find((c) => c[0] === v)?.[1] ?? text(v),
                    },
                    { key: 'interval', title: '计划周期', render: (v) => text(v) + ' 秒' },
                  ]}
                />
                <Alert
                  type="info"
                  showIcon
                  title="新资产外发默认关闭：仅规则资料（rules_only）。模型策略由有权管理员独立配置。"
                  className="notice"
                />
              </div>
              <div hidden={step !== 5}>
                <Typography.Title level={4}>核对后登记并启动首采</Typography.Title>
                <Descriptions
                  column={2}
                  items={[
                    { key: 'name', label: '实例', children: text(values.name) },
                    {
                      key: 'system',
                      label: '归属系统',
                      children: text(
                        pageItems(systems.data).find((s) => s.id === values.system_id)?.name,
                      ),
                    },
                    {
                      key: 'endpoint',
                      label: '连接',
                      children:
                        text(values.connection?.db_host) + ':' + text(values.connection?.db_port),
                    },
                    { key: 'mode', label: '方式', children: text(values.connection?.mode) },
                    { key: 'uuid', label: '已确认身份', children: text(test?.server_uuid) },
                    { key: 'environment', label: '环境', children: text(values.environment) },
                    {
                      key: 'profile',
                      label: '采集模板',
                      children: text(
                        pageItems(profiles.data).find(
                          (p) =>
                            p.id === values.collection_profile ||
                            p.name === values.collection_profile,
                        )?.name,
                      ),
                    },
                    {
                      key: 'collectors',
                      label: '已选资料',
                      span: 2,
                      children:
                        (Array.isArray(values.requested_collectors)
                          ? values.requested_collectors
                          : []
                        )
                          .map((v: unknown) => collectors.find((c) => c[0] === v)?.[1] ?? text(v))
                          .join('、') || '未选择',
                    },
                    {
                      key: 'schemas',
                      label: 'Schema 范围',
                      span: 2,
                      children:
                        Array.isArray(values.selected_schemas) && values.selected_schemas.length
                          ? values.selected_schemas.join('、')
                          : '未指定；遵循服务端策略',
                    },
                    {
                      key: 'host',
                      label: '主机来源',
                      children: values.host_profile_ref
                        ? text(
                            rows(options.data?.host_profiles).find(
                              (p) => p.id === values.host_profile_ref,
                            )?.name ?? values.host_profile_ref,
                          )
                        : '未配置 / 仅数据库视角',
                    },
                    {
                      key: 'logs',
                      label: '文件日志来源',
                      children: values.log_profile_ref
                        ? text(
                            rows(options.data?.log_profiles).find(
                              (p) => p.id === values.log_profile_ref,
                            )?.name ?? values.log_profile_ref,
                          )
                        : '未配置 / 仅数据库接口',
                    },
                    {
                      key: 'tls',
                      label: 'TLS',
                      span: 2,
                      children:
                        values.connection?.tls_mode === 'disabled'
                          ? '隔离回环 / SSH 通道内未额外启用 TLS'
                          : '校验 CA 与服务端身份' +
                            (values.connection?.tls_profile_ref
                              ? ' · ' +
                                text(
                                  rows(options.data?.tls_profiles).find(
                                    (p) => p.id === values.connection.tls_profile_ref,
                                  )?.name ?? values.connection.tls_profile_ref,
                                )
                              : ''),
                    },
                    { key: 'policy', label: '模型外发', children: '关闭 / rules_only' },
                  ]}
                />
                <Capabilities capabilities={capabilities as Capability[]} />
                <Alert
                  type="warning"
                  showIcon
                  title="登记成功与资料健康分别判断。可选来源失败将保留部分覆盖，首采失败不会显示健康。"
                  className="notice"
                />
                {created && (
                  <Alert
                    type="info"
                    title="资产已登记"
                    description={
                      <ContextLink to={`/assets/${created.id}/overview`}>
                        打开实例详情 →
                      </ContextLink>
                    }
                  />
                )}
              </div>
            </div>
          </Form>
          <div className="wizard-footer">
            <Button
              onClick={() => (step ? setStep((v) => v - 1) : navigate('/assets'))}
              disabled={busy}
            >
              {step ? '上一步' : '返回目录'}
            </Button>
            <Space>
              <span className="muted">第 {step + 1} / 6 步</span>
              {step < 5 ? (
                <Button
                  type="primary"
                  onClick={next}
                  disabled={busy || (step === 2 && !connectionTestPassed(test))}
                >
                  下一步
                </Button>
              ) : (
                <Button
                  type="primary"
                  onClick={activate}
                  loading={busy}
                  disabled={
                    firstCollectionComplete ||
                    (!!created && !can('asset.manage')) ||
                    (created?.lifecycle === 'active' && !can('collection.trigger'))
                  }
                >
                  {created
                    ? firstCollectionComplete
                      ? '首采已完成'
                      : !can('asset.manage')
                        ? '资产已登记'
                        : created.lifecycle === 'active'
                          ? can('collection.trigger')
                            ? '核对后补采缺口'
                            : '缺口待授权补采'
                          : '重试首采'
                    : can('asset.manage')
                      ? '登记并启动首采'
                      : '登记资产，等待授权启用'}
                </Button>
              )}
            </Space>
          </div>
        </Card>
      </LoadState>
    </>
  );
}
export function Capabilities({ capabilities }: { capabilities: Capability[] }) {
  return (
    <DataTable
      items={capabilities}
      columns={[
        {
          key: 'collector',
          title: '资料来源',
          render: (v) => collectors.find((c) => c[0] === v)?.[1] ?? text(v),
        },
        { key: 'availability', title: '能力状态', render: (v) => <Status value={v} /> },
        { key: 'reason_code', title: '限制说明' },
        { key: 'checked_at', title: '探测时间', render: (v) => <Time value={v} /> },
      ]}
    />
  );
}
export function AssetDetail() {
  const { id, tab = 'overview' } = useParams();
  const navigate = useScopedNavigate();
  const { refresh, scope } = useScope();
  const can = useCan();
  const { message } = App.useApp();
  const instance = useResource<Instance>(`/instances/${id}`, refresh);
  const overview = useResource<Row>(
    instance.data?.data_access === true
      ? `/instances/${id}/overview?${query(scope as unknown as Row)}`
      : null,
    refresh,
  );
  const [drawer, setDrawer] = useState(false);
  const [error, setError] = useState<unknown>();
  const [task, setTask] = useState<TaskRef>();
  const [busy, setBusy] = useState(false);
  const [rotate, setRotate] = useState(false);
  const [latestTest, setLatestTest] = useState<Row>();
  const [editName, setEditName] = useState(false);
  const [newName, setNewName] = useState('');
  const [archive, setArchive] = useState(false);
  const [archiveId, setArchiveId] = useState('');
  const [archiveReason, setArchiveReason] = useState('');
  const [form] = Form.useForm();
  const [selected, setSelected] = useState<string[]>(['identity_status', 'runtime']);
  const [collect, setCollect] = useState(false);
  async function action(name: string, body: Row = {}, async = true) {
    setBusy(true);
    setError(undefined);
    try {
      const result = await api.post<Row>(`/instances/${id}/${name}`, body, async);
      if (result.poll_url || result.type) {
        const connection = result.type === 'connection_test';
        const done = await awaitTask(
          result as TaskRef,
          setTask,
          undefined,
          connection ? { readConnectionTestFailure: true } : undefined,
        );
        if (connection) {
          const testId = connectionTestId(done);
          if (!testId) throw new Error('预检任务未返回有效结果引用。');
          const tested = await api.get<Row>(`/connections/tests/${testId}`);
          setLatestTest(tested);
          if (!connectionTestPassed(tested)) {
            message.warning('预检未通过，请核对失败步骤');
            instance.reload();
            overview.reload();
            return false;
          }
        }
      }
      message.success('操作已完成');
      instance.reload();
      overview.reload();
      return true;
    } catch (e) {
      setError(e);
      instance.reload();
      overview.reload();
      return false;
    } finally {
      setBusy(false);
    }
  }
  const active = ['overview', 'metrics'].includes(tab)
    ? 'metrics'
    : tab === 'structure'
      ? 'structure'
      : ['replication', 'config'].includes(tab)
        ? 'config'
        : ['logs', 'host'].includes(tab)
          ? 'logs'
          : tab === 'collection'
            ? 'metrics'
            : tab;
  const data = instance.data;
  return (
    <>
      <Title
        title={data?.name ?? '实例详情'}
        description="当前配置与选定绝对窗口的观测资料分别显示。"
        actions={
          <>
            {data?.data_access === true && can('investigation.create') && (
              <ContextLink to={`/investigations?instance_id=${id}`}>
                <Button type="primary">调查此实例</Button>
              </ContextLink>
            )}
            {data?.data_access === true && can('collection.trigger', 'collection.trigger') && (
              <Button onClick={() => setCollect(true)}>只读补采</Button>
            )}
            <Button onClick={() => setDrawer(true)}>采集配置</Button>
          </>
        }
      />
      <ErrorNotice error={error} />
      <TaskProgress task={task} />
      <LoadState loading={instance.loading} error={instance.error} retry={instance.reload}>
        {data && (
          <>
            <Card className="instance-summary">
              <div className="instance-title">
                <Space>
                  {data.data_access === true && <Tag>MySQL {text(data.server_version)}</Tag>}
                  <Tag>{data.environment}</Tag>
                  <Status value={data.lifecycle} />
                  {data.data_access === true ? (
                    <Status value={data.health} />
                  ) : (
                    <Tag>未授予观测资料权限</Tag>
                  )}
                </Space>
                <span className="muted">实例 ID：{data.id}</span>
              </div>
              {data.lifecycle === 'revalidation_required' && (
                <Alert
                  type="error"
                  showIcon
                  title="身份或连接配置需要重验，已停止后续采集。请核对数据库身份与配置版本。"
                  className="notice"
                />
              )}
              <FieldGrid
                data={data}
                fields={[
                  ['system_name', '业务系统'],
                  ['server_uuid', '数据库身份'],
                  ['last_success_at', '最近成功采集'],
                  ['config_revision', '配置版本'],
                  ['model_policy', '模型策略'],
                  ['health_reason', '资料说明'],
                ]}
              />
              {data.data_access === true ? (
                <LoadState {...overview} retry={overview.reload}>
                  {overview.data && <SourceHealthSummary data={overview.data} />}
                </LoadState>
              ) : (
                <Alert
                  type="info"
                  showIcon
                  title="未授予观测资料权限"
                  description="当前授权允许管理此资产及连接配置。指标、SQL、事务锁、结构、日志和采集证据需另行授予此实例的观测资料权限。"
                  className="notice"
                />
              )}
            </Card>
            {data.data_access === true && (
              <Card className="instance-tabs">
                <Tabs
                  activeKey={active}
                  onChange={(v) => navigate(`/assets/${id}/${v}`)}
                  items={[
                    ['metrics', '指标'],
                    ['sql', 'SQL'],
                    ['locks', '锁事务'],
                    ['structure', '库表索引'],
                    ['config', '配置复制'],
                    ['logs', '日志主机'],
                  ].map(([key, label]) => ({ key, label }))}
                />
                {active === 'metrics' ? (
                  <Metrics id={id!} />
                ) : active === 'sql' ? (
                  <InstanceRecords id={id!} kind="queries" />
                ) : active === 'locks' ? (
                  <InstanceRecords id={id!} kind="locks" />
                ) : active === 'structure' ? (
                  <>
                    <div className="table-toolbar">
                      <Typography.Text type="secondary">已采表、索引与列元数据</Typography.Text>
                      <ExplainAction id={id!} />
                    </div>
                    <InstanceRecords id={id!} kind="schemas" />
                  </>
                ) : active === 'config' ? (
                  <Tabs
                    defaultActiveKey={tab === 'replication' ? 'replication' : 'config'}
                    items={[
                      {
                        key: 'config',
                        label: '配置白名单',
                        children: <InstanceRecords id={id!} kind="config" />,
                      },
                      {
                        key: 'replication',
                        label: '复制来源',
                        children: <InstanceRecords id={id!} kind="replication" />,
                      },
                    ]}
                  />
                ) : (
                  <Tabs
                    defaultActiveKey={tab === 'host' ? 'host' : 'logs'}
                    items={[
                      {
                        key: 'logs',
                        label: '数据库日志',
                        children: <InstanceRecords id={id!} kind="logs" />,
                      },
                      {
                        key: 'host',
                        label: '主机来源',
                        children: (
                          <HostState
                            capabilities={data.capabilities ?? []}
                            overview={overview.data}
                          />
                        ),
                      },
                    ]}
                  />
                )}
              </Card>
            )}
          </>
        )}
      </LoadState>
      <Drawer
        title="采集配置与运行"
        width={820}
        open={drawer || tab === 'collection'}
        onClose={() => {
          setDrawer(false);
          if (tab === 'collection') navigate(`/assets/${id}/overview`);
        }}
      >
        <ErrorNotice error={error} />
        {data && (
          <>
            <FieldGrid
              data={data}
              fields={[
                ['lifecycle', '生命周期'],
                ['config_revision', '配置版本'],
                ['collection_profile', '采集模板'],
                ['credential_version', '凭据版本'],
                ['has_secret', '凭据配置'],
                ['last_success_at', '最近成功'],
              ]}
            />
            <Capabilities capabilities={data.capabilities ?? []} />
            {latestTest ? (
              <Card title="待确认的身份预检" className="section">
                <ConnectionTestView data={latestTest} />
                <Alert
                  type="warning"
                  title="请人工核对数据库身份。只有 UUID 与原资产一致才允许沿用历史。"
                  className="notice"
                />
                <Button
                  type="primary"
                  loading={busy}
                  disabled={
                    !connectionTestPassed(latestTest) || data.lifecycle !== 'revalidation_required'
                  }
                  onClick={() =>
                    action(
                      'revalidate',
                      {
                        test_id: latestTest.test_id ?? latestTest.id,
                        config_revision: data.config_revision,
                      },
                      false,
                    )
                  }
                >
                  确认身份并完成重验
                </Button>
              </Card>
            ) : null}
            <Space wrap className="section">
              {can('asset.manage', 'asset.manage') &&
                ['ready', 'paused'].includes(data.lifecycle) && (
                  <Button
                    loading={busy}
                    onClick={() => action('activate', { config_revision: data.config_revision })}
                  >
                    启用并首采
                  </Button>
                )}
              {can('asset.manage', 'asset.manage') && data.lifecycle === 'active' && (
                <Button loading={busy} onClick={() => action('pause', {}, false)}>
                  暂停采集
                </Button>
              )}
              {can('asset.manage', 'asset.manage') && (
                <Button onClick={() => action('capabilities/recheck')}>重新探测能力</Button>
              )}
              {can('asset.manage', 'asset.manage') && (
                <>
                  <Button onClick={() => setRotate(true)}>轮换凭据</Button>
                  <Button
                    onClick={() => {
                      setNewName(data.name);
                      setEditName(true);
                    }}
                  >
                    编辑名称
                  </Button>
                  <Button danger onClick={() => setArchive(true)}>
                    退役资产
                  </Button>
                </>
              )}
            </Space>
            {data.data_access === true && <InstanceRecords id={id!} kind="collection-runs" />}
          </>
        )}
      </Drawer>
      <Modal
        title="只读补采"
        open={collect}
        onCancel={() => setCollect(false)}
        confirmLoading={busy}
        onOk={async () => {
          if (await action('collect', { collectors: selected })) setCollect(false);
        }}
      >
        <Alert
          type="info"
          showIcon
          title="仅执行固定采集器，会产生数据库只读负载；任务受并发与超时限制。"
          className="notice"
        />
        <Checkbox.Group
          value={selected}
          onChange={(v) => setSelected(v as string[])}
          options={collectors.map(([value, label]) => ({ value, label }))}
        />
      </Modal>
      <Modal
        title="轮换只读凭据"
        open={rotate}
        onCancel={() => setRotate(false)}
        confirmLoading={busy}
        onOk={async () => {
          try {
            const values = await form.validateFields();
            if (
              await action('credentials/rotate', {
                credentials: values,
                config_revision: data?.config_revision,
              })
            ) {
              form.resetFields();
              setRotate(false);
            }
          } catch (e) {
            if (e instanceof Error) setError(e);
          }
        }}
      >
        <Alert
          type="warning"
          title="现有连接将暂停并进入重验；新凭据通过测试后才能生效。"
          className="notice"
        />
        <Form form={form} layout="vertical">
          <Form.Item name="username" label="只读用户名" rules={[{ required: true }]}>
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item name="password" label="新密码" rules={[{ required: true }]}>
            <Input.Password autoComplete="new-password" />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title="编辑资产名称"
        open={editName}
        onCancel={() => setEditName(false)}
        confirmLoading={busy}
        onOk={async () => {
          setBusy(true);
          setError(undefined);
          try {
            await api.patch(`/instances/${id}`, { name: newName }, data?.config_revision);
            setEditName(false);
            instance.reload();
          } catch (e) {
            setError(e);
          } finally {
            setBusy(false);
          }
        }}
      >
        <ErrorNotice error={error} />
        <Input
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          maxLength={120}
          aria-label="实例名称"
        />
      </Modal>
      <Modal
        title="退役资产并停止后续采集"
        open={archive}
        onCancel={() => setArchive(false)}
        confirmLoading={busy}
        onOk={async () => {
          if (await action('archive', { instance_id: archiveId, reason: archiveReason }, false))
            setArchive(false);
        }}
      >
        <ErrorNotice error={error} />
        <Alert
          type="warning"
          title="退役保留授权范围内历史资料。请核对实例 ID 并填写原因。"
          className="notice"
        />
        <Typography.Paragraph copyable>{id}</Typography.Paragraph>
        <Input
          placeholder="输入确认的实例 ID"
          value={archiveId}
          onChange={(e) => setArchiveId(e.target.value)}
          aria-label="确认实例 ID"
        />
        <Input.TextArea
          className="section"
          rows={3}
          placeholder="退役原因"
          value={archiveReason}
          onChange={(e) => setArchiveReason(e.target.value)}
          aria-label="退役原因"
        />
      </Modal>
    </>
  );
}
function HostState({ capabilities, overview }: { capabilities: Capability[]; overview?: Row }) {
  const host = capabilities.filter((c) => c.collector === 'host');
  return (
    <>
      <Alert
        type="info"
        showIcon
        title="数据库主机视角"
        description="仅展示已绑定主机来源，控制端资源不替代数据库主机资料。"
        className="notice"
      />
      {host.length ? (
        <Capabilities capabilities={host} />
      ) : (
        <Alert type="warning" title="尚未配置主机资料来源" />
      )}
      <RecordCard title="主机观测资料" data={overview?.host} />
    </>
  );
}
function Metrics({ id }: { id: string }) {
  const [group, setGroup] = useState('runtime');
  return (
    <>
      <div className="table-toolbar">
        <Typography.Text type="secondary">
          曲线只连接真实有效采样。空值与计数器重置保留断点。
        </Typography.Text>
        <Select
          value={group}
          onChange={setGroup}
          options={[
            { value: 'runtime', label: '连接与吞吐' },
            { value: 'buffer', label: '缓存与等待' },
          ]}
        />
      </div>
      <GridRow gutter={[18, 18]}>
        {(group === 'runtime'
          ? [
              ['connections', '连接数'],
              ['threads_running', '运行线程'],
              ['questions_per_second', '语句吞吐'],
            ]
          : [
              ['buffer_pool_hit_ratio', 'Buffer Pool 命中率'],
              ['lock_waits', '锁等待'],
            ]
        ).map(([template, title]) => (
          <Col xs={24} xl={12} key={template}>
            <MetricChart id={id} template={template} title={title} />
          </Col>
        ))}
      </GridRow>
    </>
  );
}
function MetricChart({ id, template, title }: { id: string; template: string; title: string }) {
  const { scope, refresh } = useScope();
  const resource = useResource<MetricSeries>(
    `/instances/${id}/metrics?${query({ template, from: scope.from, to: scope.to, step: 30 })}`,
    refresh,
  );
  const data = resource.data;
  const option = useMemo<EChartsOption>(
    () => ({
      color: ['#248a76'],
      tooltip: { trigger: 'axis' },
      grid: { left: 52, right: 20, top: 22, bottom: 36 },
      xAxis: {
        type: 'time',
        axisLine: { lineStyle: { color: '#d3dce0' } },
        axisLabel: { color: '#839099' },
      },
      yAxis: {
        type: 'value',
        name: data?.unit,
        splitLine: { lineStyle: { color: '#eef1f4' } },
        axisLabel: { color: '#839099' },
      },
      series: [
        {
          name: title,
          type: 'line',
          showSymbol: false,
          connectNulls: false,
          lineStyle: { width: 2 },
          areaStyle: { opacity: 0.07 },
          data: (data?.points ?? []).map((p) => [p.time, p.value]),
        },
      ],
    }),
    [data, title],
  );
  return (
    <Card size="small" title={title} className="metric-card">
      <LoadState {...resource} retry={resource.reload}>
        {data && (
          <>
            <SourceCoverage data={data} />
            {data.points?.some((p) => p.value !== null) ? (
              <Chart option={option} height={240} />
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="选定窗口内没有可计算的有效采样"
              />
            )}
            <Typography.Text type="secondary">
              实际步长 {text(data.effective_step_seconds)} 秒 · 来源 {text(data.source)}
            </Typography.Text>
          </>
        )}
      </LoadState>
    </Card>
  );
}
function numberValue(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value)
    ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 3 }).format(value)
    : '未知';
}
const tableColumns: Record<
  string,
  { key: string; title: string; render?: (v: unknown, r: Row) => React.ReactNode }[]
> = {
  queries: [
    { key: 'digest', title: 'Digest' },
    { key: 'digest_text', title: '归一化 SQL' },
    { key: 'schema_name', title: '默认 Schema' },
    { key: 'window_count', title: '观测区间次数', render: (v) => numberValue(v) },
    { key: 'total_time_ms', title: '观测区间总耗时（ms）', render: (v) => numberValue(v) },
    { key: 'avg_time_ms', title: '观测区间均值（ms）', render: (v) => numberValue(v) },
    { key: 'window_rows_examined', title: '观测区间扫描行数', render: (v) => numberValue(v) },
    { key: 'rows_examined', title: '累计扫描行数', render: (v) => numberValue(v) },
    {
      key: 'window_validity',
      title: '区间资料质量',
      render: (v, r) => <QueryValidity validity={v} reasons={r.window_reasons} />,
    },
  ],
  locks: [
    { key: 'waiting_thread_id', title: '等待线程' },
    { key: 'blocking_thread_id', title: '阻塞线程' },
    { key: 'wait_seconds', title: '等待秒数' },
    { key: 'object_schema', title: 'Schema' },
    { key: 'object_name', title: '对象' },
    { key: 'lock_mode', title: '锁模式' },
  ],
  schemas: [
    { key: 'table_schema', title: 'Schema' },
    { key: 'table_name', title: '表' },
    { key: 'engine', title: '引擎' },
    { key: 'table_rows', title: '估算行数' },
    { key: 'data_length', title: '估算数据字节' },
    { key: 'index_length', title: '估算索引字节' },
  ],
  logs: [
    { key: 'observed_at', title: '日志时间', render: (v) => <Time value={v} /> },
    { key: 'source', title: '来源' },
    { key: 'severity', title: '等级', render: (v) => <Status value={v} /> },
    { key: 'code', title: '错误码' },
    { key: 'message', title: '脱敏内容' },
  ],
  'collection-runs': [
    { key: 'collector', title: '采集器' },
    { key: 'status', title: '状态', render: (v) => <Status value={v} /> },
    { key: 'started_at', title: '开始', render: (v) => <Time value={v} /> },
    { key: 'duration_ms', title: '耗时 ms' },
    { key: 'error_code', title: '错误码' },
  ],
};
function LockGraph({ items }: { items: Row[] }) {
  const edges = items
    .filter((row) => row.waiting_thread_id !== undefined && row.blocking_thread_id !== undefined)
    .map((row) => ({
      source: text(row.waiting_thread_id),
      target: text(row.blocking_thread_id),
      type: '等待 → 阻塞',
    }));
  const ids = [...new Set(edges.flatMap((edge) => [edge.source, edge.target]))];
  return edges.length ? (
    <div>
      <Typography.Text type="secondary">
        快照等待关系：等待方 → 阻塞方。仅展示已采线程，不推断未观测过程。
      </Typography.Text>
      <RelationshipGraph nodes={ids.map((id) => ({ id, name: `线程 ${id}` }))} edges={edges} />
    </div>
  ) : null;
}
export function InstanceRecords({ id, kind }: { id: string; kind: string }) {
  const { scope, refresh } = useScope();
  const [q, setQ] = useState('');
  const [cursor, setCursor] = useCursor();
  const [selection, setSelection] = useState<Row>();
  const resource = useResource<Row>(
    `/instances/${id}/${kind}?${query({ ...scope, q, cursor })}`,
    refresh,
  );
  const data = resource.data;
  const capability = (data?.capability ?? {}) as Row;
  const items = rows(
    data?.items ??
      data?.queries ??
      data?.tables ??
      data?.waits ??
      data?.logs ??
      data?.runs ??
      data?.channels,
  );
  const columns = tableColumns[kind];
  return (
    <>
      <div className="table-toolbar">
        <Typography.Text type="secondary">
          {kind === 'queries'
            ? '累计快照与可靠的窗口差分分别标识'
            : kind === 'locks'
              ? '锁图是已采时刻的瞬时快照'
              : kind === 'schemas'
                ? '容量与行数为元数据估算，不读取业务行'
                : '仅查询已采集的资料'}
        </Typography.Text>
        {['queries', 'logs', 'schemas'].includes(kind) && (
          <Input.Search
            allowClear
            placeholder="服务端过滤"
            onSearch={(v) => {
              setQ(v);
              setCursor(undefined);
            }}
            style={{ width: 240 }}
          />
        )}
      </div>
      <LoadState {...resource} retry={resource.reload}>
        {data && (
          <>
            <SourceCoverage data={data} />
            {kind === 'queries' && <QueryWindowSummary data={data} />}
            {capability.availability && capability.availability !== 'available' ? (
              <Alert
                type="warning"
                showIcon
                title={<Status value={capability.availability} />}
                description={text(capability.reason_code ?? capability.reason)}
                className="notice"
              />
            ) : null}
            {data.message ? (
              <Alert type="info" title={text(data.message)} className="notice" />
            ) : null}
            {kind === 'locks' && items.length > 0 ? <LockGraph items={items} /> : null}
            {columns ? (
              <DataTable items={items} columns={columns} onRowClick={setSelection} />
            ) : (
              <RecordBlock data={data.data ?? data.config ?? data.replication ?? data} />
            )}
            <Pagination
              data={data.items ? (data as unknown as Page) : undefined}
              setCursor={setCursor}
            />
          </>
        )}
      </LoadState>
      <Drawer
        title="观测证据详情"
        width={720}
        open={!!selection}
        onClose={() => setSelection(undefined)}
      >
        {selection && (
          <>
            <SourceCoverage data={selection} />
            <RecordBlock data={selection} />
            {selection.evidence_id ? (
              <div className="section">
                <ContextLink to={`/evidence/${selection.evidence_id}`}>
                  打开原始证据引用 →
                </ContextLink>
              </div>
            ) : null}
          </>
        )}
      </Drawer>
    </>
  );
}

function ExplainAction({ id }: { id: string }) {
  const can = useCan();
  const navigate = useScopedNavigate();
  const { refresh } = useScope();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [task, setTask] = useState<TaskRef>();
  const [form] = Form.useForm();
  const resource = useResource<Row>(open ? `/instances/${id}/explain-options` : null, refresh);
  const tableKey = Form.useWatch('table_key', form);
  const items = rows(resource.data?.items);
  const table = items.find((item) => JSON.stringify([item.schema, item.table]) === tableKey);
  const columns = rows(table?.columns);
  const permitted = can('data.read') && can('collection.trigger') && can('evidence.read_sensitive');
  async function submit(values: Row) {
    setBusy(true);
    setError(undefined);
    try {
      const selected = items.find(
        (item) => JSON.stringify([item.schema, item.table]) === values.table_key,
      );
      if (!selected || !resource.data) throw new Error('请重新选择新鲜表元数据。');
      const ref = await api.post<TaskRef>(
        `/instances/${id}/explain`,
        {
          template: 'single_table_lookup',
          metadata_evidence_id: resource.data.metadata_evidence_id,
          config_revision: resource.data.config_revision,
          schema: selected.schema,
          table: selected.table,
          projection: values.projection,
          predicates: [{ column: values.predicate_column, value: values.constant }],
          limit: values.limit,
          order_by: values.order_column
            ? { column: values.order_column, direction: values.order_direction ?? 'asc' }
            : undefined,
        },
        true,
      );
      form.resetFields(['constant']);
      const done = await awaitTask(ref, setTask);
      if (!done.result?.evidence_id) throw new Error('任务未返回计划证据，请在任务中心核查。');
      setOpen(false);
      navigate(`/evidence/${done.result.evidence_id}`);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  if (!permitted) return null;
  return (
    <>
      <Button onClick={() => setOpen(true)}>获取单表模板计划</Button>
      <Drawer
        title="受限只读执行计划"
        width={700}
        open={open}
        onClose={() => setOpen(false)}
        footer={
          <Space>
            <Button onClick={() => setOpen(false)}>关闭</Button>
            <Button
              type="primary"
              loading={busy}
              disabled={!resource.data || resource.loading}
              onClick={() => form.submit()}
            >
              提交只读计划任务
            </Button>
          </Space>
        }
      >
        <Alert
          type="info"
          showIcon
          title="固定 single_table_lookup 模板"
          description="从已采基本表和列选择投影与等值绑定条件，仅执行 EXPLAIN FORMAT=JSON。SQL阶段总预算2秒，不执行被解释的业务查询。"
          className="notice"
        />
        <ErrorNotice error={error} />
        <TaskProgress task={task} />
        <LoadState {...resource} retry={resource.reload}>
          {resource.data && (
            <>
              <SourceCoverage data={resource.data} />
              <Form
                form={form}
                layout="vertical"
                onFinish={submit}
                initialValues={{ limit: 20, order_direction: 'asc' }}
              >
                <Form.Item name="table_key" label="已采基本表" rules={[{ required: true }]}>
                  <Select
                    placeholder="选择已采表"
                    options={items.map((item) => ({
                      value: JSON.stringify([item.schema, item.table]),
                      label: `${text(item.schema)} / ${text(item.table)}`,
                    }))}
                    onChange={() =>
                      form.resetFields([
                        'projection',
                        'predicate_column',
                        'constant',
                        'order_column',
                      ])
                    }
                  />
                </Form.Item>
                <Form.Item name="projection" label="投影列（最多8个）" rules={[{ required: true }]}>
                  <Select
                    mode="multiple"
                    maxCount={8}
                    options={columns.map((column) => ({
                      value: text(column.name),
                      label: `${text(column.name)} · ${text(column.data_type)}`,
                    }))}
                  />
                </Form.Item>
                <Form.Item name="predicate_column" label="等值条件列" rules={[{ required: true }]}>
                  <Select
                    options={columns
                      .filter((column) => column.predicate_allowed === true)
                      .map((column) => ({
                        value: text(column.name),
                        label: `${text(column.name)} · ${text(column.data_type)}`,
                      }))}
                  />
                </Form.Item>
                <Form.Item
                  name="constant"
                  label="等值绑定值"
                  rules={[{ required: true, message: '请输入绑定值' }]}
                  extra="以文本常量绑定；仅加密保存引用，任务接收后清除输入，不在计划证据中保存原值。"
                >
                  <Input.Password maxLength={256} autoComplete="off" />
                </Form.Item>
                <Form.Item name="limit" label="LIMIT上限" rules={[{ required: true }]}>
                  <InputNumber min={1} max={100} />
                </Form.Item>
                <Form.Item name="order_column" label="可选排序列">
                  <Select
                    allowClear
                    options={columns.map((column) => ({
                      value: text(column.name),
                      label: text(column.name),
                    }))}
                  />
                </Form.Item>
                <Form.Item name="order_direction" label="排序方向">
                  <Select
                    options={[
                      { value: 'asc', label: '升序' },
                      { value: 'desc', label: '降序' },
                    ]}
                  />
                </Form.Item>
                <Alert
                  type="warning"
                  showIcon
                  title="计划是估计，不能证明查询已运行或优化已生效。"
                  description="视图、函数、JOIN、子查询、任意SQL与ANALYZE均不支持。元数据、身份、权限或配置变化将拒绝任务。"
                />
              </Form>
            </>
          )}
        </LoadState>
      </Drawer>
    </>
  );
}
