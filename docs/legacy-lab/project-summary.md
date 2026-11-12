# 历史实验模块概览

此页记录固定 MySQL 实例实验阶段的技术能力与验证边界。当前平台设计及数据接入方式见 [平台说明](../../README.md)，不以此历史模块代替当前实现。

## 中文

**DB Evidence Lab：MySQL AI 故障诊断与证据工作台（个人实践，2026.11）**

- 使用 Python/FastAPI、MySQL Performance Schema 和 SQLite 实现运维证据工作台，采集实例状态、SQL 累计摘要、索引、事务等待及 Linux 主机信息，保留时间戳和原始证据。
- 接入 DeepSeek 工具调用，模型按需选择锁关系、状态、实验表结构及固定查询 EXPLAIN 四类只读证据；实现参数白名单、调用预算、重复拒绝、审计与有效证据引用校验。
- 实现事务阻塞、缺失索引两条页面诊断闭环：故障保持期间 AI 只读补证，固定程序清理并分别验收；加入健康/缺证据对照，以及真实实验库中的受控模型超时清理测试。
- 实现一致性逻辑备份、独立实例恢复和结构/行数/有序数据 SHA256 校验；在 30,000 行合成订单数据上完成正常恢复、损坏备份失败及再次恢复验证，加入实例 UUID 和目标范围保护。

该模块仅在隔离实验环境验证。没有测量或验收依据时，不宣称生产采用、效率提升比例、节省工时或生产 SLA。

## English

**DB Evidence Lab — AI-assisted MySQL Diagnosis and Evidence Workbench | Personal Project | Nov 2026**

- Built a Python/FastAPI and SQLite operations workbench to retain timestamped MySQL and Linux evidence, including statement digests, indexes and transaction lock waits.
- Integrated DeepSeek tool calling with four allowlisted read-only evidence tools, argument validation, bounded calls, duplicate rejection, audit records and validated evidence references.
- Built reproducible lock-contention and missing-index cases with model-selected read-only evidence collection while faults remain active, deterministic cleanup, and separate acceptance checks. Added healthy/insufficient-evidence controls and injected-model-timeout cleanup tests against the real synthetic database.
- Implemented logical backup, restore to a separate MySQL instance, and schema, row-count and ordered-data SHA256 comparisons. Tested successful restores and intentional backup corruption using 30,000 synthetic order rows, with UUID and target-scope checks.

## 技术设计与常见问题

**为什么不让模型直接执行任意 SQL？** 模型可以选择命名的只读证据工具，但SQL模板、参数校验、权限和执行由程序控制。真实只读账号的UPDATE请求被MySQL拒绝；模型没有凭据、写SQL或Shell入口。报告引用存在仍不代表推理必然正确。

**怎么证明锁问题？** 记录真实 data_lock_waits 与 innodb_trx 关联，能看到等待和阻塞事务、等待语句、表及锁类型；释放阻塞事务后重新采集。累计 SQL 摘要不能替代事务关联。

**怎么证明索引有效？** 主证据是 EXPLAIN 从 ALL 到选择 lab_customer_idx。客户端 15 次查询耗时只是辅助观察，包含 SSH/网络，数据规模很小，不能推断生产收益。

**为什么还要恢复校验？** 文件大小和备份命令成功只说明生成了文件。此项目必须恢复到另一 UUID 实例并比较结构、行数和数据摘要，损坏 SQL 必须在失败阶段退出。

**还有哪些缺口？** 源指纹与备份不是同一个快照事务，需要静止数据；指纹在内存计算；结构校验未覆盖所有对象；单机调度没有高可用、队列、取消和多用户权限。生产化要先解决这些问题。

**网络或磁盘是不是根因？** 当前仅采集基础 Linux 数据，缺少时间序列、设备延迟、网络丢包等证据，因此不能下根因结论。

**AI失败怎么办？** 模型没有修复权。完整案例用finally进入固定清理，索引重建或双事务回滚后再核对。实测用本地超时替身让诊断失败，两种真实故障均被清理；这不是实际服务商故障测试。进程被强杀或数据库不可达仍可能影响DDL清理，原型不承诺生产级恢复。

**AI有没有答错过？** 有。一次历史锁报告建议用SUM_LOCK_TIME推断行锁等待占比，官方字段定义实际是表锁统计。保留原始回答、记录复核，并补充提示约束和正确的TRX_WAIT_STARTED字段。有效引用不等于推理正确，这是保留DBA复核的原因。
