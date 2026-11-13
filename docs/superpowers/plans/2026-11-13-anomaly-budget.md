# Anomaly budget reservation implementation plan

> **For agentic workers:** Use superpowers:executing-plans for this bounded scheduler change.

**Goal:** 在原日次数内为异常派发保留份额，避免周期检查消耗全部机会。

**Architecture:** `platform_app/intelligence.py` 在已有策略锁下查询 UTC 日账本并限制普通派发；状态投影复用同一计算。无迁移、配置扩权或模型调用。

**Tech Stack:** Python / SQLAlchemy / pytest；React 状态标签。

**Spec:** [分配与边界](../specs/2026-11-13-anomaly-budget-design.md)。

## 验证重点

失败、手动、旧版本均消耗原日额度；跨日不永久阻塞；1 次额度不破坏旧行为；新异常不因普通受限丢失；不因报告完成误显示可派发。

### 单一任务：通用分配与投影

**Files:** 修改 `platform_app/intelligence.py`、`frontend/src/Intelligence.tsx`；新增 `tests/platform/test_inspection_budget_reservation.py`；沿用现有权限和场景回归。

- [x] 实际 SQLite 任务账本与派发路径先复现 9 项失败、4 项通过；未调用模型。
- [x] 强制手动、失败/旧版本、跨日、小额度、权限及状态回归；新 13 项修后通过。
- [x] 同日普通份额计数及 20% 预留，清晰状态标签；原权限/队列/幂等边界不变。
- [x] 相关 75 项、独立审查 58 项、干净公开后端 1229 项通过（25 跳过、6 子检查）；干净前端 98 项和构建通过。首次干净前端两个文件执行失败，单独 28 项与完整重测通过。
- [x] 手册、场景和进度已更新；源码提交 `23c9cc5e15f9d6fb6b311a1e126a6e51747a987e` 已发布读回；API/页面与 AI 后台封存 16 已切换，采集 10 保留，旧版可回退。17:09 公网 HTTP/只读/拒写/历史通过，非浏览器视觉验收。

**待自然验证：** 下一次 UTC 跨日及异常实际派发。今天原有已耗尽次数没有重置，不计作新增真实诊断通过。

## 实际执行角色更正与补验（17:28）

- [x] 查明自动入口在采集后台，上一轮 API/AI16、采集10 不算完整部署；按[交接设计](../specs/2026-11-13-scheduler-rollout-correction-design.md)补齐采集16。
- [x] 首次事务死锁回滚且未交接；核对实际主键/到期索引锁冲突，私密工具改按主键取锁，相关21项通过并独立审查。
- [x] 唯一实际采集进程、两次心跳、12条调度时间安全还原，无冲突、无故障操作；17:28自然状态已持久化异常预留、账本仍11/12。
- [x] 17:29公开A/B、历史和拒写通过；网关启动文件与当前16同步，未重载运行服务。
- [ ] 下一次普通周期到期拒派、UTC跨日、真实新异常派发分别待验；不强制触发、不改日账本。
- [ ] 完整启动/恢复入口和电脑重启路径尚未验收；旧桌面入口仍指历史部署。
