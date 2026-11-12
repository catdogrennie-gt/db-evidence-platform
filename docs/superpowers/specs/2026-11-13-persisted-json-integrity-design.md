# 新证据的持久化 JSON 哈希一致性

## 依据与目标

场景前提核查发现两条历史数值证据的哈希与读回内容不同。实际 MySQL 只读 `SELECT CAST(... AS JSON)` 精确重现了两个浮点末位变化；保留的 `raw_value` 字符串未变。只确认这些样本，不推断全部旧记录的原因。现有采集在入库前计算哈希，数据库 JSON 往返后的数值表示可能不同。

本轮目标：新采集和新冻结清单的内容哈希与实际持久化 JSON 一致。新清单遇到不可核验的旧证据时明确缺口，必需引用不可核验则失败；不修改已经提交的旧数据、旧哈希、报告或审计。

## 方案选择

额外保存原始 JSON 文本可以保留传输表示，但涉及 schema、双份存储和读取迁移。本轮选择在现有 JSON 入库后、同一事务内读回内容并封存哈希，保持存储结构和摘要算法。此为实现选择，沿用用户授权的持续改进及技术方案委托；不是用户提出的具体算法。

## 设计

- `evidence._persist_new_payload(db, row)` 仅接受 transient 新 `Evidence`，或类型为 `metric_window`、`time_series_window`、`prometheus_window`、`coverage` 的新 `ManifestItem`。先计算初始哈希、插入并 flush，只刷新 `payload`，随后对读回内容计算哈希并 flush。无提交、无额外网络调用、不重复写入 payload。pending、persistent、detached、原始证据指针和其他类型在 add/autoflush 前统一以 `ValueError` 拒绝。
- `append_batch` 使用该方法存储新 `Evidence`。保留原 `raw_value`、时间、单位和选择来源；覆盖元数据注明 `payload_hash_basis=persisted_json_v1`。不引入浮点容差或人为舍入。
- `create_manifest` 中，`evidence` 成员仍引用原始证据哈希，不能改成成员的小型来源元数据哈希。帧与时序窗口成员先完成持久化封存，再用实际成员哈希构建覆盖清单；覆盖成员封存后，其实际哈希作为 `EvidenceManifest.manifest_sha256`。
- 新冻结前校验候选原始证据的实际内容哈希。不符者不进入新清单，加入通用 `evidence_payload_checksum_unverifiable` 缺口，不泄露隐藏字段、不称为健康，也不误报为数量截断。必需证据不符返回 409。旧记录保持原样，不自动修补或推断其被篡改。
- 锁定查询同时刷新已加载的原始证据及指标帧，避免 `expire_on_commit=False` 的会话仍持有入库前 JSON。使用批查询的 `populate_existing`，不逐行追加刷新请求；冻结内容以本次数据库读回为准。
- 本轮只修改这条 Zabbix 采集与冻结路径；退休的直接 EXPLAIN 路径和旧报告的原有读取方式不作范围扩展。

## 验证与发布条件

程序回归覆盖新行、旧行拒绝、失败回滚、证据指针、时序成员和覆盖清单的一致性，以及不可核验证据的缺口与必需引用拒绝。实际 MySQL 回归使用单条固定连接的临时影子表，先确认全部临时表就绪，再允许测试写入；禁止永久 DDL、永久业务写、连接漂移或服务操作。仅测试普通数值批次，不带 cursor、events、capabilities 或 statement_digest 的附带写入。

新增测试先在旧实现失败，再在新实现通过；执行相关及完整后端回归、独立审查后才封存候选。公开 08 与采集后台在此之前继续运行。部署沿用候选、后台单实例交接及网关回退流程；提前一分钟通知，复查公开 HTTP、只读拒写和新采集哈希。浏览器视觉验收受工具连接问题限制时继续明确标注。

对应 FR-022～025 的事实与冻结证据边界；本轮不调用模型、不增加配额、不开放 A，也不把原 AI 语义 FAIL 改为通过。

参考：[SQLAlchemy refresh](https://docs.sqlalchemy.org/en/20/orm/session_api.html#sqlalchemy.orm.Session.refresh)、[MySQL JSON](https://dev.mysql.com/doc/refman/8.4/en/json.html)。具体末位差异以本机实际回归为依据。
