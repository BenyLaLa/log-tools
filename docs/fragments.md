# 0.2.4 · SFTP 范围与历史片段检索

本功能在原有 0.2.3 上增加，不需要 Linux 脚本、常驻服务、新端口或通用 SSH 命令权限。沿用现有只读 SFTP 账号及日志根目录，服务器不执行 grep/shell/Python。原配置、凭据、历史数据和原有完整下载工具兼容保留；新片段功能不会自动回退到完整下载。

## 过程与三种模式

先列出候选文件（仅元数据），按日期/级别/分卷选择明确路径；通过 SFTP seek/read 在本机内存过滤，合并命中前后文，只写命中片段及来源清单到本地数据集。内置常见密码/Token 脱敏默认也作用于片段落盘，但不是完整的业务敏感信息识别。

| 模式 | 读取策略 | 适用场景 |
|---|---|---|
| tail 最新尾部 | 从每个文件末尾读取一个窗口，默认 8 MiB；可继续向前扩展 | 当前、近期异常 |
| history 历史时段 | 从所选历史文件开头流式扫描，按内容时间过滤，至少填写一个时间边界 | 已知日期/时段的问题 |
| full 完整流式 | 从所选文件开头流式扫描，按关键词/级别/traceId 等筛选 | 不知道问题位于哪里 |

history 第一版不通过时间二分定位。日志不必严格有序；时间过滤仍需要读取内容。日期和分卷由文件清单选择，文件名/mtime 不能代替内容时间。

tail 只传选定范围。history/full 为完整检索可能经网络读取整份候选文件，区别是本地磁盘不保存原始整份内容。没有把读取窗口原文或压缩包先写入临时文件；只在内存持有有限的缓冲和上下文。

## 图形界面

升级到 LogTools-Setup-0.2.4-x64.exe 后，原配置位置及账号不用重配。打开“4 片段检索”：

1. 选择已保存的 SFTP 项目，点击“列出候选文件”。扫描沿用“历史日志扫描”的目录/深度/排除规则。
2. 查看清单中的扫描遗漏提示，勾选目标日期及各分卷；不自动按 mtime 排除文件。
3. 选择三种模式之一，填写关键词、级别、traceId、时间范围和上下文行数。默认界面级别 ERROR，可选择全部级别。
4. 点击“检索并保存片段”，结果包含 dataset_id、本地目录、网络读取量、落盘量、覆盖范围、遗漏原因及少量片段预览。
5. 有 resume_dataset_id 时可点击“继续上次扫描”。沿用原来的模式、条件和路径，预算可以调整；继续会生成新的子数据集，不覆盖上一次证据。

新工具没有筛选条件时默认 ERROR。需要搜索任意级别的特定异常，请设置关键词并选择“全部级别”。

## CLI

在项目根目录使用已有虚拟环境与配置：

```cmd
query.cmd check
query.cmd files --server YOUR_ALIAS --pattern "*2026-10-28*.log*" --limit 1000
query.cmd collect --server YOUR_ALIAS --mode tail --query NullPointerException --tail-mib 8 --before 20 --after 60 /log/sundanslim/log_error.log
query.cmd collect --server YOUR_ALIAS --mode history --query Exception --start "2026-10-28 09:00:00" --end "2026-10-28 18:00:00" /log/sundan-server/web-error-2026-10-28.0.log
query.cmd collect --server YOUR_ALIAS --mode full --query trace-123 --network-mib 64 --file-mib 32 --save-mib 2 /log/sundan-server/web_error.log
query.cmd collect --server YOUR_ALIAS --resume RETURNED_DATASET_ID
query.cmd search --dataset RETURNED_DATASET_ID --query Exception
query.cmd context --dataset RETURNED_DATASET_ID --file RETURNED_FILE_ID --line 1 --before 10 --after 30
```

路径必须替换成 files 实际返回的 remote_path，或项目根内相对路径。示例中的 sundanslim 当前日志文件名需以实际清单为准。原 `latest --server` 和 `fetch` 仍是显式完整下载命令；需要只保存片段时使用 collect。

## MCP

新增 collect_log_fragments，工具总数 11；原有工具保留。AI 应先 list_remote_logs 并收集清单全部页，选择日期/级别相关路径，再调用新工具。已有 MCP 进程需重启加载新代码，安装路径/配置路径不变通常无需重新接入。

```json
{
  "server": "YOUR_ALIAS",
  "paths": ["/log/sundan-server/web-error-2026-10-28.0.log"],
  "mode": "history",
  "query": "NullPointerException",
  "start_time": "2026-10-28 00:00:00",
  "end_time": "2026-10-28 23:59:59",
  "before": 20,
  "after": 60,
  "max_network_bytes": 67108864
}
```

继续：只需 server、resume_dataset_id 及可选预算。保存的模式、条件和路径会替代新传入的条件/path，不会更改原始请求。可跨 MCP 重启继续普通日志；凭据和项目连接信息仍必须匹配。

## 默认预算

| 项目 | 默认值 |
|---|---:|
| 尾部窗口 | 8 MiB/文件/次 |
| 网络正文读取 | 64 MiB/次 |
| 每文件网络正文读取 | 32 MiB/次 |
| 片段正文落盘 | 2 MiB/次 |
| 解压/解析内容 | 256 MiB/次 |
| 扫描时限 | SSH 连接成功后 30 秒 |
| 命中行 | 100/次 |
| 保存行数 | 10000/次 |
| 单行原始字节 | 64 KiB |
| 上下文 | 前 0–50、后 0–100 行 |

network_bytes 统计实际通过 SFTP read 收到的文件正文，包括必要的 UTF-16 BOM 读取和续扫重叠，不包括 SSH 加密/协议开销及目录元数据。saved_bytes 统计规范为 UTF-8 后的片段正文，manifest.json 另占少量空间。decoded_bytes 统计解析/解压得到的内容；gzip 解压还可能有少量内部缓冲。连接认证仍使用原项目 timeout_seconds，扫描时限从连接建立后开始，定时关闭本次专用连接中断阻塞读取。

全局配置可增加以下字段，调用参数只能降低到管理员配置上限，不能任意提高。无需改已有配置即可使用默认值：

```json
{
  "fragment_network_bytes": 67108864,
  "fragment_file_bytes": 33554432,
  "fragment_output_bytes": 2097152,
  "fragment_scan_bytes": 268435456,
  "fragment_timeout_seconds": 30
}
```

## 本地证据与覆盖范围

数据集位于当前配置的 cache_dir/datasets/<dataset_id>/。UUID.log 每个文件保存一个连续上下文片段，manifest.json 记录筛选条件、源文件版本（大小/mtime）、读取范围、网络/落盘量、未完成任务及逐行来源映射。只保存命中相关片段，不保存其余扫描内容。默认原生 GUI 为 %LOCALAPPDATA%\CompanyLogTools 下配置的缓存，源码以 config.json 的 cache_dir 为准。

search_logs/read_log_context 的 line 仍是本地片段内行号，用于调用上下文工具。source.path 是远端来源；source.line 为能确认时的原始行号；source.byte_start/byte_end 是原始字节范围（右端不包含）。tail 未从头计行时 source.line=null，必须引用字节偏移，不能编造绝对行号。gzip 的 byte_unit=decompressed，偏移对应解压后的内容，不是压缩文件偏移。

片段本地查询始终带有范围提示：它只能描述已保存片段。collection.coverage/incomplete/warnings/remaining 决定远端检查范围。即使当前子数据集 completed，也不代表服务器所有目录或历史均已扫描。没有命中只能说明已扫描且可解析的范围内没有命中；尾部模式不能无条件认定全局最新异常。

## 继续扫描与限制

- 普通文件限额中断时保存安全行边界与上下文重叠，续扫无需重读整个前缀。tail 窗口结束后可以继续更早窗口；窗口内中断先完成该窗口，再往前。重叠片段可能重复，跨数据集计数要按来源位置去重。
- 预算小于必要上下文时可能无法前进，会提示 resume_no_progress；提高网络/每文件/输出预算，或重新减少上下文。不会无限自动重试。
- 文件大小或 mtime 改变时拒绝使用旧断点，要求重新检索；读取期间变化保留已取证内容并明确提示。SFTP 属性没有原子文件版本保证，相同属性的替换/改写仍可能无法识别；不要把结果描述成原子快照。
- 范围开头可能是半行，保守丢弃第一行并在 coverage 标记；非 EOF 末尾半行不作为证据。窗口不足容纳长行时应改用更大窗口或完整流式模式。
- `.log.gz` 只在 history/full 从头流式解压，支持连续 gzip members；不落地原压缩文件。中断后不支持该压缩文件的跨进程随机续扫，会提示需要从头重试并提高预算；其他未处理文件仍可继续。
- 只接受 .log/.log.gz，排除 target/third/dist/.git 及项目排除目录，拒绝可识别的软链接。SFTP 客户端检查不是服务端文件系统隔离，仍沿用只读账号/服务端目录权限；标准 SFTP 不能提供本地 O_NOFOLLOW/inode 的同等保证。
- 返回的片段会进入 AI 对话，客户端可能保留对话记录。原完整下载和本地导入功能保留，但新工具不调用它们。
