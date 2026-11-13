# Zabbix anonymous version probe implementation plan

> **For agentic workers:** Use superpowers:executing-plans for this single bounded adapter fix.

**Goal:** 版本探测不再因多带认证头误报不可用，证据读取继续严格认证。

**Architecture:** 仅调整 `platform_app/zabbix.py` 的逐请求头；运行服务继续封存，不直接覆盖。

**Tech Stack:** Python/httpx/pytest。

**Spec:** [认证边界](../specs/2026-11-13-zabbix-version-probe-design.md)。

- [x] 新回归在修复前两项失败：匿名版本与认证历史交替，含复用/不复用连接。
- [x] 最小修复后相关检查 21 项通过；实际 Zabbix 7.0.32 版本、授权主机和历史可读，错误凭据读取主机被拒绝。
- [x] 独立审查无阻断项，审查回归 12 项通过；干净公开候选完整回归 1216 通过、25 跳过、6 子检查通过。源码提交 `d43b7444bf16b4d12ba59436d369cfbdce7a8e30` 已发布并读回。

**部署状态：待部署验证。** 未替换运行服务，命令复核使用候选适配器；API/前端 11、采集 10、AI 15 保持运行。
