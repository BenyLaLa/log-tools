import json

import pytest

from log_tools.desktop_config import DesktopSettings


def entry(**changes):
    return {"protocol": "sftp", "host": "logs.example.test", "port": 22,
            "username": "reader", "root": "/project", **changes}


def test_rename_preserves_other_projects_and_rejects_stale_editor(tmp_path):
    settings = DesktopSettings(tmp_path)
    original = entry(password_env="TEST_REFERENCE")
    settings.save_project("first", original)
    settings.save_project("second", entry(root="/second"))
    settings.save_project("renamed", original, previous_alias="first", expected_entry=original)
    servers = settings.read()["servers"]
    assert list(servers) == ["renamed", "second"]
    assert servers["renamed"]["password_env"] == "TEST_REFERENCE"
    assert servers["second"]["root"] == "/second"
    settings.update_server("renamed", entry(root="/updated"))
    with pytest.raises(ValueError, match="其他窗口"):
        settings.save_project("another", original, previous_alias="renamed", expected_entry=original)
    assert settings.read()["servers"]["renamed"]["root"] == "/updated"


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from log_tools import desktop
    app = widgets.QApplication.instance() or widgets.QApplication([])
    writes = []
    monkeypatch.setattr(desktop, "write_credential", lambda target, value: writes.append((target, value)))
    view = desktop.Window(tmp_path)
    yield view, writes
    view.close()
    view.deleteLater()
    app.processEvents()


def fill(view, alias="first"):
    view.alias.setText(alias)
    view.address.setText("logs.example.test")
    view.username.setText("reader")
    view.root.setText("/project")


def test_gui_storage_limits_apply_to_current_cache(window):
    from uuid import uuid4

    from PySide6.QtWidgets import QApplication

    from log_tools.config import MIB, load_settings
    from log_tools.store import Store

    view, _ = window
    store = Store(load_settings(view.settings.path).cache_dir)
    old = store.new("test", "utf-8")
    store.file_path(old["dataset_id"], str(uuid4())).write_bytes(b"x" * (MIB + 1))
    view.cache_limit.setValue(1)
    view.download_limit.setValue(20)
    view.save_storage_limits()
    assert view.worker.wait(5000)
    QApplication.processEvents()
    assert not store.directory(old["dataset_id"]).exists()
    assert "已清理 1 个旧数据集" in view.status.text()
    assert view.settings.read()["max_cache_bytes"] == MIB
    assert view.settings.read()["max_download_bytes"] == 20 * MIB


def test_gui_discovery_settings_save_reload_and_preview(window):
    view, writes = window
    fill(view)
    assert view.discovery_enabled.isChecked()
    view.history_dirs.setText("archive， error")
    view.discovery_depth.setValue(5)
    view.discovery_pattern.setText("*.gz")
    view.discovery_entries.setValue(3000)
    view.save_server()
    settings = view.settings.read()["servers"]["first"]["discovery"]
    assert settings["history_dirs"] == ["archive", "error"]
    assert settings["max_depth"] == 5 and settings["max_entries"] == 3000
    assert settings["pattern"] == "*.gz" and view.history_dirs.text() == "archive, error"
    view.show_preview({"server": "first", "directory": "/project", "total_files": 1,
                       "scanned_directories": 2, "incomplete": True,
                       "files": [{"relative_path": "error/old.log.gz", "size": 30, "modified_at": None}],
                       "warnings": [{"path": "/project/info", "reason": "directory_unreadable:PermissionError"}],
                       "skipped_directories": [], "diagnostics_truncated": False})
    from PySide6.QtWidgets import QTreeWidget
    tree = view.preview_dialog.findChild(QTreeWidget)
    assert tree.topLevelItem(0).text(0) == "error"
    assert tree.topLevelItem(0).child(0).text(0) == "old.log.gz"
    assert "未完整" in view.output.toPlainText()
    view.preview_dialog.close()


def test_gui_create_rename_edit_and_keep_selection(window):
    view, writes = window
    assert not view.alias.isReadOnly() and not view.address.isReadOnly()
    assert view.port.isEnabled() and view.port.value() == 22
    assert not view.key.isEnabled() and not view.key_button.isEnabled()
    fill(view)
    view.password.setText("synthetic-password")
    view.save_server()
    old_target = writes[0][0]
    assert len(writes) == 1
    assert "synthetic-password" not in view.settings.path.read_text(encoding="utf-8")
    view.alias.setText("renamed")
    view.root.setText("/other-logs")
    view.save_server()
    servers = view.settings.read()["servers"]
    assert list(servers) == ["renamed"]
    assert servers["renamed"]["root"] == "/other-logs"
    assert servers["renamed"]["password_credential"] == old_target
    assert view.editing_alias == "renamed"
    assert view.table.item(view.table.currentRow(), 0).text() == "renamed"
    assert len(writes) == 1
    view.new_project()
    assert view.editing_alias is None and view.alias.text() == ""
    assert view.port.value() == 22
    view.auth.setCurrentIndex(1)
    assert view.key.isEnabled() and view.key_button.isEnabled()


@pytest.mark.parametrize("field,value", [("address", "other.example.test"), ("port", 2222), ("username", "other-reader")])
def test_editing_connection_does_not_reuse_old_account_secret(window, field, value):
    view, writes = window
    fill(view)
    view.password.setText("synthetic-password")
    view.save_server()
    previous = view.settings.read()["servers"]["first"]["password_credential"]
    widget = getattr(view, field)
    widget.setValue(value) if field == "port" else widget.setText(value)
    view.save_server()
    updated = view.settings.read()["servers"]["first"]
    assert updated["password_credential"] != previous
    assert "password_env" not in updated
    assert len(writes) == 1


def test_duplicate_name_is_rejected_before_saving_any_credential(window):
    view, writes = window
    fill(view)
    view.save_server()
    view.new_project()
    fill(view, "second")
    view.save_server()
    before = view.settings.path.read_bytes()
    view.alias.setText("first")
    view.password.setText("must-not-overwrite-shared-secret")
    with pytest.raises(ValueError, match="已存在"):
        view.save_server()
    assert view.settings.path.read_bytes() == before
    assert writes == []
    view.new_project()
    fill(view, "first")
    with pytest.raises(ValueError, match="已存在"):
        view.save_server()
    assert len(json.loads(before)["servers"]) == 2


def test_invalid_manual_address_does_not_save_credentials(window):
    view, writes = window
    fill(view)
    view.address.setText("sftp://logs.example.test")
    view.password.setText("must-not-save")
    with pytest.raises(ValueError):
        view.save_server()
    assert writes == []
    assert view.settings.read()["servers"] == {}
