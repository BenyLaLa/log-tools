import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from log_tools import deployment


def distribution_root(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    for name in deployment.PACKAGE_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public fixture", encoding="utf-8")
    (root / "config.example.json").write_text('{"servers": {}}', encoding="utf-8")
    return root


def test_package_allowlist_excludes_real_data_and_credentials(tmp_path):
    root = distribution_root(tmp_path)
    for name in (
        "config.json", "known_hosts", ".env", "private.key", "mcp-client.local.json",
        "data/datasets/secret/manifest.json", "data/datasets/secret/data.log",
        "reports/result.json", ".setup-backups/config.json", ".setup-cache/python.exe",
        ".venv/Scripts/python.exe", ".git/config", "examples/mcp-client.json",
        "src/log_tools/unreviewed.py",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("SENSITIVE_SENTINEL", encoding="utf-8")
    archive, manifest = deployment.build_package(root)
    with zipfile.ZipFile(archive) as bundle:
        assert len(bundle.namelist()) == len(deployment.PACKAGE_FILES) + 2
        assert "log-tools/examples/logs/order.log" in bundle.namelist()
        for license_file in ("GPL-3.0-only.txt", "LGPL-3.0-only.txt", "README.txt"):
            assert "log-tools/packaging/licenses/" + license_file in bundle.namelist()
        assert all(b"SENSITIVE_SENTINEL" not in bundle.read(n) for n in bundle.namelist())
        for item in manifest["files"]:
            data = bundle.read("log-tools/" + item["path"])
            assert len(data) == item["bytes"]
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
    assert archive.with_suffix(".zip.sha256").read_text().startswith(
        hashlib.sha256(archive.read_bytes()).hexdigest()
    )


def test_package_refuses_populated_server_template(tmp_path):
    root = distribution_root(tmp_path)
    (root / "config.example.json").write_text('{"servers": {"real": {}}}')
    with pytest.raises(ValueError, match="servers"):
        deployment.build_package(root)
    assert not (root / "dist").exists()


def test_initialize_preserves_existing_configuration(tmp_path):
    (tmp_path / "config.example.json").write_text('{"servers": {}}')
    deployment.initialize(tmp_path)
    config = tmp_path / "config.json"
    config.write_text('{"servers": {}, "max_files": 7}')
    client = tmp_path / "mcp-client.local.json"
    client.write_text('{"custom": "keep me"}')
    deployment.initialize(tmp_path)
    assert json.loads(config.read_text())["max_files"] == 7
    assert json.loads(client.read_text()) == {"custom": "keep me"}


def test_configuration_backup_and_concurrent_change_guard(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(b'{"servers": {}}')
    original = path.read_bytes()
    deployment.write_json(path, {"servers": {}, "max_files": 9}, expected=original, backup=True)
    backups = list((tmp_path / ".setup-backups").glob("*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    with pytest.raises(ValueError):
        deployment.write_json(path, {"bad": True}, expected=original)
    assert json.loads(path.read_text())["max_files"] == 9


def test_env_name_cannot_overwrite_system_variables():
    for name in ("PATH", "HOME", "PYTHONPATH", "LOG_BAD=VALUE", "LOG_BAD NAME"):
        with pytest.raises(ValueError):
            deployment.validate_env_name(name)
    deployment.validate_env_name("LOG_READER_PASSWORD")


def test_wizard_without_host_verification_persists_only_references(tmp_path, monkeypatch, capsys):
    root = tmp_path / "wizard"
    root.mkdir()
    (root / "config.example.json").write_text('{"servers": {}}')
    answers = iter([
        "y", "local-test", "example.test", "22", "reader", "/logs",
        "1", "LOG_TEST_SECRET", "y", "y", "n",
    ])
    prompts = []
    def answer(prompt):
        prompts.append(prompt)
        return next(answers)
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: pytest.fail("Configuration must not contact a host"))
    monkeypatch.setattr(deployment.getpass, "getpass", lambda _: "secret-only-in-memory")
    stored = {}
    monkeypatch.setattr(deployment, "save_user_secret", lambda name, value: stored.update({name: value}))
    deployment.configure(root)
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    assert config["servers"]["local-test"]["password_env"] == "LOG_TEST_SECRET"
    assert "known_hosts" not in config["servers"]["local-test"]
    assert stored == {"LOG_TEST_SECRET": "secret-only-in-memory"}
    assert not (root / "known_hosts").exists()
    assert not any("指纹" in prompt or "管理员" in prompt for prompt in prompts)
    assert "secret-only-in-memory" not in capsys.readouterr().out
    assert all(b"secret-only-in-memory" not in f.read_bytes() for f in root.rglob("*") if f.is_file())


def test_wizard_cancel_preserves_existing_configuration(tmp_path, monkeypatch):
    (tmp_path / "config.example.json").write_text('{"servers": {}}')
    deployment.initialize(tmp_path)
    original = (tmp_path / "config.json").read_bytes()
    answers = iter([
        "y", "local-test", "example.test", "22", "reader", "/logs", "1",
        "LOG_TEST_SECRET", "n",
    ])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(deployment.getpass, "getpass", lambda _: pytest.fail("Cancelled"))
    deployment.configure(tmp_path)
    assert (tmp_path / "config.json").read_bytes() == original
    assert not (tmp_path / "known_hosts").exists()


def test_offline_payload_checks_installer_and_hashes_assets(tmp_path, monkeypatch):
    (tmp_path / "requirements.lock.txt").write_text("demo==1.0\n", encoding="utf-8")
    installer = tmp_path / "python.exe"
    installer.write_bytes(b"test official installer")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "demo-1.0-py3-none-any.whl").write_bytes(b"test wheel")
    with pytest.raises(ValueError, match="SHA256"):
        deployment.offline_payload(tmp_path, installer, wheels)
    monkeypatch.setattr(deployment, "INSTALLER_SHA256", hashlib.sha256(installer.read_bytes()).hexdigest())
    payload = deployment.offline_payload(tmp_path, installer, wheels)
    manifest = json.loads(payload["offline/manifest.json"])
    assert manifest["platform"] == "cp313-win_amd64"
    assert manifest["requirements_sha256"] == hashlib.sha256((tmp_path / "requirements.lock.txt").read_bytes()).hexdigest()
    for entry in manifest["files"]:
        assert hashlib.sha256(payload[entry["path"]]).hexdigest() == entry["sha256"]


@pytest.mark.parametrize("content", [
    "--index-url https://example.test/simple", "demo @ https://example.test/demo.whl",
    "-r other.txt", "demo>=1", "",
])
def test_offline_requirements_reject_network_instructions(tmp_path, content):
    (tmp_path / "requirements.lock.txt").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="name==version"):
        deployment.locked_requirements(tmp_path)


def test_offline_archive_contains_only_prepared_assets(tmp_path, monkeypatch):
    root = distribution_root(tmp_path)
    (root / "offline").mkdir()
    (root / "offline/secret.txt").write_text("MUST_NOT_SHIP")
    assets = {"offline/manifest.json": b"{}", "offline/wheels/demo.whl": b"verified wheel"}
    monkeypatch.setattr(deployment, "prepare_offline_assets", lambda *_: assets)
    archive, _ = deployment.build_package(root, offline=True)
    assert "-offline-" in archive.name
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.read("log-tools/offline/wheels/demo.whl") == b"verified wheel"
        assert "log-tools/offline/secret.txt" not in bundle.namelist()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell bootstrap")
def test_offline_setup_fails_closed_before_creating_environment(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/setup.ps1", scripts / "setup.ps1")
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(scripts / "setup.ps1"), "-Offline", "-SkipConfigure", "-NoPause",
    ]
    missing = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert missing.returncode != 0
    assert "Offline manifest is missing" in missing.stdout
    lock = tmp_path / "requirements.lock.txt"
    lock.write_bytes(b"demo==1.0\n")
    wheels = tmp_path / "offline/wheels"
    wheels.mkdir(parents=True)
    (wheels / "demo-1.0-py3-none-any.whl").write_bytes(b"corrupt")
    (tmp_path / "offline/manifest.json").write_text(json.dumps({
        "format": 1, "platform": "cp313-win_amd64", "python_version": "3.13.17",
        "requirements_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "files": [{"path": "offline/wheels/demo-1.0-py3-none-any.whl", "bytes": 0, "sha256": "0" * 64}],
    }))
    corrupt = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert corrupt.returncode != 0
    assert "Offline file checksum mismatch" in corrupt.stdout
    assert not (tmp_path / ".venv").exists()
