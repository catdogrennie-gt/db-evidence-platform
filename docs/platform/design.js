const pages=[['overview','运行总览'],['assets','资产中心'],['onboard','接入数据库'],['instance','实例工作台'],['events','告警与事件'],['ai','AI 证据调查'],['history','历史检索'],['knowledge','运维知识'],['tasks','采集与任务'],['admin','平台管理']];
let selected='overview',wizardStep=0,detailTab='指标趋势';
const $=s=>document.querySelector(s);
const btn=(text,page)=>`<button class="primary" data-page="${page}">${text}</button>`;
const header=(title,subtitle,actions='')=>`<div class="screen-header"><div><h3>${title}</h3><p>${subtitle}</p></div><div class="screen-actions">${actions}</div></div>`;
const panel=(title,body)=>`<div class="mock-panel"><h4>${title}</h4>${body}</div>`;
const stats=(names)=>`<div class="mock-stats">${names.map(n=>`<div class="stat"><small>${n}</small><strong>—</strong><em>接入并采集后显示真实值</em></div>`).join('')}</div>`;
const table=(heads,rows)=>`<div class="table-wrap"><table><thead><tr>${heads.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
const tag=(text,type='')=>`<span class="status ${type}">${text}</span>`;
const assetRow=(name,system)=>[`<a data-page="instance">${name}</a><span class="muted">设计示例 · 未连接</span>`,system,'MySQL / 版本待发现',tag('等待接入','gray'),'—'];
const assetTable=()=>table(['实例 / 标识','业务系统','引擎 / 版本','采集状态','最近成功'],[assetRow('mysql-orders-01','订单系统'),assetRow('mysql-inventory-01','库存系统')]);
const filters=()=>`<div class="toolbar"><span class="chip">部门：授权范围</span><span class="chip">系统：全部可见系统</span><span class="chip">环境：全部</span><span class="chip">状态：全部</span><span class="chip">时间窗：绝对起止可追溯</span></div>`;
const emptyChart=(text)=>`<div class="chart-placeholder">${text} · 接入后显示真实历史，缺口保留</div>`;
const notes={overview:'页面契约：所有统计、趋势、拓扑和事件均先由后端确定授权实例范围；健康状态同时考虑采集能力和新鲜度。空状态引导接入，失败显示影响与重试入口。',assets:'页面契约：实例数与 schema 数分开；UUID 去重；分页、搜索和导出均范围化。激活后的实例不允许通过普通编辑改变归属，避免历史证据跨部门流转。',onboard:'页面契约：测试是有状态任务，显示分项真实结果；保存绑定测试结果与配置版本。凭据只写不读，AI外发默认关闭，普通接入不会创建数据或写入业务库。',instance:'页面契约：每个采集项独立显示支持性、权限、最近成功和错误；无数据、过期、未配置、无权限分别表达。SQL/日志字段按角色脱敏。',events:'页面契约：规则事实、事件处理状态与AI结论分别记录。事件恢复不是简单点“关闭”；处置后需要同口径复验，失败可重新开启。站内通知，不发送邮件。',ai:'页面契约：发起人选择授权实例与时间窗；证据包预览并受实例外发策略约束。每次工具调用再次鉴权。报告保留证据引用、反证、未知和调用审计。',history:'页面契约：按资产、绝对时间和来源分页检索；保留 observed_at 与 ingested_at。导出再次鉴权与脱敏，不能绕过页面字段限制。',knowledge:'页面契约：知识条目从经过人工审核的事件结论生成，保留条件、来源、版本与权限。模型回答不会自动变成已验证知识。',tasks:'页面契约：采集与AI任务分进程、有租约、超时和失败原因。一个实例离线不堵塞其他实例；重启恢复计划，采集指标帮助发现平台自身故障。',admin:'页面契约：角色决定可执行动作，授权范围决定可见数据。管理员身份不隐含读取所有业务证据；权限移除、用户禁用、会话退出后立即生效。'};
function overview(){return header('运行总览','当前授权范围的实例、风险与采集覆盖',btn('＋ 接入数据库','onboard'))+filters()+stats(['已接入实例','业务系统','逻辑库 schemas','待处理事件'])+`<div class="mock-grid">${panel('性能与容量趋势',emptyChart('QPS / 活跃连接 / 容量'))}${panel('需要关注',`<div class="list-row"><strong>首次使用：接入数据库</strong><small>连接预检后选择采集配置，等待首轮真实数据。</small></div><div class="list-row"><strong>采集质量与能力</strong><small>缺失、权限不足、断连和过期单独显示。</small></div><div class="list-row"><a data-page="events">查看事件工作台 →</a></div>`)}</div>`+panel('业务系统 → 数据库实例',assetTable());}
function assets(){return header('资产中心','管理责任归属、连接能力与实例生命周期',btn('＋ 接入数据库','onboard'))+filters()+panel('数据库实例',assetTable())+`<div class="mock-grid">${panel('资产关系',`<div class="evidence-chain"><span>部门</span><span>→ 业务系统</span><span>→ MySQL 实例</span><span>→ schemas / 表 / 索引</span></div>`)}${panel('生命周期',`<p>待测试 → 待首采 → 运行 / 降级 / 断连 → 停采 → 退役</p><p>历史保留依既定策略；停采不删除历史。</p>`)}</div>`;}
const wizardNames=['归属与资产','连接方式','连接预检','采集与数据策略','首采预览','确认启用'];
const field=(label,value,help='',type='text')=>`<div class="field"><label>${label}</label><input type="${type}" value="${value}" aria-label="${label}" autocomplete="off"><small>${help}</small></div>`;
function onboard(){let body='';
 if(wizardStep===0)body=`<div class="form-grid">${field('资产名称','mysql-orders-01','示例名称，可按团队命名规范修改')}${field('数据库类型','MySQL','首批支持 MySQL；PostgreSQL 后续适配')}${field('所属部门 / 系统','示例部门 / 订单系统','系统决定实例授权范围')}${field('环境 / 负责人','测试 / 待指定','一个实例归属一个系统')}</div>`;
 if(wizardStep===1)body=`<div class="form-grid">${field('连接方式','直连 TLS / 已核验 SSH 隧道','通用接入不要求 root 或 SSH')}${field('主机 / 端口','数据库地址 / 3306','管理员允许的网络范围')}${field('监控用户名','只读监控账号','按采集能力提供最小权限指引')}${field('密码','','只写不读；不进入日志或模型','password')}${field('TLS / SSH 主机核验','校验证书 / 已知主机指纹','未知主机指纹不会自动信任')}</div>`;
 if(wizardStep===2)body=`<div class="banner">预检结果的界面示例；本设计稿不会发起网络连接。</div>`+table(['检查项','实际系统应展示的结果','失败后的行动'],[['网络 / 认证','成功或具体错误','检查地址、网络与凭据'],['版本 / UUID','真实发现值 / 重复资产提示','重复 UUID 回到已存在资产'],['指标 / 锁 / SQL / 元数据','逐项：支持 / 无权限 / 未配置','显示缺少的权限和可用能力'],['日志 / 主机','表 / 文件通道能力','允许有限能力接入，明确覆盖缺口']]);
 if(wizardStep===3)body=`<div class="form-grid">${field('采集周期 / 超时','默认 30 秒 / 单项有界超时','不同采集器可使用不同周期')}${field('元数据 / 日志周期','低频结构快照 / 增量日志','避免每轮全量扫描')}${field('时序 / 证据保留','按部署策略选择','到期清理任务本身可审计')}${field('AI 数据外发','关闭（默认）','仅明确允许的脱敏运维证据可发外部模型')}</div><div class="wizard-message">schema 选择控制采集内容，不能代替跨部门安全隔离。原始 SQL 和日志默认不外发。</div>`;
 if(wizardStep===4)body=`<div class="banner">尚未执行首采：此步只预览确认启用后将执行的计划，不展示首采成功或样本。</div>`+table(['计划类别','将取得的资料','限制'],[['身份 / 状态','UUID / 版本 / 计数','执行后保留真实时间和来源'],['SQL / 锁 / 结构','受限摘要、快照和元数据','权限和条数上限'],['日志 / 主机','明确预检且已选择的来源','未配置不等于没有问题'],['外部AI','不调用模型','默认外发关闭']]);
 if(wizardStep===5)body=`<div class="no-result"><h3>确认接入并启用持续采集</h3><p>正式系统将校验测试有效期、配置版本与 UUID，保存资产后进入持久调度。这里仅预览页面跳转。</p></div><div class="wizard-message">启用后进入实例工作台，显示待首采进度；部分采集失败会保留实际能力状态。</div>`;
 return header('接入数据库','六步完成归属、连接、能力检查与采集启用')+`<div class="steps">${wizardNames.map((s,i)=>`<span class="${i===wizardStep?'active':''}">${i+1}. ${s}</span>`).join('')}</div>`+panel(wizardNames[wizardStep],body)+`<div class="wizard-footer"><button class="secondary" data-wizard="prev" ${wizardStep===0?'disabled':''}>上一步</button><span class="chip">设计步骤 ${wizardStep+1} / 6</span><button class="primary" data-wizard="next">${wizardStep===5?'预览：进入实例工作台':'下一步'}</button></div>`;
}
const detailTabs=['指标趋势','SQL 摘要','锁与事务','库表与索引','配置与复制','日志与主机'];
function instance(){let body='';
 if(detailTab==='指标趋势')body=stats(['QPS（真实采样差分）','活跃连接','缓冲池 / IO','最近采集时间'])+panel('指标趋势与时间窗',emptyChart('当前窗口 / 故障窗口 / 前后对比'));
 if(detailTab==='SQL 摘要')body=panel('规范化 SQL · 按窗口内增量排序',table(['摘要 / 默认库','调用次数增量','总耗时 / 次均','扫描 / 返回行','证据'],[['SELECT … WHERE id = ?','—','—','—','来源 + 时间 + 覆盖率']]))+panel('计划和工具',`<p>选择已采集摘要及明确参数模板进行受限只读检查，不执行模型生成的任意 SQL。</p>`);
 if(detailTab==='锁与事务')body=panel('锁等待关系',`<div class="evidence-chain"><span>等待事务</span><span>→ 持锁事务（按字段权限展示）</span><span>→ 对象 / 锁模式 / 采样时间</span></div><div class="empty">没有观察到等待与没有采集权限，是不同状态。</div>`);
 if(detailTab==='库表与索引')body=panel('结构与容量',table(['schema / 表','行数估计 / 大小','索引','采集时间'],[['接入后发现','—','—','来源快照可追溯']]))+panel('变化对比',`<p>对比两个真实快照，标注新增/删除和配置漂移；估计值明确标识。</p>`);
 if(detailTab==='配置与复制')body=panel('配置和复制能力',table(['项目','应显示的状态','依据'],[['关键参数','值 / 变化 / 权限不足','配置快照及差异'],['复制拓扑','已配置 / 未配置 / 不支持','真实复制查询结果'],['复制延迟','值 / 未知','不把 NULL 或无复制当健康']]));
 if(detailTab==='日志与主机')body=panel('日志与主机来源',table(['来源','接入路径','能力状态'],[['MySQL 错误日志','performance_schema.error_log 或受限文件通道','支持 / 无权限 / 未配置'],['慢日志','已配置的 TABLE 或增量文件','不会擅自开启全局慢日志'],['Linux CPU / 内存 / IO','明确配置的只读通道','未配置不得显示主机正常']]))+panel('日志检索',`<div class="search-box"><input placeholder="按时间窗 / 级别 / 脱敏关键词检索" aria-label="日志检索示例"><button class="secondary" data-page="history">查看历史设计</button></div>`);
 return header('mysql-orders-01','实例工作台 · 设计示例，尚未连接',btn('发起 AI 调查','ai'))+`<div class="toolbar"><span class="chip">UUID：接入后真实发现</span><span class="chip">订单系统</span><span class="chip">状态：待首采</span><span class="chip">能力覆盖：待检测</span></div><div class="tabs">${detailTabs.map(t=>`<button data-detail="${t}" class="${t===detailTab?'active':''}">${t}</button>`).join('')}</div>`+body;
}
function events(){return header('告警与事件','规则事实、处理过程和恢复证据分别保存',btn('从事件发起 AI 调查','ai'))+filters()+`<div class="banner">以下展示事件工作流程，不代表已有真实告警。</div><div class="mock-grid">${panel('事件工作队列',table(['事件 / 影响','规则状态','处理状态'],[['锁等待关联事件（示例）','待真实触发','待确认 / 待分派'],['采集过期关联事件（示例）','待真实触发','独立于数据库健康']]))}${panel('处置时间线',`<div class="timeline"><div class="list-row">检测到异常<small>规则版本 + 资产 + 首条证据</small></div><div class="list-row">确认、分派与调查<small>负责人 + 相关告警 + AI 报告</small></div><div class="list-row">人工处置记录<small>操作人 + 方案 + 风险 + 执行结果</small></div><div class="list-row">自动采集复验<small>同口径比较，失败重新开启</small></div><div class="list-row">审核后留存知识<small>保留来源、适用条件和版本</small></div></div>`)}</div>`;}
function ai(){return header('AI 证据调查','多来源关联与按需补证，结论必须能回到来源')+`<div class="banner">新接入实例的外部 AI 默认关闭。正式系统按资产策略与用户权限判断是否允许；模型不可扩大资产范围。</div><div class="form-grid">${field('调查资产','选择一个或多个授权实例')}${field('绝对时间窗','开始时间 → 结束时间')}</div><div class="search-box" style="margin-top:18px"><input placeholder="例如：订单延迟是否与锁、SQL 或资源变化有关？" aria-label="调查问题设计"><button class="primary" data-design-message="正式系统先预览脱敏证据、检查策略，再提交独立 AI 任务。">预览证据包</button></div>`+panel('调查过程',`<div class="evidence-chain"><span>指标变化</span><span>SQL 摘要</span><span>锁关系</span><span>日志 / 配置</span><span>→ 有界只读补证</span></div><div class="empty">任务状态：排队 → 取证 → 工具调用 → 生成报告 → 待人工复核；失败和预算耗尽明确显示。</div>`)+`<div class="mock-grid">${panel('报告结构',`<div class="list-row">观察到的现象与影响<small>时间窗与输入资产</small></div><div class="list-row">候选原因及证据引用<small>支持 / 反证 / 未知分别列出</small></div><div class="list-row">建议与复验方法<small>不等于已执行或已确认根因</small></div>`)}${panel('调用审计',`<p>每次调用记录发起人、资产、工具、脱敏参数、来源、时间与结果。证据ID可点回相同时间窗和原始记录。</p>`)}</div>`;}
function history(){return header('历史检索','在同一资产与时间窗下查询指标、SQL、锁、日志和配置')+filters()+`<div class="search-box"><input placeholder="实例 / 来源 / 时间范围 / 脱敏关键词" aria-label="历史检索设计"><button class="primary" data-design-message="正式系统执行范围化查询，分页返回真实历史证据。">查询</button></div>`+panel('多源证据时间线',`<div class="tabs"><button class="active">全部来源</button><button>指标</button><button>SQL</button><button>锁</button><button>日志</button><button>配置</button></div>`+table(['采样时间 / 入库时间','来源','实例 / 身份','覆盖与状态','操作'],[['接入后采集','真实来源名称','实例 ID + UUID','新鲜度 / 截断 / 权限','查看原始脱敏证据']]))+panel('数据语义',`<p>不同来源有各自时间语义。实例重启、计数器归零、窗口缺口和无日志记录都必须保留，不能插值成一条健康曲线。</p>`);}
function knowledge(){return header('运维知识','事件复盘、适用条件与审核版本组成可检索知识')+`<div class="search-box"><input placeholder="问题现象 / MySQL版本 / 适用环境 / 标签" aria-label="知识检索设计"><button class="primary" data-design-message="知识条目需人工审核，访问时仍校验来源资产和敏感级别。">检索</button></div>`+panel('排障手册与复盘',table(['条目','来源','审核 / 版本','适用与限制'],[['锁等待排查手册（示例）','待关联真实事件','草稿 / 待审核','版本、前提与反例'],['SQL 性能调查流程（示例）','待关联真实证据','草稿 / 待审核','不承诺通用自动调优']]))+panel('从事件到知识',`<div class="evidence-chain"><span>已复验事件</span><span>→ 人工审核结论</span><span>→ 来源与适用条件</span><span>→ 版本化手册</span></div>`);}
function tasks(){return header('采集与任务','持续采集、AI调查和维护任务各自有状态与失败边界')+stats(['已启用实例','采集覆盖率','过期采集器','排队 / 失败任务'])+panel('采集器运行状态',table(['实例 / 采集器','最近成功','下一次执行','状态 / 错误'],[['接入后显示真实实例','—','—','待接入'],['独立实例故障隔离','真实运行时间','退避后重试','错误原因与影响可查']]))+panel('任务记录',table(['任务','类型','状态','重启与超时边界'],[['任务 ID','采集 / 接入测试 / 调查 / 复验','排队 / 运行 / 完成 / 失败','持久租约，过期回收及幂等处理']]))+`<div class="banner">任务失败显示失败。模型超时保留已有证据和调用记录，不能阻塞持续监控。</div>`;}
function admin(){return header('平台管理','身份、资源授权、采集与数据策略由管理员维护')+`<div class="tabs"><button class="active">用户与授权</button><button>部门 / 系统</button><button>凭据引用</button><button>模型策略</button><button>审计 / 保留</button></div>`+panel('角色与数据范围',table(['角色','允许的工作','数据范围'],[['平台管理员','组织、账号、接入、策略','不隐含全部业务证据读取权'],['DBA','详细证据、事件、调查、复验','被授予的系统 / 实例'],['开发','状态、规范化 SQL、相关事件','所属授权系统'],['查看者','范围内态势与风险摘要','部门 / 系统授权']]))+panel('默认治理',`<span class="mini-tag">凭据只写不读</span><span class="mini-tag">AI外发默认关闭</span><span class="mini-tag">会话可撤销</span><span class="mini-tag">直接HTTP也鉴权</span><span class="mini-tag">导出同样脱敏</span><p style="margin-top:12px">组织示例仅用于验证权限流程，后续按真实内部组织关系配置。</p>`);}
const screens={overview,assets,onboard,instance,events,ai,history,knowledge,tasks,admin};
function render(){ $('#page-nav').innerHTML=pages.map(([id,name])=>`<button data-page="${id}" class="${selected===id?'active':''}">${name}</button>`).join('');$('#screen').innerHTML=screens[selected]();$('#screen-notes').textContent=notes[selected]; }
document.addEventListener('click',e=>{const section=e.target.closest('[data-section]');if(section){document.querySelectorAll('[data-section]').forEach(n=>n.classList.toggle('active',n===section));document.querySelectorAll('.design-section').forEach(n=>n.classList.toggle('active',n.id===section.dataset.section));window.history.replaceState(null,'','#'+section.dataset.section);window.scrollTo({top:0,behavior:'instant'});return;}
 const page=e.target.closest('[data-page]');if(page){e.preventDefault();selected=page.dataset.page;render();return;}
 const step=e.target.closest('[data-wizard]');if(step){if(step.dataset.wizard==='prev')wizardStep=Math.max(0,wizardStep-1);else if(wizardStep<5)wizardStep++;else selected='instance';render();return;}
 const detail=e.target.closest('[data-detail]');if(detail){detailTab=detail.dataset.detail;render();return;}
 const message=e.target.closest('[data-design-message]');if(message){let box=$('#design-message');if(!box){box=document.createElement('div');box.id='design-message';box.className='wizard-message';$('#screen').append(box);}box.textContent=message.dataset.designMessage;}
});
render();const initial=location.hash.slice(1);if(['map','architecture','pages','acceptance','references'].includes(initial))document.querySelector(`[data-section="${initial}"]`).click();

// Offline design interactions only. No fetch, authentication, metrics or business writes.
const demo={role:'dba',asset:'orders',state:'normal',probe:'success',fixed:false,accepted:false,firstCollection:false,plan:false,execution:false,verification:null,closed:false,knowledge:false,published:false,merged:false,comment:false,aiPreview:false};
const roleNames={dba:'DBA',developer:'开发',viewer:'查看者'};
const stateNames={normal:'正常',degraded:'降级',offline:'失联',paused:'停采'};
const frozenWindow='2026-11-08 09:00 → 10:00 +08:00';
const originalScreens={...screens};
const originalRender=render;
const actionButton=(label,action,disabled=false)=>`<button class="secondary" data-action="${action}" ${disabled?'disabled':''}>${label}</button>`;
const demoBanner=()=>`<div class="banner">静态交互示例 · 未认证、未连接、未执行。角色与状态是前端演练，不证明真实后端权限或产品功能。</div>`;
const choose=(label,key,values,current)=>`<label class="demo-control">${label}<select data-demo="${key}" aria-label="${label}">${Object.entries(values).map(([value,text])=>`<option value="${value}" ${current===value?'selected':''}>${text}</option>`).join('')}</select></label>`;
const demoAssetTable=()=>table(['实例（静态示例）','归属','状态场景','来源 / 最近成功'],[['<button class="text-link" data-action="open-orders">mysql-orders-01</button>','订单系统',tag(stateNames[demo.state]+' · 示例',demo.state==='normal'?'':'warn'),'未连接 / —'],...(demo.role==='dba'?[['<button class="text-link" data-action="open-inventory">mysql-inventory-01</button>','库存系统',tag('停采 · 示例','gray'),'未连接 / —']]:[])]);
const roleRestriction=()=>`<div class="banner">${demo.role==='developer'?'开发示例默认只有事件读取与评论，没有认领/分派/关闭/归并或凭据管理。':'查看者示例只读，无处置、模型发起或配置权限。'}真实系统仍须逐请求鉴权。</div>`;
let demoMessage='';
pages.splice(4,0,['topology','资源拓扑']);
notes.topology='设计契约：关系有来源和确认时间；这里只绘制登记归属的静态层级，不虚构复制、主机、网络或存储路径。';
notes.events+=' P0事件一个instance、多alert episodes；跨实例归并拒绝。归并后旧复验失效，所有纳入告警均恢复且复验覆盖全部问题才可按恢复结案。';
notes.ai+=' 历史调查冻结绝对区间，只查原窗内已有资料；to之后补采不得冒充历史证据，缺口列明。';

screens.overview=()=>header('运行总览','先确认授权范围、资料新鲜度与待处理事件',demo.role==='dba'?btn('＋ 接入数据库','onboard'):btn('查看事件','events'))+demoBanner()+`<div class="toolbar"><span class="chip">系统：${demo.role==='dba'?'订单 / 库存':'订单'} · 角色示例</span><span class="chip">${frozenWindow}</span></div>`+stats(['授权实例','已启用采集','资料过期','待处理事件'])+panel('当前角色的静态资产视图',demoAssetTable())+`<div class="mock-grid">${panel('运行趋势',emptyChart('未连接，不绘制虚构曲线'))}${panel('任务入口',btn('演练事件处置与复验','events')+' '+actionButton('查看实例状态场景','open-instance'))}</div>`;
screens.assets=()=>header('资产中心','单实例单系统；归属与实际授权分开',demo.role==='dba'?btn('＋ 接入数据库','onboard'):'')+demoBanner()+panel('前端模拟范围，不能当作后端隔离',demoAssetTable())+panel('身份与生命周期','<p>同UUID新端点：更新现有实例的staged连接配置，保留旧配置历史，不建立端点池或重复计数。UUID改变：停采、核查、归档旧ID并新接入，不能覆盖旧UUID。</p>');

screens.onboard=()=>{
 if(demo.role!=='dba')return header('接入数据库','无接入动作的静态场景')+demoBanner()+roleRestriction()+btn('返回资产目录','assets');
 if(demo.firstCollection)return header('首采结果','确认启用之后的静态结果页')+demoBanner()+`<div class="banner">未真实执行首采。下面演练“核心资料成功、可选源缺口”的结果；不可作为已接入证明。</div>`+table(['项目','示例结果','边界'],[['生命周期',tag('Active · 示例'),'资料质量另行呈现'],['核心状态','成功样式 · 无真实样本数','实际实现必须返回时间与证据ID'],['锁 / 日志','partial · 示例','缺口不能补零或标健康']])+btn('打开实例工作台','instance')+' '+actionButton('重新演练向导','reset-wizard');
 let body='';
 if(wizardStep===0)body=`<div class="form-grid">${field('资产名称','mysql-orders-01','静态示例')}${field('引擎 / 归属','MySQL / 示例部门 / 订单系统','一个实例归属一个系统，归属不自动授予权限')}${field('环境 / 负责人','隔离验收 / 示例DBA','不使用真实个人资料')}</div>`;
 if(wizardStep===1)body=`<div class="form-grid">${field('连接方式','直连TLS / 已核验SSH隧道','不会发起网络请求')}${field('主机 / 端口','数据库地址占位 / 3306','正式系统校验允许目标')}${field('监控用户名','只读账号占位','最小权限指引')}${field('密码','','静态原型不保存、不发送；勿输入真实秘密','password')}${field('主机 / 日志分支','仅数据库 / 单独受限来源','DB可连不代表主机或日志可读')}</div>`;
 if(wizardStep===2){
  const passed=demo.probe==='success'||demo.fixed;
  const result=passed?'通过 · 静态示例':demo.probe==='authentication_failed'?'认证失败 · 示例':demo.probe==='partial'?'核心可用，可选权限缺失 · 示例':'identity_mismatch · 示例';
  body=choose('预检场景（静态）','probe',{success:'通过',authentication_failed:'认证失败',partial:'部分能力可用',identity_mismatch:'UUID身份不符'},demo.probe)+`<div class="result-line">${tag(result,passed?'':'warn')}</div>`+table(['阶段','检查与来源','限制'],[['网络 / TLS / 认证','逐阶段真实结果的样式','不回显秘密，不静默关TLS'],['版本 / UUID','真实发现与预期核对','同UUID去重，身份改变禁止拼历史'],['指标 / SQL / 锁 / 元数据','每源能力和最小权限指导','权限不足不能当无风险'],['主机 / 日志','独立绑定、来源和可见范围','不会自动GRANT或打开日志']]);
  if(!demo.fixed&&demo.probe==='authentication_failed')body+='<p>检查监控凭据后重新测试；下面按钮仅切换修正后的示例。</p>'+actionButton('演示：修正凭据后重试','fix-auth');
  if(!demo.fixed&&demo.probe==='partial')body+='<p>锁/日志源权限缺失：核心可以接入，但资料将标partial。</p>'+actionButton(demo.accepted?'已接受明确能力缺口':'明确接受降级示例','accept-partial',demo.accepted);
  if(!demo.fixed&&demo.probe==='identity_mismatch')body+='<p>停止连接并核查。确为新实例则由有权管理员归档旧ID并重新接入，不重绑旧UUID。</p>'+actionButton('演示：核查后按新实例接入','new-identity');
 }
 if(wizardStep===3)body=`<div class="form-grid">${field('采集计划','每源周期 / 超时 / 条数上限','有界采集，不无界扫描')}${field('资料与保留','不采业务行 / 按源保留','事件与报告证据保护')}${field('外部AI','关闭 / rules_only','默认不外发；原始SQL和日志不直接给模型')}${field('schema白名单','仅限制采集内容','不是共享实例跨部门安全隔离')}</div>`;
 if(wizardStep===4)body='<div class="banner">尚未执行首采：这里只预览确认启用后将执行的内容，不展示首采成功或已有样本。</div>'+table(['计划类别','将取得的资料','预期限制'],[['身份 / 基础状态','UUID、版本、uptime、允许计数','执行后记录真实来源和观察时间'],['SQL / 锁 / 结构','受限摘要、快照、元数据','允许范围、条数与能力缺口'],['日志 / 主机','已预检且已选择的来源','未配置不能等于零错误或资源正常'],['模型','不调用外部模型','外发默认关闭']]);
 if(wizardStep===5)body='<div class="no-result"><h3>确认清单与启用</h3><p>正式系统再次验证身份、范围、测试有效期与配置版本，登记后以activate任务执行真实首采。此处只演练完成页。</p></div>';
 return header('接入数据库','六步向导；预检内含能力，确认启用后才首采')+demoBanner()+`<div class="steps">${wizardNames.map((name,index)=>`<span class="${index===wizardStep?'active':''}">${index+1}. ${name}</span>`).join('')}</div>`+panel(wizardNames[wizardStep],body)+`<div class="wizard-footer"><button class="secondary" data-wizard="prev" ${wizardStep===0?'disabled':''}>上一步</button><span class="chip">静态步骤 ${wizardStep+1} / 6</span><button class="primary" data-wizard="next">${wizardStep===5?'演示：确认启用后的结果':'下一步'}</button></div>`;
};
screens.instance=()=>{
 const explanation={normal:'核心资料可用且新鲜的样式；未检测真实实例。',degraded:'基础指标可用，锁/日志存在能力缺口；缺失不能当作正常。',offline:'连接失败样式：保留旧历史并标记时间；最新值未知，不补零。',paused:'人工停采样式：停止未来采集，历史保留；不代表数据库健康。'}[demo.state];
 return demoBanner()+`<div class="demo-toolbar">${choose('实例状态场景','instance-state',stateNames,demo.state)}${tag(stateNames[demo.state]+' · 静态示例',demo.state==='normal'?'':'warn')}</div><div class="banner">${explanation}</div>`+originalScreens.instance().replace('mysql-orders-01',demo.asset==='inventory'?'mysql-inventory-01':'mysql-orders-01').replace('订单系统',demo.asset==='inventory'?'库存系统':'订单系统').replace('状态：待首采','状态场景：'+stateNames[demo.state]).replace('能力覆盖：待检测','能力覆盖：各源独立；未真实检测')+panel('敏感资料与采集','<p>原始SQL、会话和日志字段独立授权；默认只给归一化摘要。缺少主机源时标“仅数据库视角”。</p>'+actionButton('查看原文受限状态','raw-denied')+' '+btn('查看采集与任务','tasks'));
};
screens.topology=()=>header('资源拓扑','登记层级、观察关系与未知关系分别标记')+demoBanner()+`<div class="topology-map"><div class="topology-node">示例部门<small>来源：人工登记（静态）</small></div><div class="topology-edge">↓ 归属关系；不是运行因果</div><div class="topology-level"><div class="topology-node">订单系统<small>确认时间：静态占位</small></div>${demo.role==='dba'?'<div class="topology-node">库存系统<small>仅DBA场景显示</small></div>':''}</div><div class="topology-edge">↓ 单系统实例；范围另行授权</div><div class="topology-level"><button class="topology-node" data-page="instance">mysql-orders-01<small>静态节点 → 实例详情</small></button>${demo.role==='dba'?'<button class="topology-node" data-page="instance">mysql-inventory-01<small>静态占位</small></button>':''}</div></div>`+panel('来源与受限边界',table(['关系','来源 / 状态','允许解释'],[['部门 → 系统 → 实例','人工登记 / 静态样例','归属不自动授予data.read'],['复制 / 主机关系','未提供真实关系证据','不绘制虚构主从与主机边'],['范围外资料','受限样式说明','不暴露名称、数量或UUID；前端模拟不构成安全控制']]))+'<div class="banner">不绘制未知应用、网络和存储链路。真实系统关系必须带来源、确认时间与权限。</div>';
screens.events=()=>{
 const status=demo.closed?'closed':demo.verification==='improved'?'resolved':demo.execution?'pending_verification':demo.plan?'investigating':'open';
 const verifyNames={inconclusive:'资料不足',not_improved:'未达到所选改善条件',improved:'所选条件改善'};
 const alerts=panel('独立原始告警 episodes → 同实例事件',table(['告警示例','实例','关联'],[['DEMO-A-LOCK / 锁等待','orders','DEMO-EVT-1，保留原规则事实'],['DEMO-A-TX / 长事务','orders',demo.merged?'已关联同实例事件（示例）':'可归并的同实例告警']]))+(demo.role==='dba'?`<div class="flow-actions">${actionButton('演练：同实例归并','merge',demo.merged)}${actionButton('演练：跨实例归并被拒','cross-merge')}</div>`:'');
 const operations=demo.role==='dba'?`<div class="flow-actions">${actionButton('1 保存人工处置计划','plan',demo.plan)}${actionButton('2 保存人工执行记录','execution',!demo.plan||demo.execution)}</div>`:roleRestriction()+(demo.role==='developer'?actionButton(demo.comment?'静态评论已添加':'添加应用发布评论示例','comment',demo.comment):'');
 const verification=panel('3 确定性复验的静态演练',`<p>所有纳入告警必须有新鲜恢复证据，复验覆盖全部问题。仅一条锁恢复而其他告警仍firing/unknown不能整事件恢复；归并使旧复验失效。</p><p>锁链消失仅证明所观测链消失，仍须声明负载；SQL需同digest及可比窗口。结果不证明业务恢复，更不证明AI修复。</p><div class="result-line">${tag((verifyNames[demo.verification]||'尚未复验')+' · 示例',demo.verification==='improved'?'':'warn')}</div>${demo.role==='dba'?`<div class="flow-actions">${actionButton('资料不足 / inconclusive','verify-inconclusive',!demo.execution)}${actionButton('未改善 / not_improved','verify-not-improved',!demo.execution)}${actionButton('所选条件改善 / improved','verify-improved',!demo.execution)}${actionButton('4 人工关闭','close',demo.verification!=='improved'||demo.closed)}${actionButton('5 生成待审核知识','knowledge-draft',!demo.closed||demo.knowledge)}</div>`:''}`);
 return header('告警与事件','人工记录、原始告警与条件复验分开')+demoBanner()+panel('DEMO-EVT-1 · orders 锁等待示例',`<div class="toolbar">${tag(status+' · 静态状态')}<span class="chip">${frozenWindow}</span></div><p>一个事件一个instance_id，多个告警episode保留各自状态；跨实例关联通过调查处理。</p>`)+alerts+operations+`<div class="mock-grid">${panel('人工处置计划',demo.plan?'<p>静态计划：核对阻塞链、影响、回退和成功条件，由人在既有受控渠道处理。未执行KILL、DDL或回滚。</p>':'<p>尚无计划。点击演练添加，不操作数据库。</p>')}${panel('人工执行记录',demo.execution?'<p>静态记录：示例DBA声明已处理；不代表真实操作发生，仍需复验。</p>':'<p>尚未记录执行，模型建议不能自动变成已执行。</p>')}</div>`+verification+(demo.knowledge?btn('打开知识审核流程','knowledge'):'');
};
screens.knowledge=()=>header('运维知识','来源、适用条件与审核版本')+demoBanner()+panel('事件知识草稿',demo.knowledge?`<p>DEMO-K-1 · 锁等待案例（静态）；来源DEMO-EVT-1 / orders，版本v1，包含风险、适用条件、负载局限和反例。</p>${tag(demo.published?'审核发布 · 示例':'待审核 · 示例',demo.published?'':'warn')} ${demo.role==='dba'?actionButton('演练：人工审核发布','publish',demo.published):roleRestriction()}`:'<p>还没有本轮知识草稿。先演练事件计划、执行记录、条件复验、人工关闭。</p>')+btn('回到事件流程','events')+panel('资料范围','<p>原知识需要全部来源实例和资料类型权限；扩大共享必须另建脱敏摘要并人工审核。模型回答不自动进入可靠知识。</p>');
screens.ai=()=>header('AI 证据调查','冻结历史窗口；默认禁止外发')+demoBanner()+`<div class="banner">没有模型调用。外发默认rules_only；历史窗口：${frozenWindow}。to之后补采不纳入历史结论，缺少当时快照就列未知。</div>`+(demo.role==='viewer'?roleRestriction():actionButton('预览冻结证据包与审计样式','ai-preview'))+(demo.aiPreview?panel('冻结资料清单与缺口',table(['资料','时间','局限'],[['DEMO-E-SQL / 指标','仅原窗内既有资料','静态占位，无真实测量'],['锁快照','故障当时缺失（示例）','未知，不能以现在快照冒充'],['模型 / 工具调用','未实际运行','白名单、权限、预算与外发需真实后端验收']]))+panel('证据图与报告样式','<div class="evidence-chain"><span>问题</span><span>→ 候选解释（未确认）</span><span>→ DEMO-E-SQL引用占位</span><span>锁快照缺失 / 未知</span></div><p>事实、支持、反证、未知分开。任意SQL和跨实例请求由broker拒绝；本样式没有实际工具调用。</p>'):'');
screens.admin=()=>demoBanner()+originalScreens.admin()+panel('控制面与数据权限另授','<p>审计人员是第五类角色：只查指定范围审计和已授权报告，不修改事件。管理员可委派管理范围/可授角色allowlist与本人data.read分开，DBA不可自授；本原型不执行任何授权修改。</p>');

render=function(){
 originalRender();
 const selector=$('#role-select');if(selector)selector.value=demo.role;
 const caption=$('#role-caption');if(caption)caption.textContent=roleNames[demo.role]+' · 静态角色示例';
 const scope=$('#prototype-scope');if(scope)scope.textContent=(demo.role==='dba'?'订单 / 库存':'订单')+' · 示例';
 const time=$('#prototype-time');if(time)time.textContent=frozenWindow;
 if(selected==='topology')document.querySelectorAll('#screen .topology-node[data-page="instance"]').forEach(node=>{node.dataset.action=node.textContent.includes('mysql-inventory-01')?'open-inventory':'open-orders';});
 if(demo.role!=='dba')document.querySelectorAll('#page-nav [data-page="onboard"]').forEach(node=>node.hidden=true);
 if(demo.role!=='dba'&&selected==='instance')document.querySelectorAll('#screen [data-page="ai"]').forEach(node=>node.hidden=true);
 if(demoMessage){const box=document.createElement('div');box.className='wizard-message';box.setAttribute('role','status');box.textContent=demoMessage;$('#screen').append(box);}
};
document.addEventListener('click',event=>{
 const wizard=event.target.closest('[data-wizard]');
 if(wizard){event.stopImmediatePropagation();demoMessage='';if(demo.role!=='dba')return;
  if(wizard.dataset.wizard==='prev')wizardStep=Math.max(0,wizardStep-1);
  else if(wizardStep===2&&!demo.fixed&&['authentication_failed','identity_mismatch'].includes(demo.probe))demoMessage='先使用修正路径或返回修改连接，不能跳过失败预检。';
  else if(wizardStep===2&&demo.probe==='partial'&&!demo.accepted&&!demo.fixed)demoMessage='请明确接受可选能力缺口后继续。';
  else if(wizardStep<5)wizardStep++;else{demo.firstCollection=true;demo.state='degraded';}render();return;
 }
 const control=event.target.closest('[data-action]');if(!control||control.disabled)return;event.stopImmediatePropagation();
 const name=control.dataset.action;demoMessage='';
 const protectedActions=['fix-auth','accept-partial','new-identity','reset-wizard','plan','execution','verify-inconclusive','verify-not-improved','verify-improved','close','knowledge-draft','publish','merge','cross-merge'];
 if(protectedActions.includes(name)&&demo.role!=='dba'){demoMessage='此角色示例无该动作；真实系统需要后端拒绝。';render();return;}
 if(name==='fix-auth'){demo.fixed=true;demoMessage='切到修正后通过的静态示例；未读取或发送凭据。';}
 else if(name==='accept-partial'){demo.accepted=true;demoMessage='接受明确降级；资料缺口仍保留，不能显示完整成功。';}
 else if(name==='new-identity'){demo.fixed=true;demoMessage='演练核查与旧ID归档后的新接入路径；未修改任何真实资产或UUID。';}
 else if(name==='reset-wizard'){wizardStep=0;demo.firstCollection=false;demo.probe='success';demo.fixed=false;demo.accepted=false;}
 else if(['open-instance','open-orders','open-inventory'].includes(name)){demo.asset=name==='open-inventory'&&demo.role==='dba'?'inventory':'orders';selected='instance';}
 else if(name==='plan'){demo.plan=true;demoMessage='静态人工计划已记录；未执行数据库操作。';}
 else if(name==='execution'&&demo.plan){demo.execution=true;demoMessage='仅演练人工记录；不证明数据库实际执行或恢复。';}
 else if(name.startsWith('verify-')&&demo.execution){demo.verification={'verify-inconclusive':'inconclusive','verify-not-improved':'not_improved','verify-improved':'improved'}[name];demo.closed=false;demoMessage=demo.verification==='improved'?'静态场景假设全部告警均有新鲜恢复证据且复验覆盖全部问题：仅所选条件改善，不等于业务恢复。':'资料不足或未改善，不能按恢复结案。';}
 else if(name==='close'&&demo.verification==='improved'){demo.closed=true;demoMessage='演练人工结案；告警事实、证据与流程保留，没有真实状态写入。';}
 else if(name==='knowledge-draft'&&demo.closed){demo.knowledge=true;selected='knowledge';demoMessage='生成待审核静态草稿，未自动发布。';}
 else if(name==='publish'&&demo.knowledge){demo.published=true;demoMessage='演练人工审核发布；仍非经过真实验证的案例。';}
 else if(name==='merge'){demo.merged=true;demo.verification=null;demo.closed=false;demoMessage='同实例归并示例：旧复验失效，须覆盖新告警重新复验；原告警与旧事件保留。';}
 else if(name==='cross-merge')demoMessage='拒绝场景：P0事件只能含一个instance_id，跨实例问题使用关联调查。';
 else if(name==='comment'&&demo.role==='developer'){demo.comment=true;demoMessage='静态应用评论已添加，不改变事件分派、处置或关闭。';}
 else if(name==='raw-denied')demoMessage='默认不提供原始SQL、完整会话或原日志；真实系统按敏感资料权限返回403，不只是前端遮罩。';
 else if(name==='ai-preview'&&demo.role!=='viewer'){demo.aiPreview=true;demoMessage='展开静态证据清单、图和审计样式；未调用模型或工具。';}render();
},true);
document.addEventListener('change',event=>{
 if(event.target.id==='role-select'&&roleNames[event.target.value]){demo.role=event.target.value;demoMessage='仅切换静态角色视图，这不是登录、授权或安全验证。';if(demo.role!=='dba'){demo.asset='orders';if(selected==='onboard')selected='assets';}demo.aiPreview=false;render();}
 if(event.target.dataset.demo==='instance-state'&&stateNames[event.target.value]){demo.state=event.target.value;demoMessage='实例状态由用户选择的示例切换，未检查真实数据库。';render();}
 if(event.target.dataset.demo==='probe'&&['success','authentication_failed','partial','identity_mismatch'].includes(event.target.value)){demo.probe=event.target.value;demo.fixed=false;demo.accepted=false;demoMessage='';render();}
});
render();
