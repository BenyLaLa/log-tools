# 一键打包与新机器配置（Windows）

这版支持 Windows 10/11 x64，可生成联网安装包或完整离线安装包。发送方不需要手工挑文件；接收方不需要提前安装 Python、Git 或手动创建虚拟环境。Linux 服务器的只读账号需要管理员提前配置好；本机安装脚本不会修改服务器。

## 1. 给别人之前：一键打包

在已经可用的项目中双击 `pack.cmd`，或在项目目录执行：

```powershell
.\pack.cmd
```

给不能访问外网的机器使用时，在有网络的打包机器运行：

```powershell
.\pack.cmd -Offline
```

首次制作离线包会下载官方 Python 3.13.17 x64 完整安装器，以及锁定版本的依赖，并预先构建 wheel。完成后生成 `dist/log-tools-windows-offline-时间戳.zip` 和同名 `.sha256`。打包程序还会用本地 wheels 进行不访问索引的依赖完整性检查。

离线包包含 `offline/` 目录：Python 安装器、`wheels/` 依赖文件和 SHA256 清单；不包含发送方已安装的 Python 目录或虚拟环境。制作离线包需要 Windows x64 + Python 3.13（本项目 `.venv` 已满足），不能在其他平台随意构建后交给 Windows 使用。

已有官方安装器时可以复用，仍会核对固定 SHA256 和发布者签名：

```powershell
.\pack.cmd -Offline -InstallerPath 'D:\installers\python-3.13.17-amd64.exe'
```

默认 `pack.cmd` 仍生成小型联网安装包。只需要把 **offline 文件名的 ZIP** 和对应校验文件发给离线接收者，不需要额外发送依赖目录。

生成文件在 `dist/`：

```text
log-tools-windows-时间戳.zip
log-tools-windows-时间戳.zip.sha256
```

把这两个文件交给接收方即可。ZIP 使用明确的文件白名单，每次从当前源文件生成，不要求先提交 Git。包内有 `PACKAGE-MANIFEST.json`，列出文件、字节数及 SHA256；外部 `.sha256` 用于核对 ZIP 的传输完整性。

包括程序代码、启动/查询/安装/配置/打包脚本、锁定依赖、通用配置模板、使用文档、桌面构建脚本所需的许可证及人工编写的测试日志。

排除：`data/` 下载日志与数据集清单、归档/报告、`config.json`、`known_hosts`、环境变量和私钥、`.python/`、`.venv/`、Git 历史、安装缓存与配置备份。客户端示例在打包时重新生成通用占位路径，不复制发送方的客户端配置。

新增程序文件后需要更新 `src/log_tools/deployment.py` 的 `PACKAGE_FILES` 白名单；不要改成递归压缩项目目录。发布前仍应审查代码/文档本身，白名单不能识别被手工写进源代码的秘密。

## 2. 新机器使用前需要准备什么

1. Windows 10/11 x64、可写的本地目录，例如 `D:\tools\log-tools`。避免直接在 ZIP 预览中运行、网络共享路径或需要管理员写入的目录。
2. 使用普通包时，安装期间需要访问 `www.python.org`、`pypi.org`、`files.pythonhosted.org`。**使用离线包时，接收方安装不需要访问这些外网网站**；下载和准备依赖在打包机器完成。
3. 访问日志服务器所需的 VPN/网络，以及正确的 SSH/SFTP 端口。
4. 专用只读账号、密码或本机私钥。每个接收者应使用获授权的凭据，分享包不带发送方密码。
5. SFTP 登录后可见的日志目录，例如 `/sdscmcontract`。服务器宿主机上的 `/home/sdscmcontract` 不一定是 SFTP 内路径。
不需要 `known_hosts`、服务器主机指纹或管理员确认。此版本不校验服务器主机身份，仍验证账号凭据并加密传输。

服务器账号尚未准备好时，把 `docs/sftp-account-setup.md` 的信息表交给管理员填写，再生成并执行对应服务器配置步骤。

## 3. 解压后：一键安装与配置

先完整解压 ZIP。进入包含 `setup.cmd` 的 `log-tools` 文件夹，双击 `setup.cmd`，或在 PowerShell 执行：

```powershell
.\setup.cmd
```

离线包会自动识别 `offline/` 并使用本地安装器和依赖，无需额外参数。也可明确要求离线模式：

```powershell
.\setup.cmd -Offline
```

如果整台机器当前没有任何网络，先只安装环境：

```powershell
.\setup.cmd -Offline -SkipConfigure
```

之后能访问公司服务器时运行 `configure.cmd`。离线安装不代表能在断开服务器网络时下载远端日志；已有本地数据集仍可在离线状态查询。

脚本会依次：

1. 检测可用的 Python 3.13 x64。优先复用项目解释器、注册表中安装的解释器或 PATH 中的兼容解释器。
2. 没有兼容解释器时，普通包从官网下载安装器，离线包使用内置安装器，以当前用户身份安装 Python 3.13.17。默认安装到 `%LOCALAPPDATA%\Programs\Python\Python313`，不改系统 PATH。该固定版本对应项目原先验证过的环境，并不表示它始终是最新版本。
3. 在解压目录新建 `.venv`；安装 `requirements.lock.txt` 锁定的依赖及本项目，并检查依赖一致性。普通包的 pip 缓存写入项目 `.setup-cache/pip/`；离线包强制 `--no-index --no-cache-dir`，只从内置 wheels 安装，项目安装关闭构建隔离及依赖下载，不借用发送方缓存。
4. 缺少 `config.json` 时从模板创建；已有配置则保留。生成匹配本机绝对路径的 `mcp-client.local.json`，已有文件也保留。
5. 进入 SFTP 配置向导，填写地址、账号、目录和认证方式，在本机输入密码或私钥口令；不再核对主机指纹。
6. 最后通过真实 stdio MCP 会话运行 `check`，确认本地服务能启动。

安装器的自动安装参数依据 [Python Windows 安装说明](https://docs.python.org/3.13/using/windows.html)，固定下载及校验值见 [Python 3.13.17 发布页](https://www.python.org/downloads/release/python-31317/)。

离线包在打包机器上校验安装器发布者签名；接收方使用脚本固定的官方 SHA256 校验安装器，不调用可能联网查询证书链的签名验证。全部 wheels 和锁定文件也会校验 SHA256；文件缺失、被改动或依赖不完整时停止，不自动联网补齐。校验文件用于验证完整性，分享包本身仍应从可信渠道取得。wheel 的构建和离线安装选项参考 [pip wheel](https://pip.pypa.io/en/stable/cli/pip_wheel/) 与 [pip install](https://pip.pypa.io/en/stable/cli/pip_install/)。

首次安装一般不需要管理员权限；组织策略、运行库和证书验证要求以本机实际情况为准。脚本只在启动的 PowerShell 进程使用执行策略参数，不修改系统永久执行策略；受组织策略限制时联系管理员，不需要自行全局放宽策略。

**一键安装不等于零信息输入**：服务器地址和授权凭据仍需接收者自己提供。向导不校验服务器主机身份，不会在服务器执行配置命令。

### 只安装环境，暂不配置服务器

```powershell
.\setup.cmd -SkipConfigure
```

之后运行：

```powershell
.\configure.cmd
```

向导支持连续添加多个项目。已有别名需要确认后才替换，其他服务器条目保持不变。配置备份在 `.setup-backups/`，其中可能含服务器信息，应与 `config.json` 同等保管。

### 使用指定的 Python

```powershell
.\setup.cmd -PythonPath 'C:\Python313\python.exe'
```

该解释器必须是 Python 3.13 x64。其他 Python 版本不会被卸载或替换。已有 `.venv` 会优先使用；如果它损坏或来自另一台机器，停止 MCP 后自行把 `.venv` 改名备份，再运行安装脚本。

在终端或自动化中不需要结束暂停时可添加 `-NoPause`：

```powershell
.\setup.cmd -SkipConfigure -NoPause
.\pack.cmd -NoPause
```

## 4. 密码、密钥与 MCP 客户端

密码默认引用 `LOG_READER_PASSWORD`；私钥口令默认引用 `LOG_SFTP_KEY_PASSPHRASE`。可以给不同账号选择不同的 `LOG_` 前缀环境变量名；不要让不同账号意外共用同一个变量。

向导用隐藏输入获取凭据，并可保存到当前 Windows 用户环境变量，不写进 JSON、命令参数或 ZIP。**用户环境变量不是加密密码库**，同一 Windows 用户下的程序可能读取它。如果不希望持久保存，向导中选择不保存，由受信任的启动环境提供凭据。

`query.cmd` 会按配置中的变量名补充读取当前用户环境变量，因此配置后可直接运行。桌面 MCP 客户端未必自动读到新变量；重启客户端/重新登录 Windows，或用该客户端支持的启动环境设置，确保它确实继承变量。不要把真实密码写入共享的客户端配置。

AI 客户端使用本机生成的 `mcp-client.local.json` 中的 `command` 和 `args`。其中使用虚拟环境解释器启动 `-m log_tools --config ...`；`setup.cmd` 和 `configure.cmd` 是交互式工具，不能作为 MCP 服务启动命令。

项目目录改变后需要重新建立虚拟环境，并检查 `mcp-client.local.json` 的绝对路径。初始化会保留现有客户端配置；备份旧文件并移走后，重新运行 `setup.cmd -SkipConfigure` 可生成新路径。

## 5. 最后验证：真正连接并读取日志

假设配置的别名为 `dev-sdscmcontract`：

```powershell
.\query.cmd check
.\query.cmd files --server dev-sdscmcontract
.\query.cmd latest --server dev-sdscmcontract --directory /sdscmcontract --pattern 'log_error.log'
```

- `check`：只验证本地 MCP；`remote_connection_tested: false` 不代表远端已经连接成功。
- `files`：实际登录 SFTP 并列目录，验证凭据和路径权限。
- `latest`：下载快照后查询最新错误。下载数据保存在本机 `data/`，需要本地磁盘空间。

当前日志正在写入时，下载可能因文件变化而拒绝；改选稳定的历史文件，并指定实际的 `error/` 等目录。历史目录不会自动递归查询。

## 6. 常见问题

| 现象 | 处理 |
| --- | --- |
| Python 下载或签名检查失败 | 检查网络、代理、系统时间及证书链；不要绕过校验。也可由管理员安装可信 Python 3.13 x64，再指定 `-PythonPath` |
| pip 下载失败或版本不存在 | 检查官方索引连通性及完整报错；如需更换公司镜像，应由管理员审查配置，不要盲目删除锁定版本 |
| Offline manifest/file/checksum 错误 | 重新获取并完整解压离线 ZIP；不要混用不同版本的离线目录或锁定文件，不会自动联网回退 |
| 离线包安装成功，但配置向导连接失败 | 安装环境与访问服务器是两回事；先用 `-SkipConfigure` 安装，接通公司网络/VPN 后再配置 SFTP |
| 安装窗口结束太快 | 在 PowerShell 中运行脚本查看输出；默认脚本结束前会等待 Enter |
| 提示已有 Python 目录不可用 | 修复原安装或指定另一个兼容解释器；脚本不会覆盖这个目录 |
| 旧配置仍有 known_hosts | 新版忽略该字段，不需修改或删除原文件 |
| 凭据缺失 | 检查变量名、运行 Windows 用户，以及 MCP 进程是否能获取该变量 |
| Permission denied | 区分认证失败与目录拒绝；按服务器账号流程检查 ACL、chroot 和 SFTP 可见路径 |
| 换机器后旧 .venv 无法运行 | 不复制虚拟环境；从分享包解压并重新运行 setup.cmd |

安装失败时会返回非零退出码。修复失败原因后可重跑；脚本不会删除已有日志、覆盖现有配置或自动提交 Git。
