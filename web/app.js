let state = {}, busy = false, selectedReportId = null, currentReport = null, analysisSnapshot = null;
const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = t => new Date(t).toLocaleString('zh-CN', {hour12:false});
const duration = n => n === 0 ? '低于当次计时分辨率' : `${esc(n)}s`;
const empty = s => '<div class="empty">' + esc(s) + '</div>';
const source = (s, name) => s?.data.evidence.find(e => e.source === name)?.value || [];
const statuses = s => Object.fromEntries(source(s, 'mysql.status').map(x => [x.Variable_name, x.Value]));

function switchTab(id) {
    document.querySelectorAll('.tab').forEach(x => x.classList.toggle('visible', x.id === id));
    document.querySelectorAll('.nav').forEach(x => x.classList.toggle('active', x.dataset.tab === id));
    window.scrollTo(0, 0);
    $('title').textContent = {overview:'数据库运行总览', diagnosis:'证据与 AI 诊断', recovery:'备份恢复验证', experiments:'故障实验与记录'}[id];
}
document.querySelectorAll('.nav').forEach(x => x.onclick = () => switchTab(x.dataset.tab));

function toast(text, persist = false) {
    $('toast').textContent = text;
    $('toast').style.display = 'block';
    if (!persist) setTimeout(() => $('toast').style.display = 'none', 6500);
}
async function act(url, body) {
    if (busy) return;
    busy = true;
    document.querySelectorAll('button').forEach(b => b.disabled = true);
    toast('任务执行中，正在保存真实证据…', true);
    const phases = {checking_scope:'核对实验范围',injecting_fault:'注入合成故障',collecting_fault:'保存故障证据',diagnosing_live_fault:'故障保持中，AI 正在只读补证',cleaning_up:'清理实验变更',verifying_after:'采集并验证清理后状态',finished:'已完成并保存记录'};
    const progressTimer = setInterval(async () => {
        if (!url.includes('/incident/')) return;
        try {
            const r = await fetch('/api/task'); const task = await r.json();
            if (task.busy && task.progress?.phase) toast(phases[task.progress.phase] || task.progress.phase, true);
        } catch (_) {}
    }, 2000);
    try {
        const response = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:body ? JSON.stringify(body) : undefined});
        const data = await response.json();
        if (!response.ok) throw Error(data.detail || response.status);
        if (url.includes('analy') || url.includes('investigate')) selectedReportId = null;
        toast(['incomplete','failed'].includes(data.data?.status) ? '任务未通过完整验收；请查看诊断与清理记录' : '任务完成，结果已保存');
        await refresh();
        return data;
    } catch (e) { toast('任务未完成：' + e.message); }
    finally {
        clearInterval(progressTimer);
        busy = false;
        document.querySelectorAll('button').forEach(b => b.disabled = !!state.busy);
    }
}
let pendingTask = null;
function requestExperiment(text, task) {
    pendingTask = task;
    $('confirmationText').textContent = text;
    $('confirmation').hidden = false;
    $('confirmExperiment').focus();
}
function cancelExperiment() { pendingTask = null; $('confirmation').hidden = true; }
function confirmExperiment() {
    const task = pendingTask;
    cancelExperiment();
    if (task) task();
}
function drill(s) { requestExperiment(s === 'index' ? '将在专用源库 evidence_lab.orders 删除并重建 lab_customer_idx，同时保存前后证据。' : '将在专用实验库建立事务阻塞、采集后回滚阻塞事务。', () => act('/api/drill/' + s)); }
function incident(s) { requestExperiment('将在已登记实验源库注入' + (s === 'index' ? '缺失索引故障' : '事务阻塞') + '，故障保持期间调用 DeepSeek 只读补证（消耗 API 额度），结束后由固定程序清理并验证。仅使用合成数据。', () => act('/api/incident/' + s)); }
function recover(c) { requestExperiment('将重建专用恢复实例 :3308 的 evidence_lab，源库不重建。' + (c ? '本次会主动损坏备份，预期恢复失败；之后需再执行完整恢复。' : '执行后比较结构、行数与数据指纹。'), () => act('/api/recovery/' + (c ? 'corrupt' : 'normal'))); }
async function explainRecovery(id) { if (await act('/api/recovery/' + id + '/analyze')) switchTab('diagnosis'); }
async function investigate() {
    const s = state.snapshots?.[0];
    if (!s) return toast('尚无采集证据');
    if (await act('/api/investigate', {snapshot_id:s.id, question:$('question').value || '检查当前实验库风险，按需补充证据，区分事实和未知。'})) switchTab('diagnosis');
}
async function analyze(snapshotId) {
    const id = snapshotId || state.snapshots?.[0]?.id;
    if (!id) return toast('尚无采集证据');
    if (await act('/api/analyze', {snapshot_id:id, question:$('question').value || '解读此快照，区分当时观察事实、假设与未知；不要将历史状态当成当前状态。'})) switchTab('diagnosis');
}
function reportList() {
    return [...(state.investigations || []), ...(state.analyses || []).filter(a => !a.data.investigation_id)]
        .sort((a,b) => b.timestamp.localeCompare(a.timestamp));
}
async function selectReport(id) {
    selectedReportId = id || null;
    await refresh();
}
async function refresh() {
    try {
        const response = await fetch('/api/state');
        if (!response.ok) throw Error(response.status);
        state = await response.json();
        const reports = reportList();
        currentReport = selectedReportId ? reports.find(x => x.id === selectedReportId) : reports[0];
        if (selectedReportId && !currentReport) {
            const r = await fetch('/api/record/' + encodeURIComponent(selectedReportId));
            if (r.ok) currentReport = await r.json();
        }
        const snapshotId = currentReport?.data.evidence_snapshot_id || currentReport?.data.snapshot_id;
        if (!snapshotId) analysisSnapshot = null;
        else if (analysisSnapshot?.id !== snapshotId) {
            analysisSnapshot = null;
            const r = await fetch('/api/record/' + encodeURIComponent(snapshotId));
            if (r.ok) analysisSnapshot = await r.json();
        }
        render();
    } catch (e) {
        $('status').textContent = '服务不可用';
        $('status').className = 'tag bad';
        toast('无法读取工作台状态，请检查本地服务');
    }
}

function table(headers, rows) {
    return rows.length ? '<table><thead><tr>' + headers.map(h => '<th>' + esc(h) + '</th>').join('') + '</tr></thead><tbody>' + rows.map(r => '<tr>' + r.map(c => '<td>' + c + '</td>').join('') + '</tr>').join('') + '</tbody></table>' : empty('暂无记录');
}
function renderDiagnosis() {
    const reports = reportList();
    if (currentReport && !reports.some(x => x.id === currentReport.id)) reports.push(currentReport);
    $('reportHistory').innerHTML = '<option value="">跟随最新诊断</option>' + reports.map(r => `<option value="${esc(r.id)}">${fmt(r.timestamp)} · ${r.kind === 'investigation' ? '主动排查' : '快照解读'} · ${esc((r.data.question || '').slice(0,34))}</option>`).join('');
    $('reportHistory').value = selectedReportId || '';
    const report = currentReport, data = report?.data;
    $('lockGraph').innerHTML = analysisSnapshot ? analysisSnapshot.data.evidence.filter(e => ['mysql.lock_waits','mysql.tool.get_lock_waits'].includes(e.source) && Array.isArray(e.value) && e.value.length).map(e => `<div class="notice"><b>本次报告的锁关系 · ${esc(e.id)}</b><p class="detail">${fmt(e.collected_at)} · 历史证据，不代表此刻仍阻塞</p>${e.value.slice(0,8).map(w => `<p>阻塞事务 <b>${esc(w.blocking_trx)}</b> → 等待事务 <b>${esc(w.waiting_trx)}</b> · ${esc(w.db_name)}.${esc(w.table_name)}</p>`).join('')}</div>`).join('') : '';
    $('toolTrace').innerHTML = report?.kind === 'investigation' ? `<div class="notice"><b>本次主动排查 · ${data.status === 'complete' ? '报告结构与引用已校验' : '未完成，无有效最终报告'}</b><p>${fmt(report.timestamp)} · ${data.elapsed_seconds}s · ${data.tool_calls.length} 次工具调用</p>${data.tool_calls.map(t => `<div>${esc(t.data.name)} · ${esc(t.data.status)} · ${esc(t.data.evidence_id || t.data.error_type || '')} · ${t.data.elapsed_seconds}s</div>`).join('')}<details><summary>查看工具参数、审计与证据记录 ID</summary><pre>${esc(JSON.stringify(report,null,2))}</pre></details></div>` : '';
    const answer = data?.answer;
    $('answer').innerHTML = report ? `<p class="detail">${fmt(report.timestamp)} · ${esc(data.model)} · ${data.elapsed_seconds}s · 历史报告 ${esc(report.id.slice(0,8))}</p><p><b>问题：</b>${esc(data.question)}</p>` + (answer ? `<div class="answer">${esc(answer.summary)}</div>` + (answer.findings || []).map(f => `<div class="finding"><h3>${esc(f.title)}</h3><div class="evidence-links">证据：${esc((f.evidence_ids || []).join(' / '))}</div><p>${esc(f.reasoning)}</p><p class="detail">下一步：${esc(f.next_readonly_check)}</p></div>`).join('') + `<div class="notice">尚待确认：${esc((answer.unknowns || []).join('；'))}</div>` : `<div class="notice">未生成通过结构与引用校验的报告。${esc(data.error_type || '')}；${esc(data.stop_reason || '')}</div>`) + `<details><summary>查看此次诊断所用的原始证据</summary><pre>${esc(JSON.stringify(analysisSnapshot || '原始证据暂不可用',null,2))}</pre></details>` : empty('采集后可主动排查，或选择一条历史诊断。');
}
function render() {
    const snaps = state.snapshots || [], s = snaps[0], st = statuses(s), locks = source(s,'mysql.lock_waits');
    const stale = s && Date.now() - new Date(s.timestamp).getTime() > 90000;
    $('status').textContent = stale ? '采集已过期' : s?.data.status === 'complete' ? '实时采集正常' : s?.data.status === 'partial' ? '部分采集失败' : '暂无有效数据';
    $('status').className = 'tag ' + (s?.data.status === 'complete' && !stale ? '' : 'warn');
    $('clock').textContent = s ? fmt(s.timestamp) : '';
    const hasLocks = s?.data.evidence.some(e => e.source === 'mysql.lock_waits');
    const values = [['当前连接',st.Threads_connected ?? '—','Threads_connected'],['运行线程',st.Threads_running ?? '—','Threads_running'],['锁等待',hasLocks ? locks.length : '—','当前等待关系'],['证据来源',s?.data.evidence.length ?? 0,'MySQL + Linux']];
    $('metrics').innerHTML = values.map(v => `<div class="card metric"><label>${esc(v[0])}</label><div class="value">${esc(v[1])}</div><small>${esc(v[2])}</small></div>`).join('');
    const history = [...snaps].reverse().map(x => Number(statuses(x).Threads_connected)).filter(Number.isFinite);
    if (history.length) {
        const max = Math.max(4,...history), points = history.map((v,i) => `${10+i*480/Math.max(1,history.length-1)},${90-v/max*76}`).join(' ');
        $('trend').innerHTML = `<svg class="spark" viewBox="0 0 500 100" preserveAspectRatio="none"><path d="M0 90H500 M0 50H500 M0 10H500" stroke="#e7ecef" fill="none"/><polyline points="${points}" stroke="#267d75" stroke-width="2.5" fill="none"/></svg>`;
    } else $('trend').innerHTML = empty('等待首个实时快照');
    $('sampleInfo').textContent = `已保留 ${snaps.length} 个近期快照；采集错误 ${s?.data.errors.length ?? 0} 项。`;
    const rw = state.rate_window;
    $('sampleInfo').textContent += rw?.status === 'comparable' && rw.rates.Questions ? ` 最近 ${rw.seconds}s 平均 ${rw.rates.Questions.per_second} Questions/s（含监控查询）。` : '速率不可比较：' + (rw?.reason || '等待两个样本');
    $('digests').innerHTML = table(['SQL 摘要','执行次数','扫描行数'],source(s,'mysql.sql_digest').slice(0,5).map(x => [`<code class="digest">${esc(x.digest)}</code>`,esc(x.executions),esc(x.rows_examined)]));
    const digestEvidence = s?.data.evidence.find(e => e.source === 'mysql.sql_digest');
    if (digestEvidence?.scope_note) $('digests').innerHTML += `<p class="detail">累计耗时前 ${esc(digestEvidence.candidate_count)} 条候选中，已单列 ${esc(digestEvidence.excluded_known_templates.length)} 条已知采集 SQL 模板；模板匹配不能按账号归因。新采集连接不选择业务默认库。</p><details><summary>查看单列的采集模板与统计范围</summary><pre>${esc(JSON.stringify({scope_note:digestEvidence.scope_note,excluded_known_templates:digestEvidence.excluded_known_templates},null,2))}</pre></details>`;
    $('evidence').innerHTML = s ? `<p class="detail">最新采集 ${fmt(s.timestamp)}；右侧历史报告使用自己的原始快照。</p>` + s.data.evidence.map(e => `<details><summary>${esc(e.id)} · ${esc(e.source)} <span class="detail">${fmt(e.collected_at)}</span></summary><pre>${esc(JSON.stringify(e.value,null,2))}</pre></details>`).join('') + (s.data.errors.length ? '<pre>' + esc(JSON.stringify(s.data.errors,null,2)) + '</pre>' : '') : empty('等待首个实时快照');
    renderDiagnosis();
    const recent = [...(state.drills || []),...(state.recoveries || []),...(state.investigations || [])].sort((a,b) => b.timestamp.localeCompare(a.timestamp)).slice(0,4);
    $('recent').innerHTML = recent.length ? recent.map(r => `<div class="event"><time>${fmt(r.timestamp)}</time><b>${esc(r.kind === 'investigation' ? '主动证据诊断' : r.kind === 'recovery' ? '隔离恢复校验' : r.data.scenario === 'lock_wait' ? '锁等待演练' : '缺失索引演练')}</b><span class="detail">${esc(r.data.status || r.data.mode || '已保存实验记录')}</span></div>`).join('') : empty('实验尚未执行');
    $('recoveryList').innerHTML = (state.recoveries || []).map(r => `<div class="card" style="margin-top:18px"><div class="card-head"><h2>${r.data.corrupt_test ? '损坏备份识别' : '完整备份恢复验证'}</h2><span class="tag ${r.data.status === 'passed' ? '' : 'bad'}">${r.data.status === 'passed' ? '校验通过' : '验证失败'}</span></div><div class="detail">${fmt(r.timestamp)} · ${r.data.elapsed_seconds}s · ${r.data.backup_bytes ?? '—'} bytes</div><p>${r.data.status === 'passed' ? '源与恢复目标的表结构、行数、数据摘要一致。' : esc(r.data.error)}</p>${r.data.corrupt_test ? '<div class="notice">负向测试预期：损坏备份应被判定失败。</div>' : ''}<button onclick="explainRecovery('${r.id}')">AI 解读此报告</button><details><summary>查看原始校验报告与 SHA256</summary><pre>${esc(JSON.stringify(r,null,2))}</pre></details></div>`).join('') || empty('尚无恢复验证记录');
    $('drillList').innerHTML = (state.drills || []).map(r => {
        const index = r.data.scenario === 'missing_index', before = r.data.before_explain?.[0], after = r.data.after_explain?.[0];
        return `<div class="card" style="margin-top:18px"><div class="card-head"><h2>${index ? '缺失索引 → 全表扫描 → 索引验证' : '事务阻塞 → 等待关系 → 释放验证'}</h2><span class="tag ${!index && r.data.waiter !== 'completed' ? 'warn' : ''}">真实实验记录</span></div><p class="detail">${fmt(r.timestamp)} · 故障快照 ${esc(r.data.snapshot_id.slice(0,8))}</p>` + (index ? `<div class="table-wrap">${table(['阶段','访问类型','索引','估计扫描行'],[['缺失索引',esc(before?.type),esc(before?.key || '无'),esc(before?.rows)],['添加索引后',esc(after?.type),esc(after?.key || '无'),esc(after?.rows)]])}</div><p>15 次查询客户端耗时：${duration(r.data.before_seconds)} → ${duration(r.data.after_seconds)}</p><p class="detail">包含 SSH / 网络往返，不能等同服务端 SQL 耗时。</p>` : `<p>已回滚实验阻塞事务；等待会话结果：${esc(r.data.waiter)}</p>`) + `<button onclick="analyze('${r.data.snapshot_id}')">解读故障时快照</button><details><summary>查看原始实验报告及前后快照 ID</summary><pre>${esc(JSON.stringify(r,null,2))}</pre></details></div>`;
    }).join('') || empty('尚无故障实验记录');
    $('incidentList').innerHTML = (state.incidents || []).map(r => {
        const d = r.data, index = d.scenario === 'index';
        const test = (state.incident_tests || []).find(t => t.data.incident_id === r.id);
        const expectedFailure = test?.data.test_type === 'injected_model_timeout' && test.data.result === 'passed';
        const names = {checking_scope:'范围核对',injecting_fault:'注入故障',collecting_fault:'采集故障',diagnosing_live_fault:'AI 实时补证',cleaning_up:'程序清理',verifying_after:'清理后验证',finished:'保存结果'};
        return `<div class="card" style="margin-top:18px"><div class="card-head"><h2>${index ? '索引诊断完整案例' : '锁等待诊断完整案例'}</h2><span class="tag ${d.status === 'passed' || expectedFailure ? '' : 'bad'}">${expectedFailure ? '负向测试符合预期' : d.status === 'passed' ? '验收通过' : '未通过完整验收'}</span></div><p class="detail">${fmt(r.timestamp)} · ${esc(d.elapsed_seconds)}s · ${esc(d.run_id.slice(0,8))}</p>` + (expectedFailure ? '<div class="notice">受控模型超时测试：诊断按预期失败，程序清理通过。超时来自本地测试替身，不代表真实服务商故障；原始案例仍保留 failed 状态。</div>' : '') + `<p>${d.events.map(e => esc(names[e.data.phase] || e.data.phase)).join(' → ')}</p><p>AI 报告：${esc(d.diagnosis_status || d.error_type || '未完成')} ${esc(d.diagnosis_error_type || '')} · 故障工具证据：${d.diagnostic_evidence_confirmed ? '已确认' : '未确认'} · 清理校验：${esc(d.cleanup.status)}</p>` + (index ? `<p>访问类型：${esc(d.before_explain?.[0]?.type || '—')} → ${esc(d.after_explain?.[0]?.type || '—')}；清理后索引：${esc(d.after_explain?.[0]?.key || '—')}</p>` : `<p>账户余额保持：${d.cleanup.balances_unchanged === true ? '一致' : '未确认'}；清理后等待：${Array.isArray(d.cleanup.remaining_waits) ? d.cleanup.remaining_waits.length : '未确认'}</p>`) + (d.investigation_id ? `<button onclick="selectReport('${d.investigation_id}');switchTab('diagnosis')">查看本次 AI 补证与报告</button>` : '') + `<details><summary>查看阶段时间、清理结果与关联证据 ID</summary><pre>${esc(JSON.stringify(r,null,2))}</pre></details></div>`;
    }).join('') || empty('先运行一个完整案例，观察 AI 在故障仍存在时补证。');
    document.querySelectorAll('button').forEach(b => b.disabled = busy || !!state.busy);
}
refresh();
setInterval(() => { if (!busy) refresh(); },10000);
