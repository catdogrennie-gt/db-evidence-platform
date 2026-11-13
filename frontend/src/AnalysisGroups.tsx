import { useEffect, useState } from 'react';
import {
  Alert, App, Button, Card, Empty, Form, Input, InputNumber, Modal, Select,
  Space, Steps, Switch, Table, Tabs, Tag, Typography,
} from 'antd';
import { api, query, request, text } from './api';
import type { Row } from './api';
import { useCan, useResource, useScope, useScopedPath } from './hooks';
import { ContextLink, ErrorNotice, LoadState, Status, Time } from './ui';
import { MetricPanel } from './Intelligence';
import {
  analysisGroupPayload, analysisGroupReadyLabel, analysisHistoryCoverage, analysisItemStatus, analysisSelectionKey, MAX_ANALYSIS_GROUP_ITEMS, scopeKey, updateAnalysisSelection,
} from './intelligenceModel';
import type {
  AnalysisGroup, AnalysisRelation, DiscoveredHost, MonitoringSource, ObjectMetrics,
  ZabbixItem,
} from './intelligenceModel';

const kindLabels: Record<ZabbixItem['value_kind'], string> = {
  numeric: '数值', text: '文本', log: '日志', unsupported: '非数值 / 暂不支持',
};
const relationLabels: Record<AnalysisRelation['kind'], string> = {
  runs_on: '运行于', depends_on: '依赖', connects_to: '连接到', related_to: '关联',
};
interface TagFilter { tag: string; value: string; operator: 'equals' | 'contains' | 'exists' }
interface ItemPage {
  items: ZabbixItem[];
  total: number;
  offset: number;
  limit: number;
  next_offset: number | null;
  truncated: boolean;
}
function sampleTime(item: ZabbixItem): string | undefined {
  const clock = Number(item.lastclock);
  const date = new Date(clock * 1000);
  return Number.isFinite(clock) && clock > 0 && Number.isFinite(date.getTime()) ? date.toISOString() : undefined;
}
function ItemState({ item }: { item: ZabbixItem }) {
  const status = analysisItemStatus(item);
  return <Tag color={status.color}>{status.label}</Tag>;
}
function ItemTags({ item }: { item: ZabbixItem }) {
  return <Space size={[0, 4]} wrap>{(item.tags ?? []).map((tag, index) => (
    <Tag key={`${tag.tag}:${tag.value}:${index}`}>{tag.tag}{tag.value ? `: ${tag.value}` : ''}</Tag>
  ))}</Space>;
}

export function AnalysisGroups({ sources, sourcesLoading, version, changed }: {
  sources: MonitoringSource[]; sourcesLoading: boolean; version: number; changed: () => void;
}) {
  const can = useCan();
  const { scope } = useScope();
  const businessScope = scopeKey(scope);
  const path = useScopedPath('/monitoring/groups');
  const resource = useResource<{ items: AnalysisGroup[]; total: number }>(can('data.read') ? path : null, version);
  const [editing, setEditing] = useState<AnalysisGroup | 'new'>();
  const [viewing, setViewing] = useState<AnalysisGroup>();
  const [busyId, setBusyId] = useState<string>();
  const [error, setError] = useState<unknown>();
  const { message } = App.useApp();
  useEffect(() => { setEditing(undefined); setViewing(undefined); }, [businessScope]);
  function reload() { resource.reload(); changed(); }
  async function toggle(group: AnalysisGroup) {
    setBusyId(group.id); setError(undefined);
    try {
      const body = analysisGroupPayload({ ...group, enabled: !group.enabled }, group.items, group.relations ?? []);
      await api.put(`/monitoring/groups/${group.id}`, body);
      message.success(group.enabled ? '联合对象已暂停，历史报告保留' : '联合对象已恢复采集');
      reload();
    } catch (e) { setError(e); } finally { setBusyId(undefined); }
  }
  return (
    <section className="analysis-groups">
      <div className="intel-section-head">
        <div><h2>联合分析对象</h2><p className="intel-muted">把数据库、服务器、应用的相关指标放在一起，交给同一轮巡检分析。</p></div>
        {can('asset.onboard') && <Button type="primary" onClick={() => setEditing('new')}>新建联合分析对象</Button>}
      </div>
      <ErrorNotice error={error} />
      {!can('data.read') && <Alert type="info" showIcon title="当前身份尚无业务数据读取权限" description="来源管理权限与数据权限独立。需要获得对应业务系统的数据授权后，才能查看和组成联合分析对象。" />}
      {can('data.read') && <LoadState {...resource} retry={resource.reload}>
        {resource.data && (resource.data.items.length ? <div className="intel-object-grid">
          {resource.data.items.map(group => (
            <Card key={group.id} className="analysis-group-card" title={group.name}
              extra={<Tag color={group.enabled ? 'blue' : 'default'}>{group.enabled ? '已启用采集' : '已暂停'}</Tag>}>
              <p className="analysis-group-description">{group.description || '按所选主机和监控项联合观察'}</p>
              <div className="analysis-group-counts">
                <div><strong>{group.host_count}</strong><span>主机</span></div>
                <div><strong>{group.item_count}</strong><span>监控项</span></div>
                <div><strong>{group.value_kind_counts?.numeric ?? 0}</strong><span>数值指标</span></div>
              </div>
              <div className="analysis-host-tags">{(group.hosts ?? []).map(host => <Tag key={host.hostid}>{host.name}</Tag>)}</div>
              <div className="intel-object-meta">
                <span>{group.source_name}</span>{group.status === 'ready' ? <Tag color="blue">{analysisGroupReadyLabel(group.last_observed_at)}</Tag> : <Status value={group.status} />}
                {Object.entries(group.value_kind_counts ?? {}).filter(([, count]) => !!count).map(([kind, count]) => (
                  <span key={kind}>{kindLabels[kind as ZabbixItem['value_kind']] ?? kind} {count}</span>
                ))}
              </div>
              <div className="intel-object-footer"><span>最近观察 <Time value={group.last_observed_at} /></span><span>每 {group.interval_seconds} 秒采集</span></div>
              <Space wrap>
                <Button type="primary" onClick={() => setViewing(group)}>查看数据</Button>
                <ContextLink to="/admin/inspection"><Button>配置巡检</Button></ContextLink>
                {can('asset.onboard') && can('asset.manage') && <>
                  <Button onClick={() => setEditing(group)}>编辑范围</Button>
                  <Button type="text" loading={busyId === group.id} onClick={() => toggle(group)}>{group.enabled ? '暂停' : '启用'}</Button>
                </>}
              </Space>
            </Card>
          ))}
        </div> : <Card className="analysis-empty-card">
          <div className="analysis-flow"><span>① 选择 Zabbix 来源</span><span>② 勾选主机与监控项</span><span>③ 组成联合分析对象</span><span>④ 后台巡检、查看结果</span></div>
          <Empty description="尚未创建当前范围的联合分析对象">
            {can('asset.onboard') && <Button type="primary" onClick={() => setEditing('new')}>开始组合监控数据</Button>}
          </Empty>
          <p className="intel-muted">可同时选择数据库、Linux 主机和应用指标。按 Zabbix 的单位、值类型和标签读取，不要求先定义对象类型。</p>
        </Card>)}
      </LoadState>}
      {editing && <GroupEditor key={editing === 'new' ? 'new' : `${editing.id}:${editing.config_revision}`}
        group={editing === 'new' ? undefined : editing} sources={sources} sourcesLoading={sourcesLoading}
        close={() => setEditing(undefined)} saved={() => { setEditing(undefined); reload(); }} />}
      {viewing && <GroupData key={`${viewing.id}:${viewing.config_revision}`} group={viewing} close={() => setViewing(undefined)} />}
    </section>
  );
}

function TagFilterInput({ title, value, onChange }: { title: string; value: TagFilter; onChange: (value: TagFilter) => void }) {
  return <div className="analysis-tag-filter">
    <label>{title}</label>
    <Input aria-label={`${title}名称`} placeholder="标签名，如 component" value={value.tag} onChange={e => onChange({ ...value, tag: e.target.value })} />
    <Select aria-label={`${title}匹配方式`} value={value.operator} onChange={operator => onChange({ ...value, operator })}
      options={[{ value: 'equals', label: '等于' }, { value: 'contains', label: '包含' }, { value: 'exists', label: '存在' }]} />
    <Input aria-label={`${title}值`} placeholder="标签值" disabled={value.operator === 'exists'} value={value.value} onChange={e => onChange({ ...value, value: e.target.value })} />
  </div>;
}

function GroupEditor({ group, sources, sourcesLoading, close, saved }: {
  group?: AnalysisGroup; sources: MonitoringSource[]; sourcesLoading: boolean; close: () => void; saved: () => void;
}) {
  const [form] = Form.useForm();
  const sourceId = Form.useWatch('source_id', form) as string | undefined;
  const [step, setStep] = useState(0);
  const [selected, setSelected] = useState<ZabbixItem[]>(group?.items ?? []);
  const [relations, setRelations] = useState<AnalysisRelation[]>(group?.relations ?? []);
  const [hostids, setHostids] = useState<string[]>([]);
  const [search, setSearch] = useState('');
  const emptyTag: TagFilter = { tag: '', value: '', operator: 'equals' };
  const [hostTag, setHostTag] = useState<TagFilter>(emptyTag);
  const [itemTag, setItemTag] = useState<TagFilter>(emptyTag);
  const [filter, setFilter] = useState<Row>({});
  const [offset, setOffset] = useState(0);
  const [itemPage, setItemPage] = useState<ItemPage>();
  const [loading, setLoading] = useState(false);
  const [queryError, setQueryError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [relationFrom, setRelationFrom] = useState<string>();
  const [relationTo, setRelationTo] = useState<string>();
  const [relationKind, setRelationKind] = useState<AnalysisRelation['kind']>('depends_on');
  const [relationLabel, setRelationLabel] = useState('');
  const hosts = useResource<{ items: DiscoveredHost[]; total: number; truncated: boolean }>(sourceId ? `/monitoring/sources/${sourceId}/hosts` : null);
  const selectedHosts = [...new Map(selected.map(item => [item.hostid, { hostid: item.hostid, name: item.host_name }])).values()];
  const selectedKeys = selected.map(analysisSelectionKey);
  const { message } = App.useApp();
  useEffect(() => {
    if (!sourceId || step !== 1) return;
    const controller = new AbortController();
    setLoading(true); setQueryError(undefined); setItemPage(undefined);
    request<ItemPage>(`/monitoring/sources/${sourceId}/items/query`, {
      method: 'POST', body: { ...filter, offset, limit: 50 }, signal: controller.signal,
    }).then(result => { if (!controller.signal.aborted) setItemPage(result); })
      .catch(e => { if (!controller.signal.aborted) setQueryError(e); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [sourceId, step, filter, offset]);
  function searchItems() {
    const clause = (value: TagFilter) => value.tag.trim() ? [{ tag: value.tag.trim(), operator: value.operator, ...(value.operator === 'exists' ? {} : { value: value.value.trim() }) }] : undefined;
    setOffset(0);
    setFilter({ hostids: hostids.length ? hostids : undefined, search: search.trim() || undefined, host_tags: clause(hostTag), item_tags: clause(itemTag) });
  }
  function select(keys: React.Key[]) {
    try {
      const next = updateAnalysisSelection(selected, itemPage?.items ?? [], keys.map(String));
      setSelected(next); setError(undefined);
      const remaining = new Set(next.map(item => item.hostid));
      setRelations(previous => previous.filter(relation => remaining.has(relation.from_hostid) && remaining.has(relation.to_hostid)));
    } catch (e) { setError(e); }
  }
  function removeItem(item: ZabbixItem) {
    const next = selected.filter(candidate => analysisSelectionKey(candidate) !== analysisSelectionKey(item));
    setSelected(next);
    const remaining = new Set(next.map(candidate => candidate.hostid));
    setRelations(previous => previous.filter(relation => remaining.has(relation.from_hostid) && remaining.has(relation.to_hostid)));
  }
  async function next() {
    setError(undefined);
    if (step === 0) { try { await form.validateFields(['name', 'source_id', 'interval_seconds']); } catch { return; } }
    if (step === 1 && !selected.length) { setError(new Error('请至少选择一个监控项，数值、文本和日志均可选择')); return; }
    setStep(value => value + 1);
  }
  async function save() {
    setBusy(true); setError(undefined);
    try {
      const values = form.getFieldsValue(true) as Row;
      const body = analysisGroupPayload({ ...values, ...(group ? { config_revision: group.config_revision } : {}) }, selected, relations);
      if (group) await api.put(`/monitoring/groups/${group.id}`, body);
      else await api.post('/monitoring/groups', body, true);
      message.success('联合分析对象已保存，可查看实际数据并配置后台巡检');
      saved();
    } catch (e) { setError(e); } finally { setBusy(false); }
  }
  function addRelation() {
    if (!relationFrom || !relationTo || relationFrom === relationTo) { setError(new Error('请选择两个不同的已选主机')); return; }
    if (relations.length >= 32) { setError(new Error('最多登记 32 条关系')); return; }
    if (relations.some(r => r.from_hostid === relationFrom && r.to_hostid === relationTo && r.kind === relationKind)) return;
    setRelations(previous => [...previous, { from_hostid: relationFrom, to_hostid: relationTo, kind: relationKind, label: relationLabel.trim() }]);
    setRelationLabel(''); setError(undefined);
  }
  const chosenSource = sources.find(source => source.id === sourceId);
  return <Modal open width={1120} className="analysis-group-editor" title={group ? `编辑联合分析对象 · ${group.name}` : '新建联合分析对象'}
    onCancel={close} destroyOnHidden footer={<div className="analysis-wizard-footer">
      <span>{selectedHosts.length} 个主机 · {selected.length} / {MAX_ANALYSIS_GROUP_ITEMS} 个监控项</span>
      <Space><Button onClick={close}>取消</Button>{step > 0 && <Button onClick={() => setStep(value => value - 1)}>上一步</Button>}
        {step < 3 ? <Button type="primary" onClick={next}>下一步</Button> : <Button type="primary" loading={busy} onClick={save}>保存联合对象</Button>}</Space>
    </div>}>
    <Steps current={step} size="small" items={[{ title: '定义范围' }, { title: '选择数据' }, { title: '登记关系' }, { title: '核对保存' }]} />
    <ErrorNotice error={error} />
    <Form form={form} layout="vertical" initialValues={{ name: group?.name, description: group?.description ?? '', source_id: group?.source_id, enabled: group?.enabled ?? true, interval_seconds: group?.interval_seconds ?? 60 }}>
      <div hidden={step !== 0} className="analysis-wizard-step">
        <Alert type="info" showIcon title="一个联合对象，一组共同分析的数据" description="例如：订单 MySQL + 所在 Linux 主机 + 应用连接探针。先选同一来源，后续精确勾选需要持续分析的监控项。" />
        <div className="intel-form-pair">
          <Form.Item name="name" label="联合对象名称" rules={[{ required: true, whitespace: true }]}><Input placeholder="例如：订单系统 · 数据库与主机" maxLength={120} /></Form.Item>
          <Form.Item name="source_id" label="Zabbix 来源" rules={[{ required: true }]} extra={sources.length ? '沿用来源所属业务系统与权限，不需要数据库口令。' : '请先在监控来源中连接只读 Zabbix。'}>
            <Select disabled={!!group} loading={sourcesLoading} placeholder="选择已连接的来源" options={sources.map(source => ({ value: source.id, label: source.name }))}
              onChange={() => { setSelected([]); setRelations([]); setHostids([]); setFilter({}); setOffset(0); }} />
          </Form.Item>
        </div>
        <Form.Item name="description" label="分析目标（可选）"><Input.TextArea placeholder="例如：关注业务响应变慢时，数据库负载与主机资源是否同时变化。" rows={3} maxLength={1000} showCount /></Form.Item>
        <div className="intel-form-pair">
          <Form.Item name="interval_seconds" label="数据采集周期（秒）" rules={[{ required: true }]} extra="分析周期在巡检策略中单独配置。"><InputNumber min={10} max={3600} step={5} /></Form.Item>
          <Form.Item name="enabled" label="保存后启用数据采集" valuePropName="checked"><Switch /></Form.Item>
        </div>
      </div>
      {step === 1 && <div className="analysis-wizard-step">
        <Alert type="info" showIcon title="按实际监控项选择，过滤不会清空已选数据" description={`未知 key 也可接入。数值可绘图，文本和日志保留各自含义；尚无采样或不支持的项目会明确显示。最多 ${MAX_ANALYSIS_GROUP_ITEMS} 项，建议选择与分析目标相关的数据。`} />
        <div className="analysis-item-filters">
          <label htmlFor="analysis-hosts">主机范围</label>
          <Select id="analysis-hosts" mode="multiple" allowClear showSearch optionFilterProp="label" value={hostids} onChange={setHostids} loading={hosts.loading}
            placeholder="全部允许的主机；也可选多个主机后筛选" options={(hosts.data?.items ?? []).map(host => ({ value: host.hostid, label: `${host.name} · ${host.item_count} 项` }))} />
          <Input.Search aria-label="搜索监控项名称或key" placeholder="监控项名称或 key" allowClear value={search} onChange={e => setSearch(e.target.value)} onSearch={searchItems} />
          <details className="analysis-filter-details"><summary>按 Zabbix 标签筛选</summary><TagFilterInput title="主机标签" value={hostTag} onChange={setHostTag} /><TagFilterInput title="监控项标签" value={itemTag} onChange={setItemTag} /></details>
          <Button onClick={searchItems} loading={loading}>查询监控项</Button>
        </div>
        <ErrorNotice error={hosts.error ?? queryError} />
        {hosts.data?.truncated && <Alert type="warning" showIcon title="主机发现结果较多" description="下方可按名称、key和标签继续查询监控项；不要将当前主机列表当作全部主机。" />}
        <div className="analysis-selected-summary"><strong>已选 {selected.length} 项</strong><span>{selectedHosts.map(host => host.name).join(' · ') || '从下表勾选需要关联的数据'}</span></div>
        <Table<ZabbixItem> rowKey={analysisSelectionKey} size="small" loading={loading} pagination={false} dataSource={itemPage?.items ?? []} scroll={{ x: 870, y: 360 }}
          rowSelection={{ selectedRowKeys: selectedKeys, preserveSelectedRowKeys: true, onChange: select,
            getCheckboxProps: item => ({ disabled: item.selectable === false || (selected.length >= MAX_ANALYSIS_GROUP_ITEMS && !selectedKeys.includes(analysisSelectionKey(item))) }),
          }} columns={[
            { title: '监控项 / 来源主机', width: 310, render: (_, item) => <div><strong>{item.name}</strong><div className="intel-muted">{item.host_name} · <code>{item.key}</code></div><ItemTags item={item} /></div> },
            { title: '含义', width: 125, render: (_, item) => <><Tag>{kindLabels[item.value_kind] ?? '未知值类型'}</Tag><div className="intel-muted">{item.units || '未设置单位'} · 类型 {item.value_type}</div></> },
            { title: '实际采样', width: 175, render: (_, item) => <><ItemState item={item} /><div className="intel-muted"><Time value={sampleTime(item)} /></div>{item.error && <Typography.Text type="danger">{item.error}</Typography.Text>}</> },
            { title: '说明', width: 210, render: (_, item) => <div className="analysis-item-description">{item.description || '上游未提供说明'}{item.enhancement && <div><Tag color="blue">提供语义增强</Tag></div>}</div> },
          ]} locale={{ emptyText: queryError ? '查询失败，请重试' : '没有匹配的监控项；可放宽筛选条件' }} />
        <div className="analysis-pagination"><span>{itemPage ? `匹配 ${itemPage.total} 项 · 当前 ${itemPage.items.length ? offset + 1 : 0}–${offset + itemPage.items.length}` : '查询 Zabbix 监控项'}{itemPage?.truncated ? ' · 上游返回受限' : ''}</span><Space>
          <Button disabled={offset === 0 || loading} onClick={() => setOffset(value => Math.max(0, value - 50))}>上一页</Button>
          <Button disabled={itemPage?.next_offset == null || loading} onClick={() => setOffset(itemPage?.next_offset ?? offset)}>下一页</Button>
        </Space></div>
        <p className="intel-muted">“采样较早”提示距最近采样超过 15 分钟，不代替监控项自己的采集周期或故障判断。</p>
      </div>}
      {step === 2 && <div className="analysis-wizard-step">
        <Alert type="info" showIcon title="关系由你登记，AI 可结合证据核对" description="这一步可跳过。关系说明哪些主机或应用彼此依赖，不代表已证明故障因果。只能关联当前已选监控项所属的主机。" />
        {selectedHosts.length > 1 ? <>
          <div className="analysis-relation-builder">
            <Select aria-label="关系起点" placeholder="起点主机" value={relationFrom} onChange={setRelationFrom} options={selectedHosts.map(host => ({ value: host.hostid, label: host.name }))} />
            <Select aria-label="关系类型" value={relationKind} onChange={setRelationKind} options={Object.entries(relationLabels).map(([value, label]) => ({ value, label }))} />
            <Select aria-label="关系终点" placeholder="终点主机" value={relationTo} onChange={setRelationTo} options={selectedHosts.map(host => ({ value: host.hostid, label: host.name, disabled: host.hostid === relationFrom }))} />
            <Input aria-label="关系说明" placeholder="关系说明（可选）" maxLength={200} value={relationLabel} onChange={e => setRelationLabel(e.target.value)} />
            <Button onClick={addRelation}>添加关系</Button>
          </div>
          <div className="analysis-relations">{relations.length ? relations.map((relation, index) => <div key={index}>
            <span><strong>{selectedHosts.find(host => host.hostid === relation.from_hostid)?.name}</strong> <Tag>{relationLabels[relation.kind]}</Tag> <strong>{selectedHosts.find(host => host.hostid === relation.to_hostid)?.name}</strong>{relation.label && <span> · {relation.label}</span>} <Tag>人工登记</Tag></span>
            <Button type="text" onClick={() => setRelations(previous => previous.filter((_, i) => i !== index))}>移除</Button>
          </div>) : <Empty description="尚未登记关系，可以直接进入下一步" />}</div>
        </> : <Empty description="当前只选了一个主机，无需登记跨主机关系，可直接下一步" />}
      </div>}
      {step === 3 && <div className="analysis-wizard-step">
        <div className="analysis-review-heading"><div><h3>{text(form.getFieldValue('name'))}</h3><p>{chosenSource?.name ?? group?.source_name} · {selectedHosts.length} 个主机 · {selected.length} 个监控项 · {relations.length} 条登记关系</p></div><Tag color="blue">{group ? `更新版本 ${group.config_revision}` : '新的分析范围'}</Tag></div>
        <Alert type="info" showIcon title="只分析下面明确选中的数据" description="同一主机的其他监控项不会自动加入。保存后到“巡检策略”选择这个联合对象，后台按策略持续分析；更改范围不会重写已有报告。" />
        <Table<ZabbixItem> size="small" rowKey={analysisSelectionKey} dataSource={selected} pagination={{ pageSize: 8, showSizeChanger: false }} scroll={{ x: 650 }} columns={[
          { title: '主机', dataIndex: 'host_name' }, { title: '监控项', dataIndex: 'name' },
          { title: '值类型 / 单位', render: (_, item) => `${kindLabels[item.value_kind]} · ${item.units || '未设置'}` },
          { title: '采样状态', render: (_, item) => <ItemState item={item} /> },
          { title: '', render: (_, item) => <Button type="text" onClick={() => removeItem(item)}>移除</Button> },
        ]} />
        {selected.some(item => analysisItemStatus(item).label !== '已采样') && <Alert type="warning" showIcon title="所选项包含采样缺口或非数值类型" description="可以保存以持续观察，但这些内容不等于零或健康，AI 分析也必须保留缺口说明。" />}
        {group && <p className="intel-muted">若其他维护者已修改本对象，服务端会拒绝覆盖。请关闭编辑、重新加载列表后核对最新范围。</p>}
      </div>}
    </Form>
  </Modal>;
}

export function GroupData({ group, close }: { group: AnalysisGroup; close: () => void }) {
  const { scope, refresh } = useScope();
  const metrics = useResource<ObjectMetrics>(`/monitoring/groups/${group.id}/metrics?${query({ from: scope.from, to: scope.to })}`, refresh);
  const [itemId, setItemId] = useState<string>();
  const selectedItem = group.items.find(item => item.itemid === itemId);
  return <Modal open title={`${group.name} · 实际监控数据`} width={1180} onCancel={close} footer={<Space><ContextLink to="/admin/inspection"><Button>配置后台巡检</Button></ContextLink><Button onClick={close}>关闭</Button></Space>}>
    <div className="analysis-data-summary"><Tag>{group.host_count} 个主机</Tag><Tag>{group.item_count} 个监控项</Tag><span><Time value={scope.from} /> — <Time value={scope.to} /></span><span>来源：{group.source_name}</span></div>
    <Alert type="info" showIcon title="读取当前选定时间范围" description="数值使用实际时序，文本与日志按原值查看；缺数保留缺口。本页查询不会修改已经生成的历史分析报告。" />
    <Tabs items={[
      { key: 'numeric', label: `数值图表 (${group.value_kind_counts?.numeric ?? 0})`, children: <LoadState {...metrics} retry={metrics.reload}>{metrics.data && (metrics.data.charts.length ? <div className="intel-metrics-grid">{metrics.data.charts.map(chart => <MetricPanel key={chart.id} data={chart} />)}</div> : <Empty description="当前时间范围没有数值曲线，请核对实际采样与采集缺口" />)}</LoadState> },
      { key: 'history', label: '原始历史 / 文本与日志', children: <>
        <div className="analysis-history-picker"><Select showSearch allowClear optionFilterProp="label" placeholder="选择一个监控项查看实际历史" value={itemId} onChange={setItemId} options={group.items.map(item => ({ value: item.itemid, label: `${item.host_name} · ${item.name} · ${kindLabels[item.value_kind]}` }))} /></div>
        {selectedItem ? <ItemHistory key={`${selectedItem.itemid}:${scope.from}:${scope.to}`} group={group} item={selectedItem} /> : <Empty description="选择数值、文本或日志监控项；原值不会强制转成折线" />}
      </> },
      { key: 'scope', label: '组成与登记关系', children: <>
        <Table<ZabbixItem> rowKey={analysisSelectionKey} dataSource={group.items} size="small" pagination={{ pageSize: 8, showSizeChanger: false }} scroll={{ x: 650 }} columns={[
          { title: '主机 / 监控项', render: (_, item) => <><strong>{item.host_name}</strong><div>{item.name}</div><code className="intel-muted">{item.key}</code></> },
          { title: '类型 / 单位', render: (_, item) => `${kindLabels[item.value_kind]} · ${item.units || '未设置'}` },
          { title: '标签', render: (_, item) => <ItemTags item={item} /> },
          { title: '上游状态', render: (_, item) => <><ItemState item={item} /><div><Time value={sampleTime(item)} /></div></> },
        ]} />
        <h4>人工登记的关系</h4>{group.relations?.length ? <div className="analysis-relations">{group.relations.map((relation, index) => <div key={index}><span>{group.hosts.find(host => host.hostid === relation.from_hostid)?.name} <Tag>{relationLabels[relation.kind]}</Tag> {group.hosts.find(host => host.hostid === relation.to_hostid)?.name}{relation.label && ` · ${relation.label}`}</span><Tag>人工登记</Tag></div>)}</div> : <p className="intel-muted">未登记关系。共同采样或同时变化不能单独证明因果。</p>}
      </> },
    ]} />
  </Modal>;
}

interface HistoryRow {
  time?: string; value: unknown; clock?: string | number; ns?: string | number;
  timestamp?: string; source?: string; severity?: string; logeventid?: string;
}
interface HistoryResult { item: ZabbixItem; kind: ZabbixItem['value_kind']; rows: HistoryRow[]; coverage: Row; provenance: Row }
export function ItemHistory({ group, item }: { group: AnalysisGroup; item: ZabbixItem }) {
  const { scope, refresh } = useScope();
  const can = useCan();
  const permitted = !['text', 'log'].includes(item.value_kind) || can('evidence.read_sensitive') || Boolean(item.diagnostic_query);
  const resource = useResource<HistoryResult>(permitted ? `/monitoring/groups/${group.id}/items/${item.itemid}/history?${query({ from: scope.from, to: scope.to, limit: 100 })}` : null, refresh);
  const coverage = analysisHistoryCoverage(resource.data?.coverage ?? {});
  function renderValue(value: unknown): string {
    if (value === null || value === undefined) return '未返回值';
    return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  }
  if (!permitted) return <Alert type="info" showIcon title="查看文本或日志原文需要敏感证据读取权限" description="当前可以查看监控项元数据和数值图表。原文未请求；这属于权限限制，不表示没有采集到数据。" />;
  return <LoadState {...resource} retry={resource.reload}>{resource.data && <>
    <Space wrap><Tag>{kindLabels[resource.data.kind]}</Tag><Tag>{item.units || '未设置单位'}</Tag><ItemState item={resource.data.item ?? item} /></Space>
    {resource.data.coverage?.representation === 'redacted_diagnostics' && <Alert type="info" showIcon title="只读诊断视图" description="展示配置、性能统计和日志事件的脱敏信息；数据库对象名称、SQL 和日志正文不公开。" />}
    {resource.data.kind === 'unsupported' && <Alert type="warning" showIcon title="此值类型暂不参与数值分析" description="监控项身份和元数据保留；不会将二进制或未知内容当成数值。" />}
    <div className="analysis-history-meta"><span>覆盖状态：{coverage.label}</span><span>返回 {resource.data.rows.length} 条，单次最多 100 条</span>{resource.data.coverage?.truncated ? <Tag color="warning">历史已截断</Tag> : null}</div>
    {coverage.reasons.length > 0 && <Alert type="warning" showIcon title="历史覆盖说明" description={coverage.reasons.join('；')} />}
    {(resource.data.item?.history_semantics ?? item.history_semantics) === 'sparse_on_change' && <p className="intel-muted">此项按值变化或心跳保留历史；没有新记录不等于采集失败。不会用旧值补写当前数据。</p>}
    <Table<HistoryRow> size="small" rowKey={(row, index) => `${row.clock}:${row.ns}:${index}`} dataSource={resource.data.rows} pagination={{ pageSize: 10, showSizeChanger: false }} columns={[
      { title: '实际采样时间', width: 200, render: (_, row) => <Time value={row.time ?? (Number(row.clock) > 0 ? new Date(Number(row.clock) * 1000).toISOString() : undefined)} /> },
      { title: resource.data.coverage?.representation === 'redacted_diagnostics' ? '诊断数据' : '原始值', render: (_, row) => <pre className="analysis-history-value">{renderValue(row.value)}</pre> },
      ...(resource.data.kind === 'log' ? [{ title: '日志元数据', width: 240, render: (_: unknown, row: HistoryRow) => <div className="analysis-log-metadata">
        <div>来源：{row.source ?? '未提供'}</div><div>级别：{row.severity ?? '未提供'}</div>
        <div>事件 ID：{row.logeventid ?? '未提供'}</div><div>日志时间戳：{row.timestamp ?? '未提供'}</div>
      </div> }] : []),
    ]} locale={{ emptyText: '当前窗口没有返回历史；这不表示测量值为零' }} />
    {item.description && <p className="intel-muted">上游说明：{item.description}</p>}
  </>}</LoadState>;
}
