from __future__ import annotations

import ftplib
import posixpath
import ssl
import stat
from contextlib import contextmanager
from datetime import datetime, timezone

import paramiko

from .config import ServerConfig, remote_path, secret


class SftpRemote:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.client = paramiko.SSHClient()
        # Requested mode: accept the presented host key for this connection.
        # Do not load system/user host keys or set a persistence filename.
        # Password/private-key authentication and path restrictions still apply.
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.client.connect(
                hostname=config.host,
                port=config.port or 22,
                username=config.username,
                password=secret(config.password_env, config.password_credential),
                key_filename=config.private_key,
                passphrase=secret(config.key_passphrase_env, config.key_passphrase_credential),
                look_for_keys=False,
                allow_agent=False,
                timeout=config.timeout_seconds,
                banner_timeout=config.timeout_seconds,
                auth_timeout=config.timeout_seconds,
            )
            self.sftp = self.client.open_sftp()
            self.sftp.get_channel().settimeout(config.timeout_seconds)
            self.root = self.sftp.normalize(config.root)
        except Exception:
            self.client.close()
            raise

    def checked(self, path):
        path = remote_path(path, self.config.root)
        canonical = self.sftp.normalize(path)
        remote_path(canonical, self.root)
        return canonical

    def entries(self, directory):
        directory = remote_path(directory, self.config.root)
        # Refuse symlinks in explicitly requested paths as well as discovered children.
        relative = posixpath.relpath(directory, self.config.root)
        current = self.config.root
        for component in ([] if relative == "." else relative.split("/")):
            current = posixpath.join(current, component)
            if stat.S_ISLNK(self.sftp.lstat(current).st_mode):
                raise ValueError("Symlink directories are not scanned")
        directory = self.checked(directory)
        for item in self.sftp.listdir_iter(directory):
            mode = item.st_mode or 0
            kind = "file" if stat.S_ISREG(mode) else "directory" if stat.S_ISDIR(mode) else "symlink" if stat.S_ISLNK(mode) else "other"
            yield {"name": item.filename, "size": item.st_size, "mtime": item.st_mtime, "type": kind}

    def listing(self, directory):
        for item in self.entries(directory):
            if item["type"] == "file":
                yield {key: value for key, value in item.items() if key != "type"}

    def metadata(self, path):
        item = self.sftp.lstat(self.checked(path))
        if not stat.S_ISREG(item.st_mode):
            raise ValueError("Only regular files may be downloaded")
        return {"size": item.st_size, "mtime": item.st_mtime}

    def download(self, path, consume):
        with self.sftp.open(self.checked(path), "rb") as source:
            while block := source.read(64 * 1024):
                consume(block)

    def close(self):
        self.client.close()


class FtpRemote:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.client = (
            ftplib.FTP_TLS(
                context=ssl.create_default_context(cafile=config.ca_file), encoding=config.encoding
            )
            if config.protocol == "ftps"
            else ftplib.FTP(encoding=config.encoding)
        )
        try:
            self.client.connect(config.host, config.port or 21, timeout=config.timeout_seconds)
            self.client.login(config.username, secret(config.password_env) or "")
            if config.protocol == "ftps":
                self.client.prot_p()
            self.client.set_pasv(True)
        except Exception:
            self.client.close()
            raise

    def entries(self, directory):
        directory = remote_path(directory, self.config.root)
        # Only explicit MLSD file/dir types are traversed; links/unknowns are skipped.
        for name, facts in self.client.mlsd(directory, facts=["type", "size", "modify"]):
            kind = {"file": "file", "dir": "directory"}.get(facts.get("type"), "other")
            yield {"name": name, "size": int(facts.get("size", 0)), "mtime": facts.get("modify"), "type": kind}

    def listing(self, directory):
        for item in self.entries(directory):
            if item["type"] == "file":
                yield {key: value for key, value in item.items() if key != "type"}

    def metadata(self, path):
        path = remote_path(path, self.config.root)
        self.client.voidcmd("TYPE I")
        size = self.client.size(path)
        if size is None:
            raise ValueError("FTP server must support SIZE")
        modified = self.client.sendcmd("MDTM " + path)
        return {"size": size, "mtime": modified.removeprefix("213 ")}

    def download(self, path, consume):
        self.client.retrbinary("RETR " + remote_path(path, self.config.root), consume, 64 * 1024)

    def close(self):
        self.client.close()


@contextmanager
def connect(config):
    connection = SftpRemote(config) if config.protocol == "sftp" else FtpRemote(config)
    try:
        yield connection
    finally:
        connection.close()


def modification_time(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    if value:
        return (
            datetime.strptime(value.split(".")[0], "%Y%m%d%H%M%S")
            .replace(tzinfo=timezone.utc)
            .isoformat()
        )
    return None


def valid_child(directory, name, root):
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError("Invalid remote filename")
    return remote_path(posixpath.join(directory, name), root)
