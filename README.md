# DB Evidence Platform

**复用 Zabbix 监控，先读问题结论，再按需回查证据。**

An evidence-driven MySQL inspection and investigation platform.

面向内部 DBA 与运维人员：按 host、item 和标签选择已有监控，组成持续巡检的联合对象。打开报告，先了解**发生了什么、为什么、现在怎么做**；需要核实时，再展开分析过程、冻结证据和图表。

[公开演示指南](docs/demo-guide.md) · [结论优先阅读指南](docs/conclusion-first-guide.md) · [联合对象上手](docs/analysis-groups-guide.md) · [操作指南](docs/user-guide.md) · [安装与运行](docs/platform/07-operations.md)

## 公开演示

[临时演示入口](https://byte-texts-allergy-examples.trycloudflare.com/demo)：账号 **`demo`**，密码 **`EvidenceDemo-2026!`**。这是唯一有意公开的本部署专用账号；新部署仍没有默认账号或通用密码。临时地址可能变化，服务依赖演示电脑在线，不承诺持续可用。**本地链路已实测，公网 API 仍待本机操作者重载及公网验收**；状态与模型局限见[演示验收](docs/acceptance-public-demo.md)。

![公开演示：固定锁等待开关、实际进度与冻结报告](docs/images/public-demo.png)

进入“演示”，开启固定锁等待开关，观察实际 Zabbix 证据、分析进度与报告，再关闭并查看恢复复验。目标仅限预先核对的隔离 Lab；业务负载是合成的，Zabbix 采样是真实的，不代表生产事故或生产采用。缺数、模型失败、额度不足及恢复尚未确认都必须如实显示，不能把进度结束视为诊断或恢复成功。操作与证据边界见[演示指南](docs/demo-guide.md)。

所有访客共享一轮实验：最长 **180 秒自动终止**，直接清理通过后 **600 秒冷却**；每个 **UTC 日最多 8 轮实验、4 次 AI 分析**，失败的已接受模型调用也计入额度。停止不受冷却或额度限制；浏览器关闭不会延长故障时限。

![问题分析：先查看结论、原因与建议操作](docs/images/conclusion-overview.png)

**从问题开始阅读。** 报告将现象、各层候选原因、联合根因判断、结论和建议操作放在前面；证据不足与覆盖缺口保持可见。截图的实际窗口、模型模式与核对结果见[本轮验收](docs/acceptance-conclusion-first.md)。

![Zabbix 接入：选择来源与具体监控项，维护联合对象](docs/images/zabbix-focused.png)

**采集交给已有 Zabbix。** 在同一来源与业务系统内，按 host / item / tags 选择确切范围，保存联合对象后配置巡检。当前主路径无需目标数据库口令，不提供旧直连、自定义采集模板或单 host 独立对象创建入口。

![数据查看：在同一时间窗口对照所选数据库与主机指标](docs/images/aggregation-metrics.png)

**需要时直接核对数据。** 同一联合对象中的数据库与主机曲线集中查看；文本、日志及结构化历史按监控项下钻。这张图查询当前上游历史，报告中的冻结证据另行保留。

## 五个入口

| 入口 | 用来做什么 |
| --- | --- |
| **问题分析** `/` | 看问题、风险、巡检状态与结论优先报告 |
| **Zabbix 接入** `/monitoring` | 连接只读来源，选择监控项，维护联合对象 |
| **数据查看** `/metrics` | 看当前窗口的数值曲线、类型化历史、来源与缺口 |
| **配置** | 设置巡检、模型与外发、必要账号授权和审计 |
| **演示** `/demo` | 在固定隔离 Lab 开启 / 关闭锁等待，观察本轮证据、报告与恢复复验 |

连接来源 → 选择联合范围 → 配置后台巡检 → 阅读结论 → 按需核对证据 → 人工处置与新样本复验。配置完成后，后台运行不依赖浏览器一直打开；正常周期也保留记录，不为展示制造问题。

确定性工具负责采集、计算与验证；AI 辅助关联和解释；重要处置由人员明确控制。日常巡检不自动执行数据库变更；演示只在明确开启后执行固定隔离 Lab 事务，并回滚清理。平台不提供任意 SQL、Shell 或邮件发送能力。

## 开始使用

### 已有部署

1. 在“Zabbix 接入”连接只读 API 来源，限定业务系统和允许主机组；凭据仅保存在服务端。
2. 新建联合分析对象，选择同一来源中的 1–32 个具体 items，核对 host、单位、值类型、状态和最近实际采样。可选登记主机关系；登记和同时变化都不证明因果。
3. 在“配置”保存负责人、对象、周期、冷却和预算。默认 `rules_only` 不调用模型；真实模型还需私密供应商配置、当前授权与明确外发许可。
4. 从“问题分析”阅读结果，需要时展开证据或进入“数据查看”。当前查询不会补写旧报告。

联合对象默认每 60 秒读取所选数据，可设 30–3600 秒；轻量规则检查默认 300 秒，综合巡检默认 1800 秒，三者独立。范围、类型、稀疏历史与权限见[联合对象指南](docs/analysis-groups-guide.md)。

### 新部署

当前部署工具面向 Windows + PowerShell、Python 3.12、Node 22.12+ 或支持 Vite 8 的兼容版本。使用独立 MySQL 控制库，Zabbix 服务及只读权限由操作者提供。

```powershell
git clone https://github.com/catdogrennie-gt/db-evidence-platform.git
Set-Location db-evidence-platform
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-platform-lock.txt
npm --prefix frontend ci
npm --prefix frontend run build
```

按[安装指南](docs/platform/07-operations.md)完成仓库外私密目录、空控制库初始化、显式迁移、账号与业务授权及服务启动。API 不自动建库或迁移。新部署从零业务数据开始，无仓库通用密码。`collection_mode` 默认 `zabbix_only`；`legacy` 只用于历史兼容与旧测试，不是当前产品的接入方式。

```powershell
$platformConfig = Join-Path $env:LOCALAPPDATA 'DBEvidencePlatform-private\config.json'
.\scripts\platform\manage.ps1 -Action Start -Services mysql,api,worker,ai -ConfigPath $platformConfig
.\scripts\platform\manage.ps1 -Action Status -Services mysql,api,worker,ai -ConfigPath $platformConfig
```

默认入口为 [http://127.0.0.1:18080](http://127.0.0.1:18080)，以私密配置 `public_origin` 为准。根目录 `start.ps1` 属于更早的实验应用。

## 结论与证据的边界

- 报告先列发生的现象、独立表层原因、联合候选解释、当前结论和建议操作。证据不足或不适用是有效回答；候选解释不是已证明根因。
- 分析过程按需展开关键事实、反证、未知、关系、图表及日志 / 文本，不用空图或全量曲线挤占结论。
- 旧报告缺少完整摘要时明确标识，保留原文；不会在读取时用规则补成新的 AI 结论。包含旧 ID 的链接只读历史或提示退役，不跳到当前观测冒充原记录。
- 报告使用不可变冻结清单、组成和时间窗。模型读取的有界摘要与图表保留范围分别说明，省略继续可见。
- 新模型输入上限为 24,000 字节，输出上限可配置到 3,000 tokens；更低显式预算仍生效，不保证所有数据都能入模。DeepSeek 等供应商为私密可选配置，公开源码不绑定默认 Pro 账号、端点或密钥。

业务读取按部门、系统、环境与对象授权，来源管理员不会自动获得业务证据。文本 / 日志原文另需敏感读取权限；读取权不等于模型外发许可。自由说明保留本地，外发只接受受限脱敏数据。

缺数不补零，旧值不代表当前健康。官方模板可能按值变化 / 心跳稀疏保存 history；短窗无记录应核对实际采样和保留语义。SQL 累计值、端点和窗口极值必须区分，缺少真实 counter epoch 时不声称窗口差分已验证。

## 验证与复现

这是个人隔离 Lab 作品，使用真实监控观测和合成业务负载，不代表生产采用、自动修复或通用诊断准确率。实际测量、回放数据、确定性规则与真实模型输出分别标识。

本轮结构与页面改造、权限和执行边界、真实模型语义的实际结果统一见[结论优先验收](docs/acceptance-conclusion-first.md)。本 README 不以中间测试数量或一次 completed 宣称本轮真实模型语义已验收。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/platform -q
.\.venv\Scripts\python.exe -m pip check
npm --prefix frontend test
npm --prefix frontend run typecheck
node frontend/scripts/check-workflow-contracts.cjs
node frontend/scripts/check-connection-contracts.cjs
npm --prefix frontend run build
```

真实 MySQL / TLS 用例需显式配置，跳过不计通过；真实模型另需供应商、权限与外发许可。公开源码不携带本机账号、控制库、原始私密运行记录或个人材料。长期稳定性、高可用、生产容量与公网部署仍未完成验证。

## 历史与文档

[联合对象验收](docs/acceptance-analysis-groups.md)保留上一轮正常及受控锁周期：锁链引用匹配，但 CPU 首值 3.358 被误作范围下界约 3.4，实际最小值为 2.7538。原输出、早期失败和限定范围保留。该历史报告的 12KB 输入记录不随新 24KB 上限改写。[2026-11-08 验收](docs/acceptance-zabbix.md)另保留当时整组曲线超预算被省略等缺口。两轮结果都不替代本轮模型语义复核。

旧界面截图见历史验收与版本材料；旧直连及单 host 对象只保留授权历史读取和必要管理，不继续调度旧采集。

- [结论优先指南](docs/conclusion-first-guide.md)、[操作指南](docs/user-guide.md)、[联合对象上手](docs/analysis-groups-guide.md)。
- [官方模板与诊断扩展](docs/zabbix-official-setup.md)、[安装与运行](docs/platform/07-operations.md)、[设计与历史](docs/platform/README.md)。
- [结论优先设计](docs/superpowers/specs/2026-11-09-conclusion-first-design.md)、[变更记录](CHANGELOG.md)、[版本索引](docs/releases/README.md)。
- 历史版本 `legacy-platform-2026.11.08`、`analysis-groups-2026.11.09`、`conclusion-first-2026.11.09` 保留；当前源码增加受限公开演示，发布状态以版本索引为准。
- [原平台回溯](docs/releases/legacy-platform-2026.11.08.md)、[更早的独立实验模块](README-legacy-lab.md)。

源码：`platform_app/` 后端与 worker、`frontend/` 界面、`migrations/` 迁移、`tests/platform/` 回归、`scripts/platform/` 部署与验收工具。
