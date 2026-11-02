"""Portable server profiles and per-user settings used by the desktop manager."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import DiscoverySettings, Settings, load_settings, remote_path
from .deployment import validate_alias, write_json
from .leases import Lease

VERSION = "0.2.3"


def application_home():
    return Path(os.environ.get("LOG_TOOLS_HOME", str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CompanyLogTools"))).resolve()


class ProjectProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)
    root: str
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    authentication: Literal["password", "key"] = "password"
    credential_group: str = Field(default="default", max_length=128)
    # Accept old shared profiles, but neither verify nor re-export their keys.
    host_keys: list[dict[str, str]] | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def check(self):
        if any(c.isspace() or ord(c) < 32 for c in self.host) or any(c in self.host for c in "/\\[],*?!"):
            raise ValueError("Invalid host")
        if not self.root.startswith("/"):
            raise ValueError("Root must be absolute")
        remote_path(self.root, "/")
        return self


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["log-tools-profile"]
    version: Literal[1]
    servers: dict[str, ProjectProfile] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def aliases(self):
        for name in self.servers:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
                raise ValueError("Invalid project alias")
        return self


def read_profile(path):
    path = Path(path)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("配置文件超过 2 MiB。")
    try:
        return Profile.model_validate_json(path.read_text(encoding="utf-8-sig"))
    except Exception:
        raise ValueError("导入文件格式无效。请选择 LogTools 服务器配置；需要地址、账号和日志目录，不能包含密码或私钥。") from None


def credential_target(server, group=None, kind="password"):
    group = group if group is not None else server.get("credential_group", "default")
    identity = json.dumps([server["host"], server.get("port") or 22, server["username"], group], ensure_ascii=True)
    return "LogTools/" + hashlib.sha256(identity.encode()).hexdigest()[:32] + "/" + kind


class DesktopSettings:
    def __init__(self, home=None):
        self.home = Path(home or application_home()).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "config.json"
        if not self.path.exists():
            with self.lock():
                if not self.path.exists():
                    write_json(self.path, {"cache_dir": str(self.home / "data"), "servers": {},
                                           "local_roots": [], "redact": True})
        load_settings(self.path)

    def lock(self):
        from contextlib import contextmanager

        @contextmanager
        def locked():
            lease = Lease(self.home / ".settings-lease")
            if not lease.acquire():
                raise ValueError("另一个设置窗口正在保存，请稍后重试。")
            try:
                yield
            finally:
                lease.close()
        return locked()

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8-sig"))

    def save(self, document, expected):
        Settings.model_validate(document)
        write_json(self.path, document, expected=expected, backup=True)

    def import_profile(self, profile, replace=False):
        with self.lock():
            original = self.path.read_bytes()
            document = json.loads(original)
            conflicts = set(document["servers"]) & set(profile.servers)
            if conflicts and not replace:
                raise ValueError("存在同名项目，请确认替换后再导入。")
            for alias, project in profile.servers.items():
                entry = {"protocol": "sftp", "host": project.host, "port": project.port,
                         "username": project.username, "root": project.root,
                         "credential_group": project.credential_group,
                         "discovery": project.discovery.model_dump()}
                old = document["servers"].get(alias, {})
                same_account = all(old.get(k) == entry.get(k) for k in ("host", "username")) and (old.get("port") or 22) == project.port
                if project.authentication == "password":
                    entry["password_credential"] = credential_target(entry, project.credential_group)
                    if same_account and old.get("password_env"):
                        entry["password_env"] = old["password_env"]
                else:
                    entry["private_key"] = ""  # local file must be selected after import
                    if same_account and old.get("private_key"):
                        entry["private_key"] = old["private_key"]
                    if same_account and old.get("key_passphrase_credential"):
                        entry["key_passphrase_credential"] = old["key_passphrase_credential"]
                document["servers"][alias] = entry
            self.save(document, original)
        return len(profile.servers)

    def export_profile(self):
        settings = load_settings(self.path)
        return profile_from_settings(settings).model_dump()

    def update_server(self, alias, updates):
        with self.lock():
            original = self.path.read_bytes()
            document = json.loads(original)
            document["servers"][alias] = updates
            self.save(document, original)

    def save_project(self, alias, updates, *, previous_alias=None, expected_entry=None,
                     save_credentials=None):
        """Create/edit/rename one project without overwriting another project."""
        validate_alias(alias)
        ProjectProfile(host=updates["host"], port=updates.get("port") or 22,
                       username=updates["username"], root=updates["root"])
        with self.lock():
            original = self.path.read_bytes()
            document = json.loads(original)
            servers = document["servers"]
            if alias in servers and alias != previous_alias:
                raise ValueError("项目名称已存在，请换一个名称，或先在左侧选中该项目进行编辑。")
            if previous_alias is not None:
                if previous_alias not in servers or servers[previous_alias] != expected_entry:
                    raise ValueError("该项目已被其他窗口修改，请重新选择项目后再编辑。")
                document["servers"] = {
                    (alias if name == previous_alias else name): (updates if name == previous_alias else entry)
                    for name, entry in servers.items()
                }
            else:
                servers[alias] = updates
            Settings.model_validate(document)
            # Check collisions/concurrent changes before touching shared credentials.
            if save_credentials is not None:
                save_credentials()
            self.save(document, original)

    def set_cache(self, path):
        path = Path(path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        with self.lock():
            original = self.path.read_bytes()
            document = json.loads(original)
            document["cache_dir"] = str(path)
            self.save(document, original)


def profile_from_settings(settings):
    servers = {}
    for alias, server in settings.servers.items():
        if server.protocol != "sftp":
            raise ValueError("图形版配置导入/导出目前支持 SFTP；FTP/FTPS 可继续使用原配置。")
        # Group by endpoint/account; no credential names, values, or key paths are exported.
        servers[alias] = ProjectProfile(host=server.host, port=server.port or 22,
                                       username=server.username, root=server.root,
                                       credential_group=server.credential_group, discovery=server.discovery,
                                       authentication="key" if server.private_key is not None else "password")
    return Profile(format="log-tools-profile", version=1, servers=servers)


def mcp_entry(config_path):
    if getattr(sys, "frozen", False):
        command = str(Path(sys.executable).with_name("log-tools-mcp.exe"))
        args = ["--config", str(config_path)]
    else:
        command = sys.executable
        args = ["-m", "log_tools", "--config", str(config_path)]
    return {"command": command, "args": args, "enabled": True,
            "startup_timeout_sec": 30, "tool_timeout_sec": 120}


def register_codex(config_path, codex_path, *, replace=False):
    import tomlkit
    path = Path(codex_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_bytes() if path.exists() else None
    document = tomlkit.parse(original.decode("utf-8-sig") if original else "")
    servers = document.setdefault("mcp_servers", tomlkit.table())
    if "log-tools" in servers and not replace:
        raise ValueError("Codex 中已有 log-tools，必须确认替换。")
    servers["log-tools"] = mcp_entry(config_path)
    content = tomlkit.dumps(document).encode("utf-8")
    # Validate before replacing the user's file and preserve all unrelated settings.
    import tomllib
    tomllib.loads(content.decode())
    if (path.read_bytes() if path.exists() else None) != original:
        raise ValueError("Codex 配置发生变化，请重试。")
    if original is not None:
        with path.with_name(f"config.toml.logtools-{uuid.uuid4().hex}.bak").open("xb") as stream:
            stream.write(original)
    temporary = path.with_name(f".logtools-{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        if (path.read_bytes() if path.exists() else None) != original:
            raise ValueError("Codex 配置发生变化，请重试。")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
