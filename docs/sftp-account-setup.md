# 日志 MCP 专用 SFTP 账号：信息收集与配置流程

这份文档用于向 AI 提供服务器情况，让 AI 据此输出适合该服务器的完整配置步骤、命令和验证方法。

使用方法：填写第 2 节的信息表；不清楚的项目可使用第 3 节的只读命令收集；把填写结果和第 6 节的请求模板一起发给 AI。密码、私钥内容不需要提供。

本文件是配置需求模板和实施流程。实际修改命令应在确认系统版本、现有 SSH 配置和日志轮转方式后生成；不要把未填写的示例直接用于服务器。

## 1. 本次方案与已知情况

目标是让 Windows 上的日志 MCP 通过专用账号下载 Linux 日志，再在本地分析。

- 使用 SFTP，账号仅允许 SFTP 会话，不允许交互式 Shell、远端命令和端口转发。
- 采用 chroot 限制账号看到的文件系统范围；通过只读 bind mount 映射原目录。
- 应用继续写原有日志路径，不搬迁日志，不复制一份服务器日志。
- 使用 ACL 控制该账号能读取哪些内容，禁止读取 JAR、配置文件等非日志内容。
- 下载的数据仍会占用 Windows 本地磁盘；服务器 bind mount 不复制日志正文，只增加挂载和目录等少量管理开销。[mount 说明](https://man7.org/linux/man-pages/man8/mount.8.html)

当前已知目录如下，正式生成命令前仍需确认三个项目实际结构一致：

| 项目 | 服务器原目录 | SFTP 中计划可见的目录 | MCP 别名建议 |
| --- | --- | --- | --- |
| sdscm-dev | `/home/sdscm-dev` | `/sdscm-dev` | `dev-sdscm` |
| sdscmrestock | `/home/sdscmrestock` | `/sdscmrestock` | `dev-sdscmrestock` |
| sdscmcontract | `/home/sdscmcontract` | `/sdscmcontract` | `dev-sdscmcontract` |

每个项目预期包含：

```text
项目目录/
├── log_debug.log
├── log_error.log
├── log_info.log
├── log_warn.log
├── debug/             # 历史日志
├── error/             # 历史日志
├── info/              # 历史日志
├── warn/              # 历史日志
└── target/            # JAR，禁止读取
```

历史日志保留约 5 天。已提供的一个目录样本中，日志属主为 `root:root`，文件权限为 `644`，目录为 `755`；这不代表另外两个项目、新建日志或部署后的权限已经核实。

Windows MCP 项目路径：`D:\project\company\log-tools`。

### 方案边界

只读限制解决的是“不能修改”，不自动解决“只能看日志”。整个项目目录映射进 chroot 后，其他有读取权限的文件也可能被读取。`target` 的名称可能出现在目录列表里，即使它的内容已被禁止访问。

必须明确以下要求：

1. 如果允许看到非日志文件的名称，但禁止读取其内容，可根据实际目录结构设计 ACL。
2. 如果连名称都不能出现，需要单独设计仅展示日志的视图，不能宣称“整目录挂载 + ACL”能隐藏目录项。
3. 不应仅禁止当前 `target/` 就认为以后新增的 `.env`、配置、备份和 JAR 都安全。需要默认拒绝策略，并验证部署和轮转后的行为。
4. 不应对整个项目递归授予该账号读权限，也不应盲目给项目根目录设置允许读取所有新文件的默认 ACL。
5. 对单个当前日志文件做 bind mount，轮转替换文件后可能仍绑定旧文件对象；不能把它当成自动跟随新日志的通用方案。

## 2. 复制填写：需要提供给 AI 的信息

保留字段名即可。不知道的字段填写“不清楚”，已经配置过的步骤请写明；不要为了填表提供密码或私钥。

```text
【服务器与访问】
Linux 发行版及版本：
OpenSSH 版本：
服务器地址：                 # 可先用 SERVER_HOST 占位，最终命令再本机替换
SSH/SFTP 端口：
管理员登录方式：root / 普通用户 sudo / 其他
是否可保留现有管理员 SSH 会话：
是否有云控制台或其他恢复入口：
SSH 服务名：ssh / sshd / 不清楚
服务器是否有容器、特殊挂载或网络文件系统：
SELinux/AppArmor 状态：
客户端来源 IP：              # 用于分析 Match Address 等已有规则；可脱敏

【专用账号】
期望用户名：log_reader
账号是否已存在：
若已存在，是否正在被其他程序使用：
认证方式：密码 / SSH 密钥
密码环境变量名：LOG_READER_PASSWORD  # 只给变量名，不给密码
若用密钥，Windows 私钥文件路径：    # 只给路径，不给内容
若用密钥，待安装的客户端公钥：      # .pub 内容可提供，也可留待本机安装
是否有密钥口令：是 / 否             # 不提供口令
密钥口令环境变量名（如适用）：
chroot 根目录建议：/srv/sftp/log_reader
是否已有同名目录、挂载、SSH Match 配置：

【项目与日志】
项目 1：sdscm-dev，原目录 /home/sdscm-dev，MCP 别名 dev-sdscm
项目 2：sdscmrestock，原目录 /home/sdscmrestock，MCP 别名 dev-sdscmrestock
项目 3：sdscmcontract，原目录 /home/sdscmcontract，MCP 别名 dev-sdscmcontract
需要开放哪些项目：
当前日志文件名：log_debug.log、log_error.log、log_info.log、log_warn.log
历史日志目录名：debug、error、info、warn
历史日志文件名示例、是否压缩：
保留天数：5
禁止读取的文件/目录：target，以及________
能否看到非日志目录或文件的名称：可以 / 不可以
原目录中的符号链接、子挂载：无 / 有，说明________ / 不清楚
应用实际运行用户/组：
现有日志属主、权限、ACL：
日志轮转方式：Logback / Log4j2 / logrotate / 其他 / 不清楚
轮转时如何处理当前日志：重命名后新建 / copytruncate / 其他 / 不清楚
日志新建权限及轮转配置片段：
部署是否删除重建项目目录或 target：
部署是否复制/移动文件进项目目录，或运行 chmod/chown：
是否允许给轮转/部署添加权限维护步骤：

【约束与现有配置】
保持原日志路径：是
服务器不额外复制日志：是
是否允许创建 chroot 目录及持久化挂载：
是否允许针对专用账号修改 ACL：
是否允许安装 acl 等必要系统包：
是否允许验证后 reload SSH 服务：
已有 SSH Include / Match / AllowUsers / AllowGroups 等相关片段：
是否有密码登录限制、PAM/MFA 或云平台账号策略：
已做过哪些配置步骤，是否有备份：

【Windows 与 MCP】
项目目录：D:\project\company\log-tools
Python/虚拟环境及 query.cmd 是否可用：
当前 config.json 的 servers 配置：    # 脱敏后提供，删除任何真实凭据
已有服务器别名是否需要保留：
客户端不需要 known_hosts 或服务器主机指纹。
希望使用当前进程环境变量还是 Windows 用户环境变量：
客户端是否与设置环境变量时使用同一个 Windows 用户：
```

必须先弄清的项目：系统版本、管理员权限、账号是否存在、认证方式、项目目录和禁止访问范围、已有 SSH 限制、轮转/部署方式。缺少这些信息时，AI 应先给出有针对性的只读检查命令，再生成最终修改命令。

## 3. 不清楚环境时，先收集这些信息

以下命令在服务器现有管理员终端运行，只查询状态，不修改配置。某个工具未安装时，把报错一起提供即可。输出中的地址和业务名称可脱敏；保留目录层级、权限和配置逻辑。

### 3.1 系统和工具

```bash
cat /etc/os-release
id
ssh -V
command -v sshd
command -v getfacl
command -v setfacl
command -v findmnt
command -v mount
mount --version
systemctl status sshd.service ssh.service --no-pager
command -v getenforce >/dev/null && getenforce
command -v aa-status >/dev/null && sudo aa-status
```

`ssh -V` 主要显示客户端版本；如服务端版本另有信息也一起提供。非 systemd 系统中的 `systemctl` 报错不代表 SSH 未运行。

### 3.2 账号和现有 SSH 规则

以下按计划用户名 `log_reader` 查询；如果你使用别的名字，请替换。

```bash
getent passwd log_reader
id log_reader
sudo grep -nE '^[[:space:]]*(Include|Match|Subsystem|ChrootDirectory|ForceCommand|DisableForwarding|AllowTcpForwarding|AllowAgentForwarding|AllowStreamLocalForwarding|X11Forwarding|PermitTunnel|PermitTTY|PermitUserRC|PasswordAuthentication|PubkeyAuthentication|KbdInteractiveAuthentication|ChallengeResponseAuthentication|AuthenticationMethods|AuthorizedKeysFile|AuthorizedKeysCommand|AllowUsers|AllowGroups|DenyUsers|DenyGroups|UsePAM)[[:space:]]' /etc/ssh/sshd_config
sudo find /etc/ssh/sshd_config.d -maxdepth 1 -type f -name '*.conf' -print
sudo grep -nE '^[[:space:]]*(Include|Match|Subsystem|ChrootDirectory|ForceCommand|DisableForwarding|AllowTcpForwarding|AllowAgentForwarding|AllowStreamLocalForwarding|X11Forwarding|PermitTunnel|PermitTTY|PermitUserRC|PasswordAuthentication|PubkeyAuthentication|KbdInteractiveAuthentication|ChallengeResponseAuthentication|AuthenticationMethods|AuthorizedKeysFile|AuthorizedKeysCommand|AllowUsers|AllowGroups|DenyUsers|DenyGroups|UsePAM)[[:space:]]' /etc/ssh/sshd_config.d/*.conf
```

某些系统没有 `sshd_config.d`。若主文件中 `Include` 指向其他位置，应继续查看对应配置。规则摘要不能代替完整的 `Match` 作用域分析；必要时提供相关配置段的上下文。不要提供 `/etc/shadow` 或 SSH 私钥。

### 3.3 项目结构、权限和挂载

```bash
for project in /home/sdscm-dev /home/sdscmrestock /home/sdscmcontract; do
    printf '\n=== %s ===\n' "$project"
    sudo ls -ld "$project"
    sudo find "$project" -maxdepth 1 -printf '%M %u:%g %f -> %l\n'
    sudo getfacl -p "$project"
    findmnt -T "$project" -o TARGET,SOURCE,FSTYPE,OPTIONS
    for level in debug error info warn; do
        sudo ls -ld "$project/$level"
        sudo getfacl -p "$project/$level" "$project/log_${level}.log"
    done
    sudo getfacl -p "$project/target"
done
findmnt -rn -o TARGET,SOURCE,FSTYPE,OPTIONS
```

若目录结构不同，先修改上面的路径。只需提供少量历史日志文件名和一个代表性的 ACL 样本，无需列出或上传全部日志正文。

### 3.4 轮转与部署

提供 Logback、Log4j2 或 logrotate 中涉及路径、文件命名、轮转、新文件权限的片段，以及部署中涉及目录重建、移动、`chmod`、`chown` 的片段。删除其中的数据库密码、访问令牌等内容。

默认 ACL 主要影响在目录内创建的对象；从其他位置移动过来的文件不一定获得它，创建模式和后续 `chmod` 也会影响有效权限。因此需要结合实际轮转和部署方式，而不能只查看当前 `644` 权限。[ACL 说明](https://man7.org/linux/man-pages/man5/acl.5.html)

## 4. 收到信息后，AI 应输出的完整实施步骤

最终步骤应区分“Linux 管理员执行”“Windows 用户执行”和“SFTP 验证会话”。所有路径、用户名、端口和服务名都应根据提供的信息填好。秘密值使用交互输入，不写进命令文本或聊天。

| 顺序 | 应输出的内容 | 完成条件 |
| --- | --- | --- |
| 1 | 参数汇总、现有状态检查、适用版本和所需工具 | 不把“已配置”当成全新安装重复覆盖 |
| 2 | 备份 SSH 配置、挂载配置及将要修改对象的 ACL | 说明备份位置和对应恢复方法 |
| 3 | 创建或复用专用账号、配置认证 | 无 sudo 权限，不加入应用写权限组；既有账号用途已确认 |
| 4 | 创建 chroot 根目录并检查各级属主与权限 | chroot 路径各级符合 OpenSSH 要求；不递归改业务目录属主 |
| 5 | 针对日志授予 ACL，拒绝非日志内容 | 当前日志、历史日志可读；target/JAR/配置不可读 |
| 6 | 建立只读目录映射 | 挂载视图只读，应用原路径仍能正常写日志 |
| 7 | 配置开机挂载及轮转/部署后的权限维护 | 重启后仍有效；新日志可读，新非日志内容不意外开放 |
| 8 | 添加专用账号的 SSH 限制 | SFTP 只读、chroot 生效、禁 Shell 和转发 |
| 9 | 检查语法与该账号最终生效的配置，再 reload | 保留管理员会话，新的管理员连接和 SFTP 连接均验证成功 |
| 10 | 配置 Windows 账号认证 | 登录与日志读取验证通过，凭据不进入 Git |
| 11 | 合并 MCP 服务器配置，实际列目录、下载和查询 | 三个项目逐一验证，不能只用 check 作为远端成功依据 |
| 12 | 给出回滚、常见问题和日常维护命令 | 仅撤销本次变更，不删除业务日志或已有配置 |

### 4.1 SSH 配置要求

最终方案通常使用 `Match User`、`ChrootDirectory` 和 `ForceCommand internal-sftp -R`，并禁用转发、TTY 和用户启动脚本。具体指令应按服务器 OpenSSH 版本选择；不要直接用新版语法覆盖旧版配置。`-R` 用于拒绝 SFTP 写操作。[SFTP 参数说明](https://man.openbsd.org/sftp-server)

必须分析已有 Include、Match、认证和允许用户规则，并用 `sshd -t` 和带实际连接条件的 `sshd -T -C ...` 检查结果。不要重复添加全局 `Subsystem sftp`，也不要为专用账号关闭管理员原有认证方式。密钥存放位置需明确是在宿主机路径还是 chroot 内路径。[sshd_config 说明](https://man.openbsd.org/sshd_config)

### 4.2 ACL 和轮转要求

生成命令时必须说明每一处 ACL 修改作用于哪些对象，关注 ACL mask 对现有组权限的影响；先保存原 ACL，不能用清空全部 ACL 的方式“修复权限”。[setfacl 说明](https://man7.org/linux/man-pages/man1/setfacl.1.html)

对混合放置日志和程序文件的项目根目录，需要明确采用什么默认拒绝策略，以及如何给新建的允许日志补充读取权限。如果部署重建 `target` 或移动文件进来，必须重新验证拒绝规则。应使用正常轮转或经过授权的测试来验证，不能为了测试删除、截断生产日志。

### 4.3 挂载要求

挂载命令必须明确源路径与目标路径，并按本机 util-linux 版本设置和验证 bind 视图只读。持久化配置应避免重复添加，检查子挂载和启动依赖；不得把应用原目录的整个文件系统改成只读。回滚只卸载本次新建映射，不删除源目录。不要在尚未确认卸载时对映射路径执行递归删除。

### 4.4 Windows 和 MCP 要求

- 新版客户端不校验服务器主机公钥，不需要 `known_hosts` 或管理员确认指纹；账号密码/私钥认证仍然需要。
- 密码认证使用 `password_env` 引用环境变量名；密钥认证使用 `private_key`，需要口令时再设置 `key_passphrase_env`。
- `root` 使用 SFTP 登录后看到的路径，例如 `/sdscmcontract`，而不是宿主机路径 `/home/sdscmcontract` 或 `/srv/sftp/log_reader/sdscmcontract`。
- 保留已有 `config.json` 配置项，合并服务器条目；`local_roots` 是允许导入的 Windows 本地日志目录，不负责限制远端账号。
- `query.cmd` 可补充读取配置引用的 Windows 用户/系统环境变量；其他 MCP 启动方式需要确认启动进程实际继承了变量。
- 下载缓存、报告、真实配置、私钥和 `known_hosts` 继续排除在 Git 之外。

使用本文件建议的别名时，最终验证应至少包含以下本地命令：

```powershell
Set-Location 'D:\project\company\log-tools'
.\query.cmd check
.\query.cmd files --server dev-sdscm
.\query.cmd files --server dev-sdscmrestock
.\query.cmd files --server dev-sdscmcontract
.\query.cmd latest --server dev-sdscmcontract --directory /sdscmcontract --pattern 'log_error.log'
```

`check` 中 `connected: true` 只说明本地 MCP 握手成功；`remote_connection_tested: false` 明确表示没有测试远端。`files` 才会连接远端。`latest` 会下载日志并查询；正在写入的文件可能因下载期间变化而被拒绝，此时应选择稳定的轮转文件并指定对应目录。

MCP 的目录查询不递归。验证历史目录时，应分别指定 `/sdscmcontract/error` 等路径。

## 5. 最终验收清单

- [ ] 专用账号能通过 SFTP 登录，三个项目的允许日志均能列出并下载。
- [ ] 能读取历史日志，并能在正常轮转后读取新生成的当前日志和归档日志。
- [ ] 无法读取 `target` 内的 JAR、配置文件和其他明确禁止的内容。
- [ ] 无法通过 SFTP 离开 chroot；宿主机其他目录未映射进来。
- [ ] 交互式 SSH 和执行远端命令不成功，转发配置被禁用。
- [ ] 上传、新建、改名、删除等写请求被拒绝；测试只使用专门的测试对象，不尝试修改生产日志。
- [ ] 原应用继续正常写日志，其他账号的已有权限没有被意外破坏。
- [ ] 挂载和 SSH 配置可持久化，并有明确恢复命令。
- [ ] 已验证目录重建、轮转和部署后的 ACL，不仅检查当前存量文件。
- [ ] Windows 主机密钥验证正常，MCP 能实际下载和查询，敏感文件未进入 Git。

## 6. 填好信息后，复制这段请求给 AI

```text
请根据下面的信息，为我的日志 MCP 生成完整的 Linux SFTP 专用只读账号配置流程。

目标：
1. 保持应用日志原路径不变，服务器不额外复制日志。
2. 使用专用账号、chroot、只读 bind mount、ACL 和只读 internal-sftp。
3. 只允许下载指定日志，禁止读取 target/JAR、配置和其他非日志内容。
4. 禁止 Shell、远端命令、写操作及转发，保留管理员正常登录能力。
5. 同时给出 Windows 环境变量或密钥、MCP config.json 和 query.cmd 验证步骤。

请先核对信息是否足够。未知关键项请给出针对性的只读查询命令，不要猜系统版本、账号状态、现有 SSH 规则或轮转行为；已提供的信息不要反复询问。

信息足够后，请一次性输出按执行顺序排列的完整步骤：
- 参数与现状汇总。
- 修改前备份与检查。
- Linux 管理员命令、Windows PowerShell 命令、SFTP 验证命令分别展示。
- 每一步的用途、预期结果，以及失败时应停在哪一步。
- SSH 配置语法/生效配置检查，通过后使用适合本机的 reload 命令。
- 挂载持久化、ACL mask 分析、新日志轮转和部署后的权限处理。
- 正向与反向权限验证，不测试删除或截断生产日志。
- 逐项回滚命令和常见错误排查。

请把非秘密参数填入具体命令，仅保留确实需要现场输入的秘密值。密码由我交互设置，私钥内容不发送给你。
请审查整项目目录映射下新增非日志文件的读取风险；如果现有约束无法保证要求，明确说明并给出需要我选择的替代方案，不要声称“只读”等于“只能读日志”。
不要递归 chmod/chown 整个项目，不要清空已有 ACL，不要覆盖已有 SSH/fstab/MCP 配置，不要自动关闭 SELinux/AppArmor。
这次只生成配置文档和命令，不直接登录服务器修改，也不自动创建 Git 提交。

以下是我填写的信息和只读检查结果：
（粘贴第 2 节填写结果，以及第 3 节相关输出）
```

## 7. 维护记录

实施完成后记录：账号名、认证方式、环境变量名、开放项目、挂载源/目标、配置片段位置、备份位置、轮转处理方式、实际验证时间和结果。只记录变量名与文件路径，不记录密码、私钥或业务日志正文。

本模板按当前 `log-tools` 的配置字段和命令整理。重新生成步骤时，应以目标服务器安装版本的手册及实际生效配置为准。
