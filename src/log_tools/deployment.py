"""Windows distribution packaging and an interactive, SFTP account wizard."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import urllib.request
import warnings
import zipfile
from datetime import UTC, datetime
from pathlib import Path

# Explicit allowlist: never zip the project tree, Git index, or local runtime directories.
PACKAGE_FILES = (
    ".gitignore", "README.md", "pyproject.toml", "requirements.lock.txt",
    "config.example.json", "start.cmd", "query.cmd", "setup.cmd", "configure.cmd",
    "pack.cmd", "scripts/setup.ps1", "scripts/run.ps1", "docs/queries.md",
    "docs/sftp-account-setup.md", "docs/distribution.md", "examples/servers.json",
    "examples/logs/order.log", "src/log_tools/__init__.py", "src/log_tools/__main__.py",
    "src/log_tools/client.py", "src/log_tools/config.py", "src/log_tools/deployment.py",
    "src/log_tools/query.py", "src/log_tools/remote.py", "src/log_tools/server.py",
    "src/log_tools/service.py", "src/log_tools/store.py",
    "src/log_tools/credentials.py", "src/log_tools/leases.py",
)

PYTHON_VERSION = "3.13.17"
INSTALLER_NAME = f"python-{PYTHON_VERSION}-amd64.exe"
INSTALLER_SHA256 = "5da012101ba2e17113a9a1cd901e9ce1441086f2b7ecb5de06ec94163899a190"


def locked_requirements(root):
    data = (root / "requirements.lock.txt").read_bytes()
    lines = [line.strip() for line in data.decode("utf-8-sig").splitlines()]
    requirements = [line for line in lines if line and not line.startswith("#")]
    if not requirements or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!]+", line)
        for line in requirements
    ):
        raise ValueError("离线锁定文件只允许 name==version，不能包含 URL、索引或其他 pip 参数。")
    return data


def offline_payload(root, installer, wheels):
    lock = locked_requirements(root)
    installer_data = installer.read_bytes()
    if hashlib.sha256(installer_data).hexdigest() != INSTALLER_SHA256:
        raise ValueError("Python 安装器 SHA256 不匹配，不能生成离线包。")
    payload = {f"offline/{INSTALLER_NAME}": installer_data}
    wheel_files = sorted(wheels.glob("*.whl"))
    if not wheel_files:
        raise ValueError("离线依赖目录为空。")
    for wheel in wheel_files:
        if not re.fullmatch(r"[A-Za-z0-9_.+!-]+\.whl", wheel.name) or wheel.is_symlink():
            raise ValueError("离线 wheel 文件名或类型无效。")
        payload[f"offline/wheels/{wheel.name}"] = wheel.read_bytes()
    payload["offline/manifest.json"] = json_bytes({
        "format": 1, "platform": "cp313-win_amd64", "python_version": PYTHON_VERSION,
        "requirements_sha256": hashlib.sha256(lock).hexdigest(),
        "files": [
            {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(payload.items())
        ],
    })
    return payload


def prepare_offline_assets(root, installer_path=None):
    if os.name != "nt" or sys.version_info[:2] != (3, 13) or struct.calcsize("P") != 8:
        raise ValueError("请在 Windows x64 的 Python 3.13 环境中制作离线包。")
    locked_requirements(root)
    cache = root / ".setup-cache"
    cache.mkdir(exist_ok=True)
    installer = installer_path.resolve() if installer_path else cache / INSTALLER_NAME
    if not installer.exists():
        if installer_path:
            raise ValueError("指定的 Python 安装器不存在。")
        print("正在从 Python 官网获取离线安装器……", flush=True)
        url = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{INSTALLER_NAME}"
        temporary = installer.with_suffix(".exe.part")
        try:
            with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as stream:
                while block := response.read(1024 * 1024):
                    stream.write(block)
            temporary.replace(installer)
        finally:
            temporary.unlink(missing_ok=True)
    if hashlib.sha256(installer.read_bytes()).hexdigest() != INSTALLER_SHA256:
        raise ValueError("Python 安装器 SHA256 不匹配。请删除损坏的安装缓存后重试。")
    # Check the publisher on the connected packaging machine. Offline recipients use
    # the pinned SHA256 instead of a certificate-chain check that may contact a CA.
    quoted_installer = str(installer).replace("'", "''")
    command = (
        "$ErrorActionPreference = 'Stop'; "
        "Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/"
        "Microsoft.PowerShell.Security.psd1'); "
        f"$s = Get-AuthenticodeSignature -LiteralPath '{quoted_installer}'; "
        "if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch "
        "'Python Software Foundation') { throw 'Python publisher signature invalid' }"
    )
    subprocess.run([
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command,
    ], check=True)
    environment = {k: v for k, v in os.environ.items() if not k.upper().startswith("PIP_")}
    environment.update({
        "PIP_CONFIG_FILE": os.devnull, "PIP_CACHE_DIR": str(cache / "pip"),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1",
    })
    with tempfile.TemporaryDirectory(prefix="offline-build-", dir=cache) as temporary:
        wheels = Path(temporary)
        print("正在下载/构建锁定版本的 Windows 离线 wheels……", flush=True)
        subprocess.run([
            sys.executable, "-m", "pip", "wheel", "--no-deps", "--index-url",
            "https://pypi.org/simple", "-r", str(root / "requirements.lock.txt"),
            "--wheel-dir", str(wheels),
        ], env=environment, check=True)
        print("正在用本地 wheels 检查离线依赖是否完整……", flush=True)
        subprocess.run([
            sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
            "--no-index", "--no-cache-dir", "--only-binary=:all:",
            "--find-links", str(wheels), "-r", str(root / "requirements.lock.txt"),
        ], env=environment, check=True)
        return offline_payload(root, installer, wheels)


def stamp():
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def client_config(python, config):
    return {"mcpServers": {"log-tools": {
        "command": str(python), "args": ["-m", "log_tools", "--config", str(config)],
    }}}


def build_package(root, *, offline=False, installer_path=None):
    root = root.resolve()
    contents = {}
    for name in PACKAGE_FILES:
        path = root / name
        # Refuse symlinks/junctions, including parent components inside this project.
        if path.resolve() != path.absolute() or not path.is_file():
            raise ValueError(f"打包文件缺失或是链接：{name}")
        contents[name] = path.read_bytes()
    template = json.loads(contents["config.example.json"].decode("utf-8-sig"))
    if template.get("servers") != {}:
        raise ValueError("config.example.json 的 servers 必须为空；真实连接配置不能打包。")
    # Generate a generic example rather than copy the sender's client configuration.
    contents["examples/mcp-client.json"] = json_bytes(client_config(
        r"C:\path\to\log-tools\.venv\Scripts\python.exe",
        r"C:\path\to\log-tools\config.json",
    ))
    if offline:
        contents.update(prepare_offline_assets(root, installer_path))
    manifest = {"format": 1, "files": [
        {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in sorted(contents.items())
    ]}
    contents["PACKAGE-MANIFEST.json"] = json_bytes(manifest)
    output_dir = root / "dist"
    output_dir.mkdir(exist_ok=True)
    variant = "offline-" if offline else ""
    archive = output_dir / f"log-tools-windows-{variant}{stamp()}.zip"
    temporary = archive.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, data in sorted(contents.items()):
                bundle.writestr(f"log-tools/{name}", data)
        with zipfile.ZipFile(temporary) as bundle:
            if bundle.testzip() is not None:
                raise ValueError("ZIP 完整性检查失败。")
        temporary.rename(archive)
    finally:
        temporary.unlink(missing_ok=True)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(
        f"{checksum}  {archive.name}\n", encoding="ascii",
    )
    return archive, manifest


def write_json(path, value, *, expected=None, backup=False):
    """Check the previous content before replacing; keep local config backups private."""
    current = path.read_bytes() if path.exists() else None
    if current != expected:
        raise ValueError(f"文件在操作期间发生变化，请重试：{path.name}")
    if backup and current is not None:
        backups = path.parent / ".setup-backups"
        backups.mkdir(exist_ok=True)
        with (backups / f"{path.stem}-{stamp()}.json").open("xb") as stream:
            stream.write(current)
    descriptor, temp_name = tempfile.mkstemp(prefix=".setup-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json_bytes(value))
        if (path.read_bytes() if path.exists() else None) != expected:
            raise ValueError(f"文件在操作期间发生变化，请重试：{path.name}")
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def initialize(root):
    from log_tools.config import Settings, load_settings

    config = root / "config.json"
    if not config.exists():
        value = json.loads((root / "config.example.json").read_text(encoding="utf-8-sig"))
        Settings.model_validate(value)
        write_json(config, value)
        print("已创建 config.json（服务器列表为空）。")
    else:
        load_settings(config)
        print("保留已有 config.json。")
    client_path = root / "mcp-client.local.json"
    if not client_path.exists():
        write_json(client_path, client_config(root / ".venv/Scripts/python.exe", config))
        print("已生成本机 MCP 客户端配置：mcp-client.local.json。")
    else:
        print("保留已有 mcp-client.local.json；移动项目后请检查其中的绝对路径。")


def save_user_secret(name, value):
    if os.name != "nt":
        raise ValueError("此向导的用户环境变量写入仅支持 Windows。")
    import winreg

    # Avoid shell commands, command-line passwords, JSON credentials and console echo.
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    os.environ[name] = value


def ask(label, default=None):
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default or ""


def yes(label, default=False):
    return ask(label + " (y/n)", "y" if default else "n").lower() == "y"


def validate_alias(alias):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", alias):
        raise ValueError("别名只允许 1–64 位英文字母、数字、下划线和连字符。")


def validate_env_name(name):
    if not re.fullmatch(r"LOG_[A-Z0-9_]+", name):
        raise ValueError("凭据环境变量名必须以 LOG_ 开头，只使用大写字母、数字和下划线。")


def configure(root):
    from log_tools.config import Settings

    initialize(root)
    print("\nSFTP 配置向导。可反复运行 configure.cmd 添加多个项目；不会修改 Linux 服务器。")
    print("需要：地址/端口、账号、SFTP 内日志路径和登录凭据。不需要 known_hosts 或主机指纹。")
    print("密码和私钥口令只在本机隐藏输入；Windows 用户环境变量不是加密密码库。")
    if not yes("现在添加或更新一个服务器", True):
        return
    while True:
        config_path = root / "config.json"
        original = config_path.read_bytes()
        document = json.loads(original.decode("utf-8-sig"))
        alias = ask("服务器别名（例如 dev-sdscmcontract）")
        validate_alias(alias)
        if alias in document.get("servers", {}) and not yes("此别名已存在，确认替换这个服务器条目"):
            return
        host = ask("服务器地址（不含协议和端口）")
        if not host or any(c.isspace() for c in host) or any(c in host for c in "/\\[]"):
            raise ValueError("服务器地址格式错误。IPv6 请填写不带方括号的地址。")
        port = int(ask("SFTP 端口", "22"))
        username = ask("只读账号", "log_reader")
        remote_root = ask("SFTP 登录后看到的日志目录（例如 /sdscmcontract）")
        auth = ask("认证方式：1=密码，2=私钥", "1")
        server = {
            "protocol": "sftp", "host": host, "port": port, "username": username,
            "root": remote_root, "timeout_seconds": 30,
        }
        env_name = None
        if auth == "1":
            env_name = ask("密码环境变量名", "LOG_READER_PASSWORD")
            validate_env_name(env_name)
            server["password_env"] = env_name
        elif auth == "2":
            key_path = Path(ask("本机私钥绝对路径").strip('"')).expanduser()
            if not key_path.is_absolute() or not key_path.is_file():
                raise ValueError("私钥必须是本机已存在的文件，并使用绝对路径。")
            server["private_key"] = str(key_path.resolve())
            if yes("私钥是否有口令"):
                env_name = ask("私钥口令环境变量名", "LOG_SFTP_KEY_PASSPHRASE")
                validate_env_name(env_name)
                server["key_passphrase_env"] = env_name
        else:
            raise ValueError("认证方式必须选择 1 或 2。")
        document.setdefault("servers", {})[alias] = server
        Settings.model_validate(document)
        if not yes("保存此服务器配置", True):
            return
        secret_value = None
        if env_name and yes(f"在本机输入并保存 {env_name} 到当前 Windows 用户环境变量", True):
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                secret_value = getpass.getpass("密码或私钥口令（输入不回显）: ")
            if not secret_value:
                raise ValueError("凭据不能为空；没有保存本次配置。")
        if config_path.read_bytes() != original:
            raise ValueError("config.json 已被其他程序修改，请重试。")
        if secret_value is not None:
            save_user_secret(env_name, secret_value)
            secret_value = None
        write_json(config_path, document, expected=original, backup=True)
        print(f"已保存 {alias}。下一步执行：.\\query.cmd files --server {alias}")
        print("配置已保存，尚未验证账号登录与日志读取权限。")
        if not yes("继续添加另一个项目"):
            return


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pack", "initialize", "configure"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--offline", action="store_true", help="Bundle Python and offline wheels")
    parser.add_argument("--installer-path", type=Path, help="Reuse a verified official installer")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.action == "pack":
            archive, manifest = build_package(
                root, offline=args.offline, installer_path=args.installer_path,
            )
            print(f"分享包：{archive}\nSHA256：{archive}.sha256")
            print(f"已打包 {len(manifest['files'])} 个文件；完整清单见 ZIP 内 PACKAGE-MANIFEST.json。")
            if args.offline:
                print("离线包已包含 Python 安装器和依赖。接收方 setup.cmd 自动使用离线模式。")
            print("未包含下载日志、真实配置、凭据、虚拟环境和 Git 历史。接收方解压后运行 setup.cmd。")
        elif args.action == "initialize":
            initialize(root)
        else:
            configure(root)
    except (Exception, KeyboardInterrupt) as error:
        # Pydantic errors can echo input values; never print validation input bodies.
        if error.__class__.__name__ == "ValidationError":
            print("配置格式不符合要求。请检查端口、绝对远端路径及必填字段。", file=sys.stderr)
        elif isinstance(error, KeyboardInterrupt):
            print("操作已取消。", file=sys.stderr)
        else:
            print(f"操作失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
