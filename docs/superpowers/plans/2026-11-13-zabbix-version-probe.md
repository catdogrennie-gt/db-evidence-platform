# Zabbix anonymous version probe implementation plan

> **For agentic workers:** Use superpowers:executing-plans for this single bounded adapter fix.

**Goal:** 版本探测不再因多带认证头误报不可用，证据读取继续严格认证。

**Architecture:** 仅调整 `platform_app/zabbix.py` 的逐请求头；运行服务继续封存，不直接覆盖。

**Tech Stack:** Python/httpx/pytest。

**Spec:** [认证边界](../specs/2026-11-13-zabbix-version-probe-design.md)。

- [ ] 新回归在修复前失败：匿名版本与认证历史交替，含复用/不复用连接。
- [ ] 最小修复通过相关运输边界测试，实际 Zabbix 只读命令检查。
- [ ] 独立审查、干净公开候选完整回归及源码发布；记录部署是否待验。
