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

- [ ] 用实际 SQLite 测试账本与派发路径复现：12 次策略普通第 10 次应被拒，异常仍可派发，之后硬总额不能超。
- [ ] 补强制手动、失败/旧版本、跨日、小额度及状态回归，先确认有意义失败。
- [ ] 实现同日普通份额计数及 20% 预留；新增清晰状态标签，保留原权限/队列/幂等边界。
- [ ] 相关调度/只读/展示回归与前端检查，独立审查；干净公开候选完整回归。
- [ ] 更新手册、场景与实际进度，精确范围发布并记录运行部署待验；收尾公网与额度核查。
