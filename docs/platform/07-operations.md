# Windows 本地部署与运维

本指南部署独立平台，包含原 MySQL 直采与新增 Zabbix 自动巡检，不依赖旧实验 runtime/config.json、固定目标、VM、SSH 账号或模型密钥。真实测量来自个人隔离实验，不代表生产采用。新增链路见[本轮实际验收](../acceptance-zabbix.md)，原平台记录见 [06-acceptance-status.md](06-acceptance-status.md)。

新部署生成器的默认 API/UI 端口为 **18080**，独立控制 MySQL **13306**，Prometheus **9090**，采集 worker 指标桥 **9120**；并行运行时须选择自己的不冲突配置。浏览器入口以私密配置的 public_origin 为准。旧 start.ps1 的 8765 实验应用不是本平台入口。目标地址、UUID、口令及私密报告不写入本指南。

## 1. 软件和目录

| 内容 | 位置 |
| --- | --- |
| 可发布源码 | 本仓库，含锁文件、前端、迁移和文档 |
| 私密运行目录 | 仓库外的新绝对目录，含配置、密钥、凭据、控制库、TSDB、日志、PID、导出 |
| 官方组件 | 单独安装目录，由参数指定，不复制到源码 |
| 私密备份 | 另一受保护位置，保留匹配的控制库、主密钥和凭据 |

需要 Windows、Python **3.12**、Node **22.12+** 或支持 Vite 8 的兼容版本、npm、PowerShell。参考环境组件为 **MySQL Community 8.4.13 LTS Windows x64** 和 **Prometheus 3.13.5 Windows amd64**，不表示“最新版本”。从 [MySQL 官方下载页](https://dev.mysql.com/downloads/mysql/8.4.html) 和 [Prometheus 官方下载页](https://prometheus.io/download/) 获取，人工核验版本及官方签名/校验值。生成器不下载或执行组件，不把目录名当版本证明。

以下命令在仓库根目录执行。示例路径需自行选择，私密目录父目录须已存在。先确认端口空闲；冲突时改端口，不停止未知进程。

~~~powershell
$platformRepo = (Resolve-Path .).Path
$platformPrivate = 'D:\Private\DBA-Platform-New'
$platformMysql = 'D:\Components\mysql-8.4.13-winx64'
$platformPrometheus = 'D:\Components\prometheus-3.13.5.windows-amd64'
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-platform-lock.txt
Push-Location frontend
npm ci
npm run build
Pop-Location
.\.venv\Scripts\python.exe scripts/platform/prepare_local.py --private-dir $platformPrivate --mysql-dir $platformMysql --prometheus-dir $platformPrometheus
$platformConfig = Join-Path $platformPrivate 'config.json'
$env:DBA_PLATFORM_CONFIG = $platformConfig
~~~

可在生成时提供 --control-port、--api-port、--prometheus-port、--bridge-port；四端口必须不同。默认仅回环绑定，无 SSH、目标和模型外发配置。更改 public_origin 不是公网部署方案；远程访问需要另行完成 TLS、反向代理、身份与网络设计。

生成器在写凭据前将新目录 ACL 限为当前 Windows SID 与 SYSTEM，子目录继承；拒绝仓库内路径、指向仓库的路径以及**任何已有目标目录，包括空目录**。没有覆盖/重置开关。ACL 失败会停止，留下的未完成目录不会自动接管，检查后换新目录重试。

### 新旧版本并行时的目录与服务归属

保留旧源码及其匹配运行目录，在另一个 checkout / worktree、虚拟环境和私密配置中验证新版。新版至少使用独立控制 schema、配置、密钥与凭据目录、日志、PID、导出和应用端口；不要把旧库路径复制成新库，也不要用空平台初始化覆盖原运行数据。

若在同一个已核验 MySQL 服务上建立独立 schema，服务本身仍由其原管理配置负责。新版只管理自己的 API / 采集 / AI 进程，不拿新版 ConfigPath 启停另一环境的 MySQL；Prometheus 服务及 TSDB 也必须有明确归属。完全独立部署则使用自己的数据目录与端口。始终显式传 `-ConfigPath`，不要在并行环境依赖脚本的默认私密目录。

新部署生成自己的主密钥；恢复旧数据必须使用与该备份匹配的原密钥，二者不是同一个操作。历史源码回溯见[已发布历史版本](../releases/legacy-platform-2026.11.08.md)。新版迁移、测试或停启不应改变旧环境的 PID、数据文件和配置。

## 2. 生成物与权限

| 文件 | 用途 |
| --- | --- |
| config.json | 控制库连接、私密绝对路径、回环端口；SSH/TLS/主机/日志 profile 及模型配置为空 |
| fernet-master.key | Fernet 主密钥，丢失后旧密文不可恢复，不可用新密钥替换 |
| secrets/ | 后续保存的加密凭据、EXPLAIN 参数 |
| bootstrap-private.json | 随机 root、admin、可选示例 DBA 凭据，只在本机私密查看 |
| bootstrap-plan.json | 默认仅建控制面管理员，零部门、零系统、零业务数据授权 |
| dba-example-plan.json | **单独选择执行**的示例部门、系统、DBA 授权计划 |
| my.ini | 正常 MySQL 配置：回环、禁用 X Protocol/本地文件导入，**没有 init-file** |
| mysql-first-start.sql | 首次启动 SQL，含随机凭据，成功后移除 |
| mysql-root-client.cnf | 私密维护选项，避免密码出现在命令行 |
| prometheus.yml | 本机指标桥抓取，TSDB 14 天保留由启动参数设置 |
| components.json、DEPLOYMENT-PLAN.txt | 组件参考版本、路径和人工清单 |

不打印这些文件到共享记录，不复制到 issue，不提交 Git 或打进公开包。生成器不输出密码，也不读取旧实验配置。默认 admin 没有 data.read、敏感证据或模型外发权限；默认计划不创建业务资料。

可选 DBA 计划只给示例系统内置 dba 角色，不授全局业务范围、敏感字段、模型外发或实验操作。人工审阅后，明确以该计划运行 bootstrap，并使用另一输出文件；默认初始化不会运行它。新增目标仍须私密监控凭据、预检、注册与激活。

## 3. 首次初始化控制 MySQL

仅针对生成器新建的空 mysql-data 执行；禁止对已有数据目录执行 --initialize-insecure。Windows 在 skip-name-resolve 下按回环 TCP 身份匹配维护账号；模板显式建立该本机身份，只授控制库权限和全局 SHUTDOWN，不授全局 ALL。它与业务目标无关。依据 [官方数据目录初始化说明](https://dev.mysql.com/doc/refman/8.4/en/data-directory-initialization.html) 和 [临时 init-file 说明](https://dev.mysql.com/doc/refman/8.4/en/resetting-permissions.html)：

~~~powershell
& (Join-Path $platformMysql 'bin\mysqld.exe') "--defaults-file=$platformPrivate\my.ini" --initialize-insecure
if ($LASTEXITCODE -ne 0) { throw 'Fresh initialization failed; inspect private logs.' }
$platformFirstArguments = @("--defaults-file=$platformPrivate\my.ini","--init-file=$platformPrivate\mysql-first-start.sql") | ForEach-Object { '"' + $_ + '"' }
$platformFirstProcess = Start-Process -FilePath (Join-Path $platformMysql 'bin\mysqld.exe') -ArgumentList ($platformFirstArguments -join ' ') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $platformPrivate 'logs\mysql-first-stdout.log') -RedirectStandardError (Join-Path $platformPrivate 'logs\mysql-first-stderr.log')
~~~

在私密 MySQL 错误日志确认启动与首次 SQL 全部成功。可重复执行以下 ping 等待就绪。参数只含私密文件路径，无密码；首次 SQL 不是可重入迁移，失败时检查日志，不反复创建用户或删除数据目录。

~~~powershell
& (Join-Path $platformMysql 'bin\mysqladmin.exe') "--defaults-extra-file=$platformPrivate\mysql-root-client.cnf" ping --silent
~~~

ping 只证明服务响应，不能证明首次 SQL 或账号授权成功。下面的只读检查必须成功，且不打印连接信息；未就绪时稍后重试。

~~~powershell
@'
from platform_app.config import load_config
from platform_app.db import create_db_engine
from sqlalchemy import text
try:
    engine=create_db_engine(load_config())
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT 1"))==1
    engine.dispose()
except Exception:
    raise SystemExit("Control login failed; inspect the new private config/logs.") from None
print("Control database login ready.")
'@ | .\.venv\Scripts\python.exe -
if ($LASTEXITCODE -ne 0) { throw 'Control login not ready.' }
~~~

成功后先优雅关闭首次实例，再移除一次性 SQL，按正常配置重启：

~~~powershell
& (Join-Path $platformMysql 'bin\mysqladmin.exe') "--defaults-extra-file=$platformPrivate\mysql-root-client.cnf" shutdown
if ($LASTEXITCODE -ne 0) { throw 'Graceful shutdown failed; preserve init SQL and investigate.' }
if (-not $platformFirstProcess.WaitForExit(30000)) { throw 'First-start process did not exit.' }
Remove-Item -LiteralPath (Join-Path $platformPrivate 'mysql-first-start.sql')
.\scripts\platform\manage.ps1 -Action Start -Services mysql -ConfigPath $platformConfig
~~~

正常启动后再次执行上面的只读登录检查，确认成功后才进行下一节迁移；创建进程不等于数据库已就绪。

只移除明确的新私密目录中的该文件，不递归清理。不把 init-file 放进正常 my.ini，也不让正常启动重复执行首次 SQL。简单删除不等于物理擦除；私密目录应采用受控存储，避免公共同步。

首次空实例初始化失败、尚无业务数据且无法优雅关闭时，应保留失败目录并由操作者另行批准处置。此次 cleanroom 验证曾使用该例外：严格核对 PID、可执行文件、创建时间、完整首次启动参数及新私密 initfile 后停止失败实例，再换新目录重验。它不是现有控制库或业务实例的通用停止策略。

## 4. 显式迁移、身份初始化与启动

本轮迁移在原控制库结构上增加来源及巡检记录，不另建身份或分析数据库：

| Revision | 内容 |
| --- | --- |
| `0005_monitoring` | `monitoring_sources`、`monitoring_mappings`；外部对象 UUID 可未知，原直连注册仍做身份验证 |
| `0006_inspection` | `inspection_policies`、`inspection_dispatches`、`inspection_findings`；关联原用户、实例、事件、调查与任务 |

在目标运行目录的私密配置已核对、控制 schema 已选定后，显式设置配置再迁移。以下不打印配置或数据库口令：

~~~powershell
$env:DBA_PLATFORM_CONFIG = $platformConfig
.\.venv\Scripts\python.exe -m alembic -c migrations/alembic.ini current
.\.venv\Scripts\python.exe -m alembic -c migrations/alembic.ini upgrade head
if ($LASTEXITCODE -ne 0) { throw 'Migration failed; do not start application workers.' }
.\.venv\Scripts\python.exe -m alembic -c migrations/alembic.ini current
.\.venv\Scripts\python.exe -m platform_app.bootstrap --config $platformConfig --plan (Join-Path $platformPrivate 'bootstrap-plan.json') --output (Join-Path $platformPrivate 'bootstrap-identifiers.json')
if ($LASTEXITCODE -ne 0) { throw 'Bootstrap failed.' }
.\scripts\platform\manage.ps1 -Action Start -Services prometheus,api,worker,ai -ConfigPath $platformConfig
.\scripts\platform\manage.ps1 -Action Status -Services mysql,prometheus,api,worker,ai -ConfigPath $platformConfig
~~~

API 不自动迁移或创建用户，bootstrap 不重置已有用户密码。管理员先建组织/系统/用户，按业务范围授权，再接入目标。不要为展示便利给管理员补全局业务权限。空平台不会从旧实验自动发现目标。

本轮 head 为 `0006_inspection`。实际独立新 schema 已迁移到此版本并通过 readiness；全部旧数据形态的原位升级尚未逐项验证。已有环境升级先执行第 6 节的恢复点与停写流程，不重新运行空库初始化。`downgrade` 可能删除巡检记录，且外部对象未知 UUID 会限制 `0005` 回退；恢复旧源码不等于安全回退数据结构。

监控连接不应使用旧实验 operator/root。远端直连验证证书和主机身份；SSH 先人工核验 known_hosts，主机和日志路径由私密 profile 允许。平台不创建目标用户、GRANT、业务库、DDL 或开启日志，缺权限应显示缺口。模型默认空配置，系统默认 rules_only；启用外部模型还需私密供应商配置、系统/实例策略及用户明确授权。AI 不执行建议 SQL，人工方案、审批、执行记录和复核分别留存。

Zabbix 路径在“监控对象”连接只读来源、限制允许主机组、发现并选择已有对象，再在“巡检设置”保存当前负责人的授权对象及策略。无需提供目标 MySQL 口令。确认来源实际成功时间、对象能力与有效历史后再解释结果；模板名或保存成功提示不证明锁 / SQL / 日志已经存在。详细步骤见[操作指南](../user-guide.md)。

## 5. 健康、日志和安全启停

~~~powershell
$platformRuntimeConfig = Get-Content -LiteralPath $platformConfig -Raw | ConvertFrom-Json
$platformOrigin = $platformRuntimeConfig.public_origin
Invoke-RestMethod ($platformOrigin + '/api/v1/health/live')
Invoke-RestMethod ($platformOrigin + '/api/v1/health/ready')
.\scripts\platform\manage.ps1 -Action Restart -Services api,worker,ai -ConfigPath $platformConfig
.\scripts\platform\manage.ps1 -Action Stop -Services api,worker,ai -ConfigPath $platformConfig
~~~

live 只证明 API 响应，ready 检查控制库/迁移，不证明所有依赖或实例健康。登录后检查平台状态页的 worker 心跳、维护错误、队列、失败运行、各来源新鲜度；检查 Prometheus /-/ready、目标 up 及真实窗口。指标桥由采集 worker 持有，重复启动可能端口冲突。

日志在私密 logs/，进程身份记录在 pids/。以请求编号、Task ID、CollectionRun ID 定位，不粘贴配置、驱动原文或秘密。管理脚本按可执行文件、完整参数、模块、配置路径、进程创建时间核验 PID，身份不符即拒绝。不按端口或全部 python.exe 批量杀进程，不伪造 PID 文件接管手工进程。控制 MySQL 使用私密选项优雅关闭，不强杀数据目录写入进程。

采集和 AI 独立队列，重启后靠控制库时钟、租约/fence 收敛中断任务，迟到写入不能覆盖继任者。配置 revision 改变使旧任务失效，UUID 变化隔离资产而非覆盖旧历史。失败后检查真实重试和来源新鲜度，不把缺数当零。

## 6. 备份、恢复与升级

先检查受保护备份位置和空间。停止应用写入后，使用私密客户端选项做控制库一致性逻辑备份；保存源码、迁移、组件版本。TSDB 文件备份先正常停止 Prometheus，或采用经批准快照。一起保存配置、**匹配的**主密钥、secrets/、known_hosts、私密 profiles、所需导出/日志。包含 bootstrap/root 客户端的备份含明文密码，按凭据管理。

先恢复到另一隔离目录/端口，不覆盖现有环境。控制库、加密凭据、密钥必须同一备份链；新密钥不能解密旧凭据。prepare_local.py 是新部署生成器，不是恢复工具。恢复后检查迁移、登录/撤权、解密、冻结 manifest/证据引用，再核对目标身份和来源新鲜度；不重放旧处置。实测恢复前不能宣称备份可用。

升级：保存恢复点 → 停 API/采集/AI 写入 → 新 checkout/venv 安装 Python 锁与 npm ci/build → 审阅迁移并显式 upgrade head → 启动并验证健康、队列收敛和真实采集。恢复源码不等于回滚数据库，迁移不保证可逆；失败时使用验证过的备份。旧 verify_lab.py 等故障演练有独立目标/写权限边界，不用于验证新平台部署。

## 7. 保留、限制与复现

默认证据 14 天、MetricFrame 7 天、业务对象/审计 90 天、Prometheus 14 天，以平台设置及启动参数为准。其中业务对象/审计 90 天为配置策略，依赖感知的物理清理尚未完整实现，平台保留对应 warning。manifest/pin 延长相关证据/帧保留；冻结 TSDB 窗口为有限数据的独立物化副本。保留失败、磁盘不足和缺口应可见，不以清空历史修复。

P0 为 Windows 单机本机入口，未承诺高可用、互联网发布或生产 RTO/RPO。SFTP 指纹无法证明无损轮转，迟到日志超出回看窗口、摘要重置、首帧和过大间隔均保留未知；Prometheus 缺数不补零，锁链消失不自动证明业务恢复。EXPLAIN 仅支持受元数据校验的结构化单表模板，不接受自由 SQL。

~~~powershell
.\.venv\Scripts\python.exe -m pytest tests/platform/test_prepare_local.py -q
.\.venv\Scripts\python.exe -m pytest tests/platform -q
~~~

第一条仅在临时目录验证模板，不启动服务。SQLite、mock、inline 适配器不是后台运行证明。真实 MySQL 并发/迁移检查需显式配置/开关；真实故障、模型外发和目标写操作各自需要批准的隔离范围，不能从“运行测试”推导授权。

本轮真实来源、四身份 HTTP、冻结窗口、新 schema、双事务及多进程派发结果见[验收矩阵](../acceptance-zabbix.md)。其中正常模型周期完成与真实故障模型分析通过分别记录，SQL epoch 缺失时不把累计摘要当成已验证的窗口延迟。
