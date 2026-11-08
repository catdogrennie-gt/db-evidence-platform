# 开源技能与设计依据

日期：2026-11-08。技能提供工作方法，不提供本平台已经实现的功能。本次以本地源码及设计文件为交付位置。

## 1. 实际安装结果

使用本机 `skill-installer` 的官方安装脚本，按确切提交安装。逐项检查 `SKILL.md` 存在并记录 SHA256，核验文件位于私密工作目录 `research/installed-skills-verified.json`。Superpowers 安装成功；OpenAI 安全技能首次 ZIP 下载中断，随后用安装器 `--method git` 成功回退，无需用户操作。

| 来源与固定提交 | 已安装技能 | 本项目如何参考 |
| --- | --- | --- |
| [obra/superpowers](https://github.com/obra/superpowers/tree/22f0b463e7a5e579904dd6211a520321035526f7) | brainstorming | 查原始资料、明确业务闭环、比较可行方案、写设计并让独立工程师检查重要歧义 |
| 同上 | writing-plans | 建立需求→模块→接口→任务→验收的对应关系；工程任务有文件职责、验证命令和交付标准 |
| 同上 | systematic-debugging | 先固定复现和失败证据，定位原因再修改；环境故障预先准备替代方案 |
| 同上 | verification-before-completion | 未执行最新检查不能声称完成；区分实现、待验、未实现及外部条件不可验 |
| 同上 | requesting-code-review | 分开检查需求符合性与工程质量；复核权限、并发、数据时间和异常恢复 |
| 同上 | subagent-driven-development | 参考任务交接和可恢复进度记录；分工按文件所有权进行，避免并发覆盖；实际执行服从本会话的协作约束 |
| [openai/skills](https://github.com/openai/skills/tree/49f948faa9258a0c61caceaf225e179651397431) | security-best-practices | 已安装备用；与 Python API / TypeScript 界面相关的安全指导按需要使用 |
| 同上 | security-threat-model | 已安装备用；没有因此启动额外威胁建模报告或审批流程 |

以上共八项技能已安装到当前用户的 Codex 技能目录。新安装技能在后续轮次可被识别；本轮已直接读取与设计相关的规范。没有把插件搜索结果当作安装成功，没有安装重复的 Superpowers 插件包。

## 2. 采用的工作成果

| 方法 | 本项目中的具体成果 |
| --- | --- |
| 需求先行 | `01-requirements.md` 的 FR 编号、采集矩阵、异常态与验收编号 |
| 方案比较 | `02-architecture.md` 比较 PMM、Prometheus 组件组合及全自建，并写明取舍 |
| 设计可见 | `system-map.svg`、`architecture.svg` 与 `index.html` 的静态可点击页面流程 |
| 工程契约 | 资产、采集能力、证据封套、任务租约、权限、API 和时间语义 |
| 独立检查 | 架构与需求的 builder review，整套需求/架构/页面/任务的一致性检查，记录发现与修订 |
| 验证驱动 | 用额外实例接入、真实异常、直接 HTTP 越权和服务重启验收平台，不以截图代替功能证据 |

同时参考 [GitHub Spec Kit](https://github.com/github/spec-kit) 的规格、计划、任务及实现对应方式。**未安装或运行 Spec Kit CLI**，不把参考其方法描述为工具已经集成。

用户已授权自主推进，并要求先设计再实施；因此先形成可查看的完整设计并处理审查问题，再按实施计划执行。不会因参考技能额外反复索要已授予的执行授权。新的真实业务数据外发或外部发布不由这种开发授权代替。

## 3. 组件和专业规范来源

只将公开的一手资料用于组件能力判断；工程选择及待测假设在架构文件中分别标记。

| 资料 | 用途 |
| --- | --- |
| [Percona PMM 3 官方文档](https://docs.percona.com/percona-monitoring-and-management/3/) | 对照成熟监控、库存、QAN、部署及访问控制能力；避免重复发明和错误贬低现有平台 |
| [Prometheus 安全模型](https://prometheus.io/docs/operating/security/) | 数值服务置于后台；业务查询走平台授权模板 |
| [Prometheus 标签规范](https://prometheus.io/docs/practices/naming/) | 控制标签基数，SQL 与日志不进入指标标签 |
| [官方 mysqld_exporter](https://github.com/prometheus/mysqld_exporter) | 可选采集适配器的能力和部署约束；本批不能未经集成即声明使用 |
| [Grafana 数据源权限说明](https://grafana.com/docs/grafana/latest/administration/data-source-management/) | 看板权限不能代替同组织数据源隔离；普通用户采用后端范围化查询 |
| [MySQL 8.4 官方手册](https://dev.mysql.com/doc/refman/8.4/en/) | performance_schema、状态、日志和复制能力；按版本与权限探测 |
| [OWASP 授权指导](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html) | 默认拒绝、逐请求检查、对象范围及直接 API 越权验证 |
| [OWASP 会话指导](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html) | 会话失效、Cookie、敏感令牌与服务端验证 |
| [Bond DBCheck 公开版本说明](https://github.com/jeameshf/Bond-DBCheck/releases/tag/V26ai1.0) | 资源、连接、巡检、任务和报告的产品工作流参考 |
| [backup-platform 公共仓库](https://github.com/Zhh9126/backup-platform) | 预检、任务、历史、审计和验证证据参考；备份恢复仍为附属模块 |

本轮没有复制这些参考项目的产品源码。未来若复用代码，须记录提交、许可证、改动及归属。个人邮件附件提供的 AI 观点只提炼原则，原件不放进公共源码或模型输入。
