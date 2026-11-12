import { InspectionSettings } from './Monitoring';
import { useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Drawer,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Tabs,
  Table,
  Tag,
  Typography,
} from 'antd';
import { Link, useLocation } from 'react-router-dom';
import { pageItems, rows, text } from './api';
import type { Page, Row } from './api';
import { useCan, useResource, useScope, useScopedNavigate, useScopedPath } from './hooks';
import {
  DataTable,
  ErrorNotice,
  LoadState,
  RecordBlock,
  RecordCard,
  Status,
  Time,
  Title,
} from './ui';
import { CommandModal, Required, ResourceList } from './Workflow';
const adminPages = [
  ['organization', '组织与系统', ['organization.manage']],
  ['users', '用户与授权', ['user.manage', 'grant.manage']],
  ['groups', '组与授权', ['group.manage', 'grant.manage']],
  ['inspection', '巡检', ['investigation.create']],
  ['rules', '规则与维护窗', ['rule.manage']],
  ['ai-policy', '模型与外发', ['model_policy.manage']],
  ['platform', '平台状态与保留', ['platform.read', 'settings.manage']],
  ['audit', '操作审计', ['audit.read']],
] as const;
export function Admin() {
  const can = useCan();
  const location = useLocation();
  const navigate = useScopedNavigate();
  if (can('configuration.read') && !adminPages.some(p => can(...p[2]))) return <ReadonlySettings />;
  const active = location.pathname.split('/')[2] ?? adminPages.find((p) => can(...p[2]))?.[0];
  const pages = adminPages.filter((p) => can(...p[2]));
  return (
    <>
      <Title
        title="配置"
        description="控制面管理权限与业务资料读取权限独立。所有修改保留审计。"
      />
      <Tabs
        activeKey={active}
        onChange={(key) => navigate('/admin/' + key)}
        items={pages.map((p) => ({ key: p[0], label: p[1] }))}
      />
      {!active || !pages.some((p) => p[0] === active) ? (
        <Alert type="warning" showIcon title="当前身份没有此管理能力" />
      ) : active === 'organization' ? (
        <Organization />
      ) : active === 'users' ? (
        <Users />
      ) : active === 'groups' ? (
        <Groups />
      ) : active === 'inspection' ? (
        <InspectionSettings />
      ) : active === 'rules' ? (
        <Rules />
      ) : active === 'ai-policy' ? (
        <AiPolicies />
      ) : active === 'platform' ? (
        <Platform />
      ) : (
        <Audit />
      )}
    </>
  );
}

interface ReadonlySnapshot {
  sources: Row[]; systems: Row[]; objects: Row[]; policies: Row[]; rules: Row[];
  model: Row; tasks: Row[]; platform: Row; access: Row;
}

function ReadonlySettings() {
  const { refresh } = useScope();
  const path = useScopedPath('/readonly/overview');
  const resource = useResource<ReadonlySnapshot>(path, refresh);
  return <>
    <Title title="配置与运行状态 · 只读" description="查看平台如何接入、检查和分析演示业务；本页不提交任何变更或分析任务。" />
    <LoadState {...resource} retry={resource.reload}>{resource.data && <ReadonlySettingsView data={resource.data} />}</LoadState>
  </>;
}

export function ReadonlySettingsView({ data }: { data: ReadonlySnapshot }) {
  const objectNames = new Map(data.objects.map(row => [text(row.id), text(row.name)]));
  const names = (ids: unknown) => Array.isArray(ids) ? ids.map(id => objectNames.get(String(id)) ?? '已授权对象').join('、') : '—';
  return <div className="intel-page">
    <Alert type="info" showIcon title="完整浏览，操作只读" description="业务指标、分析结果与配置状态均在当前演示授权范围内。保存、启停、主动分析及凭据管理由管理员操作。" />
    <div className="intel-form-pair">
      <Card title="业务范围"><Descriptions size="small" column={1} items={[
        { key: 'scope', label: '可见范围', children: `${text(data.access.system_count)} 个业务系统 / ${text(data.access.object_count)} 个联合对象` },
        { key: 'role', label: '当前角色', children: <Tag>演示访客 · 只读</Tag> },
        { key: 'write', label: '可用操作', children: '查看、筛选、查询；不可写入或触发任务' },
      ]} /><p>{data.systems.map(row => `${text(row.name)}（${text(row.environment)}）`).join('、') || '当前范围没有业务系统'}</p></Card>
      <Card title="分析模型与安全边界"><Descriptions size="small" column={1} items={[
        { key: 'model', label: '模型', children: `${text(data.model.provider)} / ${text(data.model.model)}` },
        { key: 'configured', label: '配置状态', children: data.model.configured ? '已配置' : '尚未配置' },
        { key: 'access', label: '模型能力', children: '只读获准的脱敏证据，不执行 SQL、Shell 或数据库变更' },
      ]} /></Card>
    </div>
    <Card title="Zabbix 来源"><Table<Row> rowKey="id" size="small" pagination={false} dataSource={data.sources} columns={[
      { title: '来源', dataIndex: 'name' }, { title: '状态', render: (_, row) => <Status value={row.status} /> },
      { title: '联合对象', dataIndex: 'group_count' }, { title: '主机', dataIndex: 'host_count' }, { title: '已选监控项', dataIndex: 'item_count' },
      { title: '最近成功', render: (_, row) => <Time value={row.last_success_at} /> },
    ]} scroll={{ x: 650 }} locale={{ emptyText: '当前范围没有已关联来源' }} /></Card>
    <Card title="实际运行的巡检策略"><Table<Row> rowKey="id" size="small" pagination={false} dataSource={data.policies} columns={[
      { title: '分析范围', render: (_, row) => names(row.instance_ids) },
      { title: '状态', render: (_, row) => <><Tag>{row.enabled ? '已启用' : '已停用'}</Tag><Status value={row.status} /></> },
      { title: '检查周期', render: (_, row) => `${text(row.signal_interval_seconds)} 秒` },
      { title: '综合分析周期', render: (_, row) => `${text(row.ai_interval_seconds)} 秒` },
      { title: '分析方式', render: (_, row) => row.mode === 'live_redacted' ? '脱敏 AI 分析' : '确定性规则' },
      { title: '最近检查', render: (_, row) => <Time value={row.last_checked_at} /> },
      { title: '最近分析完成', render: (_, row) => <Time value={row.last_completed_at} /> },
    ]} scroll={{ x: 850 }} locale={{ emptyText: '当前范围尚无完整获准的服务策略' }} /></Card>
    <Card title="已配置规则"><Table<Row> rowKey="id" size="small" pagination={false} dataSource={data.rules} columns={[
      { title: '规则', dataIndex: 'name' }, { title: '类型', dataIndex: 'template' },
      { title: '范围', render: (_, row) => names(row.instance_ids) },
      { title: '启用', render: (_, row) => row.enabled ? '是' : '否' },
      { title: '阈值参数', render: (_, row) => Object.entries((row.parameters ?? {}) as Row).map(([key, value]) => `${key}: ${text(value)}`).join('；') || '—' },
    ]} locale={{ emptyText: '当前范围没有单独配置的规则；周期综合分析仍按巡检策略运行' }} /></Card>
    <Card title="后台执行与最近任务">
      <Space wrap><span>平台数据库 <Status value={data.platform.control_database} /></span>{rows(data.platform.workers).map(row => <span key={text(row.queue)}>{row.queue === 'ai' ? 'AI 分析' : row.queue === 'collection' ? '数据处理' : '后台队列'} <Status value={row.status} /></span>)}</Space>
      <Table<Row> rowKey="id" size="small" pagination={false} dataSource={data.tasks} columns={[
        { title: '任务', render: (_, row) => row.type === 'investigation' ? '问题分析' : row.type === 'verification' ? '核验' : '数据处理' },
        { title: '范围', render: (_, row) => names(row.instance_ids) },
        { title: '状态', render: (_, row) => <Status value={row.status} /> },
        { title: '创建', render: (_, row) => <Time value={row.created_at} /> },
        { title: '完成', render: (_, row) => <Time value={row.finished_at} /> },
        { title: '错误代码', dataIndex: 'error_code' },
      ]} scroll={{ x: 650 }} locale={{ emptyText: '当前范围暂无可见任务' }} />
      <p className="intel-muted">仅展示最近任务的安全状态摘要。分析报告与图表从“问题分析”和“数据查看”进入；人员资料、凭据及原始内部错误不在公开页展示。</p>
    </Card>
  </div>;
}
function Organization() {
  const [kind, setKind] = useState('systems');
  const [open, setOpen] = useState(false);
  const [version, setVersion] = useState(0);
  const departments = useResource<Page>('/departments', version);
  return (
    <>
      <Tabs
        activeKey={kind}
        onChange={setKind}
        items={[
          { key: 'systems', label: '业务系统' },
          { key: 'departments', label: '部门' },
        ]}
      />
      <ResourceList
        key={kind + version}
        title={kind === 'systems' ? '业务系统' : '部门'}
        description="配置状态按现在显示。归属变更不会自动扩大资料授权。"
        endpoint={'/' + kind}
        filters={<Typography.Text type="secondary">仅管理范围内</Typography.Text>}
        actions={
          <Button type="primary" onClick={() => setOpen(true)}>
            新增{kind === 'systems' ? '系统' : '部门'}
          </Button>
        }
        columns={[
          { key: 'name', title: '名称' },
          { key: 'code', title: '编码' },
          { key: 'department_name', title: '所属部门' },
          { key: 'active', title: '启用', render: (v) => text(v) },
          { key: 'instance_count', title: '管理范围内资产数' },
        ]}
      />
      <CommandModal
        title={kind === 'systems' ? '新增业务系统' : '新增部门'}
        open={open}
        onClose={() => setOpen(false)}
        endpoint={'/' + kind}
        onSuccess={() => {
          setOpen(false);
          setVersion((v) => v + 1);
        }}
        fields={
          <>
            <Required name="name" label="名称" />
            <Required name="code" label="唯一编码" />
            {kind === 'systems' && (
              <Form.Item name="department_id" label="所属部门" rules={[{ required: true }]}>
                <Select
                  options={pageItems(departments.data).map((d) => ({
                    value: text(d.id),
                    label: text(d.name),
                  }))}
                />
              </Form.Item>
            )}
            {kind === 'systems' && (
              <Form.Item name="environment" label="环境" initialValue="test">
                <Select
                  options={['production', 'staging', 'test', 'lab'].map((value) => ({
                    value,
                    label: value,
                  }))}
                />
              </Form.Item>
            )}
            <Form.Item name="description" label="说明">
              <Input.TextArea rows={3} />
            </Form.Item>
            <Form.Item name="active" label="启用" valuePropName="checked" initialValue={true}>
              <Switch />
            </Form.Item>
          </>
        }
      />
    </>
  );
}
function Users() {
  const { refresh } = useScope();
  const can = useCan();
  const [version, setVersion] = useState(0);
  const resource = useResource<Page<Row> & { delegable_roles?: Row[]; delegable_scopes?: Row[] }>(
    '/users',
    version + refresh,
  );
  const access = useResource<Row>(can('grant.manage') ? '/settings/access-options' : null, version);
  const [selected, setSelected] = useState<Row>();
  const [open, setOpen] = useState(false);
  const [edit, setEdit] = useState<Row>();
  const roles = rows(access.data?.delegable_roles ?? resource.data?.delegable_roles);
  const scopes = rows(access.data?.delegable_scopes ?? resource.data?.delegable_scopes);
  return (
    <>
      <Title
        title="用户与授权"
        description="建立账号与授予业务读取分别操作；可授角色及范围由服务端限定。"
        actions={
          can('user.manage') && (
            <Button
              type="primary"
              onClick={() => {
                setEdit(undefined);
                setOpen(true);
              }}
            >
              ＋ 建立账号
            </Button>
          )
        }
      />
      <LoadState {...resource} retry={resource.reload}>
        <Card>
          <DataTable
            items={pageItems(resource.data)}
            columns={[
              {
                key: 'username',
                title: '用户名',
                render: (v, r) => (
                  <Button type="link" onClick={() => setSelected(r)}>
                    {text(v)}
                  </Button>
                ),
              },
              { key: 'display_name', title: '显示名' },
              { key: 'active', title: '启用', render: (v) => text(v) },
              {
                key: 'roles',
                title: '角色',
                render: (v) => (Array.isArray(v) ? v.map((x) => text(x)).join(' / ') : text(v)),
              },
              { key: 'permission_version', title: '权限版本' },
              { key: 'last_login_at', title: '最后登录', render: (v) => <Time value={v} /> },
              {
                key: 'actions',
                title: '操作',
                render: (_, r) =>
                  can('user.manage') ? (
                    <Button
                      size="small"
                      onClick={() => {
                        setEdit(r);
                        setOpen(true);
                      }}
                    >
                      编辑 / 停用
                    </Button>
                  ) : (
                    '—'
                  ),
              },
            ]}
          />
        </Card>
      </LoadState>
      <Drawer
        title="账号授权详情"
        width={760}
        open={!!selected}
        onClose={() => setSelected(undefined)}
      >
        {selected && (
          <>
            <RecordCard title="身份与权限范围" data={selected} />
            {can('grant.manage') && (
              <div className="section">
                <Button
                  type="primary"
                  onClick={() => {
                    setEdit(selected);
                    setSelected(undefined);
                    setOpen(true);
                  }}
                >
                  编辑授权
                </Button>
              </div>
            )}
          </>
        )}
      </Drawer>
      <CommandModal
        title={edit ? '编辑账号与授权' : '建立本地账号'}
        open={open}
        onClose={() => setOpen(false)}
        endpoint={edit ? `/users/${edit.id}` : '/users'}
        method={edit ? 'PATCH' : 'POST'}
        initialValues={edit ?? { active: true }}
        transform={(values) => {
          const v = { ...values };
          if (!v.password) delete v.password;
          const normalized = (x: unknown) =>
            JSON.stringify(
              rows(x).map((g) => ({
                role_id: g.role_id,
                scope_type: g.scope_type,
                scope_id: g.scope_id,
              })),
            );
          if (edit && normalized(v.grants) === normalized(edit.grants)) delete v.grants;
          return v;
        }}
        onSuccess={() => {
          setOpen(false);
          setVersion((v) => v + 1);
        }}
        fields={
          <>
            {!edit && <Required name="username" label="用户名" />}
            <Required name="display_name" label="显示名" />
            <Form.Item
              name="password"
              label={edit ? '新密码（留空不修改）' : '初始密码'}
              rules={edit ? [] : [{ required: true, message: '请设置初始密码' }]}
            >
              <Input.Password autoComplete="new-password" />
            </Form.Item>
            <Form.Item name="active" label="启用账号" valuePropName="checked" initialValue={true}>
              <Switch />
            </Form.Item>
            {can('grant.manage') && (
              <>
                <ErrorNotice error={access.error} />
                <GrantFields roles={roles} scopes={scopes} />
              </>
            )}
            <Alert
              type="info"
              showIcon
              title="权限变更会提升授权版本并使旧会话失效；密码保存后不可读回。"
            />
          </>
        }
      />
    </>
  );
}
function GrantFields({ roles, scopes }: { roles: Row[]; scopes: Row[] }) {
  return (
    <>
      <Typography.Title level={5}>明确角色与资源范围</Typography.Title>
      <Alert
        type="warning"
        showIcon
        title="授予控制面管理不等于授予业务数据读取。"
        className="notice"
      />
      <Form.List name="grants">
        {(fields, { add, remove }) => (
          <>
            <Space orientation="vertical" style={{ width: '100%' }}>
              {fields.map((field) => (
                <Card
                  size="small"
                  key={field.key}
                  title={`授权 ${field.name + 1}`}
                  extra={
                    <Button type="text" danger onClick={() => remove(field.name)}>
                      移除
                    </Button>
                  }
                >
                  <Form.Item
                    name={[field.name, 'role_id']}
                    label="可委派角色"
                    rules={[{ required: true }]}
                  >
                    <Select
                      options={roles.map((role) => ({
                        value: text(role.id),
                        label: text(role.name),
                      }))}
                    />
                  </Form.Item>
                  <Form.Item
                    name={[field.name, 'scope_type']}
                    label="范围类型"
                    rules={[{ required: true }]}
                  >
                    <Select
                      options={[...new Set(scopes.map((s) => text(s.scope_type)))].map((value) => ({
                        value,
                        label: value,
                      }))}
                    />
                  </Form.Item>
                  <Form.Item
                    name={[field.name, 'scope_id']}
                    label="可委派资源"
                    rules={[{ required: true }]}
                  >
                    <Select
                      options={scopes.map((scope) => ({
                        value: text(scope.scope_id),
                        label: `${text(scope.name ?? scope.scope_id)} / ${text(scope.scope_type)}`,
                      }))}
                    />
                  </Form.Item>
                </Card>
              ))}
            </Space>
            <Button
              className="section"
              onClick={() => add()}
              disabled={!roles.length || !scopes.length}
            >
              ＋ 增加一项明确授权
            </Button>
          </>
        )}
      </Form.List>
      {!roles.length || !scopes.length ? (
        <Alert type="info" title="没有可委派的角色或范围，无法新增授权。" className="notice" />
      ) : null}
    </>
  );
}
function Groups() {
  const { refresh } = useScope();
  const [version, setVersion] = useState(0);
  const resource = useResource<Page>('/groups', version + refresh);
  const users = useResource<Page>('/users');
  const access = useResource<Row>('/settings/access-options');
  const [selected, setSelected] = useState<Row>();
  const [command, setCommand] = useState('');
  return (
    <>
      <Title
        title="组与授权"
        description="组成员和能力 × 范围分别配置，撤销将使相关成员会话失效。"
        actions={
          <Button
            type="primary"
            onClick={() => {
              setSelected(undefined);
              setCommand('create');
            }}
          >
            ＋ 建立组
          </Button>
        }
      />
      <LoadState {...resource} retry={resource.reload}>
        <Card>
          <DataTable
            items={pageItems(resource.data)}
            columns={[
              { key: 'name', title: '组名' },
              { key: 'description', title: '说明' },
              { key: 'member_count', title: '成员数' },
              {
                key: 'actions',
                title: '操作',
                render: (_, r) => (
                  <Space>
                    <Button
                      size="small"
                      onClick={() => {
                        setSelected(r);
                        setCommand('members');
                      }}
                    >
                      成员
                    </Button>
                    <Button
                      size="small"
                      onClick={() => {
                        setSelected(r);
                        setCommand('grants');
                      }}
                    >
                      范围授权
                    </Button>
                  </Space>
                ),
              },
            ]}
          />
        </Card>
      </LoadState>
      <CommandModal
        title={
          command === 'create' ? '建立组' : command === 'members' ? '替换组成员' : '替换组授权'
        }
        open={!!command}
        onClose={() => setCommand('')}
        endpoint={command === 'create' ? '/groups' : `/groups/${selected?.id}/${command}`}
        method={command === 'create' ? 'POST' : 'PUT'}
        initialValues={selected}
        onSuccess={() => {
          setCommand('');
          setVersion((v) => v + 1);
        }}
        fields={
          command === 'create' ? (
            <>
              <Required name="name" label="组名" />
              <Form.Item name="description" label="说明">
                <Input.TextArea />
              </Form.Item>
              <GrantFields
                roles={rows(access.data?.delegable_roles)}
                scopes={rows(access.data?.delegable_scopes)}
              />
            </>
          ) : command === 'members' ? (
            <>
              <Alert
                type="warning"
                title="此次提交替换当前成员列表，请核对新增与移除项。"
                className="notice"
              />
              <Form.Item name="user_ids" label="成员">
                <Select
                  mode="multiple"
                  options={pageItems(users.data).map((u) => ({
                    value: text(u.id),
                    label: text(u.display_name ?? u.username),
                  }))}
                />
              </Form.Item>
            </>
          ) : (
            <>
              <ErrorNotice error={access.error} />
              <GrantFields
                roles={rows(access.data?.delegable_roles)}
                scopes={rows(access.data?.delegable_scopes)}
              />
            </>
          )
        }
      />
    </>
  );
}
function Rules() {
  const [tab, setTab] = useState('rules');
  const [open, setOpen] = useState(false);
  const [version, setVersion] = useState(0);
  const [selected, setSelected] = useState<Row>();
  const templates = useResource<Row>('/rules', version);
  const can = useCan();
  function transform(values: Row): Row {
    if (tab !== 'rules') return values;
    const parameters: Row = {
      threshold: values.threshold,
      min_duration_seconds: values.min_duration_seconds,
      freshness_multiplier: values.freshness_multiplier,
    };
    if (values.scope !== 'all') parameters.instance_ids = values.instance_ids;
    if (values.template === 'sql_latency') {
      parameters.min_executions = values.min_executions;
      if (values.schema) parameters.schema = values.schema;
      if (values.digest) parameters.digest = values.digest;
    }
    if (['collector_stale', 'collector_unreachable'].includes(text(values.template))) {
      parameters.collector = values.collector;
      parameters.stale_intervals = values.stale_intervals;
    }
    return {
      name: values.name,
      template: values.template,
      severity: values.severity,
      enabled: values.enabled,
      scope: values.scope,
      parameters,
    };
  }
  return (
    <>
      <Tabs
        activeKey={tab}
        onChange={(key) => {
          setTab(key);
          setOpen(false);
        }}
        items={[
          { key: 'rules', label: '确定性规则' },
          { key: 'maintenance-windows', label: '维护窗' },
        ]}
      />
      <ErrorNotice error={templates.error} />
      <ResourceList
        key={tab + version}
        title={tab === 'rules' ? '规则模板' : '维护窗'}
        description="规则缺少新鲜资料时为未知；限定范围只影响所选实例。维护窗不修改采集事实。"
        endpoint={'/' + tab}
        filters={<TagLabel value="固定模板 · 无任意表达式执行" />}
        actions={
          (tab === 'rules' || can('maintenance.manage')) && (
            <Button type="primary" onClick={() => setOpen(true)}>
              ＋ 新增{tab === 'rules' ? '规则' : '维护窗'}
            </Button>
          )
        }
        columns={
          tab === 'rules'
            ? [
                { key: 'name', title: '规则' },
                { key: 'type', title: '固定模板' },
                {
                  key: 'version',
                  title: '定义版本',
                  render: (value) => <span title={text(value)}>{text(value).slice(0, 8)}</span>,
                },
                {
                  key: 'scope',
                  title: '完整作用范围',
                  render: (value, row) =>
                    value === 'all'
                      ? '全部实例（全局）'
                      : rows(row.instances)
                          .map((instance) => text(instance.name))
                          .join('、') || '范围资料未返回',
                },
                {
                  key: 'sql',
                  title: 'SQL 筛选',
                  render: (_, row) => {
                    const p = (row.parameters ?? {}) as Row;
                    return row.type === 'sql_latency' ? (
                      <Space orientation="vertical" size={3}>
                        <span>Schema：{text(p.schema, '未限定')}</span>
                        <span title={text(p.digest, '')}>
                          摘要：
                          {typeof p.digest === 'string' ? p.digest.slice(0, 16) + '…' : '未限定'}
                        </span>
                        <span>
                          至少 {text(p.min_executions, '10')} 次执行；均值超过 {text(p.threshold)}{' '}
                          秒
                        </span>
                      </Space>
                    ) : (
                      '—'
                    );
                  },
                },
                { key: 'severity', title: '级别', render: (value) => <Status value={value} /> },
                {
                  key: 'enabled',
                  title: '状态',
                  render: (value) => <Status value={value ? 'active' : 'disabled'} />,
                },
                {
                  key: 'actions',
                  title: '启用生命周期',
                  render: (_, row) => (
                    <Button size="small" onClick={() => setSelected(row)}>
                      {row.enabled ? '停用' : '启用'}
                    </Button>
                  ),
                },
              ]
            : [
                { key: 'reason', title: '维护原因' },
                { key: 'starts_at', title: '开始', render: (value) => <Time value={value} /> },
                { key: 'ends_at', title: '结束', render: (value) => <Time value={value} /> },
                { key: 'status', title: '状态', render: (value) => <Status value={value} /> },
              ]
        }
      />
      <CommandModal
        title={tab === 'rules' ? '建立固定模板规则' : '建立维护窗'}
        open={open}
        onClose={() => setOpen(false)}
        endpoint={'/' + tab}
        transform={transform}
        onSuccess={() => {
          setOpen(false);
          setVersion((value) => value + 1);
        }}
        fields={
          tab === 'rules' ? (
            <RuleFields
              templates={rows(templates.data?.templates)}
              instances={rows(templates.data?.instance_options)}
              canManageAll={templates.data?.can_manage_all === true}
              truncated={templates.data?.instance_options_truncated === true}
            />
          ) : (
            <>
              <Form.Item name="instance_id" label="作用实例" rules={[{ required: true }]}>
                <Select
                  options={rows(templates.data?.instance_options).map((instance) => ({
                    value: text(instance.id),
                    label: text(instance.name),
                  }))}
                />
              </Form.Item>
              <Required name="starts_at" label="绝对开始时间（UTC ISO）" />
              <Required name="ends_at" label="绝对结束时间（UTC ISO）" />
              <Required name="reason" label="维护原因与通知影响" area />
            </>
          )
        }
      />
      <CommandModal
        title={selected?.enabled ? '停用这条规则' : '启用这条规则'}
        open={!!selected}
        onClose={() => setSelected(undefined)}
        endpoint={`/rules/${selected?.id}`}
        method="PATCH"
        transform={(values) => ({
          reason: values.reason,
          version: selected?.rule_version ?? selected?.version,
          expected_enabled: selected?.enabled,
          enabled: !selected?.enabled,
        })}
        onSuccess={() => {
          setSelected(undefined);
          setVersion((value) => value + 1);
        }}
        fields={
          <>
            <Alert
              type="info"
              showIcon
              title={text(selected?.name)}
              description="只修改启用状态并保留审计；规则定义及历史解释不变。服务端会再次核验完整范围、定义版本和原启用状态。"
              className="notice"
            />
            <Required name="reason" label="启用 / 停用原因" area />
          </>
        }
      />
    </>
  );
}
function RuleFields({
  templates,
  instances,
  canManageAll,
  truncated,
}: {
  templates: Row[];
  instances: Row[];
  canManageAll: boolean;
  truncated: boolean;
}) {
  const form = Form.useFormInstance();
  const template = Form.useWatch('template', form) ?? 'sql_latency';
  const scope = Form.useWatch('scope', form) ?? 'instances';
  const sourceTemplate = ['collector_stale', 'collector_unreachable'].includes(text(template));
  return (
    <>
      <Required name="name" label="规则名称" />
      <Form.Item
        name="template"
        label="固定规则模板"
        initialValue="sql_latency"
        rules={[{ required: true }]}
      >
        <Select
          options={templates.map((item) => ({
            value: text(item.type ?? item.id),
            label: text(item.name),
          }))}
        />
      </Form.Item>
      <Form.Item
        name="scope"
        label="完整作用范围"
        initialValue="instances"
        rules={[{ required: true }]}
      >
        <Select
          options={[
            { value: 'instances', label: '明确选择1–5个实例' },
            ...(canManageAll ? [{ value: 'all', label: '全部实例（需要全局规则管理权限）' }] : []),
          ]}
        />
      </Form.Item>
      {scope === 'instances' && (
        <Form.Item
          name="instance_ids"
          label="规则管理范围内的实例"
          preserve={false}
          rules={[
            { required: true, message: '请选择1–5个实例' },
            { type: 'array', min: 1, max: 5 },
          ]}
        >
          <Select
            mode="multiple"
            maxCount={5}
            optionFilterProp="label"
            options={instances.map((instance) => ({
              value: text(instance.id),
              label: text(instance.name),
            }))}
          />
        </Form.Item>
      )}
      {truncated && (
        <Alert
          type="warning"
          title="实例候选已达到500项上限；请由管理者核对范围后使用受限 API 创建。"
          className="notice"
        />
      )}
      <Form.Item name="severity" label="严重度" initialValue="medium" rules={[{ required: true }]}>
        <Select
          options={['critical', 'high', 'medium', 'low'].map((value) => ({
            value,
            label: <Status value={value} />,
          }))}
        />
      </Form.Item>
      <Form.Item
        name="threshold"
        label={
          template === 'sql_latency'
            ? '平均延迟阈值（秒）'
            : sourceTemplate
              ? '过期阈值（采集周期倍数）'
              : '锁等待条数阈值'
        }
        initialValue={0.5}
        rules={[{ required: true }]}
      >
        <InputNumber min={0} max={1e9} step={template === 'sql_latency' ? 0.001 : 1} />
      </Form.Item>
      <Form.Item
        name="min_duration_seconds"
        label="新观察连续满足条件的最短秒数"
        initialValue={60}
        rules={[{ required: true }]}
      >
        <InputNumber min={0} max={3600} precision={0} />
      </Form.Item>
      <Form.Item
        name="freshness_multiplier"
        label="新鲜度上限（采集周期倍数）"
        initialValue={1.5}
        rules={[{ required: true }]}
      >
        <InputNumber min={1} max={10} step={0.5} />
      </Form.Item>
      {template === 'sql_latency' && (
        <>
          <Typography.Title level={5}>固定 SQL 摘要筛选</Typography.Title>
          <Typography.Paragraph type="secondary">
            仅匹配已采集的摘要标识和 Schema，不执行
            SQL。留空表示在所选实例中比较满足最低执行次数的摘要。
          </Typography.Paragraph>
          <Form.Item
            name="schema"
            label="Schema（可选，精确匹配）"
            preserve={false}
            rules={[
              { max: 64 },
              {
                validator: (_, value) =>
                  !value || !value.includes('\0')
                    ? Promise.resolve()
                    : Promise.reject(new Error('名称不能包含空字符')),
              },
            ]}
          >
            <Input maxLength={64} />
          </Form.Item>
          <Form.Item
            name="digest"
            label="64位十六进制摘要（可选，精确匹配）"
            preserve={false}
            rules={[
              { pattern: /^[0-9a-fA-F]{64}$/, message: '请填写64位十六进制摘要，不接受SQL文本' },
            ]}
          >
            <Input maxLength={64} placeholder="从真实 SQL 摘要资料复制 DIGEST" />
          </Form.Item>
          <Form.Item
            name="min_executions"
            label="有效窗口最低执行次数"
            initialValue={10}
            preserve={false}
            rules={[{ required: true }]}
          >
            <InputNumber min={10} max={10000} precision={0} />
          </Form.Item>
        </>
      )}
      {sourceTemplate && (
        <>
          <Form.Item
            name="collector"
            label="采集来源"
            initialValue="identity_status"
            preserve={false}
            rules={[{ required: true }]}
          >
            <Select
              options={[
                'identity_status',
                'runtime',
                'statement_digest',
                'schema_config',
                'replication',
                'error_log',
                'slow_log',
                'host',
              ].map((value) => ({ value, label: value }))}
            />
          </Form.Item>
          <Form.Item
            name="stale_intervals"
            label="超过多少采集周期后触发"
            initialValue={3}
            preserve={false}
            rules={[{ required: true }]}
          >
            <InputNumber min={1} max={100} />
          </Form.Item>
        </>
      )}
      <Form.Item name="enabled" label="创建后启用" valuePropName="checked" initialValue={true}>
        <Switch />
      </Form.Item>
      <Alert
        type="info"
        showIcon
        title="仅由新鲜、有效的确定性资料触发"
        description="规则参数保存后不可原地修改；需要调整时建立新规则，并明确停用旧规则。"
      />
    </>
  );
}
function AiPolicies() {
  const [tab, setTab] = useState('policies');
  const [version, setVersion] = useState(0);
  const policies = useResource<Row>('/settings/model-policies', version);
  const profiles = useResource<Page>('/model-profiles', version);
  const [open, setOpen] = useState(false);
  return (
    <>
      <Title
        title="模型与外发策略"
        description="新资产默认关闭外发。模型凭据只写入服务端，策略更新影响排队和运行任务。"
        actions={
          <Button type="primary" onClick={() => setOpen(true)}>
            {tab === 'policies' ? '修改范围策略' : '更新默认配置'}
          </Button>
        }
      />
      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          { key: 'policies', label: '资料外发策略' },
          { key: 'profiles', label: '模型配置' },
        ]}
      />
      {tab === 'policies' ? (
        <LoadState {...policies} retry={policies.reload}>
          <RecordCard title="生效策略与资料类别" data={policies.data} />
        </LoadState>
      ) : (
        <LoadState {...profiles} retry={profiles.reload}>
          <Card>
            <DataTable
              items={pageItems(profiles.data)}
              columns={[
                { key: 'name', title: '名称' },
                { key: 'provider', title: '服务商' },
                { key: 'model', title: '模型' },
                { key: 'has_secret', title: '密钥已配置', render: (v) => text(v) },
                { key: 'updated_at', title: '最近修改', render: (v) => <Time value={v} /> },
              ]}
            />
          </Card>
        </LoadState>
      )}
      <CommandModal
        title={tab === 'policies' ? '修改明确范围的外发策略' : '更新受信默认模型'}
        open={open}
        onClose={() => setOpen(false)}
        endpoint={tab === 'policies' ? '/settings/model-policies' : '/model-profiles'}
        method={tab === 'policies' ? 'PATCH' : 'POST'}
        onSuccess={() => {
          setOpen(false);
          setVersion((v) => v + 1);
        }}
        fields={
          tab === 'policies' ? (
            <>
              <Form.Item name="scope_type" label="作用域类型" rules={[{ required: true }]}>
                <Select
                  options={['system', 'instance'].map((value) => ({ value, label: value }))}
                />
              </Form.Item>
              <Required name="scope_id" label="授权范围 ID" />
              <Form.Item name="mode" label="策略模式" initialValue="rules_only">
                <Select
                  options={[
                    { value: 'disabled', label: '停用调查' },
                    { value: 'rules_only', label: '仅规则 / 外发关闭' },
                    { value: 'live_redacted', label: '脱敏资料实时模型' },
                  ]}
                />
              </Form.Item>
              <Form.Item name="allowed_sources" label="允许资料类别">
                <Checkbox.Group
                  options={[
                    'identity_status',
                    'runtime',
                    'statement_digest',
                    'schema_config',
                    'replication',
                    'error_log',
                    'slow_log',
                    'host',
                    'monitoring',
                  ].map((value) => ({ value, label: value === 'monitoring' ? '联合监控数据（monitoring）' : value }))}
                />
              </Form.Item>
              <Required name="reason" label="修改理由与脱敏核对" area />
            </>
          ) : (
            <>
              <RecordCard
                title="已核实模型身份（私密运行配置）"
                data={pageItems(profiles.data)[0]}
              />
              <Required name="name" label="配置名称" />
              <Form.Item name="api_key" label="新 API 密钥（只写，留空保持原值）">
                <Input.Password autoComplete="new-password" />
              </Form.Item>
              <Form.Item name="max_calls" label="每次调查模型轮数上限" initialValue={4}>
                <InputNumber min={1} max={4} />
              </Form.Item>
              <Form.Item name="max_tokens" label="每轮输出 Token 上限" initialValue={2000}>
                <InputNumber min={1} max={4096} />
              </Form.Item>
              <Alert type="warning" title="模型配置不会自动授权任何业务资料外发。" />
            </>
          )
        }
      />
    </>
  );
}
function Platform() {
  const { refresh } = useScope();
  const can = useCan();
  const status = useResource<Row>('/platform-status', refresh);
  const retention = useResource<Row>(
    can('settings.manage') ? '/settings/retention' : null,
    refresh,
  );
  const [open, setOpen] = useState(false);
  return (
    <>
      <Title
        title="平台状态与保留"
        description="进程存活、控制库就绪、Prometheus、worker 和备份状态分别报告。"
        actions={
          can('settings.manage') && <Button onClick={() => setOpen(true)}>修改资料保留策略</Button>
        }
      />
      <LoadState {...status} retry={status.reload}>
        <RecordCard title="依赖、队列与真实心跳" data={status.data} />
      </LoadState>
      {can('settings.manage') && (
        <LoadState {...retention} retry={retention.reload}>
          <RecordCard title="资料保留与受保护证据" data={retention.data} />
        </LoadState>
      )}
      <CommandModal
        title="修改资料保留策略"
        open={open}
        onClose={() => setOpen(false)}
        endpoint="/settings/retention"
        method="PATCH"
        onSuccess={() => {
          setOpen(false);
          retention.reload();
        }}
        fields={
          <>
            <Alert
              type="warning"
              title="收紧保留须先核对受保护证据影响；服务端不会立即清除受保护内容。"
              className="notice"
            />
            <Form.Item name="evidence_days" label="结构化证据保留天数" rules={[{ required: true }]}>
              <InputNumber min={1} max={3650} />
            </Form.Item>
            <Form.Item name="metrics_days" label="时序资料保留天数" rules={[{ required: true }]}>
              <InputNumber min={1} max={3650} />
            </Form.Item>
            <Required name="reason" label="调整原因" area />
          </>
        }
      />
    </>
  );
}
function Audit() {
  const [selected, setSelected] = useState<Row>();
  return (
    <>
      <ResourceList
        title="操作审计"
        description="追加记录身份、范围变更、凭据使用、工具、模型外发及取消。"
        endpoint="/audit-logs"
        filters={<Typography.Text type="secondary">只读审计，不提供删除入口</Typography.Text>}
        columns={[
          { key: 'created_at', title: '时间', render: (v) => <Time value={v} /> },
          { key: 'actor_name', title: '操作者' },
          {
            key: 'action',
            title: '动作',
            render: (v, r) => (
              <Button type="link" onClick={() => setSelected(r)}>
                {text(v)}
              </Button>
            ),
          },
          { key: 'resource_type', title: '对象类型' },
          { key: 'outcome', title: '结果' },
          { key: 'summary', title: '公开摘要' },
          { key: 'request_id', title: '请求 ID' },
        ]}
      />
      <Drawer title="审计详情" width={700} open={!!selected} onClose={() => setSelected(undefined)}>
        <RecordBlock data={selected} />
        {selected?.task_id ? <Link to={`/tasks/${selected.task_id}`}>查看关联任务</Link> : null}
      </Drawer>
    </>
  );
}
function TagLabel({ value }: { value: string }) {
  return <Typography.Text type="secondary">{value}</Typography.Text>;
}
