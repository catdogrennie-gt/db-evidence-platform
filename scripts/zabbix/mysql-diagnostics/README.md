# MySQL 诊断补充查询

给 **Zabbix 7.0 MySQL Agent2 官方模板**补充配置、事务、会话、SQL 摘要、表/文件 IO 和日志信息。平台依旧读取 Zabbix API，不恢复自建直连采集入口。

将 `.sql` 文件放进只允许管理员修改的目录，在 Agent2 配置 `Plugins.Mysql.CustomQueriesPath`，使用命名会话保存数据库凭据。例如 `mysql.custom.query[DbA,,,configuration]`；不要把口令放进监控项 key。配置变更先用独立 agent/端口验证，再按运维流程替换。

每个查询固定 SELECT、超时和行数上限。需要 `PROCESS`、`SELECT ON performance_schema.*`；慢日志另需 `SELECT ON mysql.slow_log`。本目录不授予写权限，不自动开启日志。

配置只查询白名单变量；会话不采用户名、客户端地址和 SQL；SQL 只采归一化摘要；日志不采原 SQL、错误正文或请求参数。累计 IO 与语句计数必须结合实例启动/重置时刻做差，不能直接当成本窗口速率。达到 LIMIT 表示可能截断，不代表全量。

慢日志必须已启用且 `log_output` 含 TABLE。未启用、无权限和没有事件分别处理；空结果不代表整个历史时段健康。大量日志应使用增量文件采集（例如 Alloy/Loki），不要无限扫描日志表。Linux 主机资源使用独立的官方 Linux 模板，应用指标与日志使用应用自身来源，登记关联，不伪造每个数据库都有独占物理机。
