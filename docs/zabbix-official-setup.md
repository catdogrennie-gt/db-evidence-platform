# 官方 Zabbix 模板与只读 MySQL 诊断扩展

本文说明如何准备已有 Zabbix 数据，再让 DB Evidence Platform 选择和联合分析这些数据。参考组合为 **Zabbix 7.0.32、原生 Zabbix Agent 2 7.0.32、Ubuntu 24.04、MySQL 8.4**。示例主机名、路径及 JSON 都是安装说明，不是实际测量；仓库不提供本机密码、Token、目标地址或私密运行文件。

部署分成两部分：保留官方模板原样，采集常规主机、数据库指标；另建独立扩展模板，补充锁链和 SQL 摘要。扩展不是官方模板的一部分，平台也不负责执行数据库变更。

## 1. 先让官方模板有实际历史

1. 在目标 Linux 系统原生安装 Agent 2。使用 [Zabbix 官方仓库](https://repo.zabbix.com/zabbix/7.0/ubuntu/)，核对发行版、架构、版本及软件包校验信息，不只按文件名判断来源。本轮复现参考包为 Ubuntu 24.04 amd64 的 `zabbix-agent2=1:7.0.32-2+ubuntu24.04`；其他系统使用对应官方包。
2. 确认 Zabbix server 或 proxy 能访问 agent 的受限监听地址，agent 的 `Server` 允许列表只包含必要采集端。若 Zabbix 运行在容器里，容器的 `127.0.0.1` 是容器自身；应使用实际可达的 agent 地址。不要为解决路由问题把 MySQL 无限制发布到宿主机。
3. 在 Zabbix 建立或选择 Linux host，绑定 **Linux by Zabbix agent**；再建立代表 MySQL 实例的逻辑 host，绑定 **MySQL by Zabbix agent 2**。一个原生 agent 可以服务这两个逻辑 host；这不表示创建了两台服务器。也可以按自己的对象组织方式把两套模板放到同一 host，再在平台勾选项目。
4. 按官方模板说明配置 MySQL 监控账号和 named session，等待发现规则、主项和依赖项实际产生历史。

官方 [Linux 模板 7.0.32](https://raw.githubusercontent.com/zabbix/zabbix/7.0.32/templates/os/linux/template_os_linux.yaml) 要求 agent 7.0 或以上；[MySQL Agent 2 模板 7.0.32](https://raw.githubusercontent.com/zabbix/zabbix/7.0.32/templates/db/mysql_agent2/template_db_mysql_agent2.yaml) 明确要求监控 MySQL 8.4 时使用 Agent 2 **7.0.22 或以上**。本轮选用 7.0.32，未重写这些官方模板。

### MySQL 账号、named session 与 TLS

由数据库管理员在受控位置创建专用监控账号，限制实际连接源。官方模板所需权限及本扩展增加的权限如下；替换示例账号和来源，不使用业务管理员账号：

```sql
GRANT REPLICATION CLIENT, PROCESS, SHOW DATABASES, SHOW VIEW
    ON *.* TO 'zbx_db_monitor'@'<AGENT_SOURCE_ADDRESS>';
GRANT SELECT ON performance_schema.*
    TO 'zbx_db_monitor'@'<AGENT_SOURCE_ADDRESS>';
```

第二条仅用于本文三个扩展项。不要授予业务表 DML、全局 `ALL` 或任意执行能力。权限仍应按 MySQL 版本、组织策略及实际所需字段审核；`PROCESS` 和诊断元数据可能涉及多个会话，并不是无敏感信息。

按 [官方 MySQL 入门](https://www.zabbix.com/documentation/7.0/en/manual/quickstart/monitor_mysql)和 [Agent 2 MySQL 插件参数](https://www.zabbix.com/documentation/7.0/en/manual/appendix/config/zabbix_agent2_plugins/mysql_plugin)，在 agent 的受保护配置中建立名为 `OperationsDB` 的 session，设置 `Uri`、`User`、`Password`。Zabbix host 宏 `{$MYSQL.DSN}` 指向 `OperationsDB`，`{$MYSQL.USER}` 和 `{$MYSQL.PASSWORD}` 留空。这样不需要把口令写入可见 item key。配置与口令文件放在源码之外，仅允许管理员写、agent 服务账号读；Linux 可使用 `root:zabbix`、`0640`，并核对父目录的访问权限。

真实部署的 session 应配置 `TLSConnect=verify_full` 及正确的 `TLSCAFile`，证书名称或 IP 与连接地址相匹配；需要双向认证时再配置客户端证书和私钥。Agent 2 与 MySQL 的 TLS、agent 与 Zabbix server/proxy 的传输保护是两条链路，应分别配置。参数的准确拼写和取值见 [7.0.32 官方插件配置](https://raw.githubusercontent.com/zabbix/zabbix/7.0.32/src/go/conf/zabbix_agent2.d/plugins.d/mysql.conf)。

### 如何判断采集接通

在 Zabbix Latest data 中核对主机接口可用性、item 的启用/支持状态、单位、实际采样时间和数值。至少观察两轮：CPU、可用内存、文件系统使用率、MySQL 状态、连接数和查询速率应有时间推进的历史。主机注册成功、模板绑定成功或 `lastvalue=0` 不能单独证明采集成功。

保留以下官方语义：

- **主项 `history=0`**：例如 MySQL `mysql.get_status_variables[...]` 可以持续执行并供依赖项预处理，同时不保存自身原始历史。平台应选有历史的依赖数值项；不能把空主项历史解释为数据库未采集，也不能为填图而改写官方主项。
- **依赖项 `delay=0`**：表示由主项的新值驱动，并不是每零秒执行一次。实际节奏需追溯主项；例如文件系统项由发现、主项及中间依赖项形成链路。[Zabbix 依赖项说明](https://www.zabbix.com/documentation/7.0/en/manual/config/items/itemtypes/dependent_items)
- **丢弃未变化值与 heartbeat**：部分配置、计数或容量项在值不变时只按 heartbeat 保存记录。历史稀疏不能一概判定采集掉线；也不能把稀疏点补零或冒充连续采样。[预处理说明](https://www.zabbix.com/documentation/7.0/en/manual/config/items/preprocessing)
- **冷启动与计算基线**：速率项通常需要前后值；官方 `mysql.innodb_log_file_size` 的计算引用一小时前的日志写入值，新 host 在形成所需历史前可能 unsupported。应保留原因和基线缺口，不改公式、不回填假历史。本轮验收没有把全部已发现项目称为全部健康。

## 2. 独立扩展模板的三项契约

新建独立模板，例如 **DB Evidence MySQL diagnostics extension**，与官方 MySQL 模板同时链接到目标 host。以下三项是本轮实际采用的扩展形态；所有 item 都是 **Zabbix agent，被动式，API `type=0`**，不是 trapper，也不是模型执行 SQL。

| Key | 更新间隔 | 数据类型 / API `value_type` | 单位 | 输出 |
| --- | --- | --- | --- | --- |
| `dbops.mysql.lock_waits` | `30s` | Numeric unsigned / `3` | `count` | 当前实例锁等待数量 |
| `dbops.mysql.lock_snapshot` | `30s` | Text / `4` | 无 | 当前锁链 JSON |
| `dbops.mysql.statement_digest_snapshot` | `60s` | Text / `4` | 计时字段为 `ps` | 规范化 SQL 摘要 JSON |

三项均配置 `history=7d`、`trends=0`、不丢弃未变化值。诊断数据保留期应按自己的容量和敏感度调整。本轮数值项使用 tag 表示 `count`；不依赖 Zabbix 自定义显示单位进行换算。

共同 tags：

| Tag | Value |
| --- | --- |
| `source` | `performance_schema` |
| `extension` | `db-evidence-readonly` |
| `dbops.evidence_kind` | 对应 `lock_waits`、`lock_snapshot` 或 `statement_digest_snapshot` |
| `component` | 两个锁项为 `locks`；摘要项为 `sql` |

附加 tags：

- `dbops.mysql.lock_waits`：`sl.unit=count`。
- `dbops.mysql.lock_snapshot`：`sl.snapshot_scope=current_observation`。
- `dbops.mysql.statement_digest_snapshot`：`sl.field_unit.SUM_TIMER_WAIT=ps`、`sl.field_unit.MAX_TIMER_WAIT=ps`、`sl.snapshot_scope=counter_epoch_unverified`。

这些 tags 为检索、单位说明及已有证据投影提供线索，不替代 item 身份、数据库权限或数据来源校验。用户仍可自由组合所需 host/item；未知 tag 或新应用指标不应仅因缺少预设类别而丢弃。

### 固定查询与可手工重建的采集器

下面是一份与本轮三类查询及 JSON 契约对应的手工安装范例。它采用验证 CA 与目标身份的 TLS 默认值；**不是本轮 Lab 的私有运行脚本副本**。本轮隔离 Lab 的扩展连接使用既有自签名证书，实际验证了加密而未验证证书身份，这个限制保留在验收中，不能作为下面安装范例的默认设置。

在 agent 系统准备 Python 3 和隔离依赖环境，例如将 `PyMySQL==1.1.2` 装入 `/opt/db-evidence-zabbix-ext/.venv`。把以下脚本保存为 `/opt/db-evidence-zabbix-ext/mysql_diagnostics.py`，由管理员拥有且服务账号不可写：

```python
import argparse
import datetime
import json
import ssl
import sys
from pathlib import Path

import pymysql

QUERIES = {
    "lock_waits": """
        SELECT COUNT(*) AS count
        FROM performance_schema.data_lock_waits
    """,
    "lock_snapshot": """
        SELECT
            CAST(w.REQUESTING_ENGINE_TRANSACTION_ID AS CHAR) AS waiting_trx_id,
            CAST(w.BLOCKING_ENGINE_TRANSACTION_ID AS CHAR) AS blocking_trx_id,
            waiting.PROCESSLIST_ID AS waiting_thread_id,
            blocking.PROCESSLIST_ID AS blocking_thread_id,
            l.OBJECT_SCHEMA AS object_schema, l.OBJECT_NAME AS object_name,
            l.LOCK_TYPE AS lock_type, l.LOCK_MODE AS lock_mode
        FROM performance_schema.data_lock_waits w
        LEFT JOIN performance_schema.threads waiting
            ON waiting.THREAD_ID = w.REQUESTING_THREAD_ID
        LEFT JOIN performance_schema.threads blocking
            ON blocking.THREAD_ID = w.BLOCKING_THREAD_ID
        LEFT JOIN performance_schema.data_locks l
            ON l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID
        ORDER BY w.REQUESTING_ENGINE_TRANSACTION_ID
        LIMIT 51
    """,
    "statement_digest_snapshot": """
        SELECT DIGEST, SCHEMA_NAME, LEFT(DIGEST_TEXT, 500) AS DIGEST_TEXT,
            COUNT_STAR, SUM_TIMER_WAIT, MAX_TIMER_WAIT,
            SUM_ROWS_EXAMINED, SUM_ROWS_SENT, FIRST_SEEN, LAST_SEEN
        FROM performance_schema.events_statements_summary_by_digest
        WHERE DIGEST IS NOT NULL AND SCHEMA_NAME = 'lab_orders'
        ORDER BY SUM_TIMER_WAIT DESC
        LIMIT 10
    """,
}

parser = argparse.ArgumentParser()
parser.add_argument("kind", choices=QUERIES)
kind = parser.parse_args().kind
try:
    cfg = json.loads(Path("/etc/db-evidence/mysql-monitor.json").read_text())
    password = Path(cfg["password_file"]).read_text().strip()
    tls = ssl.create_default_context(cafile=cfg["ca_file"])
    with pymysql.connect(
        host=cfg["host"], port=int(cfg.get("port", 3306)),
        user=cfg["user"], password=password, ssl=tls,
        connect_timeout=3, read_timeout=5, write_timeout=3,
        autocommit=True, cursorclass=pymysql.cursors.DictCursor,
    ) as db:
        with db.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            observed = datetime.datetime.now(datetime.timezone.utc).isoformat()
            cursor.execute(QUERIES[kind])
            rows = cursor.fetchall()
    if kind == "lock_waits":
        print(rows[0]["count"])
    else:
        payload = {
            "schema_version": 1, "collected_at": observed,
            "source": "performance_schema." + (
                "data_lock_waits" if kind == "lock_snapshot"
                else "events_statements_summary_by_digest"
            ),
            "rows": rows[:50] if kind == "lock_snapshot" else rows,
        }
        if kind == "lock_snapshot":
            payload.update(
                scope="current_observation", row_limit=50,
                producer_contract="db-evidence-lock-snapshot-v2",
                snapshot_complete=len(rows) <= 50,
                snapshot_truncated=len(rows) > 50,
                total_row_count=len(rows) if len(rows) <= 50 else None,
            )
        else:
            payload.update(
                timer_unit="picoseconds", cumulative_counters=True,
                counter_epoch=None,
                scope="since_server_start_or_last_digest_reset",
            )
        print(json.dumps(payload, default=str))
except Exception as error:
    print("Read-only diagnostic unavailable: " + type(error).__name__, file=sys.stderr)
    raise SystemExit(1)
```

`lab_orders` 是公开实验库名。实际部署应由操作者在安装时审查并修改这个固定 schema 范围；不要增加从模型、API、item key 或用户输入接收任意 SQL、schema 或 shell 的入口。锁查询多读一行检测截断，只返回前 50 行；摘要仍限前 10 条，不能当作全量语句/事务清单。

为 `dbops.mysql.lock_snapshot` 增加 tag `sl.snapshot_contract=db-evidence-lock-snapshot-v2`，并使用上面的明确完整性字段。平台会区分“当前锁快照完整”和“历史查询窗口完整”：历史只取最近几条造成的截断缺口继续保留，不能改写成全窗口完整。固定演示只有在 tag、生产者契约、行数、来源、host/item、映射版本及时间均核对通过时才允许据此触发分析或确认监控恢复；旧输出缺少这些字段时不自动升级为完整证明。

在源码之外创建 `/etc/db-evidence/mysql-monitor.json`，仅填自己的部署值。示例没有口令：

```json
{
  "host": "db.example.internal",
  "port": 3306,
  "user": "zbx_db_monitor",
  "password_file": "/etc/db-evidence/mysql-monitor.password",
  "ca_file": "/etc/db-evidence/mysql-ca.pem"
}
```

分别在受保护口令文件中保存随机口令，在 CA 文件中保存可信 CA；配置和口令可用 `root:zabbix`、`0640`，目录需允许 agent 服务账号遍历。不要将文件提交、显示到日志或作为命令行参数。证书或主机名校验失败时修正证书、CA 或连接地址，保留失败，不把 TLS 校验关闭作为通用修复。

按 [Zabbix UserParameter 文档](https://www.zabbix.com/documentation/7.0/en/manual/config/items/userparameters)，在 agent 已包含的配置目录增加三个**无参数** key：

```ini
UserParameter=dbops.mysql.lock_waits,/opt/db-evidence-zabbix-ext/.venv/bin/python /opt/db-evidence-zabbix-ext/mysql_diagnostics.py lock_waits
UserParameter=dbops.mysql.lock_snapshot,/opt/db-evidence-zabbix-ext/.venv/bin/python /opt/db-evidence-zabbix-ext/mysql_diagnostics.py lock_snapshot
UserParameter=dbops.mysql.statement_digest_snapshot,/opt/db-evidence-zabbix-ext/.venv/bin/python /opt/db-evidence-zabbix-ext/mysql_diagnostics.py statement_digest_snapshot
```

确认配置 `Include` 实际覆盖此文件并重启 agent。先以 agent 服务账号测试脚本三种模式，再使用 `zabbix_agent2 -t <key>` 或已授权采集端的 `zabbix_get` 核对；随后在 Zabbix 独立扩展模板中按上述 item/tag 表逐项创建并绑定。模板/主机/监控项 ID 由自己的 Zabbix 生成，不复用本机验收 ID。查询错误输出诊断失败并非零退出，不能捕获异常后输出 `0` 或 `{"rows":[]}` 来冒充正常。

### JSON 与单位语义

锁快照结构示例，`rows` 也可能为空；下例仅说明外形：

```json
{
  "schema_version": 1,
  "collected_at": "2026-01-01T00:00:00+00:00",
  "source": "performance_schema.data_lock_waits",
  "scope": "current_observation",
  "row_limit": 50,
  "rows": []
}
```

非空行包含 `waiting_trx_id`、`blocking_trx_id`、`waiting_thread_id`、`blocking_thread_id` 及对象/锁元数据。这里两个 `*_thread_id` 实际来自 **`performance_schema.threads.PROCESSLIST_ID`**，即连接/进程列表 ID；不是 `performance_schema.THREAD_ID`。查询用内部 THREAD_ID 关联 threads 后再输出 PROCESSLIST_ID。事务 ID 是另一类标识，不与连接 ID 混用。[MySQL threads 字段](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-threads-table.html)、[data_lock_waits 关系](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-data-lock-waits-table.html)

SQL 摘要外形：

```json
{
  "schema_version": 1,
  "collected_at": "2026-01-01T00:00:00+00:00",
  "source": "performance_schema.events_statements_summary_by_digest",
  "timer_unit": "picoseconds",
  "cumulative_counters": true,
  "counter_epoch": null,
  "scope": "since_server_start_or_last_digest_reset",
  "rows": []
}
```

实际行使用上面 SELECT 的列名。`SUM_TIMER_WAIT` 和 `MAX_TIMER_WAIT` 为皮秒，换算秒除以 `1e12`，毫秒除以 `1e9`；`COUNT_STAR` 是累计次数。服务重启或摘要重置都可能改变计数基础，当前未确定重置 epoch，因此保留 `null`，不填字符串 `unknown` 或当前时间作为有效 epoch。仅两个时间戳不足以证明两个摘要计数可安全差分。

`MAX_TIMER_WAIT` 是累计范围内最大值，不能称作某个分析窗口的最大耗时；`FIRST_SEEN` / `LAST_SEEN` 也不是连续日志或每次执行记录。规范化摘要不保留业务字面值，但仍可能包含 schema、对象名等内部元数据，应按权限展示和外发。[MySQL 语句摘要说明](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-statement-summary-tables.html)

## 3. 接入联合分析并验收

在平台的来源连接中使用只读 Zabbix API 账号/Token，限制允许的 host group；不需要把 MySQL 写凭据交给平台。发现 host 后先查看项目名、key、单位、tags、支持状态、实际采样/历史，再勾选联合分析范围。例如选 MySQL 的连接数、查询速率和锁证据，同时选相关 Linux host 的 CPU、内存和文件系统指标；应用指标也可以按同样方式加入，不用伪装成 MySQL 项。

联合范围是用户登记的分析关联，不能仅凭两条曲线同时波动证明因果。原生 Linux 指标观测的是该 Linux 系统，若同机运行数据库、应用与监控组件，CPU 等值不能直接全部归因于数据库。

建议按以下顺序验收：

1. 在 Zabbix 看到两轮真实新历史，并检查所有所选项的 unsupported、禁用、保留期和 heartbeat，而非只统计已发现数量。
2. 使用平台相同的只读 API 权限确认新 host/item 可见、数值历史与 Text JSON 均可取；文本项不会自动拥有数值趋势。
3. 对照原始 JSON 核对锁边方向、连接 ID、观察时间，核对 SQL 单位与未知 epoch。采集失败和空锁快照应能区分；锁消失只说明新样本未再看到该等待，不自动证明业务恢复。
4. 启用有界巡检，检查被冻结的证据和图表是否属于所选对象及窗口，再核对 AI 引用。模型完成状态不等于根因已确认。控制故障只在明确登记的独立 Lab 中另行执行；安装本扩展不运行故障或修改业务数据。

已有的 [SignalLens-Lab 固定版本采集脚本](https://github.com/catdogrennie-gt/SignalLens-Lab/tree/ceb08451dbdd2e6f1df128fb9e3c55e63862e7d3/lab)可作为独立实验的兼容替代，其 `sl.*` 项通过 trapper 发送实际样本。它与本轮原生官方 agent 的 `dbops.mysql.*` 被动查询路径不同；不能把使用 Lab trapper 说成安装了官方模板，也不能直接忽略独立 Lab 的目标保护与清理条件。

[平台操作指南](user-guide.md) · [平台部署与运行](platform/07-operations.md) · [本轮联合对象验收](acceptance-analysis-groups.md) · [上一轮验收](acceptance-zabbix.md)
