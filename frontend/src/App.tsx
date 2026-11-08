import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  App as AntApp,
  Avatar,
  Badge,
  Button,
  Card,
  Col,
  Form,
  Input,
  Layout,
  Menu,
  Row as GridRow,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tag,
  Typography,
} from 'antd';
import { Navigate, Route, Routes, useLocation, useSearchParams } from 'react-router-dom';
import { api, ApiError, pageItems, query, rows, setCsrf, text } from './api';
import type { Actor, Instance, Page, Row } from './api';
import {
  ScopeContext,
  SessionContext,
  useCan,
  useResource,
  useScope,
  useScopedPath,
} from './hooks';
import type { Scope } from './hooks';
import {
  ContextLink,
  DataTable,
  ErrorNotice,
  LoadState,
  RecordBlock,
  Status,
  Time,
  Title,
} from './ui';
import { AssetDetail, AssetList, Onboarding } from './Inventory';
import {
  EventDetail,
  Events,
  EventInbox,
  Investigations,
  InvestigationDetail,
  Knowledge,
  KnowledgeDetail,
  Evidence,
  Tasks,
  TaskDetail,
  VerificationDetail,
} from './Workflow';
import { Admin } from './Admin';
import { TopologyView } from './Presentation';
import { IntelligenceHome, IssueReport } from './Intelligence';
import { InspectionSettings, MetricsDashboard, Monitoring } from './Monitoring';
import { initialSidebarCollapsed } from './intelligenceModel';
export default function App() {
  const [actor, setActor] = useState<Actor>();
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const expired = () => {
      setActor(undefined);
      setCsrf('');
    };
    window.addEventListener('session-expired', expired);
    return () => window.removeEventListener('session-expired', expired);
  }, []);
  useEffect(() => {
    setLoading(true);
    api
      .get<Actor>('/auth/me')
      .then((value) => {
        setActor(value);
        setCsrf(value.csrf_token ?? '');
        setError(undefined);
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) {
          setActor(undefined);
          setError(undefined);
        } else setError(e);
      })
      .finally(() => setLoading(false));
  }, [revision]);
  if (loading)
    return (
      <div className="boot">
        <div className="brand-mark">DB</div>
        <Typography.Title level={3}>正在建立安全会话</Typography.Title>
        <LoadState loading />
      </div>
    );
  if (error)
    return (
      <div className="boot">
        <ErrorNotice error={error} />
        <Button onClick={() => setRevision((v) => v + 1)}>重新连接</Button>
      </div>
    );
  if (!actor)
    return (
      <Login
        onLogin={(value) => {
          setActor(value);
          setCsrf(value.csrf_token ?? '');
        }}
      />
    );
  return (
    <SessionContext.Provider value={{ actor, reload: () => setRevision((v) => v + 1) }}>
      <Shell
        actor={actor}
        onLogout={() => {
          setActor(undefined);
          setCsrf('');
        }}
      />
    </SessionContext.Provider>
  );
}
function Login({ onLogin }: { onLogin: (actor: Actor) => void }) {
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(false);
  const [form] = Form.useForm();
  async function login(values: Row) {
    setLoading(true);
    setError(undefined);
    try {
      const nonce = await api.get<{ csrf_token: string }>('/auth/csrf');
      setCsrf(nonce.csrf_token);
      await api.post('/auth/login', values);
      form.resetFields(['password']);
      onLogin(await api.get<Actor>('/auth/me'));
    } catch (e) {
      setError(e);
      form.resetFields(['password']);
    } finally {
      setLoading(false);
    }
  }
  return (
    <div className="login-page">
      <div className="login-intro">
        <div className="brand">
          <div className="brand-mark">DB</div>
          <span>DB EVIDENCE</span>
        </div>
        <Tag color="cyan">内部数据库运维平台</Tag>
        <h1>
          先核对证据，
          <br />
          再作出判断。
        </h1>
        <p>
          持续采集 · 确定性规则 · 有边界的 AI 调查
          <br />
          每项结论可追溯，每次处置有记录。
        </p>
        <div className="login-foot">MySQL 观测与协作工作区</div>
      </div>
      <div className="login-form">
        <Card>
          <Typography.Title level={2}>登录工作区</Typography.Title>
          <Typography.Paragraph type="secondary">
            使用管理员创建的本地账号。资料访问以当前授权范围为准。
          </Typography.Paragraph>
          <ErrorNotice error={error} />
          <Form form={form} layout="vertical" onFinish={login} requiredMark={false}>
            <Form.Item
              name="username"
              label="用户名"
              rules={[{ required: true, message: '请输入用户名' }]}
            >
              <Input autoComplete="username" size="large" autoFocus />
            </Form.Item>
            <Form.Item
              name="password"
              label="密码"
              rules={[{ required: true, message: '请输入密码' }]}
            >
              <Input.Password autoComplete="current-password" size="large" />
            </Form.Item>
            <Button htmlType="submit" type="primary" size="large" block loading={loading}>
              登录
            </Button>
          </Form>
        </Card>
        <p className="login-note">数据库凭据与模型密钥由服务端保管，不在浏览器读取。</p>
      </div>
    </div>
  );
}
const menuDefinitions = [
  { key: '/', label: '问题与风险', symbol: '◈', permissions: ['data.read', 'event.read'] },
  {
    key: '/monitoring',
    label: '监控对象',
    symbol: '▤',
    permissions: ['data.read', 'asset.onboard'],
  },
  { key: '/metrics', label: '指标看板', symbol: '▥', permissions: ['data.read'] },
  {
    key: '/inspection-settings',
    label: '巡检设置',
    symbol: '◷',
    permissions: ['investigation.create'],
  },
  {
    key: '/operations',
    label: '原有运维工作台',
    symbol: '◈',
    permissions: ['data.read', 'data.read', 'platform.read'],
  },
  {
    key: '/assets',
    label: '资产目录',
    symbol: '▤',
    permissions: ['data.read', 'asset.manage', 'asset.onboard', 'asset.manage', 'asset.manage'],
  },
  { key: '/topology', label: '资源拓扑', symbol: '⌘', permissions: ['data.read', 'data.read'] },
  { key: '/evidence', label: '证据查询', symbol: '▥', permissions: ['data.read', 'data.read'] },
  { key: '/events', label: '事件中心', symbol: '◉', permissions: ['event.read'] },
  {
    key: '/investigations',
    label: 'AI 调查',
    symbol: '✧',
    permissions: ['investigation.create', 'investigation.create'],
  },
  { key: '/knowledge', label: '知识库', symbol: '▣', permissions: ['knowledge.read'] },
  {
    key: '/tasks',
    label: '任务中心',
    symbol: '≋',
    permissions: ['data.read', 'task.manage', 'asset.manage', 'asset.onboard'],
  },
  {
    key: '/admin',
    label: '平台管理',
    symbol: '⚙',
    permissions: [
      'user.manage',
      'grant.manage',
      'organization.manage',
      'settings.manage',
      'platform.read',
      'audit.read',
      'collection_profile.manage',
      'rule.manage',
      'model_policy.manage',
    ],
  },
];
function Shell({ actor, onLogout }: { actor: Actor; onLogout: () => void }) {
  const can = useCan();
  const location = useLocation();
  const [search, setSearch] = useSearchParams();
  const [collapsed, setCollapsed] = useState(() => initialSidebarCollapsed(window.innerWidth));
  const [refresh, setRefresh] = useState(0);
  const [rolling, setRolling] = useState(false);
  const [logoutError, setLogoutError] = useState<unknown>();
  const initial = useMemo(
    () => ({ from: new Date(Date.now() - 3600000).toISOString(), to: new Date().toISOString() }),
    [],
  );
  const scope: Scope = {
    department_id: search.get('department_id') ?? undefined,
    system_id: search.get('system_id') ?? undefined,
    environment: search.get('environment') ?? undefined,
    instance_id: search.get('instance_id') ?? undefined,
    from: search.get('from') ?? initial.from,
    to: search.get('to') ?? initial.to,
    timezone: search.get('timezone') ?? 'Asia/Shanghai',
  };
  const departments = useResource<Page>('/departments');
  const systems = useResource<Page>('/systems');
  const assets = useResource<Page<Instance>>(
    can('data.read', 'asset.manage') ? '/instances?limit=100' : null,
    refresh,
  );
  useEffect(() => {
    if (!search.has('from') || !search.has('to'))
      setSearch(
        (p) => {
          if (!p.has('from')) p.set('from', initial.from);
          if (!p.has('to')) p.set('to', initial.to);
          return p;
        },
        { replace: true },
      );
  }, [initial, search, setSearch]);
  useEffect(() => {
    if (!rolling) return;
    const interval = setInterval(() => {
      setSearch(
        (p) => {
          p.set('from', new Date(Date.now() - 3600000).toISOString());
          p.set('to', new Date().toISOString());
          return p;
        },
        { replace: true },
      );
      setRefresh((v) => v + 1);
    }, 30000);
    return () => clearInterval(interval);
  }, [rolling, setSearch]);
  useEffect(() => {
    if (location.pathname.match(/^\/(issues|investigations|evidence|events|knowledge)\/.+/))
      setRolling(false);
  }, [location.pathname]);
  const setFilter = (key: string, value?: string) =>
    setSearch((p) => {
      value ? p.set(key, value) : p.delete(key);
      p.delete('cursor');
      if (key === 'department_id') {
        p.delete('system_id');
        p.delete('instance_id');
      }
      if (key === 'system_id') p.delete('instance_id');
      return p;
    });
  const nav = menuDefinitions.filter((item) => can(...item.permissions));
  const username = text(
    actor.display_name ?? actor.username ?? actor.user?.display_name ?? actor.user?.username,
    '当前用户',
  );
  async function logout() {
    try {
      await api.post('/auth/logout');
      onLogout();
    } catch (e) {
      setLogoutError(e);
    }
  }
  return (
    <ScopeContext.Provider value={{ scope, params: query(scope as unknown as Row), refresh }}>
      <Layout className="app-shell">
        <Layout.Sider
          width={214}
          collapsed={collapsed}
          breakpoint="lg"
          onBreakpoint={(value) => setCollapsed(value)}
          className="sidebar"
        >
          <div className="sidebar-brand">
            <div className="brand-mark">DB</div>
            {!collapsed && (
              <div>
                <strong>DB EVIDENCE</strong>
                <small>内部数据库平台</small>
              </div>
            )}
          </div>
          <div className="workspace-label">{!collapsed ? '工作区 / OPERATIONS' : 'OPS'}</div>
          <Menu
            theme="dark"
            selectedKeys={[
              location.pathname.startsWith('/assets')
                ? '/assets'
                : location.pathname.startsWith('/issues')
                  ? '/'
                  : location.pathname.startsWith('/admin')
                    ? '/admin'
                    : '/' + location.pathname.split('/')[1],
            ]}
            items={[
              ...nav
                .filter((item) =>
                  ['/', '/monitoring', '/metrics', '/inspection-settings'].includes(item.key),
                )
                .map((item) => ({
                  key: item.key,
                  icon: <span aria-hidden>{item.symbol}</span>,
                  label: <ContextLink to={item.key}>{item.label}</ContextLink>,
                })),
              {
                key: 'workflows',
                label: '调查与管理',
                icon: <span aria-hidden>≋</span>,
                children: nav
                  .filter(
                    (item) =>
                      !['/', '/monitoring', '/metrics', '/inspection-settings'].includes(item.key),
                  )
                  .map((item) => ({
                    key: item.key,
                    label: <ContextLink to={item.key}>{item.label}</ContextLink>,
                  })),
              },
            ]}
          />
          {!collapsed && (
            <div className="sidebar-footer">
              <span className="status-dot" />
              事实优先 · 操作留痕
              <br />
              <small>只读观测 / 人工处置</small>
            </div>
          )}
        </Layout.Sider>
        <Layout>
          <Layout.Header className="topbar">
            <div>
              <Button type="text" aria-label="切换导航" onClick={() => setCollapsed((v) => !v)}>
                ☰
              </Button>
              <span className="workspace-name">DB Evidence · 智能巡检与问题诊断</span>
              <Tag className="internal-tag">内部</Tag>
            </div>
            <Space>
              <InboxIndicator />
              <Avatar size="small" style={{ background: '#dceae7', color: '#225b50' }}>
                {username.slice(0, 1)}
              </Avatar>
              <span>{username}</span>
              <Typography.Text type="secondary">{(actor.roles ?? []).join(' / ')}</Typography.Text>
              <Button size="small" onClick={logout}>
                退出
              </Button>
            </Space>
          </Layout.Header>
          <Layout.Content className="content">
            <ErrorNotice error={logoutError} />
            <div className="scopebar">
              <Space wrap>
                <span className="filter-label">范围</span>
                <Select
                  aria-label="部门"
                  placeholder="已授权全部部门"
                  allowClear
                  value={scope.department_id}
                  onChange={(v) => setFilter('department_id', v)}
                  options={pageItems(departments.data).map((d) => ({
                    value: text(d.id),
                    label: text(d.name),
                  }))}
                  style={{ width: 166 }}
                />
                <Select
                  aria-label="业务系统"
                  placeholder="全部系统"
                  allowClear
                  value={scope.system_id}
                  onChange={(v) => setFilter('system_id', v)}
                  options={pageItems(systems.data)
                    .filter((s) => !scope.department_id || s.department_id === scope.department_id)
                    .map((s) => ({ value: text(s.id), label: text(s.name) }))}
                  style={{ width: 166 }}
                />
                <Select
                  aria-label="环境"
                  placeholder="全部环境"
                  allowClear
                  value={scope.environment}
                  onChange={(v) => setFilter('environment', v)}
                  options={['production', 'staging', 'test', 'lab'].map((v) => ({
                    value: v,
                    label: v,
                  }))}
                  style={{ width: 130 }}
                />
                <Select
                  aria-label="资产"
                  placeholder="全部授权资产"
                  allowClear
                  showSearch
                  optionFilterProp="label"
                  value={scope.instance_id}
                  onChange={(v) => setFilter('instance_id', v)}
                  options={pageItems(assets.data)
                    .filter((i) => !scope.system_id || i.system_id === scope.system_id)
                    .map((i) => ({ value: i.id, label: i.name }))}
                  style={{ width: 180 }}
                />
              </Space>
              <Tag>后端授权范围内</Tag>
            </div>
            <div className="timebar">
              <Space wrap>
                <span className="filter-label">
                  {location.pathname.startsWith('/issues/') ? '指标查询时间' : '时间'}
                </span>
                <label className="datetime-label">
                  起始{' '}
                  <input
                    type="datetime-local"
                    value={localInput(scope.from)}
                    onChange={(e) =>
                      validLocal(e.target.value) &&
                      setFilter('from', new Date(e.target.value).toISOString())
                    }
                  />
                </label>
                <span>→</span>
                <label className="datetime-label">
                  结束{' '}
                  <input
                    type="datetime-local"
                    value={localInput(scope.to)}
                    onChange={(e) =>
                      validLocal(e.target.value) &&
                      setFilter('to', new Date(e.target.value).toISOString())
                    }
                  />
                </label>
                <Typography.Text type="secondary">
                  {Intl.DateTimeFormat().resolvedOptions().timeZone}
                </Typography.Text>
                <Button
                  size="small"
                  onClick={() => {
                    setSearch((p) => {
                      p.set('from', new Date(Date.now() - 3600000).toISOString());
                      p.set('to', new Date().toISOString());
                      return p;
                    });
                    setRefresh((v) => v + 1);
                  }}
                >
                  最近 1 小时
                </Button>
              </Space>
              <Space>
                <Switch size="small" checked={rolling} onChange={setRolling} />
                <span>{rolling ? '滚动窗口 / 30 秒' : '自动刷新关闭'}</span>
                <Button size="small" onClick={() => setRefresh((v) => v + 1)}>
                  刷新
                </Button>
              </Space>
            </div>
            {new Date(scope.from) >= new Date(scope.to) ? (
              <Alert type="warning" showIcon title="时间窗无效：起始时间须早于结束时间。" />
            ) : null}
            <Routes>
              <Route path="/" element={<IntelligenceHome />} />
              <Route path="/issues/:id" element={<IssueReport />} />
              <Route path="/monitoring" element={<Monitoring />} />
              <Route path="/metrics" element={<MetricsDashboard />} />
              <Route path="/inspection-settings" element={<InspectionSettings />} />
              <Route path="/operations" element={<Overview />} />
              <Route path="/assets" element={<AssetList />} />
              <Route path="/assets/new" element={<Onboarding />} />
              <Route path="/assets/onboarding/:draftId" element={<Onboarding />} />
              <Route path="/assets/:id/:tab?" element={<AssetDetail />} />
              <Route path="/topology" element={<Topology />} />
              <Route path="/evidence/:id?" element={<Evidence />} />
              <Route path="/events" element={<Events />} />
              <Route path="/events/inbox" element={<EventInbox />} />
              <Route path="/events/:id" element={<EventDetail />} />
              <Route path="/investigations" element={<Investigations />} />
              <Route path="/investigations/:id" element={<InvestigationDetail />} />
              <Route path="/knowledge" element={<Knowledge />} />
              <Route path="/knowledge/:id" element={<KnowledgeDetail />} />
              <Route path="/tasks" element={<Tasks />} />
              <Route path="/tasks/:id" element={<TaskDetail />} />
              <Route path="/verifications/:id" element={<VerificationDetail />} />
              <Route path="/admin/*" element={<Admin />} />
              <Route
                path="*"
                element={
                  <Card>
                    <Typography.Title level={3}>页面不存在</Typography.Title>
                    <ContextLink to="/">
                      <Button>返回工作台</Button>
                    </ContextLink>
                  </Card>
                }
              />
            </Routes>
            <footer className="page-footer">
              DB Evidence · 内部数据库平台{' '}
              <span>采集事实、规则结论、模型输出与人工记录分别留存</span>
            </footer>
          </Layout.Content>
        </Layout>
      </Layout>
    </ScopeContext.Provider>
  );
}
function InboxIndicator() {
  const can = useCan();
  const { message } = AntApp.useApp();
  const { refresh, scope } = useScope();
  const [tick, setTick] = useState(0);
  const enabled = can('data.read') && can('event.read');
  const path = enabled
    ? `/events/inbox?${query({ limit: 1, instance_id: scope.instance_id, system_id: scope.system_id, department_id: scope.department_id, environment: scope.environment })}`
    : null;
  const resource = useResource<Row>(path, refresh + tick);
  const seen = useRef<{ path: string | null; versions: Set<string> }>({
    path: null,
    versions: new Set(),
  });
  useEffect(() => {
    if (!enabled) return;
    const timer = setInterval(() => setTick((value) => value + 1), 30000);
    return () => clearInterval(timer);
  }, [enabled]);
  useEffect(() => {
    if (!resource.data) return;
    const keys = rows(resource.data.items).map((row) => `${row.id}:${row.version}`);
    if (seen.current.path === path && keys.some((key) => !seen.current.versions.has(key)))
      message.info({ content: '待确认事件已更新，请查看收件箱。', key: 'event-inbox-update' });
    if (seen.current.path !== path) seen.current = { path, versions: new Set() };
    seen.current.versions = new Set(keys);
  }, [resource.data, path, message]);
  if (!enabled) return null;
  return (
    <Badge
      count={typeof resource.data?.total === 'number' ? resource.data.total : undefined}
      showZero={!!resource.data}
      overflowCount={999}
    >
      <ContextLink to="/events/inbox">
        <Button size="small" title="只依据当前授权与维护策略计算；不代表未读消息">
          {resource.error ? '收件箱暂不可用' : '未确认事件'}
        </Button>
      </ContextLink>
    </Badge>
  );
}
function validLocal(value: string) {
  return !!value && !isNaN(new Date(value).valueOf());
}
function localInput(value: string) {
  const date = new Date(value);
  if (isNaN(date.valueOf())) return '';
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}
function Overview() {
  const { refresh } = useScope();
  const can = useCan();
  const resource = useResource<Row>(useScopedPath('/overview'), refresh);
  const data = resource.data;
  const counts = (data?.counts ?? {}) as Row;
  return (
    <>
      <Title
        title="工作台"
        description="从授权范围内的异常与资料缺口开始今天的工作。"
        actions={
          <>
            {can('asset.onboard') && (
              <ContextLink to="/assets/new">
                <Button type="primary">＋ 接入 MySQL</Button>
              </ContextLink>
            )}
            <ContextLink to="/events">
              <Button>处理事件</Button>
            </ContextLink>
          </>
        }
      />
      <LoadState {...resource} retry={resource.reload}>
        {data && (
          <>
            <GridRow gutter={[16, 16]}>
              {[
                ['registered', '已登记实例'],
                ['enabled', '已启用采集'],
                ['healthy', '资料新鲜且健康'],
                ['open_events', '待处理事件'],
              ].map(([key, label]) => (
                <Col xs={12} xl={6} key={key}>
                  <Card className="stat-card">
                    <Statistic
                      title={label}
                      value={typeof counts[key] === 'number' ? (counts[key] as number) : '未知'}
                    />
                    <Typography.Text type="secondary">当前授权范围</Typography.Text>
                  </Card>
                </Col>
              ))}
            </GridRow>
            <div className="section-grid">
              <Card
                title="需要关注的实例"
                extra={<ContextLink to="/assets">查看目录 →</ContextLink>}
              >
                <DataTable
                  items={rows(data.instances)}
                  columns={[
                    {
                      key: 'name',
                      title: '实例',
                      render: (_, r) => (
                        <ContextLink to={`/assets/${r.id}/overview`}>{text(r.name)}</ContextLink>
                      ),
                    },
                    { key: 'lifecycle', title: '生命周期', render: (v) => <Status value={v} /> },
                    { key: 'health', title: '资料状态', render: (v) => <Status value={v} /> },
                    {
                      key: 'last_success_at',
                      title: '最近成功',
                      render: (v) => <Time value={v} />,
                    },
                  ]}
                />
              </Card>
              <Card title="待处理事件" extra={<ContextLink to="/events">全部事件 →</ContextLink>}>
                <DataTable
                  items={rows(data.events)}
                  columns={[
                    { key: 'severity', title: '级别', render: (v) => <Status value={v} /> },
                    {
                      key: 'title',
                      title: '事件',
                      render: (_, r) => (
                        <ContextLink to={`/events/${r.id}`}>{text(r.title)}</ContextLink>
                      ),
                    },
                    { key: 'status', title: '进展', render: (v) => <Status value={v} /> },
                  ]}
                />
              </Card>
            </div>
            {data.metadata_only !== true && can('data.read') && (
              <OverviewSources
                sources={rows(data.source_health)}
                instances={rows(data.instances)}
              />
            )}
            <div className="updated-line">
              汇总更新时间 <Time value={data.updated_at} />
            </div>
          </>
        )}
      </LoadState>
    </>
  );
}
const overviewCollectorNames: Record<string, string> = {
  identity_status: '身份与状态',
  runtime: '运行与锁事务',
  statement_digest: 'SQL 摘要',
  schema_config: '结构与配置',
  replication: '复制基础',
  error_log: '错误日志',
  slow_log: '慢日志',
  host: '主机',
};
const overviewSourceReasons: Record<string, string> = {
  valid: '资料在新鲜度范围内',
  disabled: '已停用采集',
  latest_attempt_failed: '最近尝试失败，旧成功记录仅供参考',
  collecting: '正在采集，尚待有效观察',
  no_observation: '尚无可用观察',
  configuration_changed: '配置已变更，需要新观察',
  identity_mismatch: '观察身份与登记身份不一致',
  stale: '成功观察已过期',
  not_applicable: '此实例不适用，不计为故障',
  not_configured: '来源尚未配置',
  unsupported: '来源不受支持',
  permission_denied: '来源权限不足',
};
function sourceSeconds(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 }).format(value)} 秒`
    : '未知';
}
function OverviewSources({ sources, instances }: { sources: Row[]; instances: Row[] }) {
  const names = new Map(instances.map((instance) => [text(instance.id), text(instance.name)]));
  return (
    <Card title="逐源采集状态与缺口" className="section">
      <Typography.Paragraph type="secondary">
        最近尝试与最近成功观察分别保留；正在采集不代表已有新资料，不适用的来源不计为故障。
      </Typography.Paragraph>
      <Table<Row>
        size="small"
        rowKey={(source) => `${text(source.instance_id)}:${text(source.collector)}`}
        dataSource={sources}
        scroll={{ x: 'max-content' }}
        pagination={{
          defaultPageSize: 8,
          showSizeChanger: false,
          hideOnSinglePage: true,
          showTotal: (total) => `当前授权范围内 ${total} 个来源`,
        }}
        locale={{ emptyText: '当前范围尚无采集来源记录' }}
        expandable={{
          expandedRowRender: (source) => <RecordBlock data={source} />,
        }}
        columns={[
          {
            key: 'instance_id',
            dataIndex: 'instance_id',
            title: '实例',
            render: (value) => (
              <ContextLink to={`/assets/${encodeURIComponent(text(value))}/overview`}>
                {names.get(text(value)) ?? text(value).slice(0, 8)}
              </ContextLink>
            ),
          },
          {
            key: 'collector',
            dataIndex: 'collector',
            title: '来源',
            render: (value) => overviewCollectorNames[text(value)] ?? text(value),
          },
          {
            key: 'availability',
            title: '当前资料',
            render: (_, source) => (
              <Space orientation="vertical" size={0}>
                <Status
                  value={
                    source.reason_code === 'not_applicable'
                      ? 'not_applicable'
                      : (source.availability ?? 'unknown')
                  }
                />
                {source.reason_code !== 'not_applicable' && (
                  <Typography.Text type="secondary">
                    {source.up === true
                      ? '新鲜有效'
                      : source.up === false
                        ? '未满足当前可用条件'
                        : '新鲜度未知'}
                  </Typography.Text>
                )}
              </Space>
            ),
          },
          {
            key: 'latest_attempt_status',
            title: '最近尝试',
            render: (_, source) => (
              <Space orientation="vertical" size={0}>
                <Space size={0}>
                  <Status value={source.latest_attempt_status} />
                  {source.attempt_in_progress === true && (
                    <Tag color="processing">新尝试进行中</Tag>
                  )}
                </Space>
                {source.last_attempt_at ? <Time value={source.last_attempt_at} /> : null}
              </Space>
            ),
          },
          {
            key: 'last_success_at',
            dataIndex: 'last_success_at',
            title: '最近成功观察',
            render: (value) => (value ? <Time value={value} /> : '尚无成功观察'),
          },
          {
            key: 'sample_age_seconds',
            title: '观察年龄 / 周期',
            render: (_, source) => (
              <Space orientation="vertical" size={0}>
                <span>距成功 {sourceSeconds(source.sample_age_seconds)}</span>
                <Typography.Text type="secondary">
                  周期 {sourceSeconds(source.interval_seconds)} · 上限{' '}
                  {sourceSeconds(source.freshness_limit_seconds)}
                </Typography.Text>
              </Space>
            ),
          },
          {
            key: 'reason_code',
            dataIndex: 'reason_code',
            title: '原因与缺口',
            render: (value) => overviewSourceReasons[text(value)] ?? text(value, '来源未返回'),
          },
        ]}
      />
    </Card>
  );
}
function Topology() {
  const { refresh } = useScope();
  const resource = useResource<Row>(useScopedPath('/topology'), refresh);
  return (
    <>
      <Title title="资源拓扑" description="配置归属、人工主机关联与当前窗口的复制观测分别呈现。" />
      <LoadState {...resource} retry={resource.reload}>
        {resource.data && <TopologyView data={resource.data} />}
      </LoadState>
    </>
  );
}
export function Require({ actions, children }: { actions: string[]; children: React.ReactNode }) {
  const can = useCan();
  return can(...actions) ? <>{children}</> : <Navigate to="/" replace />;
}
