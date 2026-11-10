import json
from contextlib import contextmanager

import pytest

from log_tools.config import DiscoverySettings, ServerConfig, Settings
from log_tools.desktop_config import DesktopSettings, Profile
from log_tools.service import LogService


def file(name):
    return {"type": "file", "name": name, "size": 42, "mtime": 1793318400}


def directory(name):
    return {"type": "directory", "name": name, "size": 0, "mtime": None}


@pytest.fixture
def discovery_service(tmp_path):
    tree = {
        "/logs": [file("log_error.log"), directory("error"), directory("info"), directory("target"), directory("unknown")],
        "/logs/error": [file("old.log.gz"), directory("2026"), {**directory("link"), "type": "symlink"}],
        "/logs/error/2026": [directory("10")],
        "/logs/error/2026/10": [file("28.log.gz"), directory("deeper")],
        "/logs/info": [file("info.log"), file("notes.txt")],
    }
    opened = []

    class Remote:
        def entries(self, path):
            opened.append(path)
            rows = tree[path]
            if isinstance(rows, Exception):
                raise rows
            yield from rows

    @contextmanager
    def connect(config):
        yield Remote()

    service = LogService(Settings(cache_dir=tmp_path / "data", servers={
        "test": ServerConfig(host="example.test", username="reader", root="/logs"),
        "other": ServerConfig(host="example.test", username="reader", root="/logs"),
    }), connector=connect)
    yield service, tree, opened
    service.close()


def test_default_discovers_history_reports_depth_and_skips_jars(discovery_service):
    service, tree, opened = discovery_service
    result = service.list_remote("test")
    assert {row["relative_path"] for row in result["files"]} == {
        "log_error.log", "error/old.log.gz", "error/2026/10/28.log.gz", "info/info.log"}
    assert result["incomplete"]
    assert result["warnings"] == [{"path": "/logs/error/2026/10/deeper", "reason": "depth_limit"}]
    assert not any("target" in path or "link" in path or "unknown" in path for path in opened)
    assert {row["reason"] for row in result["skipped_directories"]} >= {"excluded", "outside_history_dirs", "symlink_or_unsupported_type"}


def test_single_directory_and_custom_pattern(discovery_service):
    service, tree, opened = discovery_service
    result = service.list_remote("test", recursive=False)
    assert [row["name"] for row in result["files"]] == ["log_error.log"]
    assert not result["incomplete"] and opened == ["/logs"]
    result = service.list_remote("test", directory="info", pattern="*.txt")
    assert [row["name"] for row in result["files"]] == ["notes.txt"]
    with pytest.raises(ValueError, match="excluded"):
        service.list_remote("test", directory="target")
    with pytest.raises(ValueError, match="traversal"):
        service.list_remote("test", directory="../outside")


def test_snapshot_pages_stay_stable_and_expire(discovery_service):
    service, tree, opened = discovery_service
    tree["/logs/error/2026/10"].pop()
    result = service.list_remote("test", limit=1)
    all_files = list(result["files"])
    assert result["has_more"] and result["truncated"] and not result["incomplete"]
    tree["/logs"].append(file("new.log"))
    initial_opens = len(opened)
    first_cursor = result["next_cursor"]
    with pytest.raises(ValueError, match="match"):
        service.list_remote("other", cursor=first_cursor)
    while result["next_cursor"]:
        result = service.list_remote("test", cursor=result["next_cursor"], limit=1)
        all_files.extend(result["files"])
    assert len(opened) == initial_opens
    assert len(all_files) == len({row["remote_path"] for row in all_files}) == 4
    assert not result["truncated"]
    service.discovery.snapshots[first_cursor.split(":")[0]]["expires"] = 0
    with pytest.raises(ValueError, match="expired"):
        service.list_remote("test", cursor=first_cursor)


def test_permission_failure_continues_other_directories(discovery_service):
    service, tree, opened = discovery_service
    tree["/logs/error"] = PermissionError("sensitive server text")
    result = service.list_remote("test")
    assert result["incomplete"]
    assert "info/info.log" in {row["relative_path"] for row in result["files"]}
    assert "sensitive" not in json.dumps(result)
    assert result["warnings"][0]["path"] == "/logs/error"


def test_budget_counts_non_matching_entries_and_invalid_names(discovery_service):
    service, tree, opened = discovery_service
    tree["/logs"] = [file("../escape.log"), file("other.txt")] * 20
    service.settings.servers["test"].discovery.max_entries = 4
    result = service.list_remote("test")
    assert result["scanned_entries"] == 4 and result["incomplete"]
    assert not result["files"]
    assert "entry_limit" in {row["reason"] for row in result["warnings"]}


def test_time_limit_reports_incomplete(discovery_service, monkeypatch):
    from log_tools import discovery
    service, tree, opened = discovery_service
    times = iter([0, 21, 22, 23])
    monkeypatch.setattr(discovery.time, "monotonic", lambda: next(times, 23))
    result = service.list_remote("test")
    assert result["incomplete"] and result["warnings"][0]["reason"] == "time_limit"


def test_profile_round_trip_and_old_config_defaults(tmp_path):
    store = DesktopSettings(tmp_path)
    project = {"host": "example.test", "username": "reader", "root": "/logs"}
    profile = Profile.model_validate({"format": "log-tools-profile", "version": 1, "servers": {"test": project}})
    assert profile.servers["test"].discovery.enabled
    profile.servers["test"].discovery = DiscoverySettings(history_dirs=["archive"], pattern="*.gz", max_depth=5, enabled=False)
    store.import_profile(profile)
    exported = store.export_profile()
    assert exported["servers"]["test"]["discovery"] == profile.servers["test"].discovery.model_dump()
    assert "password_credential" not in json.dumps(exported)
    with pytest.raises(ValueError):
        DiscoverySettings(history_dirs=["../outside"])


def test_preview_collects_all_files_in_one_scan(tmp_path, monkeypatch, discovery_service):
    pytest.importorskip("PySide6")
    from log_tools import service as module
    from log_tools.desktop import preview_logs
    store = DesktopSettings(tmp_path)
    service, tree, opened = discovery_service
    tree["/logs"] = [file(f"file-{index}.log") for index in range(1001)]
    monkeypatch.setattr(module, "LogService", lambda settings: service)
    result = preview_logs(store.path, "test")
    assert len(result["files"]) == 1001 and not result["truncated"]
    assert result["next_cursor"] is None and opened == ["/logs"]
