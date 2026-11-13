# Control MySQL Single Attempt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox syntax for tracking.

**Goal:** 以隔离替身证明互斥、启动前重检、一次启动及有界等待，不改现有服务。

**Architecture:** 独立工程函数复用mysql_recovery.evaluate；可信回调提供观测/固定启动，私密排他锁与不可覆盖意图防止重复尝试。没有平台接口、命令执行实现或实际恢复适配器。

**Tech Stack:** Python标准库、pytest。

**Spec:** [设计](../specs/2026-11-13-control-mysql-attempt-design.md)

## Global Constraints

不停止当前控制库；不初始化、不接AI工具、不改生产式运行配置。锁与意图不自动清除，回调必须有自身超时。完整自启动仍WAIT。

## Review Focus

- 观测间外部进程已启动：不重复启动。
- 同状态目录并发与残留锁：忙而不接管。
- 启动报错或就绪超时：意图保留，不重放。
- 配置/UUID/端口变化：最早检查拒绝。
- 超时、回调异常、锁文件替换：无私密异常泄漏，不删除他人锁。

## Task 1: Single attempt orchestrator

**Files:** 新增`scripts/platform/mysql_recovery_attempt.py`及`tests/platform/test_mysql_recovery_attempt.py`。

**Interfaces:** `run_attempt(contract, *, state_dir, observe, start, timeout_seconds=20, poll_seconds=1, monotonic, sleep, utcnow)`；`observe(timeout_seconds=remaining)`返回既有判定器观测，`start(timeout_seconds=remaining)`仅固定能力、不接命令参数。输出状态、原因码、启动尝试次数。

- [ ] 写健康、检查间竞态、并发、未就绪、冲突、超时、未知结果及意图保留测试；先证实失败。
- [ ] 最小实现互斥、锁内重检、意图落盘和有界等待。
- [ ] 跑相关新鲜回归、独立审查并修正重要问题。
- [ ] 仅已审阅范围封存验证及发布源码；更新状态、手册、场景卡和私密CHECKPOINT，明确替身与实际恢复边界。
