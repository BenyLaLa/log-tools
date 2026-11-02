# 常用日志排查操作

统一入口为项目根目录下的 `query.cmd`，或 `.venv\Scripts\python.exe -m log_tools.client`。它启动 MCP、执行工具调用、等待下载完成，然后关闭连接。查询结果为 JSON，默认只写到终端；不生成临时 Python、查询 JSON 或 stderr 文件。

```powershell
cd D:\project\company\log-tools
.\query.cmd --help
```

## 1. 检查连接和凭据

```powershell
.\query.cmd check
.\query.cmd servers
.\query.cmd files --server dev-sdscm
```

`check` 检查 MCP 握手、工具列表和凭据是否存在，不连接远端 SFTP。`files` 会实际连接服务器并列出文件。

客户端依次使用显式客户端配置中的 `env`、当前进程环境变量；缺少配置引用的密码/私钥口令变量时，在 Windows 下尝试读取当前用户和系统环境变量。只读取 `password_env`、`key_passphrase_env` 引用的名称，不枚举全部变量，不打印或保存凭据值。已有进程变量不会被注册表中的值覆盖。

因此，在 Windows 用户环境变量中配置密码后，不必把密码写进 JSON。使用 `--inherit-env-only` 可关闭注册表补充读取。若程序运行在另一个 Windows 用户或受限沙箱下，它看到的用户变量可能不同，`check` 会报告 `missing`；须使用正确的运行身份或从启动环境传入。

## 2. 查最新报错

从默认目录选取名称匹配 `*error*.log*` 的文件，下载后查询：

```powershell
.\query.cmd latest --server dev-sdscm
```

指定目录、文件名和最近 3 条错误，并多读一些堆栈：

```powershell
.\query.cmd latest --server dev-sdscm --directory /home/sdscm-dev --pattern "log_error.log" --limit 3 --before 3 --after 50
```

默认最多选择 10 个文件；超过上限或远端列表不完整时拒绝自动挑选，要求缩小 `--pattern`，防止遗漏真正最新的事件。`latest` 默认不递归，加 `--recursive` 可包含历史子目录。`--max-files` 可调整文件数；`--max-bytes` 指定本次下载上限，未指定时使用服务配置的上限。

`latest_errors` 对所选数据集做一次有预算的扫描，按日志事件时间倒序排序，包含 ERROR/FATAL/CRITICAL，不将异常堆栈尾行当作新事件。返回最新 N 条、前后文、文件 ID、行号和数据集 ID。没有独立时间戳的错误无法准确排序，扫描超限、部分数据集等情况会明确返回 `warnings` / `incomplete`，不能宣称已确定最新一条。

这只是**所选文件下载快照**中的最新错误，不是对服务器全部目录的实时监控，也不是自动根因结论。默认上下文为前 3 行、后 30 行，最大各 50 行；更长堆栈可继续用 `context` 读取。

## 3. 重用已经下载的日志

```powershell
.\query.cmd datasets
.\query.cmd status --dataset "数据集ID"
.\query.cmd latest --dataset "数据集ID"
```

`latest --dataset` 不重新下载。`status` 返回对应的原始文件名和 `file_id`。

## 4. 下载指定日志并等待完成

```powershell
.\query.cmd fetch --server dev-sdscm /home/sdscm-dev/log_error.log /home/sdscm-dev/log_info.log /home/sdscm-dev/log_debug.log
```

返回 `dataset_id` 后可对这组文件统一搜索。默认等待上限 120 秒，可使用 `--wait-timeout 300`。每次运行是独立 MCP 子进程：超时或中断关闭后，它的后台下载不会由这个客户端继续托管；根据返回的 ID 检查状态，必要时重试。`partial`、`failed` 会保留失败信息并以非零状态退出。

本版本不跨任务去重；多个新版 MCP 进程可共享缓存，使用系统文件锁识别活跃下载。不要与尚未包含该修复的旧版进程同时操作缓存。

## 5. 按关键词、时间、级别或 traceId 搜索

```powershell
.\query.cmd search --dataset "数据集ID" --query "促销审核同步"
.\query.cmd search --dataset "数据集ID" --start "2026-10-26T11:42:00+08:00" --end "2026-10-26T11:44:00+08:00" --level ERROR
.\query.cmd search --dataset "数据集ID" --trace-id "实际traceId"
.\query.cmd search --dataset "数据集ID" --query "实际单据号" --limit 100
```

条件可以组合。关键词不区分大小写，traceId 为区分大小写的字面匹配。结果含 `next_offset` 时，将它传给 `--offset` 继续下一页；默认 50 条，单页最大 100 条。

时间窗示例是具体历史时间，不自动代表今天。无时区输入按服务器的 `timezone_offset` 解释，建议显式带上 `+08:00`。

## 6. 读取异常上下文与统计

```powershell
.\query.cmd context --dataset "数据集ID" --file "文件ID" --line 1454 --before 10 --after 40
.\query.cmd summary --dataset "数据集ID"
```

`summary` 是启发式分组计数，不等同于根因分析。所有结果都要检查 `truncated` / `warnings`，长行也会有 `text_truncated` 标记。

## 7. 导入已有的本地日志

```powershell
.\query.cmd import "D:\logs\app.log" "D:\logs\app.log.gz"
```

文件必须在 `config.json` 的 `local_roots` 白名单内。导入会生成可继续查询的数据集，不修改源文件。

## 8. 按需保存结果或使用已有客户端配置

`--output`、`--config`、`--client-config` 等全局选项放在子命令前：

```powershell
.\query.cmd --output .\reports\latest-20261027.json latest --dataset "数据集ID"
.\query.cmd --client-config .\examples\mcp-client.json check
.\query.cmd --client-config .\examples\mcp-client.json files --server dev-sdscm
```

`--output` 只创建新文件，已有文件会报错，避免覆盖旧证据。默认直接使用项目的 `config.json` 和当前虚拟环境启动服务；`--client-config` 可以复用已有 `mcpServers` 配置的启动命令、参数和 `env`，条目名默认 `log-tools`，可用 `--client-name` 更改。

仅连接可信的客户端配置，因为配置中的 `command` 会在本机执行。输出文件可能含业务日志片段，应按原日志要求保管；常见敏感字段的脱敏不能覆盖所有业务内容。

## 给 AI 使用

已直接接入 MCP 的 AI 可以调用 `latest_errors` 和现有其他工具；通过命令行连接的 AI 则使用同一个 `query.cmd` 入口即可。标准流程为：

1. `latest --server` 定位最新事件，取得数据集 ID 和证据行。
2. 根据需要 `fetch` 同时段的 INFO/DEBUG 日志。
3. `search` 按时间、线程关键字、traceId 或单据号关联。
4. `context` 补全异常堆栈和业务步骤。
5. 区分日志直接证明的原因与待验证推测；只有需要交付时才另存结果。


## 历史日志发现（0.2.3）

`files` 默认使用项目扫描设置，自动发现当前及历史日志，并在同一次命令会话中取完所有分页。`--limit` 是每页大小，不再限制最终输出总数。查看 `incomplete` 和 `warnings` 判断是否有漏扫；按配置排除的目录不会下载。

```powershell
.\query.cmd files --server dev-sdscmcontract
.\query.cmd files --server dev-sdscmcontract --no-recursive
.\query.cmd files --server dev-sdscmcontract --directory error --recursive --max-depth 3
.\query.cmd latest --server dev-sdscmcontract --directory error --recursive --pattern "*.log*" --max-files 50
```

`latest --server` 为控制下载量，默认只查指定目录的当前日志；加 `--recursive` 才包含历史子目录。它会拒绝对未完整扫描的结果或超过 `--max-files` 的候选集合声称找到了最新异常。先用 `files` 检查范围，再按目录或文件名缩小候选；文件名时间不是日志事件时间。MCP 工具本身默认按项目设置发现历史。
