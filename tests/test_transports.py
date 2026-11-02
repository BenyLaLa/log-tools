import os
import socket
import threading

import paramiko
import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from log_tools.config import ServerConfig
from log_tools.remote import FtpRemote, SftpRemote


@pytest.fixture
def ftp_server(tmp_path, monkeypatch):
    root = tmp_path / "remote"
    root.mkdir()
    (root / "app.log").write_bytes(b"2026-10-22 10:00:00 ERROR ftp integration\n")
    auth = DummyAuthorizer()
    auth.add_user("reader", "test-only-password", str(root), perm="elr")

    class Handler(FTPHandler):
        authorizer = auth

    server = FTPServer(("127.0.0.1", 0), Handler)
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.1}, daemon=True)
    thread.start()
    monkeypatch.setenv("TEST_LOG_PASSWORD", "test-only-password")
    try:
        yield ServerConfig(
            protocol="ftp",
            host="127.0.0.1",
            port=port,
            username="reader",
            password_env="TEST_LOG_PASSWORD",
            root="/",
            allow_plain_ftp=True,
        )
    finally:
        server.close_all()
        thread.join(timeout=3)


def test_real_ftp_list_download_and_search(service, ftp_server):
    service.settings.servers["test"] = ftp_server
    result = service.list_remote("test")
    assert [row["name"] for row in result["files"]] == ["app.log"]
    job = service.download("test", ["/app.log"])
    service.jobs[job["job_id"]].result(timeout=10)
    status = service.status(job["job_id"])
    assert status["status"] == "completed", status
    assert service.query.search(job["dataset_id"], query="ftp integration")["matches"]


def test_reusable_client_remote_latest(ftp_server, tmp_path):
    import json
    import subprocess
    import sys

    config = tmp_path / "client-test.json"
    config.write_text(
        json.dumps(
            {
                "cache_dir": str(tmp_path / "client-cache"),
                "servers": {"test": ftp_server.model_dump()},
            }
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "log_tools.client",
            "--config",
            str(config),
            "latest",
            "--server",
            "test",
            "--pattern",
            "*.log",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["download"]["status"] == "completed"
    assert "ftp integration" in data["errors"][0]["text"]
    assert not data["incomplete"]


class AuthServer(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        return (
            paramiko.AUTH_SUCCESSFUL
            if (username, password) == ("reader", "test-only-password")
            else paramiko.AUTH_FAILED
        )

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return (
            paramiko.OPEN_SUCCEEDED
            if kind == "session"
            else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        )


class TestSftp(paramiko.SFTPServerInterface):
    __test__ = False

    def __init__(self, server, root):
        super().__init__(server)
        self.root = root

    def path(self, value):
        result = (self.root / value.lstrip("/")).resolve()
        if not result.is_relative_to(self.root):
            raise PermissionError
        return result

    def canonicalize(self, path):
        if path == "/logs/escape.log":
            return "/outside/secret.log"
        return "/" + path.lstrip("/")

    def list_folder(self, path):
        result = []
        for item in self.path(path).iterdir():
            attr = paramiko.SFTPAttributes.from_stat(item.lstat())
            attr.filename = item.name
            result.append(attr)
        return result

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.path(path).stat())
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    lstat = stat

    def open(self, path, flags, attr):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            return paramiko.SFTP_PERMISSION_DENIED
        handle = paramiko.SFTPHandle(flags)
        handle.readfile = self.path(path).open("rb")
        return handle


@pytest.fixture
def sftp_server(tmp_path, monkeypatch):
    root = tmp_path / "sftp"
    (root / "logs").mkdir(parents=True)
    (root / "logs/app.log").write_bytes(b"2026-10-22 10:00:00 ERROR sftp integration\n")
    key = paramiko.RSAKey.generate(2048)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    stop = threading.Event()
    transports = []

    def accept():
        while not stop.is_set():
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            transport = paramiko.Transport(sock)
            transports.append(transport)
            transport.add_server_key(key)
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer, TestSftp, root=root)
            try:
                transport.start_server(server=AuthServer())
            except (EOFError, ConnectionError, paramiko.SSHException):
                transport.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    hosts = tmp_path / "known_hosts"
    hosts.write_text(f"[127.0.0.1]:{port} {key.get_name()} {key.get_base64()}\n")
    monkeypatch.setenv("TEST_LOG_PASSWORD", "test-only-password")
    config = ServerConfig(
        host="127.0.0.1",
        port=port,
        username="reader",
        root="/logs",
        password_env="TEST_LOG_PASSWORD",
        known_hosts=str(hosts),
    )
    try:
        yield config
    finally:
        stop.set()
        listener.close()
        for transport in transports:
            transport.close()
        thread.join(timeout=3)


def test_real_sftp_and_root_escape(service, sftp_server):
    service.settings.servers["test"] = sftp_server
    listing = service.list_remote("test")
    assert listing["files"][0]["name"] == "app.log"
    job = service.download("test", ["/logs/app.log"])
    service.jobs[job["job_id"]].result(timeout=10)
    status = service.status(job["job_id"])
    assert status["status"] == "completed", status
    assert service.query.search(job["dataset_id"], query="sftp integration")["matches"]
    connection = SftpRemote(sftp_server)
    try:
        with pytest.raises(ValueError, match="outside"):
            connection.metadata("/logs/escape.log")
    finally:
        connection.close()


@pytest.mark.parametrize("legacy", ["absent", "missing-file", "wrong-key"])
def test_sftp_accepts_host_without_loading_or_saving_keys(sftp_server, tmp_path, legacy):
    hosts = tmp_path / "legacy-hosts"
    if legacy == "wrong-key":
        other = paramiko.RSAKey.generate(1024)
        hosts.write_text(f"[127.0.0.1]:{sftp_server.port} {other.get_name()} {other.get_base64()}\n")
    original = hosts.read_bytes() if hosts.exists() else None
    sftp_server.known_hosts = None if legacy == "absent" else str(hosts)
    connection = SftpRemote(sftp_server)
    try:
        assert list(connection.listing("/logs"))[0]["name"] == "app.log"
    finally:
        connection.close()
    assert (hosts.read_bytes() if hosts.exists() else None) == original


def test_sftp_still_rejects_incorrect_account_password(sftp_server, monkeypatch):
    sftp_server.known_hosts = None
    monkeypatch.setenv("TEST_LOG_PASSWORD", "incorrect-password")
    with pytest.raises(paramiko.AuthenticationException):
        SftpRemote(sftp_server)


def test_ftps_requires_certificate_and_data_encryption(monkeypatch):
    calls = []

    class FakeTls:
        def __init__(self, context, encoding):
            assert context.check_hostname
            assert context.verify_mode == 2

        def connect(self, *args, **kwargs):
            calls.append("connect")

        def login(self, *args):
            calls.append("login")

        def prot_p(self):
            calls.append("encrypted-data")

        def set_pasv(self, enabled):
            assert enabled

        def close(self):
            pass

    monkeypatch.setattr("log_tools.remote.ftplib.FTP_TLS", FakeTls)
    FtpRemote(ServerConfig(protocol="ftps", host="example", username="u", root="/logs")).close()
    assert calls == ["connect", "login", "encrypted-data"]


@pytest.mark.parametrize("transport", ["ftp_server", "sftp_server"])
def test_real_nested_history_download_and_query(service, request, tmp_path, transport):
    import gzip

    config = request.getfixturevalue(transport)
    root = tmp_path / ("remote" if transport == "ftp_server" else "sftp/logs")
    (root / "error/2026").mkdir(parents=True)
    (root / "target").mkdir()
    (root / "target/app.jar").write_bytes(b"not a log")
    (root / "target/hidden.log").write_text("must not be discovered")
    with gzip.open(root / "error/2026/old.log.gz", "wb") as stream:
        stream.write(b"2026-10-29 08:00:00 ERROR archived-sample\n")
    service.settings.servers["test"] = config
    first = service.list_remote("test", limit=1)
    assert first["total_files"] == 2 and first["has_more"]
    second = service.list_remote("test", cursor=first["next_cursor"], limit=1)
    assert not second["truncated"]
    files = first["files"] + second["files"]
    archive = next(row["remote_path"] for row in files if row["name"].endswith(".gz"))
    job = service.download("test", [archive])
    service.jobs[job["job_id"]].result(timeout=10)
    assert service.status(job["job_id"])["status"] == "completed"
    assert service.query.search(job["dataset_id"], query="archived-sample")["matches"]


def test_sftp_explicit_symlink_directory_is_not_followed():
    import stat
    from types import SimpleNamespace

    remote = object.__new__(SftpRemote)
    remote.config = ServerConfig(host="example.test", username="reader", root="/logs")
    remote.sftp = SimpleNamespace(lstat=lambda path: SimpleNamespace(st_mode=stat.S_IFLNK))
    with pytest.raises(ValueError, match="Symlink"):
        list(remote.entries("/logs/link"))
