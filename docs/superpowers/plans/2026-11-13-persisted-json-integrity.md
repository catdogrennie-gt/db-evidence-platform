# 持久化 JSON 哈希一致性实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新采集证据和新清单的内容哈希能够按实际持久化 JSON 重算。

**Architecture:** 保持现有 JSON 列与 digest 算法，在同一写事务内刷新新 payload 后封存哈希；只纳入可核验的原始证据。已提交的旧记录不改写。

**Tech Stack:** Python、SQLAlchemy 2.0、MySQL、pytest。

**Spec:** [设计说明](../specs/2026-11-13-persisted-json-integrity-design.md)。用户已委托持续改进和实现选择，依既有执行方式由协调者实现共享代码、独立贡献者编写测试并审查。

## Global Constraints

- 不改 schema、依赖、全局 digest、配额或旧数据，不人为舍入、不引入哈希容差。
- 仅新行允许封存；证据指针成员继续引用原始证据哈希。
- 新清单明确不可核验缺口；必需原始引用不符则 409，不能静默遗漏。
- MySQL 测试仅用一条绑定连接上的临时影子表；执行前写守卫、固定 CONNECTION_ID、无永久 DDL/DML。
- 公开 08、A 维护状态和已有后台保持可用，候选验收前不切换。

## Review Focus

- SQLAlchemy identity map 仍保留入库前数值：真实刷新后再哈希。
- flush 涉及其他待写对象：测试会话起始无 pending/dirty/deleted，真实入口保留原事务与 fencing。
- ManifestItem 的原始证据指针与自身 payload 哈希语义不同：分别处理。
- 数据库回读改变窗口成员内容：覆盖清单必须使用已封存成员的哈希。
- 临时表或连接未固定导致真实写入：影子表全部就绪及执行前守卫为测试硬门槛，失败不得降级。

### Task 1：新行封存与原始证据写入

**Files:** Modify `platform_app/evidence.py`; Create `tests/platform/test_persisted_json_integrity.py`.

**Interfaces:** `_persist_new_payload(db, row)` 返回新封存行，旧行拒绝；`append_batch` 对外签名保持。

- [x] 写回归：新行内容/哈希一致、原 raw_value 不变、旧行不得重封存、回滚无残留；真实 MySQL 精确浮点样本先 RED。
- [x] 用绑定临时表连接运行真实 RED；保留首次失败，不对旧数据“修正”以通过。
- [x] 实现新行同事务插入、刷新 payload、哈希封存；更新 append_batch 并记录新哈希依据。
- [x] 运行新回归和相关采集/权限/事务回归，确认 SQL 更新不包含第二次 payload 写入。

### Task 2：清单成员与覆盖清单封存

**Files:** 同一产品文件和测试文件。

**Interfaces:** `create_manifest` 签名不变；原始成员仍用 Evidence 哈希；自含 payload 成员使用实际读回哈希。

- [x] 先写失败回归：成员 JSON 往返、覆盖清单引用实际哈希、清单摘要等于实际覆盖内容、旧无效原始证据形成缺口、无效必需引用 409、数量截断与完整性缺口区分。
- [x] 重排新成员封存与覆盖清单创建顺序；新冻结时过滤不符的旧 raw，完整性判断不修改历史。
- [x] 真实 MySQL 与 SQLite 相关回归通过；验证所有测试写仅发生于已确认的临时表，未向永久业务表写入。
- [x] 完整后端回归、独立代码审查，保存可复现命令与状态。

专项 **24 passed**（20 SQLite、4 真实 MySQL），完整后端 **1144 passed、25 skipped、6 subtests**。审查补充发现的原始证据与指标帧缓存边界，均由实际数据库触发器先重现失败，再以批量 `populate_existing` 修复。旧有效 fixture 仅同步真实摘要，断言不放宽；历史数据不重新封存。

### Task 3：候选、真实新采集及发布

**Files:** 私密运行目录中的封存/切换证明；更新操作手册、验证清单、演进与验收状态。

- [x] 基于 08 封存唯一代码候选，候选 API 验证；前端资源不变。
- [x] 提前一分钟通知，按现有单实例与 fencing 流程交接采集及 AI，再切 API；保留精确回退。
- [x] 只读核对自然新采集 payload 与哈希，实际 MySQL 临时表核对新清单；旧报告保持原样，公开 HTTP/资源/拒写检查通过。
- [x] 更新手册和实际验证状态，原模型分析和跳过案例不升级为通过。

09 仅替换 08 的 `evidence.py`，另外 89 个封存文件不变。2026-11-12 22:10:19～22:10:32 UTC 完成公网切换，26 项主链与随后 17 项三站检查通过；两后台唯一且心跳递增，临时排期 10 行、11 字段还原无冲突。明确只读事务重新计算自然采集的 40 条证据全部相符，包括仍保留原字符串的浮点表示差异。启动配置同步并保留 08 回退；未重启电脑、未新增视觉验收。

公开源码采用实际公开父提交的精确增量；冻结、完整私密值扫描和推送为独立发布步骤，结果以仓库提交记录为准，不推送工作目录中的私有历史。

本轮原则时间盒：2026-11-13 05:40～06:40 Asia/Shanghai。每 30 分钟继续检查方向及公开页面；关键条件或测试隔离不能满足时保留失败、停止实际写入，不用弱化哈希验证代替修复。
