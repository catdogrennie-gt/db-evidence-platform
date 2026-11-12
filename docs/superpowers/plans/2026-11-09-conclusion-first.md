# 聚合分析产品收敛实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 让 Zabbix 联合分析直接给出发生什么、为什么、怎么做，并移除无关旧平台入口。
**Architecture:** 增量扩展存储中的 AI 报告 JSON，复用冻结证据与权限。四入口前端仅使用联合对象。Zabbix-only 模式在服务端停止旧采集入口，私密部署先备份再清理旧资源。
**Tech Stack:** Python / FastAPI / Pydantic / MySQL，React / TypeScript / Ant Design，Zabbix。
**Spec:** `docs/superpowers/specs/2026-11-09-conclusion-first-design.md`

## Global Constraints

- 不发邮件，不播放音乐，不部署公网；秘密与私人资料不进入公共源码。
- 历史报告和冻结清单不可回写；真实测量、规则、旧报告与新 live 模型严格区分。
- `executive_summary` 字段、枚举、上限按 Spec；所有嵌套引用仍需授权冻结清单校验。
- 不增加任意 SQL/Shell/写库/自动修复工具，manual_change 建议必须人工批准。
- 每轮输入24,000字节；累计token预算32,000不变；输出上限3,000且不突破显式配置。
- 仅保留四主入口；旧历史ID不能重定向成当前数据。
- 本机删除仅限核实后的旧三host；当前官方监控与联合组必须保留；旧平台历史归档不级联删除。

## Review Focus

1. 摘要嵌套反证/动作引用越权或虚构：Task1 拒绝测试。
2. 旧报告/失败/正常巡检被渲染成已确认根因：Task1/2 兼容与状态测试。
3. 旧自定义入口隐藏但后台仍采集：Task3 路由和worker调度测试。
4. 历史链接指向当前数据或丢失授权：Task2 历史ID回查与拒绝测试。
5. 数据/图为空仍占主版面或降低证据可用性：Task2 UI测试与Task4实际页面检查。

### Task 1: AI 结论契约与输入预算

**Files:** modify `platform_app/investigations.py`；create `platform_app/report_summary.py`（若拆分有助聚焦）；tests `tests/platform/test_report_summary.py`、`test_ai_failures.py`、`test_ai_scope.py`、`test_analysis_groups.py`。
**Consumes:** 现有冻结 bundle、引用集合、provider预算。
**Produces:** `report.executive_summary` 的 Spec 契约；旧报告仍可解析；新 live version 要求摘要。

- [x] 编写并运行失败用例：嵌套未知引用/反证引用、无引用observed、未批准manual_change、缺摘要的新live、旧版/规则兼容。
- [x] 实现紧凑中文 prompt、Pydantic校验与版本要求；增加24KB上下文并同步初始限额；输出显式配置仍为上限。
- [x] 更新受预算上限影响的压力测试，保持大输入/工具输出确实超过新界、跨主机与锁证据优先断言。
- [x] 运行相关pytest并报告结果；不调用真实API，不改运行配置；仅提交所属文件并报告范围。

### Task 2: 四入口与结论优先界面

**Files:** `frontend/src/App.tsx`、`Monitoring.tsx`、`Intelligence.tsx`、`intelligenceModel.ts`、`Admin.tsx`、相关样式/新摘要组件及 frontend tests/scripts。
**Consumes:** Task1 `report.executive_summary` 契约；来源/groups/metrics现有API。
**Produces:** 四导航；完整结论优先报告；只读历史和引用可用；无旧采集/单主机区。

- [x] 增加摘要先于图表、无根因/无动作、旧版/失败/过时、无单主机区和四导航的有效测试。
- [x] 实现新报告摘要及按需过程；现象/原因/结论/操作醒目，状态与证据引用可读；空图不占位。
- [x] 精简导航、来源与配置页，移除旧自定义采集/直连入口；旧列表迁移，历史ID保留授权只读入口；移除旧创建链接。
- [x] 执行前端test/typecheck/build和更新后的两个contracts脚本；仅提交所属文件。

### Task 3: Zabbix-only 服务端与部署清理

**Files:** `platform_app/config.py`、`routes/inventory.py`、`routes/admin.py`、`routes/monitoring.py`、`intelligence.py`、`worker.py`、采集scheduler必要处、prepare脚本及新 `tests/platform/test_zabbix_only.py`；私密运行脚本在仓库之外。
**Consumes:** 当前官方host/组和历史权限，Spec collection_mode。
**Produces:** 默认Zabbix-only；legacy仅显式兼容；组正常采集，旧采集禁用，旧历史可读。

- [x] 测试Zabbix-only新直连/自定义模板/旧selection明确410，组/来源正常，非monitoring调度被阻止，旧历史仍可授权读取。
- [x] 实现配置与统一guard；旧测试fixture显式legacy保持历史模块可验证；不修改Task1文件。
- [x] 在worker执行边界拒绝预先排队/重试的直连连接测试、直连采集和mapping非group的旧Zabbix采集；transport spy证明零目标调用，组采集正常。
- [x] 清理本机前一致备份；确认ID后删除旧Zabbix三host、归档平台旧实例和停用旧采集器，保留历史、官方采集及当前组。
- [x] 在私密运行环境完成必要的凭据维护并更新实际依赖；不在工具输出显示秘密；验收重连和官方样本推进。
- [x] 运行定向测试；私密记录清理前后身份、备份、counts与检查结果，公开代码仅提交所属文件。

### Task 4: 集成、真实报告、文档与发布

**Files:** 指南/README/验收/图片；仓库外验收脚本与parent CHECKPOINT。
**Consumes:** Task1–3完成结果。
**Produces:** 可运行的精简产品、实际新报告和截图、可回溯提交/Release。

- [x] 全平台pytest、前端test/type/build和contracts通过；新差异独立代码复核。
- [x] 重启服务，确认readiness，真实HTTP验证新模式与旧对象退出；独立浏览器验证四入口、摘要顺序、图/引用/配置。
- [x] 使用历史真实锁窗口创建新的live分析（新建清单，与原冻结输入不声称相同，保留原版不改写），或已有新巡检窗口；核对关键事实、原因措辞、建议和引用，正常结果不制造故障。
- [x] 更新文档/截图/交付与CHECKPOINT，秘密扫描后提交与GitHub回读；新Release保留此前版本。

## 执行决定

无bash时用原生PowerShell/Python创建本计划专属ignored ledger、brief与review package。前后端共享接口先冻结，文件所有权分开；必要时可并行实现无共享写入的任务以节约用户时间。所有运行时数据清理在私密目录执行并留备份。
