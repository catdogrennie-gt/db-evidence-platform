import { useEffect, useState } from 'react';
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Switch,
  Tag,
} from 'antd';
import { useSearchParams } from 'react-router-dom';
import { api, pageItems, query, rows, text } from './api';
import type { Page, Row, TaskRef } from './api';
import { errorMessage, useCan, useResource, useScope, useScopedPath } from './hooks';
import { ContextLink, ErrorNotice, LoadState, Status, Time, Title } from './ui';
import { AccessNotice, InspectionStatus, MetricPanel } from './Intelligence';
import { AnalysisGroups, ItemHistory } from './AnalysisGroups';
import { scopeKey, selectionForScope, sourcePayload } from './intelligenceModel';
import type {
  InspectionPolicy,
  AnalysisGroup,
  MonitoringSource,
  ObjectMetrics,
} from './intelligenceModel';

function MonitoringStatus({ status, source = false }: { status: string; source?: boolean }) {
  if (status === 'ready') return <Tag color="blue">{source ? '来源可访问' : '已获得有效观察'}</Tag>;
  if (status === 'unknown') return <Tag color="warning">等待有效观察</Tag>;
  return <Status value={status} />;
}

export function Monitoring() {
  const { scope, refresh } = useScope();
  const can = useCan();
  const sources = useResource<{ items: MonitoringSource[]; total: number }>(
    can('asset.onboard') ? '/monitoring/sources' : can('configuration.read') ? `/readonly/sources?${query({ system_id: scope.system_id })}` : null,
    refresh,
  );
  const [editing, setEditing] = useState<MonitoringSource | 'new'>();
  const businessScope = scopeKey(scope);
  useEffect(() => {
    setEditing(undefined);
  }, [businessScope]);
  const visibleSources = (sources.data?.items ?? []).filter(
    (s) => !scope.system_id || s.system_id === scope.system_id,
  );
  return (
    <div className="intel-page">
      <Title
        title="Zabbix 接入"
        description="从 Zabbix 勾选相关主机和监控项，组成联合分析对象，持续观察并分析系统问题。"
        actions={
          <Space wrap>
            {can('asset.onboard') && (
              <Button onClick={() => setEditing('new')}>
                连接 Zabbix 来源
              </Button>
            )}
            <ContextLink to="/admin/inspection">
              <Button>巡检策略</Button>
            </ContextLink>
          </Space>
        }
      />
      <AnalysisGroups sources={visibleSources} sourcesLoading={sources.loading} version={refresh} changed={sources.reload} />
      {!can('asset.onboard') && can('configuration.read') && <Card className="intel-source-card" title="Zabbix 监控来源 · 只读">
        <LoadState {...sources} retry={sources.reload}>
          {sources.data && <ReadonlySourceList sources={sources.data.items} />}
        </LoadState>
      </Card>}
      {can('asset.onboard') && (
        <Card
          className="intel-source-card"
          title="Zabbix 监控来源"
          extra={<span className="intel-muted">管理员配置一次 · 令牌只保存在服务端</span>}
        >
          <LoadState {...sources} retry={sources.reload}>
            {sources.data &&
              (visibleSources.length ? (
                <div className="intel-source-list">
                  {visibleSources.map((source) => (
                    <div className="intel-source-row" key={source.id}>
                      <div>
                        <strong>{source.name}</strong>
                        <div className="intel-muted">
                          最近成功 <Time value={source.last_success_at} />
                          {source.error_code && (
                            <span className="intel-source-error"> · {source.error_code}</span>
                          )}
                        </div>
                      </div>
                      <Space wrap>
                        <MonitoringStatus status={source.status} source />
                        <Button type="text" onClick={() => setEditing(source)}>
                          配置 / 轮换凭据
                        </Button>
                      </Space>
                    </div>
                  ))}
                </div>
              ) : (
                <Empty description="尚未配置当前范围的监控来源">
                  <Button type="primary" onClick={() => setEditing('new')}>
                    连接只读 Zabbix
                  </Button>
                </Empty>
              ))}
          </LoadState>
        </Card>
      )}
      <Alert
        className="intel-gap-banner"
        type="info"
        showIcon
        title="只使用已采集的内容"
        description="模板名称不能证明锁、SQL、日志明细已存在。未识别或缺失的能力会单独列明，且不会自动当作健康。取消选择只暂停未来巡检，历史证据保留。"
      />
      {editing && (
        <SourceForm
          key={typeof editing === 'string' ? editing : editing.id}
          source={editing === 'new' ? undefined : editing}
          close={() => setEditing(undefined)}
          saved={() => {
            sources.reload();
            setEditing(undefined);
          }}
        />
      )}
    </div>
  );
}

export function ReadonlySourceList({ sources }: { sources: { id: string; name: string; status: string; last_success_at?: string | null; group_count?: number; host_count?: number; item_count?: number }[] }) {
  return <>
    <p className="intel-muted">可查看演示业务范围的来源状态和已选数据，凭据与内部连接地址不公开。数据组成可在上方“查看数据”中核对。</p>
    {sources.length ? <div className="intel-source-list">{sources.map(source => <div key={text(source.id)} className="intel-source-row">
      <div><strong>{text(source.name)}</strong><div className="intel-muted">{text(source.group_count)} 个联合对象 · {text(source.host_count)} 个主机 · {text(source.item_count)} 个已选监控项</div></div>
      <Space><MonitoringStatus status={text(source.status)} source /><span>最近成功 <Time value={source.last_success_at} /></span></Space>
    </div>)}</div> : <Empty description="当前授权范围还没有关联的 Zabbix 来源" />}
  </>;
}

function SourceForm({
  source,
  close,
  saved,
}: {
  source?: MonitoringSource;
  close: () => void;
  saved: () => void;
}) {
  const { scope } = useScope();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const systems = useResource<Page>('/systems?limit=100');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  async function submit() {
    let values: Row;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setBusy(true);
    setError(undefined);
    try {
      const body = sourcePayload(values, !!source);
      if (source) await api.put(`/monitoring/sources/${source.id}`, body);
      else await api.post('/monitoring/sources', body, true);
      message.success('来源配置已保存；连接状态以服务端结果为准');
      saved();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={source ? `来源配置 · ${source.name}` : '连接 Zabbix 只读来源'}
      open
      onCancel={close}
      onOk={submit}
      confirmLoading={busy}
      okText="检查并保存"
      cancelText="取消"
      destroyOnHidden
    >
      <ErrorNotice error={error} />
      <Alert
        type="info"
        showIcon
        title="使用只读 API 令牌"
        description="来源绑定业务系统。已有映射的来源不能切换到另一套 Zabbix，请为新上游建立独立来源。"
      />
      <Form
        form={form}
        layout="vertical"
        className="intel-form"
        initialValues={{
          name: source?.name,
          url: source?.url,
          system_id: source?.system_id ?? scope.system_id,
        }}
      >
        <Form.Item label="来源名称" name="name" rules={[{ required: true, whitespace: true }]}>
          <Input placeholder="例如：内部 Zabbix" maxLength={120} />
        </Form.Item>
        <Form.Item
          label="Zabbix 地址"
          name="url"
          rules={[{ required: true }, { type: 'url', message: '请填写完整 http(s) 地址' }]}
        >
          <Input placeholder="https://zabbix.example.internal" />
        </Form.Item>
        <Form.Item
          label="归属系统"
          name="system_id"
          rules={[{ required: true }]}
          extra={
            systems.error
              ? errorMessage(systems.error)
              : '只列出当前身份可访问的系统；服务端还会校验接入权限。'
          }
        >
          <Select
            disabled={!!source}
            loading={systems.loading}
            showSearch
            optionFilterProp="label"
            options={pageItems(systems.data).map((s) => ({
              value: text(s.id),
              label: text(s.name),
            }))}
          />
        </Form.Item>
        <Form.Item
          label={source ? '新 API 令牌（留空保留现有凭据）' : '只读 API 令牌'}
          name="api_token"
          rules={source ? [] : [{ required: true }]}
        >
          <Input.Password
            autoComplete="new-password"
            placeholder={
              source?.has_token ? '已保存凭据；不能读取原令牌' : '令牌仅提交到本系统服务端'
            }
          />
        </Form.Item>
        <Form.Item
          label="允许的主机组 ID（可选）"
          name="groups"
          extra={
            source
              ? '多个 ID 用英文逗号分隔；留空保留当前已保存的主机组限制。'
              : '多个 ID 用英文逗号分隔；留空表示按来源 API 账户权限发现。'
          }
        >
          <Input placeholder="例如：12, 24" />
        </Form.Item>
      </Form>
    </Modal>
  );
}

export function MetricsDashboard() {
  const can = useCan();
  const { scope, refresh } = useScope();
  const [search, setSearch] = useSearchParams();
  const objectsPath = useScopedPath('/monitoring/groups');
  const resource = useResource<{ items: AnalysisGroup[]; total: number }>(
    can('data.read') ? objectsPath : null,
    refresh,
  );
  const [selection, setSelection] = useState<{ scope: string; ids: string[] }>({
    scope: scopeKey(scope),
    ids: scope.instance_id ? [scope.instance_id] : [],
  });
  const businessScope = scopeKey({ ...scope, instance_id: undefined });
  const selected = scope.instance_id
    ? (resource.data?.items ?? []).filter(group => group.instance_id === scope.instance_id).map(group => group.id)
    : selectionForScope(selection.scope, businessScope, selection.ids);
  const validObjects = resource.data?.items ?? [];
  function select(ids: string[]) {
    setSearch((p) => {
      p.delete('instance_id');
      return p;
    });
    setSelection({ scope: businessScope, ids });
  }
  return (
    <div className="intel-page">
      <Title
        title="数据查看"
        description="按所选时间范围重新查询上游历史，与报告冻结证据分开显示。可同窗对照数据库、主机与网络信号。"
        actions={
          <ContextLink to="/monitoring">
            <Button>{can('asset.manage') ? '管理联合分析对象' : '查看联合分析对象'}</Button>
          </ContextLink>
        }
      />
      {!can('data.read') ? (
        <AccessNotice />
      ) : (
        <>
          <Alert
            type="info"
            showIcon
            title="上游历史查询"
            description="本页使用顶部时间范围读取监控来源。它不会改变已有报告；空缺与无效采样保留断点。"
          />
          <LoadState {...resource} retry={resource.reload}>
            {resource.data && (
              <>
                <div className="intel-metric-picker">
                  <label htmlFor="metric-objects">对照对象</label>
                  <Select
                    id="metric-objects"
                    mode="multiple"
                    maxCount={5}
                    allowClear
                    showSearch
                    optionFilterProp="label"
                    placeholder="选择最多 5 个已授权联合分析对象"
                    value={selected}
                    onChange={select}
                    options={validObjects.map((o) => ({
                      value: o.id,
                      label: `${o.name} · ${o.source_name}`,
                    }))}
                  />
                </div>
                {selected.some(id => !validObjects.some(group => group.id === id)) && <Alert type="warning" title="所选对象不在当前授权联合对象中" description="请重新选择联合分析对象；不会用其他对象的数据替代。" />}
                {selected.length ? (
                  selected.filter(id => validObjects.some(group => group.id === id)).map((id) => <ObjectMetricView key={`${businessScope}:${id}`} id={id} group={validObjects.find(group => group.id === id)!} />)
                ) : (
                  <Card>
                    <Empty
                      description={
                        validObjects.length
                          ? '选择对象后展示实际可用指标'
                          : '没有可读取的监控对象，请先选择对象或申请授权'
                      }
                    />
                  </Card>
                )}
              </>
            )}
          </LoadState>
        </>
      )}
      {search.get('report') && (
        <p className="intel-muted">
          来自报告的指标下钻。按顶部时间范围重新查询上游历史；报告本身的冻结证据保持不变。
        </p>
      )}
    </div>
  );
}

function ObjectMetricView({ id, group }: { id: string; group: AnalysisGroup }) {
  const { scope, refresh } = useScope();
  const resource = useResource<ObjectMetrics>(
    `/monitoring/groups/${id}/metrics?${query({ from: scope.from, to: scope.to })}`,
    refresh,
  );
  const [filter, setFilter] = useState('');
  const [itemId, setItemId] = useState<string>();
  const item = group.items.find(item => item.itemid === itemId);
  const charts =
    resource.data?.charts.filter((c) =>
      `${c.title} ${c.unit}`.toLowerCase().includes(filter.toLowerCase()),
    ) ?? [];
  return (
    <section className="intel-section">
      <LoadState {...resource} retry={resource.reload}>
        {resource.data && (
          <>
            <div className="intel-section-head">
              <h2>{resource.data.object.name}</h2>
              <Input.Search
                aria-label={`筛选 ${resource.data.object.name} 的指标`}
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="筛选指标名称"
                allowClear
                style={{ maxWidth: 260 }}
              />
            </div>
            <section className="intel-details"><h3>原始历史、文本与日志</h3><Select showSearch allowClear optionFilterProp="label" placeholder="选择一个监控项查看原始历史" value={itemId} onChange={setItemId} options={group.items.map(item => ({ value: item.itemid, label: `${item.host_name} · ${item.name}` }))} style={{ width: '100%' }} />{item && <ItemHistory key={`${item.itemid}:${scope.from}:${scope.to}`} group={group} item={item} />}</section>
            <p className="intel-muted">当前已选 {group.items.length} 个监控项；曲线与原始历史分别查看，采样缺口在各项覆盖信息中说明。</p>
            {charts.length ? (
              <div className="intel-metrics-grid">
                {charts.map((chart) => (
                  <MetricPanel key={chart.id} data={chart} />
                ))}
              </div>
            ) : (
              <Card>
                <Empty
                  description={
                    resource.data.charts.length
                      ? '没有匹配的指标'
                      : '此对象尚无可用的时序指标；请核对上游采集能力'
                  }
                />
              </Card>
            )}
          </>
        )}
      </LoadState>
    </section>
  );
}

export function InspectionSettings() {
  const { scope } = useScope();
  return <PolicyForm key={scopeKey(scope)} />;
}

function PolicyForm() {
  const { refresh } = useScope();
  const can = useCan();
  const { message } = App.useApp();
  const [form] = Form.useForm<InspectionPolicy>();
  const policyPath = '/intelligence/policy';
  const objectsPath = '/monitoring/groups';
  const resource = useResource<{ policy: InspectionPolicy | null; status: Row }>(
    can('investigation.create') ? policyPath : null,
    refresh,
  );
  const objects = useResource<{ items: AnalysisGroup[]; total: number }>(
    can('data.read') ? objectsPath : null,
    refresh,
  );
  const mode = Form.useWatch('mode', form);
  const enabled = Form.useWatch('enabled', form);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [tasks, setTasks] = useState<TaskRef[]>([]);
  useEffect(() => {
    if (resource.data)
      form.setFieldsValue(
        resource.data.policy ?? {
          enabled: false,
          instance_ids: [],
          signal_interval_seconds: 300,
          ai_interval_seconds: 1800,
          urgent_enabled: true,
          cooldown_seconds: 1800,
          max_daily_runs: 24,
          mode: 'rules_only',
          external_confirmed: false,
        },
      );
  }, [resource.data, form]);
  async function save() {
    let values: InspectionPolicy;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setBusy(true);
    setError(undefined);
    try {
      await api.put('/intelligence/policy', values);
      message.success('后台巡检策略已保存');
      resource.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function run() {
    setBusy(true);
    setError(undefined);
    try {
      const result = await api.post<{ tasks?: TaskRef[]; items?: TaskRef[] }>(
        '/intelligence/policy/run',
        {},
        true,
      );
      const refs = result.tasks ?? result.items ?? [];
      setTasks(refs);
      message.info(
        refs.length
          ? '已提交后台任务，请查看实际执行状态'
          : '请求已处理；本轮没有返回新任务，请核对巡检状态',
      );
      resource.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="intel-page">
      <Title
        title="巡检策略"
        description="配置一次，由后台持续检查。日常使用从问题分析进入。"
        actions={
          <ContextLink to="/">
            <Button>返回结果首页</Button>
          </ContextLink>
        }
      />
      {!can('investigation.create') ? (
        <AccessNotice detail="当前身份没有调查创建权限，不能维护自动巡检策略。" />
      ) : (
        <LoadState {...resource} retry={resource.reload}>
          {resource.data && (
            <div className="intel-policy-layout">
              <Card title="自动巡检" className="intel-policy-form">
                <ErrorNotice error={error} />
                <Alert
                  type="info"
                  showIcon
                  title="当前用户的持久巡检策略"
                  description="策略按下面保存的对象运行。顶部业务筛选和查看时间不会自动改变后台巡检范围。"
                  className="intel-gap-banner"
                />
                <Form form={form} layout="vertical" onFinish={save}>
                  <Form.Item name="enabled" label="启用后台巡检" valuePropName="checked">
                    <Switch />
                  </Form.Item>
                  <Form.Item
                    name="instance_ids"
                    label="联合分析对象"
                    rules={[
                      {
                        validator: (_, value) =>
                          enabled && !value?.length
                            ? Promise.reject(new Error('启用前请选择至少一个已授权对象'))
                            : Promise.resolve(),
                      },
                    ]}
                    extra={
                      objects.error
                        ? errorMessage(objects.error)
                        : '选择当前授权范围内的对象；后台每次执行时重新检查权限。超过 5 个对象由后台分组。'
                    }
                  >
                    <Select
                      mode="multiple"
                      showSearch
                      optionFilterProp="label"
                      loading={objects.loading}
                      options={(objects.data?.items ?? []).map((o) => ({
                        value: o.instance_id,
                        label: `${o.name} · ${o.source_name}`,
                        disabled: !o.enabled,
                      }))}
                    />
                  </Form.Item>
                  <div className="intel-form-pair">
                    <Form.Item
                      name="signal_interval_seconds"
                      label="监控信号检查周期"
                      rules={[{ required: true }]}
                    >
                      <Select
                        options={[
                          { value: 5, label: '每 5 秒' },
                          { value: 300, label: '每 5 分钟' },
                          { value: 900, label: '每 15 分钟' },
                          { value: 1800, label: '每 30 分钟' },
                        ]}
                      />
                    </Form.Item>
                    <Form.Item
                      name="ai_interval_seconds"
                      label="综合巡检周期"
                      rules={[{ required: true }]}
                    >
                      <Select
                        options={[
                          { value: 900, label: '每 15 分钟' },
                          { value: 1800, label: '每 30 分钟' },
                          { value: 3600, label: '每 60 分钟' },
                        ]}
                      />
                    </Form.Item>
                  </div>
                  <Form.Item name="urgent_enabled" valuePropName="checked">
                    <Checkbox>新出现或发生变化的异常加急调查</Checkbox>
                  </Form.Item>
                  <Form.Item name="mode" label="分析方式" rules={[{ required: true }]}>
                    <Select
                      options={[
                        { value: 'rules_only', label: '确定性规则 · 不调用外部模型' },
                        { value: 'live_redacted', label: '脱敏 AI 综合分析 · 使用已配置模型' },
                      ]}
                    />
                  </Form.Item>
                  {mode === 'live_redacted' && (
                    <>
                      <Alert
                        type="warning"
                        showIcon
                        title="外部模型会接收授权范围内的脱敏证据"
                        description="保存前请确认允许持续外发；系统仍执行既有模型权限、脱敏和预算限制。模型建议不等于根因已被证实。"
                      />
                      <Form.Item
                        name="external_confirmed"
                        valuePropName="checked"
                        rules={[
                          {
                            validator: (_, value) =>
                              value
                                ? Promise.resolve()
                                : Promise.reject(
                                    new Error('请确认脱敏证据的外发授权，或改用规则模式'),
                                  ),
                          },
                        ]}
                      >
                        <Checkbox>我确认授权本策略持续调用已配置模型分析脱敏证据</Checkbox>
                      </Form.Item>
                    </>
                  )}
                  <div className="intel-form-pair">
                    <Form.Item
                      name="cooldown_seconds"
                      label="同类问题冷却时间（秒）"
                      rules={[{ required: true }]}
                    >
                      <InputNumber min={60} max={86400} step={300} />
                    </Form.Item>
                    <Form.Item
                      name="max_daily_runs"
                      label="每日综合调查次数上限"
                      rules={[{ required: true }]}
                    >
                      <InputNumber min={1} max={100} />
                    </Form.Item>
                  </div>
                  <Button type="primary" htmlType="submit" loading={busy}>
                    保存后台策略
                  </Button>
                </Form>
              </Card>
              <aside>
                <Card title="执行状态">
                  <InspectionStatus
                    status={
                      typeof resource.data.status.status === 'string'
                        ? resource.data.status.status
                        : (resource.data.policy?.status ?? 'not_configured')
                    }
                    reason={
                      typeof resource.data.status.reason === 'string'
                        ? resource.data.status.reason
                        : undefined
                    }
                  />
                  <dl className="intel-definition">
                    <dt>最近完成</dt>
                    <dd>
                      <Time
                        value={
                          resource.data.status.last_completed_at ??
                          resource.data.policy?.last_completed_at
                        }
                      />
                    </dd>
                    <dt>下次计划</dt>
                    <dd>
                      <Time
                        value={
                          resource.data.status.next_due_at ?? resource.data.policy?.next_due_at
                        }
                      />
                    </dd>
                    <dt>已保存版本</dt>
                    <dd>{resource.data.policy?.version ?? '尚未保存'}</dd>
                  </dl>
                  <p className="intel-muted">
                    关闭浏览器不会停止后台巡检。离线、额度、授权变化与失败会暂停或限制后续任务。
                  </p>
                  <details className="intel-details">
                    <summary>维护 / 验证操作</summary>
                    <p>使用已保存策略提交有预算限制的后台运行；不使用尚未保存的表单值。</p>
                    <Button disabled={!resource.data.policy?.enabled} loading={busy} onClick={run}>
                      请求一次受控运行
                    </Button>
                    {tasks.map((task) => (
                      <p key={task.id}>
                        <ContextLink to={`/tasks/${task.id}`}>查看后台任务 ↗</ContextLink>
                        <Status value={task.status} />
                      </p>
                    ))}
                  </details>
                </Card>
                <Card title="最近巡检记录">
                  {rows(resource.data.status.runs).length ? (
                    <div className="intel-run-history">
                      {rows(resource.data.status.runs)
                        .slice(0, 5)
                        .map((run) => (
                          <div key={text(run.id)}>
                            <ContextLink to={`/investigations/${run.id}`}>
                              <Time value={run.created_at} /> 查看报告 ↗
                            </ContextLink>
                            <div>
                              <Status value={run.status} />
                              <Status value={run.mode} />
                            </div>
                            <small>
                              {{
                                routine: '周期综合巡检',
                                periodic: '周期综合巡检',
                                urgent: '异常加急',
                                manual: '维护请求',
                                signal: '信号检查',
                              }[text(run.trigger)] ?? text(run.trigger, '触发方式未提供')}
                              {run.error_code ? ` · ${text(run.error_code)}` : ''}
                            </small>
                          </div>
                        ))}
                    </div>
                  ) : (
                    <p className="intel-muted">
                      尚无自动巡检执行记录。启用策略并产生任务后在这里查看，包括未发现异常的正常巡检。
                    </p>
                  )}
                  <ContextLink to="/">返回问题分析 →</ContextLink>
                </Card>
                <Card title="怎样形成结果" className="intel-policy-explainer">
                  <ol>
                    <li>轻量检查实际监控信号，发现变化。</li>
                    <li>即使没有阈值告警，也按周期综合检查。</li>
                    <li>结合相关对象、SQL / 日志等已具备的能力。</li>
                    <li>同一问题更新报告版本；正常结果保留为巡检记录。</li>
                  </ol>
                  <p className="intel-muted">不会自动修改数据库，也不会发送邮件。</p>
                </Card>
              </aside>
            </div>
          )}
        </LoadState>
      )}
    </div>
  );
}
