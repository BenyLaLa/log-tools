from __future__ import annotations

import json
import os
import posixpath
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def remote_path(value: str, root: str) -> str:
    if not value or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Remote path contains forbidden characters")
    if ".." in value.split("/"):
        raise ValueError("Parent traversal is not allowed")
    result = posixpath.normpath(value if value.startswith("/") else posixpath.join(root, value))
    root = posixpath.normpath(root)
    if result != root and not result.startswith(root.rstrip("/") + "/"):
        raise ValueError("Remote path is outside the configured root")
    return result


class DiscoverySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    history_dirs: list[str] = Field(default_factory=lambda: ["error", "warn", "info", "debug"], max_length=100)
    max_depth: int = Field(default=3, ge=0, le=10)
    pattern: str = Field(default="*.log*", min_length=1, max_length=200)
    exclude_dirs: list[str] = Field(default_factory=lambda: ["target", ".git"], max_length=100)
    max_entries: int = Field(default=10000, ge=1, le=10000)

    @model_validator(mode="after")
    def validate_names(self):
        for name in self.history_dirs + self.exclude_dirs:
            if not name or name in (".", "..") or any(c in name for c in "/\\") or any(ord(c) < 32 for c in name):
                raise ValueError("Discovery directories must be single directory names")
        return self


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["sftp", "ftp", "ftps"] = "sftp"
    host: str = Field(min_length=1)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str = Field(min_length=1)
    password_env: str | None = None
    password_credential: str | None = None
    credential_group: str = "default"
    private_key: str | None = None
    key_passphrase_env: str | None = None
    key_passphrase_credential: str | None = None
    known_hosts: str | None = None  # Legacy compatibility only; no longer read.
    ca_file: str | None = None
    root: str
    allow_plain_ftp: bool = False
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    encoding: str = "utf-8"
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)

    @model_validator(mode="after")
    def validate_server(self):
        if not self.root.startswith("/"):
            raise ValueError("Server root must be an absolute POSIX path")
        self.root = remote_path(self.root, "/")
        if self.protocol == "ftp" and not self.allow_plain_ftp:
            raise ValueError("Plain FTP requires allow_plain_ftp=true")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cache_dir: Path = Path("data")
    servers: dict[str, ServerConfig] = Field(default_factory=dict)
    local_roots: list[Path] = Field(default_factory=list)
    log_encoding: Literal["utf-8", "utf-8-sig", "gb18030", "utf-16"] = "utf-8-sig"
    timezone_offset: str = Field(default="+08:00", pattern=r"^[+-](?:0\d|1[0-3]):[0-5]\d$")
    max_download_bytes: int = Field(default=1024**3, ge=1, le=100 * 1024**3)
    max_files: int = Field(default=200, ge=1, le=2000)
    scan_max_bytes: int = Field(default=64 * 1024**2, ge=1, le=2 * 1024**3)
    scan_timeout_seconds: int = Field(default=20, ge=1, le=120)
    redact: bool = True


def load_settings(config_path: str | Path) -> Settings:
    path = Path(config_path).resolve()
    settings = Settings.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))

    def absolute(value):
        result = Path(os.path.expandvars(str(value))).expanduser()
        return result.resolve() if result.is_absolute() else (path.parent / result).resolve()

    settings.cache_dir = absolute(settings.cache_dir)
    settings.local_roots = [absolute(root) for root in settings.local_roots]
    for server in settings.servers.values():
        for field in ("private_key", "ca_file"):
            if value := getattr(server, field):
                setattr(server, field, str(absolute(value)))
    return settings


def secret(name: str | None, credential: str | None = None) -> str | None:
    from .credentials import read_credential, windows_environment
    if credential:
        value = read_credential(credential)
        if value:
            return value
        if not name:
            raise ValueError("Required Windows credential is missing; open LogTools settings")
    if name is None:
        return None
    value = os.environ.get(name) or windows_environment(name)
    if not value:
        raise ValueError(f"Required credential environment variable is missing: {name}")
    return value
