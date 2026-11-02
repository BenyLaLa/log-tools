import json
import os
import subprocess
import sys
import time
from pathlib import Path

import paramiko
import pytest

from log_tools.config import Settings, load_settings, secret
from log_tools.desktop_config import DesktopSettings, Profile, read_profile, register_codex
from log_tools.service import LogService


def profile(key=None, **overrides):
    project = {"host": "logs.example.test", "username": "reader", "port": 2222,
               "root": "/app"}
    if key:
        project["host_keys"] = [{"type": key.get_name(), "key": key.get_base64()}]
    project.update(overrides)
    return Profile.model_validate({"format": "log-tools-profile", "version": 1,
                                   "servers": {"app": project, "app-two": {**project, "root": "/app-two"}}})


def test_profile_import_export_needs_no_public_keys_and_excludes_secrets(tmp_path):
    store = DesktopSettings(tmp_path)
    store.import_profile(profile())
    config = store.read()
    assert config["servers"]["app"]["password_credential"] == config["servers"]["app-two"]["password_credential"]
    assert "known_hosts" not in config["servers"]["app"]
    assert not (tmp_path / "trust").exists()
    config["servers"]["app"]["password_env"] = "DO_NOT_EXPORT_ENV"
    store.update_server("app", config["servers"]["app"])
    exported = json.dumps(store.export_profile())
    assert "DO_NOT_EXPORT_ENV" not in exported
    assert "password_credential" not in exported
    assert "known_hosts" not in exported
    assert "host_keys" not in exported
    assert str(tmp_path) not in exported
    target = DesktopSettings(tmp_path / "receiver")
    target.import_profile(Profile.model_validate_json(exported))
    assert target.read()["servers"]["app-two"]["root"] == "/app-two"


def test_import_and_export_ignore_missing_legacy_hosts_and_changed_keys(tmp_path):
    store = DesktopSettings(tmp_path)
    store.import_profile(profile(paramiko.RSAKey.generate(1024)))
    entry = store.read()["servers"]["app"]
    entry["known_hosts"] = str(tmp_path / "does-not-exist")
    store.update_server("app", entry)
    assert "host_keys" not in store.export_profile()["servers"]["app"]
    store.import_profile(profile(paramiko.RSAKey.generate(1024)), replace=True)
    assert "known_hosts" not in store.read()["servers"]["app"]
    assert not (tmp_path / "trust").exists()


def test_profile_rejects_missing_address_and_inline_passwords(tmp_path):
    document = profile().model_dump()
    document["servers"]["app"]["password"] = "must-not-enter-config"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="导入文件格式"):
        read_profile(path)
    del document["servers"]["app"]["password"]
    del document["servers"]["app"]["host"]
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        read_profile(path)


def test_key_profile_does_not_export_local_private_key_path(tmp_path):
    store = DesktopSettings(tmp_path)
    store.import_profile(profile(paramiko.RSAKey.generate(1024), authentication="key"))
    assert store.read()["servers"]["app"]["private_key"] == ""
    assert load_settings(store.path).servers["app"].private_key == ""
    assert store.export_profile()["servers"]["app"]["authentication"] == "key"


def test_codex_merge_keeps_other_settings_and_requires_confirmation(tmp_path):
    pytest.importorskip("tomlkit")
    config = tmp_path / "codex/config.toml"
    config.parent.mkdir()
    config.write_text('# preserve this comment\nmodel = "example"\n[mcp_servers.other]\ncommand="other"\n')
    register_codex(tmp_path / "app/config.json", config)
    text = config.read_text()
    assert "preserve this comment" in text and 'command="other"' in text
    original = config.read_bytes()
    with pytest.raises(ValueError, match="确认"):
        register_codex(tmp_path / "other.json", config)
    assert config.read_bytes() == original
    register_codex(tmp_path / "other.json", config, replace=True)
    assert len(list(config.parent.glob("*.bak"))) == 2


def test_secret_uses_credential_store_then_legacy_env(monkeypatch):
    from log_tools import credentials
    monkeypatch.setattr(credentials, "read_credential", lambda _: "credential-value")
    assert secret("TEST_UNUSED", "LogTools/test") == "credential-value"
    monkeypatch.setattr(credentials, "read_credential", lambda _: None)
    monkeypatch.setattr(credentials, "windows_environment", lambda _: "legacy-value")
    monkeypatch.delenv("TEST_UNUSED", raising=False)
    assert secret("TEST_UNUSED", "LogTools/test") == "legacy-value"


def test_other_process_cannot_interrupt_live_job(tmp_path):
    code = """
import sys
from pathlib import Path
from log_tools.store import Store
store=Store(Path(sys.argv[1]))
manifest,lease=store.new('test','utf-8',reserve=True)
print(manifest['dataset_id'],flush=True)
sys.stdin.readline()
lease.close()
"""
    child = subprocess.Popen([sys.executable, "-u", "-c", code, str(tmp_path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=dict(os.environ))
    service = LogService(Settings(cache_dir=tmp_path))
    try:
        dataset = child.stdout.readline().strip()
        assert dataset
        assert service.status(dataset)["status"] == "queued"
        assert service.datasets()["datasets"][0]["status"] == "queued"
        # Simulate an abrupt MCP exit; the OS must release its lease automatically.
        child.terminate()
        child.wait(timeout=10)
        # Windows may finish releasing file locks just after its process handle
        # becomes signalled. Recovery is intentionally conservative meanwhile.
        deadline = time.monotonic() + 3
        while service.status(dataset)["status"] == "queued" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert service.status(dataset)["status"] == "interrupted"
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()
        service.close()


def test_frozen_entry_survives_sdk_closing_stdout():
    code = """
import runpy
import sys
import log_tools.__main__
def simulate_sdk_shutdown():
    print('protocol-response', flush=True)
    sys.stdout.close()
log_tools.__main__.main = simulate_sdk_shutdown
entry = sys.argv[1]
sys.argv = [entry, '--config', 'unused-by-this-test.json']
runpy.run_path(entry, run_name='__main__')
# PyInstaller's bootloader flushes both objects after the entry returns.
exec('import sys; sys.stdout.flush(); (sys.__stdout__.flush if sys.__stdout__ is not sys.stdout else (lambda: None))()')
"""
    entry = Path(__file__).resolve().parents[1] / "src/log_tools/mcp_entry.py"
    result = subprocess.run([sys.executable, "-c", code, str(entry)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "protocol-response"
    assert result.stderr == ""
