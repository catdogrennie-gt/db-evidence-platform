import test from 'node:test';
import assert from 'node:assert/strict';
import * as model from '../src/intelligenceModel.ts';

const item = (hostid, itemid, extra = {}) => ({
  hostid, itemid, host_name: `Host ${hostid}`, name: '同名指标', key: 'custom.metric',
  units: '', value_type: 0, value_kind: 'numeric', tags: [], status: '0', state: '0',
  lastclock: '1000', selectable: true, ...extra,
});

test('item selections preserve host identity across pages and enforce the 32-item boundary', () => {
  assert.equal(typeof model.analysisSelectionKey, 'function');
  assert.equal(typeof model.updateAnalysisSelection, 'function');
  const first = item('1', '11');
  const second = item('2', '22');
  assert.notEqual(model.analysisSelectionKey(first), model.analysisSelectionKey(second));
  assert.deepEqual(model.updateAnalysisSelection([first], [second], ['1:11', '2:22']), [first, second]);
  assert.deepEqual(model.updateAnalysisSelection([first], [second], ['2:22']), [second]);
  assert.throws(() => model.updateAnalysisSelection([], [first], ['not-a-returned-item']), /重新/);
  const many = Array.from({ length: 33 }, (_, index) => item('1', String(index)));
  assert.equal(model.updateAnalysisSelection([], many, many.slice(0, 32).map(model.analysisSelectionKey)).length, 32);
  assert.throws(() => model.updateAnalysisSelection([], many, many.map(model.analysisSelectionKey)), /32/);
});

test('group payload contains exact item references and revision, never discovery metadata or credentials', () => {
  assert.equal(typeof model.analysisGroupPayload, 'function');
  const selected = [item('1', '11'), item('2', '22', { value_kind: 'text', value_type: 4 })];
  const group = model.analysisGroupPayload({
    name: '  支付系统  ', source_id: 'source', description: '运行范围', enabled: true,
    interval_seconds: 60, config_revision: 7, api_token: 'must-not-be-copied',
  }, selected, [{ from_hostid: '2', to_hostid: '1', kind: 'depends_on', label: '调用' }]);
  assert.deepEqual(group.items, [{ hostid: '1', itemid: '11' }, { hostid: '2', itemid: '22' }]);
  assert.equal(group.name, '支付系统');
  assert.equal(group.config_revision, 7);
  assert.equal(JSON.stringify(group).includes('must-not-be-copied'), false);
  assert.equal(JSON.stringify(group).includes('custom.metric'), false);
  assert.throws(() => model.analysisGroupPayload({ name: '空', source_id: 's' }, [], []), /监控项/);
  assert.throws(() => model.analysisGroupPayload({ name: '错', source_id: 's' }, selected, [{ from_hostid: 'outside', to_hostid: '1', kind: 'related_to' }]), /已选/);
});

test('item status keeps missing, unsupported, disabled and stale distinct from sampled zero', () => {
  assert.equal(typeof model.analysisItemStatus, 'function');
  assert.deepEqual(model.analysisItemStatus(item('1', '1', { lastclock: '0' }), 1100), { label: '尚无采样', color: 'warning' });
  assert.equal(model.analysisItemStatus(item('1', '1', { state: '1', error: 'fail' }), 1100).label, '采集不支持');
  assert.equal(model.analysisItemStatus(item('1', '1', { status: '1' }), 1100).label, '上游已停用');
  assert.equal(model.analysisItemStatus(item('1', '1'), 1100).label, '已采样');
  assert.equal(model.analysisItemStatus(item('1', '1'), 10000).label, '采样较早');
  assert.equal(model.analysisItemStatus(item('1', '1', { value_kind: 'unsupported' }), 1100).label, '不参与数值分析');
});

test('fresh group observations are not labelled as awaiting first collection', () => {
  assert.equal(typeof model.analysisGroupReadyLabel, 'function');
  assert.equal(model.analysisGroupReadyLabel('2026-11-09T01:00:00Z'), '已获得有效观察');
  assert.equal(model.analysisGroupReadyLabel(null), '等待有效观察');
});

test('typed history coverage exposes incomplete and sparse records without implying zero or collection failure', () => {
  assert.equal(typeof model.analysisHistoryCoverage, 'function');
  assert.deepEqual(model.analysisHistoryCoverage({ complete: true, truncated: false, reason_codes: [] }), { label: '覆盖完整', reasons: [] });
  const partial = model.analysisHistoryCoverage({ complete: false, truncated: true, reason_codes: ['history_truncated', 'no_samples'] });
  assert.equal(partial.label, '覆盖不完整');
  assert.deepEqual(partial.reasons, ['达到历史条数上限', '当前窗口没有历史样本']);
  const sparse = model.analysisHistoryCoverage({ complete: false, reason_codes: ['sparse_history_no_value_change_or_missing'] });
  assert.match(sparse.reasons[0], /无新历史不等于采集失败/);
  assert.equal(model.analysisHistoryCoverage({}).label, '覆盖未知');
});

test('large frozen windows explicitly disclose retained original samples instead of full-history claims', () => {
  assert.equal(typeof model.metricSamplingSummary, 'function');
  assert.equal(model.metricSamplingSummary({}), null);
  assert.equal(model.metricSamplingSummary({ sampling: { original_point_count: 100, retained_point_count: 200 } }), null);
  assert.match(model.metricSamplingSummary({ sampling: { method: 'original_point_bucket_minmax', original_point_count: 1000, retained_point_count: 200 } }), /保留 200 \/ 1000 个原始点/);
  assert.match(model.metricSamplingSummary({ sampling: { method: 'original_point_bucket_minmax', original_point_count: 1000, retained_point_count: 200 } }), /桶极值抽样/);
});
