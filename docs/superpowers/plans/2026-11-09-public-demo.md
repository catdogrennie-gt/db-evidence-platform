# Public demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 提供可公开分享的受限账号和真实、可停止、有进度的固定MySQL锁等待演示。

**Architecture:** API写入持久化意图和互斥，collection worker旁的独立控制器管理固定Lab连接，不占用采集InstanceLease。Zabbix依旧唯一业务证据来源，分析复用AI队列与冻结报告；直接实验查询只用于安全护栏和清理验证。

**Tech Stack:** FastAPI、SQLAlchemy/Alembic、PyMySQL/Paramiko、React/Ant Design。

**Spec:** `docs/superpowers/specs/2026-11-09-public-demo-design.md`

## Global Constraints

- 全站单任务，180秒最大故障时间、600秒冷却、UTC当日8轮/4次模型；停止不受额度限制。
- 公开身份无通用分析创建/模型外发/配置/敏感读取；目标、命令和SQL不可由浏览器提供。
- 真实测量、演示合成负载、规则和模型输出清晰区分；不发送邮件、不改简历。
- 私密配置、凭据、原始验收资料均在源码外；README仅公开专用演示口令。

## Review Focus

- 两个API和两个worker同时运行仍只注入一轮。
- 进程/网络中断后未知清理不会被标作恢复，也不自动重放注入。
- 关闭早于采样时不提交空证据假诊断；历史报告标明历史。
- 共享账号无法通过通用路由绕开固定场景及费用上限。
- 公网轮询显示真实阶段、失败和恢复；不会依赖浏览器维持故障TTL。

## Tasks

### 1. Durable demo backend
Files: `platform_app/demo.py`, `routes/demo.py`, `models.py`, `config.py`, `access.py`, `api.py`, `worker.py`, `migrations/versions/0007_demo.py`, `tests/platform/test_demo.py`.
- [ ] 先测试权限、全站互斥、限额、过期/停止/恢复、旧证据拒绝与模型失败。
- [ ] 实现独立控制器，复用受限服务身份 create_investigation；transport按spec接口注入依赖。
- [ ] 更新迁移/ready检查，运行相关Python测试与回归。

### 2. Fixed Lab transport and private provisioning
Files: `platform_app/demo_fault.py`, `tests/platform/test_demo_fault.py`, `scripts/platform/prepare_demo.py`；私密运行配置在源码外。
- [ ] 先测试固定目标身份、只回滚、看门狗/超时、连接关闭、数据不变与故障未知。
- [ ] 实现经SSH认证的固定Lab连接和专用低权限账户，不启用旧自定义采集。
- [ ] 准备精确instance授权的demo与runner，核对真实Zabbix采样可达。

### 3. Demo page
Files: `frontend/src/Demo.tsx`, `App.tsx`, `styles.css`, `frontend/tests/demo.test.mjs`, nav相关测试。
- [ ] 先测试不可选目标/SQL、只读/忙碌/冷却/故障未知/失败与历史报告。
- [ ] 实现开关、真实阶段、倒计时、限额、报告与引用，轮询保留快照但错误明确。
- [ ] 运行frontend测试/typecheck/contracts/build。

### 4. Integration, review, publication
Files: README、`docs/demo-guide.md`、`docs/acceptance-public-demo.md`、真实截图；父目录CHECKPOINT。
- [ ] 真实开启、取证、模型、停止、数据/会话清理和Zabbix恢复闭环；浏览器走公开账号，不增自动巡检预算。
- [ ] 独立审查并修复，运行相关完整回归、源码隐私检查与文档链接检查。
- [ ] 更新README演示入口/账号/限制/截图，发布经检查的源树至GitHub并核对。
