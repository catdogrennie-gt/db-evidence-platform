# 内部 MySQL AI 运维平台实施与验收计划

> **For agentic workers:** 按任务执行并保留交接记录。参考 Superpowers 的任务分解、独立评审和验证规范；当前会话的用户授权与协作规则优先。

**Goal:** 建成能接入新增 MySQL、持续收集多源证据、按组织范围查询、处理事件并完成 AI 调查和复验的内部运维平台。

**Architecture:** 模块化 FastAPI API、独立采集进程与独立 AI 进程。MySQL 存控制和结构化证据，Prometheus 存数值时序；同源 React 页面经 API 统一鉴权。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy/Alembic、PyMySQL、Paramiko、Prometheus、React/TypeScript/Vite、ECharts；具体可运行版本在安装时锁定。

**Spec:** [需求](01-requirements.md)、[架构与契约](02-architecture.md)、[页面设计](03-pages-and-workflows.md)。本计划不降低这些规格的验收标准。

## 当前交付进度（2026-11-08）

实际逐项状态以[验收记录与证据索引](06-acceptance-status.md)为准。本文后续 Task 复选框保留设计时的原始执行清单，未逐项改勾；不能把未勾理解为没有实现，也不能把已实现模块理解为全部验收通过。设计基线和静态原型均不作为真实产品成果。

**已有实现及明确范围内的实际证明：** 同源React/FastAPI、独立控制MySQL和Prometheus、服务端会话与范围授权、六步新增真实资产、多源后台采集、固定指标与历史、事件/人工计划/复验、知识版本、持久任务/导出和管理。最新真实权限矩阵781通过且完整；三个真实目标、五角色四百请求有热缓存P95；分来源相位和新鲜度有短窗测量。真实DeepSeek两轮成功且引用可读，预算失败另保留。真实锁与固定摘要SQL案例各有无效动作负控、后续条件改善与知识发布；知识由自动化验收账号审核并显著标待用户人工审阅。真实浏览器错误口令指引、禁止继续及纠正重试也已通过，未登记新资产。具体数值、样本、来源与限制见06的E01、E13–E32。

**最新补查：** 管理重载后五角色172个GET、253项检查全部通过；SQL历史三个真实窗口的次数/耗时/扫描量一致，浏览器基线、事件概况和异步导出跳转保留时间范围均已核对。完整平台隔离回归414通过、9条件跳过、6个子检查通过；最新工作台来源分页和 1265px 桌面无整页横向溢出已实测；具体边界见06的E33–E35。

**继续验收：** 其它网络/TLS失败指引、正确/错误凭据轮换、缺主密钥恢复、小屏和完整键盘路径；完整历史、冷缓存、十实例/持续并发与关闭采集的开销对照。已有cleanroom证明正常新配置/数据目录启停，复用了现有venv/dist，完整fresh install尚未验。模拟提供商下的真实进程中断/撤权测试不冒充真实DeepSeek或HTTPS故障。

**明确边界：** 业务对象/审计90天物理清理未完整实现，平台状态保留warning；连接压力专用规则和若干异常组合未完整实现或验收。P1受控执行、企业集成、巡检预测及PostgreSQL没有完成。平台不执行任意SQL或目标变更，不发邮件；案例执行器的显式受控操作与平台通用能力分开记录。不能从三个实例、短历史或两个300s间隔推断原规模达标。

## Global Constraints

- 不发送任何邮件，不自动向他人发布项目资料。
- P0 每个实例仅归属一个业务系统，系统仅归属一个部门。
- schema 选择仅控制采集范围，不构成安全边界。
- 新接真实资产默认禁止外发；合成验收数据可按明确策略使用已提供模型服务。
- 模型不能把推测写回为采集值。
- AI 不直接执行 DDL、KILL、恢复或任意 SQL。
- 列表、计数、候选项、时序、证据、事件、报告、导出、知识和 AI 工具逐次检查。
- 未执行最新验证的能力，不标记为已完成；设计、回放、规则、模型、人工记录分别标识。
- 不删除现有 VM 或实验数据来解决环境问题；旧实验资产及写入口与通用接入隔离。

## Review Focus

1. 已撤权用户继续读取跨实例报告、已排队任务或导出：每次访问和工具调用重新计算权限；任务5/6/8验证。
2. 接入测试后修改连接、数据库 UUID 改变或重复端点：测试绑定配置版本，身份变化隔离历史；任务2/3验证。
3. 没有新采集时规则也不运行，旧值继续显示健康：独立时钟检查新鲜度，TSDB缓存门控；任务3/4/5验证。
4. 长 SQL 日志迟到、轮转或慢日志表缺失：保留覆盖边界及游标，明确缺口；任务3/8验证。
5. 模型或单个数据库阻塞所有任务：采集与AI独立进程，有限超时和租约；任务3/6/8验证。

## 1. 执行次序和时间管理

以今天剩余时间推进，按依赖交付，不按“运行了一小时”作为停止条件。下面是工作量估算，用于调整并行和排查预算，不能替代完成证据。

| 阶段 | 目标 | 估算与结束条件 |
| --- | --- | --- |
| D 设计 | 需求、设计图、架构、页面、接口与验收一致 | 设计已评审并作为实施基线；实际进度见06，不以原型代替产品 |
| F 基础 | 控制库/迁移、身份授权、资产接入 | 约 1.5–2.5 小时；从向导接入真实实例，直接 API 越权受控 |
| O 可观测性 | 连续多源采集、时序、历史与总览 | 约 1.5–2.5 小时；多实例并行和一个离线不相互阻塞 |
| I 运维流程 | 规则、事件、AI、人工处置、复验和知识 | 约 2–3 小时；真实故障从发现到知识有可核查链路 |
| V 验证迭代 | 浏览器、故障、权限、持久性、打包与说明 | 约 1.5–2.5 小时；逐项报告通过、失败、未验，不隐藏缺口 |

界面可在接口冻结后与后端独立模块并行实现。根代理负责公共模型/接口，其他实施者独占文件；不让多个代理同时修改迁移、依赖或根路由。

预判和替代方案：

| 可能阻塞 | 预先判断与替代 |
| --- | --- |
| 浏览器输入不可靠 | 本地开发、CLI HTTP 和数据库检查继续；页面验收使用已验证可操作的浏览器；不把重新注册账号当开发前置 |
| Windows 控制 MySQL 包下载/依赖问题 | 限时核实错误；使用独立控制实例及单独目录的 Linux 部署路径，记录单节点风险；不能退回共享实验表或伪称 MySQL |
| Prometheus 版本或包不可运行 | 官方包、校验、版本/就绪检查；换经验证兼容包，不把自建 JSON 记录改名为 TSDB |
| SSH / 日志能力不足 | 先独立探测数据库能力；通用接入可明确降级，验收环境另外接通真实主机与日志路径 |
| 模型不可用 | 持续采集、事件、查询继续；调查任务失败和原始调用留存；不拿离线文本冒充实时 AI |
| 无关账号/插件故障 | 不阻塞本地源码、Git、设计、实现与验证；不发送邮件、不自动公开发布 |

同一故障首次复现后先查官方说明；若两轮修复仍无进展，先切换已设计的替代路径，并保留原错误和影响，避免停在一个工具上。

## 2. 工程文件职责

下列为设计时的模块职责清单，现有实现以仓库为准，完成程度按06验收记录判断。既有实验文件保持独立，不将固定实验操作泛化到新资产。

| 路径 | 职责 |
| --- | --- |
| `platform_app/config.py`、`db.py`、`models.py`、`schemas.py` | 私密配置引用、数据库会话、业务模型、稳定请求/响应封套 |
| `platform_app/identity.py`、`access.py`、`secrets.py` | 本地身份、服务端会话、CSRF、动作/范围授权、加密凭据 |
| `platform_app/inventory.py`、`connectors.py` | 资产生命周期、预检绑定、直连/TLS与SSH连接、UUID核验 |
| `platform_app/collectors/`、`scheduling.py`、`worker.py` | 采集适配器、能力检测、持久租约和采集进程 |
| `platform_app/evidence.py`、`metrics.py`、`metrics_bridge.py` | 证据封套、分页检索、实际窗口差分、时序模板、过期门控 |
| `platform_app/events.py`、`remediation.py`、`knowledge.py` | 版本化规则、事件处理、人工处置记录、复验和审核知识 |
| `platform_app/investigations.py`、`tools.py`、`ai_worker.py` | 授权证据包、模型调用、有界只读工具和独立AI进程 |
| `platform_app/api.py`、`routes/` | 同源 API、统一异常/审计、业务路由和静态产物 |
| `migrations/` | Alembic 有序数据库迁移；不在请求中隐式改表 |
| `frontend/src/` | 共享类型/API客户端、布局、路由、工作页面及图表 |
| `infra/prometheus/`、`scripts/platform/` | 时序配置、官方组件安装记录、初始化/启动/停止/健康检查 |
| `tests/platform/`、`reports/platform/` | 重要逻辑/权限/恢复测试、真实集成报告与浏览器证据 |

公共 Python 类型和路由以架构第4、12节为准；任务中的函数只是各模块内部实现契约，不能产生第二套 HTTP API。

## 3. 工程任务

以下复选框保留原计划状态，不作为当前完成度统计。每次验收记录实际命令、提交、结果和未覆盖项，当前结果集中见06；验证不通过则保留失败、修复再验，不提前声称通过。

### Task 1: 控制库、身份和统一授权

**Files:** 创建 `platform_app/{config,db,models,schemas,identity,access,secrets}.py`、`migrations/`；测试 `tests/platform/test_access.py`、`test_secrets.py`。

**Interfaces:** `authenticate(session) -> ActorContext`；`authorize(actor, action, instance_ids) -> AuthorizedScope`；`put_secret(value) -> SecretRef`；统一类型遵守架构契约。

- [ ] 先写测试：未登录401；功能拒绝403；范围对象统一404；管理员没有显式数据grant不能读业务；会话禁用/撤销立即失效；密文无明文、主密钥丢失明确失败。
- [ ] 执行 `.venv\Scripts\python.exe -m pytest tests/platform/test_access.py tests/platform/test_secrets.py -q`，保存尚未实现时的失败证据。
- [ ] 实现 MySQL 迁移、随机会话散列、Argon2id、CSRF、RBAC+实例范围、Fernet与仓库外独立主密钥及审计；本地初始化口令写私密文件，不入仓库。管理员的可委派管理范围/角色allowlist不等于本人业务读取权。
- [ ] 同命令通过，并在真实 MySQL 跑迁移/重启读取；覆盖 AT-004、021、022 的基础路径。
- [ ] 独立评审和本地提交，记录模型/迁移版本及真实部署位置。

### Task 2: 通用资产和接入向导后端

**Files:** 创建 `platform_app/{inventory,connectors}.py`、`routes/inventory.py`；测试 `tests/platform/test_onboarding.py`。

**Interfaces:** `get_instance(scope, id) -> InstanceProfile`；`open_mysql(profile, secret_handle, deadline) -> MySQLSession`；HTTP `POST /api/v1/connections/test`、`POST /instances`、`POST /instances/{id}/activate`、`POST /instances/{id}/revalidate`。

- [ ] 先写测试：不同DNS/TCP/认证/TLS错误；重复UUID；篡改测试配置；过期test_id；系统归属越权；GET不回密钥。
- [ ] 执行 `.venv\Scripts\python.exe -m pytest tests/platform/test_onboarding.py -q`，确认失败来自未实现行为。
- [ ] 实现按配置版本和测试有效期绑定的接入；能力分项结果；新实例身份、停采、退役和凭据轮换；没有业务写操作。
- [ ] 同命令通过；真实接入 A/B，再创建未预置 C，通过 HTTP/页面接入且不改代码；执行 AT-001、002、003、023。
- [ ] 独立评审和本地提交。确认旧实验写路由不能收到 C 的凭据。

### Task 3: 持续采集、真实日志和任务恢复

**Files:** 创建 `platform_app/collectors/{mysql,host,logs}.py`、`scheduling.py`、`worker.py`；测试 `test_collection.py`、`test_leases.py`、`test_log_cursor.py`。

**Interfaces:** `collect(context, profile_name) -> CollectionBatch`；`claim(worker_id, queue) -> LeasedTask`；`complete(task_id, fence, result)`；`append(batch, fence) -> EvidenceRefs`。queue区分采集和AI。

- [ ] 先写测试：失效租约不能写；实例停采后过期任务不能回填；一个目标超时不阻塞其他目标；没有权限、未配置、不支持和空结果区分；日志重复/轮转/迟到有覆盖声明。
- [ ] 执行 `.venv\Scripts\python.exe -m pytest tests/platform/test_collection.py tests/platform/test_leases.py tests/platform/test_log_cursor.py -q`，记录初始失败。
- [ ] 实现有界固定 SQL、指标/锁/事务/digest/配置/元数据/复制/错误日志和慢日志；可选已验证主机通道；持久任务、fence、取消、退避、按实例并发限制。
- [ ] 同命令通过；A/B/C 持续采集30分钟，并执行真实离线/权限不足/日志/轮转/worker重启；AT-006、007、009、017。
- [ ] 独立评审和提交；报告每类来源、采样时间、游标覆盖、缺口及连接开销。

### Task 4: 数值时序、历史查询和范围化统计

**Files:** 创建 `platform_app/{evidence,metrics,metrics_bridge}.py`、`routes/observability.py`、`infra/prometheus/prometheus.yml`；测试 `test_metrics.py`、`test_history_scope.py`。

**Interfaces:** `derive(previous, current) -> DerivedMetrics`；`query_template(scope, query) -> MetricSeries`；`query(scope, filters) -> Page`。禁止任意 PromQL 透传。

- [ ] 先写测试：首帧、UUID/uptime变化、计数器归零、采样间隔缺口不给假rate；缓存过期不导出业务值；仅停主机来源时主机曲线断开而DB曲线继续；所有计数/筛选/历史先范围化。
- [ ] 执行 `.venv\Scripts\python.exe -m pytest tests/platform/test_metrics.py tests/platform/test_history_scope.py -q` 并确认初始失败。
- [ ] 安装核验官方 Prometheus，配置本机 bridge；历史模板、单位、时区、分页与范围化总览；用来源新鲜度屏蔽无效点，缺失不补0。
- [ ] 同命令通过；实际比对SQL计数和Prometheus窗口；AT-008、010及 AT-004 的时序/计数路径。
- [ ] 独立评审和提交，保留版本、校验值、就绪检查及时间窗对照。

### Task 5: 规则、事件、处置、复验与知识

**Files:** 创建 `platform_app/{events,remediation,knowledge}.py`、相关 routes；测试 `test_events.py`、`test_verification.py`、`test_knowledge_scope.py`。

**Interfaces:** `evaluate(run_id, rule_version) -> EvaluationResult`；按架构定义事件状态、动作、处置计划和复验任务。独立时钟tick检查陈旧与UNKNOWN。

- [ ] 先写测试：持续阈值、原始alert episode保留、同实例多告警事件归并/跨实例merge拒绝、维护窗口、无新数据仍能过期；开发可备注但不可分派/关闭；一个episode恢复但其他仍firing/UNKNOWN时不能整件恢复；merge后旧verification失效；旧数据/无数据/不可比负载复验不成功；跨实例知识和报告全范围鉴权。
- [ ] 执行 `.venv\Scripts\python.exe -m pytest tests/platform/test_events.py tests/platform/test_verification.py tests/platform/test_knowledge_scope.py -q`，保存失败。
- [ ] 实现锁、digest性能、不可达/过期规则，独立原始告警及同实例事件归并，事件确认/分派/备注/维护窗口，人工处置记录、复验和知识审核；所有动作审计。锁模板仅验证观测链消失；SQL模板每窗至少10次且调用率比0.5~2等可比条件按版本保存，不满足为inconclusive。
- [ ] 同命令通过；真实故障→触发→无效处置复验失败→正确处置复验→审核知识，执行 AT-011、012、016。
- [ ] 独立评审和提交。人工执行记录不能写成平台已自动修复。

### Task 6: 有界、可审计的 AI 调查

**Files:** 创建 `platform_app/{investigations,tools,ai_worker}.py`、`routes/investigations.py`；测试 `test_ai_scope.py`、`test_ai_failures.py`。

**Interfaces:** `run(actor_ref, authorized_scope, time_range, question) -> Investigation`；EvidenceBundle冻结输入时间窗和来源，工具调用实时复核权限与数据策略。

- [ ] 先写测试：禁止外发无网络调用；模型提交其他实例不连接；历史工具不把to之后的新采混入冻结窗口；原始敏感字面量不进入bundle；权限撤销/取消/预算耗尽停止；虚构证据引用拒绝。
- [ ] 执行 `.venv\Scripts\python.exe -m pytest tests/platform/test_ai_scope.py tests/platform/test_ai_failures.py -q`，确认失败。
- [ ] 实现独立AI队列和进程、脱敏预览、只读工具白名单、调用/费用预算、报告结构校验与人工反馈；模型密钥仅本地加载。
- [ ] 同命令通过；真实 DeepSeek 调查已授权合成故障，核对事实、引用和反证；超时/无效配置/提示注入不会影响采集；AT-005、013、014、015。
- [ ] 独立评审和提交。报告语义正确性需要已知故障对照，不能只检JSON合法。

### Task 7: 完整工作页面和接入交互

**Files:** 创建 `frontend/src/{api,types,app}` 与 `pages/`，以页面设计路由和本目录原型为准；构建配置及同源托管由根代理统一整合。

**Interfaces:** 仅消费 `/api/v1` 的稳定契约；所有后台任务消费统一TaskRef和状态；页面局部失败与全局失效区别展示。

- [ ] 依据03逐页检查字段、操作和九态；先固定OpenAPI与共享类型，不另造后端路径。
- [ ] 实现登录、运行总览、资产/向导、实例各来源、历史、事件/处置/复验、调查/审计、知识、任务和管理；页面没有后端能力时不得显示成功按钮。
- [ ] 执行前端类型检查和生产构建；处理实际错误并重新构建。不为静态文案写实现镜像测试。
- [ ] 浏览器走AT-001、004、007、011、013、016核心流程，刷新和返回后上下文保留；检查桌面与小屏、键盘焦点和全部异常态。
- [ ] 独立评审和提交，截图必须来自实际产品；原型截图仍标设计，不混入运行证据。

### Task 8: 全链路验收、运行脚本与案例交付

**Files:** 创建 `scripts/platform/{init,start,stop,health}.ps1`、`verify_platform.py`、`reports/platform/acceptance.json`、运行指南及案例说明；更新根README和私密CHECKPOINT。

**Interfaces:** 初始化不覆写既有资产/授权；启动分别管理API/采集/AI/时序，健康接口能区分依赖失效；停止只终止本项目记录的进程。

- [ ] 按需求AT清单逐项运行真实验收，报告每项命令、状态、证据和未覆盖项；包含直接HTTP/下载越权而非只看菜单。平台交付前停止经PID/命令行核实的旧无鉴权实验服务；验证旧全局API不再构成旁路，新API未知路径返回404而非SPA页面。
- [ ] 执行完整平台测试、前端构建、真实集成与重启恢复；保存30分钟连续采样和模型失败期间的采集证据。
- [ ] 执行保留/保护证据、磁盘阈值、敏感文件/导出扫描；验证从未发起AI的事件、处置及复验引用也按对象保留期受保护；回放材料仅作已标记的独立验证，不混入健康统计。
- [ ] 做代表性查询性能与开销测试；若未达到10实例目标，记录实际规模与未验结论，不能声称达到设计NFR。
- [ ] 浏览器复验主要旅程，审阅剩余缺口和风险；生成启动入口、案例展示路线、可核实项目说明及可复现步骤。
- [ ] 最终本地提交、源码包和清单；不自动公开GitHub，不发送邮件；只有全部必需验收有证据才标首批平台完成。

## 4. 需求覆盖与依赖

| 工程任务 | 需求 | 依赖 |
| --- | --- | --- |
| 1 基础与授权 | FR-001、002、006、026 | 设计一致性审查与独立控制库 |
| 2 接入与资产 | FR-003~007 | 1；可与前端框架搭建并行 |
| 3 采集与任务 | FR-008~011、015 | 1、2 的连接和资产接口 |
| 4 时序与历史 | FR-009、012~015 | 1、3 的证据封套；前端可按冻结接口并行 |
| 5 事件闭环 | FR-016~021 | 1、3、4 |
| 6 AI 调查 | FR-022~025、007、026 | 1、3；事件关联依赖5；独立模型故障测试 |
| 7 工作页面 | FR-001~026 的页面入口 | 02路由冻结；逐模块接真实后端 |
| 8 验收与交付 | AT-001~023、NFR-001~010 | 前7项及真实隔离验收资产 |

P1及PostgreSQL不在本批任务里伪装实现；后续使用相同的资产、权限、证据和任务契约扩展。

## 5. 持续推进与需要用户时的通知

不以完成一个小实验或遇到一个工具错误作为停止点。可自行判断的实现选择写入进度记录后继续；涉及真正缺少的账号操作、用户判断或现场验收时，先准备具体可查看结果和替代方案。

用户已要求声音提醒。本地辅助脚本 `D:\Codex_WS\TDX_DBA\tools\user-attention.ps1` 提供循环提示音，仅在确实需要用户而必须等待时启动；用户回复后停止。根目录 `停止提示音.cmd` 可手动停止。工作持续进行时不播放循环声音，也不把“音频函数返回”描述成已证实扬声器发声。

每个可运行批次更新 `CHECKPOINT.txt`，至少包含当前提交、完成/待验/未实现、运行入口、失败原因、下一项动作和复现命令。任务记录保留到最终交付，避免上下文切换后重复已完成工作。

### 5.1 当前演进约定（2026-11-13）

按用户后续要求，不再播放音乐。源码和文档可发布到已经指定的公开仓库，仍须排除私密材料。公开故障入口在场景通过前关闭，完整平台保持在线；切换提前一分钟通知，不等待额外确认。

每轮只做一个问题，先检查所需数据能否采到、时间/单位/权限是否匹配，再预演发现、分析与恢复路径。关键前提无法补齐则跳过。每 30 分钟复盘方向和进度，并实查公开页面、静态资源、只读登录及历史问题；原则一小时仍无结果则记录阻碍后暂停，不堆积多个故障。

当前 BOUNDARY-01 对应 FR-022～025 / AT-013：新自主分析选择边界已实现，完成回归、真实数据只读复核及不可变版本部署；新故障场景仍需单独重测；不将此状态记为整体需求已验收。既有报告保持不可变，不能因当前删选而改写历史。


## 2026-11-13 当前优先级调整

先补采集再验证诊断。官方模板与固定只读 SQL / 应用事件扩展已落地，实际样本和自然后台入库已核对；自主分析沿用原权限、冻结证据和独立队列。界面默认 7 天，自动刷新默认关闭。接下来逐个场景复现、评价报告、定位不足、优化及重测；不以新增采集替代诊断验收。每 30 分钟复查方向、实际进度及公开服务，优先处理影响演示可用性的问题。详见[本轮计划](../superpowers/plans/2026-11-13-collection-foundation.md)和[采集说明](../collection-foundation.md)。
