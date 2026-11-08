# 复现环境

本机已初始化，无需再次执行初始化。`provision_lab.py` 专供新建的隔离实验环境，已有 `/opt/tdx-lab/3307` 或 `3308` 即拒绝执行；它不是修复或重置现有实例的脚本。

## Linux 准备

使用专用 Linux VM，准备 mysql 系统账号和 MySQL 8.4.5 官方二进制（本次使用 glibc2.17 x86_64 minimal）。在 VM 上先检查发行版依赖：

```sh
id mysql || useradd -r -s /sbin/nologin mysql
mkdir -p /opt/tdx-lab/mysql
tar -xJf /path/to/mysql-8.4.5-linux-glibc2.17-x86_64-minimal.tar.xz -C /opt/tdx-lab/mysql --strip-components=1
ldd /opt/tdx-lab/mysql/bin/mysqld
/opt/tdx-lab/mysql/bin/mysqld --no-defaults --version
```

确保没有缺失的依赖，3307/3308 空闲。不要覆盖已有 MySQL 安装目录。本次仅在 Oracle Linux 7 模板和现有官方压缩包上实际完成了安装；其他发行版尚未验证。

## SSH 与私密配置

先通过可信控制台核对 SSH 主机指纹，将经核验的主机公钥放入控制端 `runtime/known_hosts`。可以用 `ssh-keyscan` 获取候选公钥，但不能省略与 VM 控制台的指纹比较。程序不会自动信任未知主机。

复制 `config.example.json` 为 `runtime/config.json`，填入专用 VM 的 SSH 登录、DeepSeek key 文件绝对路径。首次初始化会生成随机数据库密码并注册实际 UUID，不要把该配置提交到仓库。

```powershell
.\.venv\Scripts\python.exe provision_lab.py
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8765
```

脚本按固定目录新建两个实例，初始化本地 root、实验写账号和只读账号，创建测试库并生成数据。初始化失败可能留下部分目录，必须先检查原因，不自动清空或覆盖。新初始化脚本已做语法检查和“拒绝现有目录”验证；本机最初初始化用等价步骤完成，尚未在第二台空白 VM 上重跑该打包脚本。

## 本机网络约定

当前 VM 文件在用户配置的 `vm_path`，网卡从 VMnet5 切到 VMnet2；原 VMX 备份保存在项目外。VMnet2 宿主地址为 192.168.2.1，VM ens35 添加 192.168.2.132/24 次要地址，原地址保留。两套 MySQL 仅绑定 VM 回环，Windows 通过 SSH 转发连接。

`resume_environment.py` 的 VMware、网卡和 IP 路径为此电脑定制。新环境应自行准备 SSH 可达网络，直接启动 Uvicorn，或调整该恢复脚本。此脚本不修改 Windows 防火墙、不关闭安全保护、不删除 VM。

## 数据与备份

源实例：3307，恢复实例：3308。数据目录、日志、socket 和本地 root client 配置均位于 `/opt/tdx-lab/<port>`；client 配置权限 600。逻辑备份留在宿主忽略提交的 `runtime/*.sql`；远端临时 SQL 在任务结束时清理。负向测试会留下不完整目标库，随后运行正常恢复验证即可重建。

不要将整个 `D:\Codex_WS\TDX_DBA` 打包发布；那里含原始邮件和私密环境脚本。对外仅审阅并分享本子目录已跟踪源码及合成实验报告。
