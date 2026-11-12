# DB Evidence Platform · 2026-11-08 历史快照

**状态：历史 Release 已发布。** [查看 `legacy-platform-2026.11.08`](https://github.com/catdogrennie-gt/db-evidence-platform/releases/tag/legacy-platform-2026.11.08)。发布后已回读标签目标与源码树；该历史版本不是草稿、不是预发布，也未设为最新版本。

这是 Zabbix 自动巡检改造前的内部 MySQL 运维平台实现。个人隔离环境中的目标业务数据为合成数据，采集、模型调用及受控实验有真实记录；不代表企业生产采用。

## 固定源码身份

| 内容 | 固定值 |
| --- | --- |
| 公开仓库 | [catdogrennie-gt/db-evidence-platform](https://github.com/catdogrennie-gt/db-evidence-platform) |
| 公开提交 | `96ab3aea7b7803b652c352740b4fc5f88c8b8474` |
| Git tree | `03911a3f2fc8b8fd8463265da7bd004aab4b6ce9` |
| 跟踪文件 | 199 个源码 / 文档文件 |
| 对应本机审核提交 | `969d59883a51e18bcdf66be1e1ee0bace5006307`；提交记录不同，源码 tree 一致 |
| 已保留入口 | [archive/platform-2026-11-08](https://github.com/catdogrennie-gt/db-evidence-platform/tree/archive/platform-2026-11-08) |
| 已发布标签 | [`legacy-platform-2026.11.08`](https://github.com/catdogrennie-gt/db-evidence-platform/releases/tag/legacy-platform-2026.11.08)；历史快照不设为最新产品版本 |

[查看当时的 README](https://github.com/catdogrennie-gt/db-evidence-platform/blob/96ab3aea7b7803b652c352740b4fc5f88c8b8474/README.md)。远端标签精确指向上表公开提交；公开提交与本机审核提交不可互相替代作为标签目标。

## 当时已经实现的主流程

平台采用 React / TypeScript、Ant Design / ECharts、FastAPI、独立 MySQL 控制库和 Prometheus。采集 worker 与 AI worker 分离，后台固定规则、模型解释及人工记录分别留存。

| 环节 | 历史实现 |
| --- | --- |
| 资产接入 | MySQL 六步接入向导、只读预检、UUID 去重、能力确认及首次采集；第三个独立目标曾通过实际界面接入 |
| 持续采集 | 状态、SQL 摘要、事务与锁、结构配置、复制基础状态、日志及主机资料；来源状态、时间和缺口可查询 |
| 授权与事件 | 本地角色、实例范围、管理与业务权限分离；版本化规则、事件确认分派、维护窗口及站内提醒 |
| AI 调查 | 脱敏预览、显式外发策略、冻结证据、受限只读工具、预算与引用校验；模型没有任意 SQL / Shell 或目标写操作工具 |
| 处置与复验 | 保存人工计划、审核与外部执行记录；根据新样本返回 `improved`、`not_improved` 或 `inconclusive` |
| 知识与运维 | 版本化知识及受保护证据清单，显式迁移、worker 心跳、进程身份校验和限时导出 |

这些能力的完整正常 / 异常组合仍有待验项目，不能由主流程已运行推断全部需求已经通过。

## 有真实记录的两个案例

**事务阻塞。** 隔离目标中制造等待链。执行无效的 `SELECT 1` 后，三个新快照仍包含同一等待链，复验返回 `not_improved`；外部受控工具回滚阻塞事务后，新的三个快照满足条件，返回 `improved` 并结案。平台保存执行记录，没有自动执行目标库回滚。锁链消失不等于业务恢复已经获证明。

**固定负载 SQL 索引对照。** 对三万行合成数据以固定频率执行同一查询，比较基线、无效连通检查及索引后的窗口，联合后台摘要、固定模板 EXPLAIN 和复验记录。归档保留早期失败与 `inconclusive`；结束时恢复原索引集合并核对有序数据摘要。小表、固定负载的结果不能外推成企业收益。

案例及知识中的审核明确由自动化验收账号完成，不能写成用户已经亲自审阅。详情见固定版本的[案例说明](https://github.com/catdogrennie-gt/db-evidence-platform/blob/96ab3aea7b7803b652c352740b4fc5f88c8b8474/docs/platform/08-case-and-demo.md)。

## 历史验证范围

该源码快照中的文档记录后端 **414 通过、9 跳过、6 个子检查通过**，另有 **781 项真实 HTTP 权限检查**；前端契约检查与生产构建通过，保留非阻断体积提示。跳过项依赖显式的真实 MySQL / TLS 环境，未计作通过。

这些是当时的验证记录，本发布说明编写时未重新执行，不构成后续改造版本的新验收。逐项证据、失败及边界见固定版本的[验收索引](https://github.com/catdogrennie-gt/db-evidence-platform/blob/96ab3aea7b7803b652c352740b4fc5f88c8b8474/docs/platform/06-acceptance-status.md)。

## 当时的明确边界

- 验证限三个独立 MySQL 合成目标；十实例容量、长期稳定性、高可用、生产部署和采集开销归因未完成。
- PostgreSQL、Zabbix 接入、Grafana 集成及新版自动巡检不属于本历史快照的已实现能力。
- 处置流程记录人工方案与外部执行；没有通用目标变更执行器，也没有自动治愈数据库。
- 业务对象及审计的完整物理保留清理仍有缺口；保留策略不是长期实测结果。
- 备份恢复保留为更早的独立实验模块，尚未整合成平台的备份作业产品。
- 原始运行库、TSDB、账号、主密钥、目标凭据与私密资料不随公共源码提供。

## 回溯与重新运行

在单独目录获取固定源码；以下命令不会重置当前开发目录：

```powershell
git clone https://github.com/catdogrennie-gt/db-evidence-platform.git db-evidence-platform-legacy
Set-Location db-evidence-platform-legacy
git switch --detach 96ab3aea7b7803b652c352740b4fc5f88c8b8474
git rev-parse HEAD
git rev-parse 'HEAD^{tree}'
```

最后两项应分别为上述公开提交和 tree。运行固定源码中的 `docs/platform/07-operations.md`，使用独立私密目录、控制库及端口初始化自己的环境；需要 Windows / PowerShell、Python 3.12、兼容的 Node、MySQL 和 Prometheus，完整组件及命令以该版本安装指南为准。

源码回溯不会恢复原实验数据库、历史观测或账号。已经保管的私密运行数据必须使用与其匹配的控制库、主密钥和配置；不要对现有数据执行新空库初始化。旧实例身份、窗口和事件也不能冒充新部署的观察。

## 发布核对结果

已核对标签指向 `96ab3aea7b7803b652c352740b4fc5f88c8b8474`，tree 为 `03911a3f2fc8b8fd8463265da7bd004aab4b6ce9`，Release 为 `draft=false`、`prerelease=false`，创建时使用 `latest=false`。归档分支与固定提交链接继续保留，便于独立回溯。

本说明不承诺额外安装包、原运行数据或未核验的附件哈希；GitHub 自动生成的源码归档对应此标签，部署仍需自行初始化私密环境。

新版内容与状态见[变更记录](../../CHANGELOG.md)。
