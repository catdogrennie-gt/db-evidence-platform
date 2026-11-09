import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Avatar,
  Button,
  Card,
  Form,
  Input,
  Layout,
  Menu,
  Select,
  Space,
  Switch,
  Tag,
  Typography,
} from 'antd';
import { Navigate, Route, Routes, useLocation, useSearchParams } from 'react-router-dom';
import { api, ApiError, pageItems, query, setCsrf, text } from './api';
import type { Actor, Instance, Page, Row } from './api';
import {
  ScopeContext,
  SessionContext,
  useCan,
  useResource,
} from './hooks';
import type { Scope } from './hooks';
import {
  ContextLink,
  ErrorNotice,
  LoadState,
} from './ui';
import { HistoricalPage, RetiredPage } from './Historical';
import { Admin } from './Admin';
import { Demo } from './Demo';
import { IntelligenceHome, IssueReport } from './Intelligence';
import { MetricsDashboard, Monitoring } from './Monitoring';
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
export const menuDefinitions = [
  { key: '/', label: '问题分析', symbol: '◈', permissions: ['data.read', 'event.read'] },
  { key: '/monitoring', label: 'Zabbix 接入', symbol: '▤', permissions: ['data.read', 'asset.onboard'] },
  { key: '/metrics', label: '数据查看', symbol: '▥', permissions: ['data.read'] },
  { key: '/admin', label: '配置', symbol: '⚙', permissions: ['investigation.create', 'user.manage', 'grant.manage', 'group.manage', 'organization.manage', 'settings.manage', 'platform.read', 'audit.read', 'rule.manage', 'model_policy.manage'] },
  { key: '/demo', label: '演示', symbol: '▶', permissions: ['demo.operate'] },
];
export function showGlobalFilters(path: string) {
  return path.replace(/\/+$/, '') !== '/demo';
}
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
    can('data.read') ? '/monitoring/groups?limit=100' : null,
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
    if (!rolling || !showGlobalFilters(location.pathname)) return;
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
  }, [rolling, setSearch, location.pathname]);
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
            items={nav.map((item) => ({ key: item.key, icon: <span aria-hidden>{item.symbol}</span>, label: <ContextLink to={item.key}>{item.label}</ContextLink> }))}
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
            {showGlobalFilters(location.pathname) && <><div className="scopebar">
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
                  aria-label="联合分析对象"
                  placeholder="全部联合分析对象"
                  allowClear
                  showSearch
                  optionFilterProp="label"
                  value={scope.instance_id}
                  onChange={(v) => setFilter('instance_id', v)}
                  options={pageItems(assets.data)
                    .filter((i) => !scope.system_id || i.system_id === scope.system_id)
                    .map((i) => ({ value: String(i.instance_id), label: i.name }))}
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
            ) : null}</>}
            <Routes>
              <Route path="/" element={<IntelligenceHome />} />
              <Route path="/issues/:id" element={<IssueReport />} />
              <Route path="/monitoring" element={<Monitoring />} />
              <Route path="/metrics" element={<MetricsDashboard />} />
              <Route path="/demo" element={<Demo />} />
              <Route path="/inspection-settings" element={<ScopedRedirect to="/admin/inspection" />} />
              {['operations', 'topology', 'events', 'events/inbox', 'investigations', 'knowledge', 'tasks'].map(path => <Route key={path} path={'/' + path} element={<ScopedRedirect to="/" />} />)}
              {['assets', 'assets/new'].map(path => <Route key={path} path={'/' + path} element={<ScopedRedirect to="/monitoring" />} />)}
              <Route path="/evidence" element={<ScopedRedirect to="/metrics" />} />
              <Route path="/assets/onboarding/:draftId" element={<RetiredPage />} />
              <Route path="/assets/:id/:tab?" element={<RetiredPage />} />
              {['investigations', 'evidence', 'events', 'tasks', 'verifications', 'knowledge'].map(kind => <Route key={kind} path={'/' + kind + '/:id'} element={<HistoricalPage kind={kind} />} />)}
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
function ScopedRedirect({ to }: { to: string }) {
  const location = useLocation();
  return <Navigate replace to={to + location.search} />;
}
function validLocal(value: string) {
  return !!value && !isNaN(new Date(value).valueOf());
}
function localInput(value: string) {
  const date = new Date(value);
  if (isNaN(date.valueOf())) return '';
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}
