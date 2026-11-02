# 日志排查 MCP · Python 第一版

图形安装版：双击 `LogTools-Setup-0.2.3-x64.exe` 离线安装，可手动新增、编辑项目名称/服务器地址/端口，也可“一键导入服务器配置”带入多个日志项目，之后在界面设置自己的凭据。无需 known_hosts、主机指纹或管理员确认。详见 [图形版使用与配置导入](docs/desktop.md)。

## 分享与新机器安装

发送方运行 `pack.cmd`，把 `dist/` 下生成的 ZIP 和 SHA256 文件交给接收方。接收方完整解压后运行 `setup.cmd`，自动准备 Python 3.13 x64、虚拟环境和依赖，再按提示配置 SFTP。需要 Windows 10/11 x64；分享包不包含日志、真实配置或凭据。

接收方不能访问外网时，发送方运行 `pack.cmd -Offline`，生成包含 Python 安装器和全部依赖的离线包。接收方仍运行 `setup.cmd`，会自动使用本地文件；完全断网时可先运行 `setup.cmd -Offline -SkipConfigure`，之后接通公司网络再配置 SFTP。

后续添加服务器可运行 `configure.cmd`。详细准备事项、命令与故障处理见 [一键打包与新机器配置](docs/distribution.md)；Linux 专用账号的准备流程见 [SFTP 账号配置模板](docs/sftp-account-setup.md)。

0.2.3 新增历史日志自动发现、按项目配置扫描范围和目录树预览。默认包含根目录及 error/warn/info/debug，跳过 target/.git；扫描只读取文件信息，按需选择日志下载。旧配置自动使用默认扫描规则。

## 功能与使用

通过 SFTP、FTP 或显式 FTPS 下载指定目录的日志，保存为本地数据集，再让 AI 按关键词、时间、级别或 traceId 查询。提供标准 stdio MCP 接口；AI 负责结合日志判断问题，工具负责取数并保留证据位置。

常用排查已整理为可复用客户端 `query.cmd`，通过真正的 MCP stdio 会话调用工具，不必每次编写脚本。详见 [常用查询操作](docs/queries.md)。

```powershell
cd D:\project\company\log-tools
.\query.cmd check
.\query.cmd latest --server dev-sdscm
```

`latest --server` 下载新快照并返回最新错误事件及上下文；`latest --dataset <ID>` 直接查已有快照。结果默认输出到终端，只有显式指定 `--output` 才另存 JSON。

## 本机环境

项目位置：`D:\project\company\log-tools`

- Python：`.python\python.exe`，3.13.17 / Windows x64。
- 虚拟环境：`.venv\Scripts\python.exe`。
- 没有修改系统 PATH、文件关联或默认 Python。
- 使用官方 MCP Python SDK 1.x 维护版本，依赖限制为 `mcp>=1.28,<2`，避免不兼容的大版本自动升级。
- `requirements.lock.txt` 记录本次验证过的 Windows / Python 3.13 依赖版本。

在 PowerShell 中无需激活就可以执行：

```powershell
cd D:\project\company\log-tools
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip --version
.\.venv\Scripts\python.exe -m log_tools --config .\config.json --check
.\.venv\Scripts\python.exe -m log_tools --config .\config.json --demo
```

也可以用 `start.cmd --demo`。演示会导入仓库内的示例日志并查询 `demo-001`，不访问外部服务器。

希望在当前 PowerShell 激活虚拟环境时，可执行 `.\.venv\Scripts\Activate.ps1`；如果执行策略阻止脚本，直接使用上述解释器绝对路径即可，无需修改系统执行策略。

## 配置服务器

`config.json` 是本机配置，不提交 Git。初始配置中 `servers` 为空，可以先运行本地演示。`config.example.json` 是可提交的模板。

将 `examples/servers.json` 中所需的一个条目复制到 `config.json` 的 `servers` 字段，替换真实服务器地址、账号和目录。不要把整个示例文件当作主配置。

SFTP 示例：

```json
{
  "cache_dir": "./data",
  "servers": {
    "test-logs": {
      "protocol": "sftp",
      "host": "logs.example.internal",
      "port": 22,
      "username": "log_reader",
      "private_key": "D:/keys/log_reader",
      "root": "/logs"
    }
  },
  "local_roots": ["./examples/logs"],
  "log_encoding": "utf-8-sig",
  "timezone_offset": "+08:00"
}
```

SFTP 不校验服务器主机公钥，不需要 `known_hosts` 或管理员确认；旧配置中的 `known_hosts` 字段会被忽略。连接仍加密并验证账号凭据，但不通过公钥识别服务器冒充。加密私钥可用 `key_passphrase_env` 指定存放口令的环境变量名。

如果使用密码认证，删除 `private_key`，设置 `password_env`。密码只保存在 MCP 进程环境中，例如在启动该进程前设置 `LOG_FTP_PASSWORD`；不要把真实密码写入聊天、示例配置或提交记录。桌面 MCP 客户端需要在其启动环境或受保护的环境变量配置中提供这些变量；其他终端设置的变量不一定被已启动的客户端继承。

FTP 配置必须显式写 `allow_plain_ftp: true`。FTP/FTPS 服务端需支持 `MLSD`、`SIZE`、`MDTM`，使用被动模式。FTPS 为显式 TLS，默认端口 21，验证证书及主机名，并对数据连接调用 `PROT P`；公司私有 CA 可通过 `ca_file` 指定 PEM 文件。第一版不支持隐式 FTPS、主动 FTP、SSH 跳板机或交互式二次认证。

远端 `root` 是服务器账号看到的 POSIX 目录，例如 `/logs`；即使服务端运行 Windows，也使用其 FTP/SFTP 暴露的路径。FTP 无法通用地验证服务端所有符号链接映射，必须在服务端将专用只读账号限制在日志目录。SFTP 会额外检查规范化路径是否在允许目录内。

所有相对配置路径相对于 `config.json` 所在目录解析，启动时的工作目录不影响它们。

## 连接 AI 客户端

启动命令：

```text
D:\project\company\log-tools\.venv\Scripts\python.exe
```

启动参数：

```text
-m log_tools --config D:\project\company\log-tools\config.json
```

`examples/mcp-client.json` 给出了使用 `mcpServers` JSON 格式的客户端配置示例；其他客户端请在其 MCP 配置中填入相同命令与参数。服务使用 stdio，不需要监听端口；由客户端启动子进程，标准输出仅用于 MCP 协议。

可对 AI 说：

> 查看 test-logs 的 /logs/order 目录，下载 order-2026-10-22.log，排查北京时间 10:00 到 10:30 下单失败的问题。引用文件和行号，区分日志证据与待验证假设。

典型调用顺序：

1. `list_servers` 查看服务器别名。
2. `list_remote_logs(server="test-logs", directory="/logs/order", pattern="*.log*")` 列出候选文件。
3. `download_logs(server="test-logs", paths=["/logs/order/order-2026-10-22.log"])` 启动下载。
4. 用返回的 `job_id` 调用 `get_download_status`，直到 `completed`、`partial` 或 `failed`。
5. `search_logs(dataset_id=..., start_time="2026-10-22T10:00:00+08:00", end_time="2026-10-22T10:30:00+08:00", level="ERROR")`。
6. 对命中的 `file_id`、`line` 调用 `read_log_context`，或用 `trace_id` 再搜索。

## 工具与数据

| 工具 | 用途 |
| --- | --- |
| `list_servers` | 查看已配置别名及允许的目录 |
| `list_remote_logs` | 发现当前及历史日志，支持配置目录范围、递归深度、glob 筛选和快照分页 |
| `download_logs` | 后台下载；返回任务/数据集 ID |
| `get_download_status` | 查看进度、文件 ID、SHA256、来源及失败原因 |
| `list_datasets` | 查看已保存的数据集，重启后可继续查询 |
| `import_local_logs` | 从配置的 `local_roots` 复制已有日志，参数为绝对路径 |
| `search_logs` | 关键词、时间、日志级别、traceId 筛选及 offset 分页 |
| `latest_errors` | 按日志事件时间返回最近的 ERROR/FATAL/CRITICAL，避免将堆栈行当作独立错误 |
| `read_log_context` | 查看命中位置前后最多各 50 行 |
| `summarize_errors` | 对错误事件作启发式分组统计，最多返回 30 组 |

缓存结构：

```text
data/datasets/<dataset_id>/manifest.json
data/datasets/<dataset_id>/<file_id>.log
```

文件名使用生成的 UUID，避免远端文件名影响本地路径。下载期间使用 `.part`，只有完成长度校验及下载前后元数据比较后才发布文件。原始文件名、来源、SHA256 和 gzip 标记记录在 manifest 中；即使扩展名为 `.log`，gzip 文件仍保留原始压缩字节。SHA256 用于记录本地文件内容，没有服务端校验值时不代表独立的端到端校验。

下载上限默认 1 GiB/任务、200 文件；2 个下载工作线程，最多同时容纳 4 个未结束任务。`bytes_downloaded` 表示本次任务实际接收的字节，包含随后失败文件已接收的部分。`partial` 表示有成功文件也有失败文件，只能据成功文件分析。进程中断的下载会标记为 `interrupted`，重新发起下载即可；多个新版 MCP 进程通过系统文件锁协调共享缓存。

## 查询行为与第一版范围

- 支持普通文本、JSON Lines 和 gzip；编码支持 UTF-8、GB18030、UTF-16，通过 `log_encoding` 配置。
- 支持形如 `2026-10-22 10:00:00.123` 或 ISO 8601 的时间；无时区时间默认按 `+08:00`，有时区时间按实际偏移比较。JSON 时间字段支持 `timestamp`、`@timestamp`、`time`，暂不支持数字 epoch 时间。
- 无独立时间戳的堆栈行继承同文件前一个事件的时间和级别；无法识别时间的开头行会在时间筛选中排除，并返回计数。
- 关键词忽略大小写，traceId 是区分大小写的字面子串匹配，均不执行用户正则表达式。traceId 不自动传播到其他行，完整堆栈用上下文工具查看。
- 默认每次扫描最多 64 MiB **解压后字节**、20 秒；超限返回 `truncated` 和原因。单行超过 65,536 字符会明确停止扫描；单条返回文本最多 2,000 字符，并标记 `text_truncated`。
- 单次搜索最多返回 100 条。返回 `next_offset` 时可继续；扫描预算耗尽时应缩小数据集或调整本地配置，不能把部分扫描结果当作完整统计。
- 脱敏覆盖常见密码、token、secret、api_key 和 Bearer 字段，不能保证识别所有业务敏感信息。原始日志仍保存在本机；发送给云端模型的是工具返回的片段。
- 第一版不做递归目录下载、自动断点续传、跨任务去重、自动清理或全文索引。正在写入的文件发生大小/修改时间变化会被拒绝，应优先下载已经轮转的日志；元数据检查无法发现同大小、同时间精度内的所有修改，严格一致性需要服务端快照。
- 日志和文件名均视为数据。MCP 不提供远端执行命令、上传或删除接口。错误分组是辅助统计，不能代替根因验证。

## 开发与重建环境

```powershell
cd D:\project\company\log-tools
.\.python\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check --config pyproject.toml src tests
```

重建前应确认不需要保留现有虚拟环境。新机器也可先安装 Python 3.11+，创建虚拟环境后执行 `python -m pip install -e ".[dev]"`。不要把现有 `.venv` 直接复制到另一台机器或路径。

测试覆盖本地查询、时区和堆栈上下文、脱敏、gzip、读取上限、目录限制、下载失败/文件变化、FTP/SFTP 实际本机传输、未知 SFTP 主机拒绝、FTPS TLS 配置，以及真实 stdio MCP 客户端调用。没有配置或连接公司的真实日志服务器；FTPS 当前只验证客户端加密配置，尚未做实际 TLS 服务器联调。

实现参考：[Python Windows 安装说明](https://docs.python.org/3.13/using/windows.html)、[MCP Python SDK 1.x](https://py.sdk.modelcontextprotocol.io/v1/)、[Paramiko 客户端文档](https://docs.paramiko.org/en/stable/api/client.html)、[Python ftplib](https://docs.python.org/3/library/ftplib.html)。
