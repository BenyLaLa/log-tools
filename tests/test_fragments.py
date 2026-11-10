import asyncio
import gzip
import io
import json
import os
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import paramiko
import pytest
from test_transports import TestSftp, sftp_server  # noqa: F401

from log_tools.config import MIB, ServerConfig, Settings
from log_tools.fragments import Collector
from log_tools.query import Query
from log_tools.store import Store


class MemoryRemote:
    def __init__(self, files):
        self.files, self.reads = files, []
        self.modified = 123

    def metadata(self, path):
        return {"size": len(self.files[path]), "mtime": self.modified}

    @contextmanager
    def open_reader(self, path):
        remote = self

        class Stream(io.BytesIO):
            def stat(self):
                return SimpleNamespace(st_size=len(remote.files[path]), st_mtime=remote.modified, st_mode=stat.S_IFREG)
            def settimeout(self, value):
                pass
            def read(self, size=-1):
                offset = self.tell()
                data = super().read(size)
                remote.reads.append((path, offset, len(data)))
                return data
        with Stream(self.files[path]) as stream:
            yield stream

    def download(self, *_):
        raise AssertionError("Full download must never be used for fragment collection")


@pytest.fixture
def memory(tmp_path):
    remote = MemoryRemote({"/logs/app.log": b"2026-11-04 10:00:00 INFO start\n2026-11-04 10:01:00 ERROR needle password=private\nValueError: stack\n2026-11-04 10:02:00 INFO finish\n"})
    settings = Settings(cache_dir=tmp_path / "cache")
    store = Store(settings.cache_dir)

    @contextmanager
    def connect(config):
        yield remote
    return Collector(settings, store, connect), remote, ServerConfig(host="test", username="reader", root="/logs")


def test_full_stream_only_selected_redacted_context_saved_and_query_origin(memory):
    collector, remote, config = memory
    result = collector.collect("test", config, ["app.log"], mode="full", query="needle", before=0, after=1)
    assert result["status"] == "completed", result
    assert result["collection"]["network_bytes"] == len(remote.files["/logs/app.log"])
    assert result["collection"]["saved_bytes"] < result["collection"]["network_bytes"]
    files = list(collector.store.directory(result["dataset_id"]).glob("*.log"))
    assert len(files) == 1
    assert b"start" not in files[0].read_bytes() and b"finish" not in files[0].read_bytes()
    assert b"private" not in files[0].read_bytes() and b"[REDACTED]" in files[0].read_bytes()
    query = Query(collector.settings, collector.store)
    evidence = query.search(result["dataset_id"], "needle")
    row = evidence["matches"][0]
    assert row["line"] == 1 and row["source"]["line"] == 2
    assert row["source"]["byte_start"] == remote.files["/logs/app.log"].index(b"2026-11-04 10:01")
    context = query.context(result["dataset_id"], row["file_id"], 1)
    assert context["lines"][1]["source"]["line"] == 3
    assert evidence["warnings"] and query.latest_errors(result["dataset_id"])["warnings"]


def test_service_preview_locates_hit_after_long_context(memory):
    from log_tools.service import LogService
    collector, remote, config = memory
    collector.settings.servers["test"] = config
    remote.files["/logs/app.log"] = b"2026-11-04 10:00:00 INFO padding\n" * 30 + b"2026-11-04 10:01:00 ERROR needle\nValueError: stack\n"
    service = LogService(collector.settings, collector.connector)
    try:
        result = service.collect_fragments("test", paths=["app.log"], query="needle", before=20)
        assert "needle" in result["preview"]["matches"][0]["text"]
        assert any("stack" in row["text"] for row in result["preview"]["contexts"][0]["lines"])
    finally:
        service.close()


def test_tail_reads_only_range_and_resume_goes_earlier(memory):
    collector, remote, config = memory
    remote.files["/logs/app.log"] = (b"2026-11-04 09:00:00 ERROR OLD_NEEDLE\n" + b"2026-11-04 10:00:00 INFO padding abcdefghijklmnopqrstuvwxyz\n" * 300 + b"2026-11-04 11:00:00 ERROR LAST_NEEDLE\n")
    result = collector.collect("test", config, ["app.log"], mode="tail", query="NEEDLE", before=0, after=0, tail_bytes=1024)
    assert result["status"] == "partial" and result["resume_dataset_id"]
    assert sum(length for _, _, length in remote.reads) == result["collection"]["network_bytes"] <= 1024
    assert min(offset for _, offset, _ in remote.reads) >= len(remote.files["/logs/app.log"]) - 1024
    query = Query(collector.settings, collector.store)
    rows = query.search(result["dataset_id"], "NEEDLE")["matches"]
    assert len(rows) == 1 and "LAST_NEEDLE" in rows[0]["text"] and rows[0]["source"]["line"] is None
    remote.reads.clear()
    second = collector.collect("test", config, resume_dataset_id=result["dataset_id"])
    assert second["collection"]["coverage"][0]["byte_end"] < len(remote.files["/logs/app.log"])
    assert max(offset + length for _, offset, length in remote.reads) < len(remote.files["/logs/app.log"])


def test_history_filters_content_times_without_assuming_sorted_logs(memory):
    collector, remote, config = memory
    remote.files["/logs/app.log"] = (b"2026-11-04 15:00:00 ERROR later\n2026-11-04 08:00:00 ERROR earlier\n2026-11-04 10:00:00 ERROR historical-needle\n")
    result = collector.collect("test", config, ["app.log"], mode="history", start_time="2026-11-04 09:00:00", end_time="2026-11-04 11:00:00", before=0, after=0)
    rows = Query(collector.settings, collector.store).search(result["dataset_id"])["matches"]
    assert len(rows) == 1 and "historical-needle" in rows[0]["text"]
    assert rows[0]["source"]["line"] == 3


def test_history_and_saved_fragment_resolve_short_dates_from_source_mtime(memory):
    collector, remote, config = memory
    remote.modified = int(datetime(2026, 11, 4, 2, tzinfo=timezone.utc).timestamp())
    remote.files["/logs/app.log"] = (
        b"11-04 10:00:00.123 ERROR historical-needle payloadDate=2030-10-22 00:00:00\n"
        b"11-04 15:00:00.000 ERROR outside time window\n"
    )
    result = collector.collect("test", config, ["app.log"], mode="history",
                               start_time="2026-11-04 09:59:59", end_time="2026-11-04 10:00:01",
                               before=0, after=0)
    rows = Query(collector.settings, collector.store).search(result["dataset_id"])["matches"]
    assert [row["timestamp"] for row in rows] == ["2026-11-04T10:00:00.123000+08:00"]
    assert "historical-needle" in rows[0]["text"]


def test_persistent_resume_strict_network_cap_and_source_change(memory):
    collector, remote, config = memory
    remote.files["/logs/app.log"] = b"2026-11-04 10:00:00 INFO padding\n" * 200 + b"2026-11-04 10:01:00 ERROR final-needle\n"
    result = collector.collect("test", config, ["app.log"], mode="full", query="final-needle", before=0, after=0, max_network_bytes=1024)
    assert result["collection"]["network_bytes"] <= 1024 and result["resume_dataset_id"]
    fresh = Collector(collector.settings, Store(collector.store.root), collector.connector)
    remote.reads.clear()
    next_result = fresh.collect("test", config, resume_dataset_id=result["dataset_id"], max_network_bytes=20000)
    assert next_result["status"] == "completed"
    assert Query(collector.settings, collector.store).search(next_result["dataset_id"], "final-needle")["matches"]
    assert min(offset for _, offset, _ in remote.reads) > 0
    remote.modified += 1
    changed = fresh.collect("test", config, resume_dataset_id=result["dataset_id"])
    assert changed["status"] == "partial" and not changed["files"]
    assert changed["collection"]["warnings"][0]["reason"] == "source_changed_restart_search"


def test_gzip_stream_no_original_file_saved_and_expansion_limit(memory):
    collector, remote, config = memory
    data = b"2026-11-04 10:01:00 ERROR gzip-needle\nValueError: compressed stack\n"
    remote.files["/logs/old.log.gz"] = gzip.compress(data) + gzip.compress(b"2026-11-04 11:00:00 INFO other member\n")
    result = collector.collect("test", config, ["old.log.gz"], mode="full", query="gzip-needle", before=0, after=1)
    assert result["status"] == "completed", result
    assert result["files"][0]["source_compressed"]
    assert b"gzip-needle" in next(collector.store.directory(result["dataset_id"]).glob("*.log")).read_bytes()
    assert not list(collector.store.directory(result["dataset_id"]).glob("*.gz"))
    collector.settings.fragment_scan_bytes = 1024
    remote.files["/logs/bomb.log.gz"] = gzip.compress(b"x\n" * 100000)
    limited = collector.collect("test", config, ["bomb.log.gz"], mode="full", query="needle")
    assert limited["status"] == "partial"
    assert any(row["reason"] == "decoded_byte_limit" for row in limited["collection"]["warnings"])
    skipped = collector.collect("test", config, ["old.log.gz"], query="needle")
    assert skipped["collection"]["network_bytes"] == 0


@pytest.mark.parametrize("encoding", ["utf-8-sig", "gb18030", "utf-16"])
def test_encodings_and_long_line_boundary(memory, encoding):
    collector, remote, config = memory
    collector.settings.log_encoding = encoding
    text = "2026-11-04 10:00:00 INFO 前置\n2026-11-04 10:01:00 ERROR 中文异常\n"
    remote.files["/logs/app.log"] = text.encode(encoding)
    result = collector.collect("test", config, ["app.log"], mode="full", query="中文异常", before=0, after=0)
    assert result["status"] == "completed", result
    row = Query(collector.settings, collector.store).search(result["dataset_id"], "中文异常")["matches"][0]
    assert row["source"]["line"] == 2


def test_output_limit_overlap_merging_and_no_match_files(memory):
    collector, remote, config = memory
    remote.files["/logs/app.log"] = b"2026-11-04 10:00:00 ERROR needle\n" * 15
    result = collector.collect("test", config, ["app.log"], mode="full", query="needle", before=2, after=2)
    assert len(result["files"]) == 1 and len(collector.store.read(result["dataset_id"])["files"][0]["line_map"]) == 15
    limited = collector.collect("test", config, ["app.log"], mode="full", query="needle", max_output_bytes=50)
    assert limited["collection"]["saved_bytes"] <= 50 and limited["status"] == "partial"
    none = collector.collect("test", config, ["app.log"], mode="full", query="absent")
    assert none["status"] == "completed" and not none["files"]
    assert not list(collector.store.directory(none["dataset_id"]).glob("*.log"))


def test_invalid_paths_parameters_and_resume_do_not_connect(memory):
    collector, remote, config = memory
    for path in ("../secret.log", "/outside/app.log", "web.config", "target/app.log", "third/app.log", "dist/app.log"):
        with pytest.raises(ValueError):
            collector.collect("test", config, [path], query="needle")
    for args in ({"mode": "history"}, {"mode": "arbitrary"}, {"max_network_bytes": 0}, {"before": 100}, {"start_time": "wrong"}):
        with pytest.raises(ValueError):
            collector.collect("test", config, ["app.log"], **args)
    assert not remote.reads


def test_resume_match_limit_progress_and_hit_survives_small_output_budget(memory):
    collector, remote, config = memory
    remote.files["/logs/app.log"] = b"2026-11-04 10:00:00 ERROR hit-one\n2026-11-04 11:00:00 ERROR hit-two\n2026-11-04 12:00:00 ERROR hit-three\n"
    first = collector.collect("test", config, ["app.log"], mode="full", query="hit", limit=1, before=1, after=0)
    second = collector.collect("test", config, resume_dataset_id=first["dataset_id"])
    rows = Query(collector.settings, collector.store).search(second["dataset_id"], "hit-two")["matches"]
    assert rows
    assert second["collection"]["coverage"][0]["processed_until"] > first["collection"]["coverage"][0]["processed_until"]
    remote.files["/logs/app.log"] = b"2026-11-04 10:00:00 INFO " + b"x" * 200 + b"\n2026-11-04 10:01:00 ERROR needle\n"
    limited = collector.collect("test", config, ["app.log"], mode="full", query="needle", max_output_bytes=50, before=1, after=0)
    assert Query(collector.settings, collector.store).search(limited["dataset_id"], "needle")["matches"]


def test_gzip_corruption_and_long_lines_are_explicit(memory):
    collector, remote, config = memory
    remote.files["/logs/bad.log.gz"] = b"not a gzip"
    bad = collector.collect("test", config, ["bad.log.gz"], mode="full", query="needle")
    assert bad["status"] == "partial" and not bad["files"]
    remote.files["/logs/long.log"] = b"x" * 70000
    long = collector.collect("test", config, ["long.log"], mode="full", query="needle")
    assert long["status"] == "partial" and not long["files"]


def test_scan_deadline_interrupts_blocked_read(memory):
    collector, remote, config = memory
    released = threading.Event()
    remote.close = released.set
    original = remote.open_reader

    @contextmanager
    def slow(path):
        with original(path) as stream:
            read = stream.read
            def blocked(size=-1):
                released.wait(3)
                return read(size)
            stream.read = blocked
            yield stream
    remote.open_reader = slow
    started = time.monotonic()
    result = collector.collect("test", config, ["app.log"], mode="full", query="needle", timeout_seconds=1)
    assert time.monotonic() - started < 2.5 and released.is_set()
    assert result["status"] == "partial"
    assert any(row["reason"] == "time_limit" for row in result["collection"]["warnings"])


def test_tail_budget_resume_advances_inside_window_and_source_change_is_flagged(memory):
    collector, remote, config = memory
    remote.files["/logs/app.log"] = b"2026-11-04 10:00:00 INFO padding\n" * 300
    first = collector.collect("test", config, ["app.log"], query="absent", tail_bytes=4096, max_network_bytes=1024, before=0, after=0)
    second = collector.collect("test", config, resume_dataset_id=first["dataset_id"], max_network_bytes=1024)
    assert second["collection"]["coverage"][0]["processed_until"] > first["collection"]["coverage"][0]["processed_until"]
    original = remote.metadata
    def changed(path):
        info = original(path)
        return {**info, "mtime": info["mtime"] + 1}
    remote.metadata = changed
    changed_result = collector.collect("test", config, ["app.log"], mode="full", query="absent")
    assert changed_result["status"] == "partial"
    assert any(row["reason"] == "source_changed_during_read" for row in changed_result["collection"]["warnings"])


@pytest.fixture
def metered_sftp(request, monkeypatch):
    config = request.getfixturevalue("sftp_server")
    original_open = TestSftp.open
    calls = []

    def opened(self, path, flags, attr):
        handle = original_open(self, path, flags, attr)
        assert isinstance(handle, paramiko.SFTPHandle)
        handle.stat = lambda: paramiko.SFTPAttributes.from_stat(os.fstat(handle.readfile.fileno()))
        original_read = handle.read
        def read(offset, length):
            data = original_read(offset, length)
            if isinstance(data, bytes):
                calls.append((offset, len(data)))
            return data
        handle.read = read
        return handle
    monkeypatch.setattr(TestSftp, "open", opened)
    return config, calls


def test_real_sftp_partial_read_and_mcp_collect(metered_sftp, tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    config, calls = metered_sftp
    log = tmp_path / "sftp/logs/app.log"
    log.write_bytes(b"2026-11-04 10:00:00 INFO padding\n" * 400 + b"2026-11-04 10:01:00 ERROR real-tail-needle\nValueError: stack\n")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"cache_dir": str(tmp_path / "cache"), "servers": {"test": config.model_dump()}}))

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "log_tools", "--config", str(path)], env=dict(os.environ))
        with open(os.devnull, "w") as diagnostics:
            async with stdio_client(params, errlog=diagnostics) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    response = await session.call_tool("collect_log_fragments", {"server": "test", "paths": ["app.log"], "mode": "tail", "query": "real-tail-needle", "tail_bytes": 1024, "after": 1, "before": 0})
                    assert not response.isError, response
                    result = response.structuredContent
                    assert result["collection"]["network_bytes"] == sum(size for _, size in calls) <= 1024
                    assert min(offset for offset, _ in calls) >= log.stat().st_size - 1024
                    row = result["preview"]["matches"][0]
                    assert row["source"]["line"] is None
                    context = await session.call_tool("read_log_context", {"dataset_id": result["dataset_id"], "file_id": row["file_id"], "line": row["line"]})
                    assert not context.isError and any("stack" in r["text"] for r in context.structuredContent["lines"])
    asyncio.run(run())


def test_cli_collect_and_resume_across_mcp_processes(metered_sftp, tmp_path):
    config, calls = metered_sftp
    log = tmp_path / "sftp/logs/app.log"
    log.write_bytes(b"2026-11-04 10:00:00 INFO padding\n" * 40000 + b"2026-11-04 10:01:00 ERROR cli-final-needle\n")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"cache_dir": str(tmp_path / "cache"), "servers": {"test": config.model_dump()}}))
    prefix = [sys.executable, "-m", "log_tools.client", "--config", str(path), "collect", "--server", "test"]
    first = subprocess.run(prefix + ["app.log", "--mode", "full", "--query", "cli-final-needle", "--network-mib", "1", "--before", "0", "--after", "0"], capture_output=True, text=True, timeout=30)
    assert first.returncode == 0, first.stderr
    result = json.loads(first.stdout)
    assert result["resume_dataset_id"] and result["collection"]["network_bytes"] <= 1024**2
    calls.clear()
    second = subprocess.run(prefix + ["--resume", result["resume_dataset_id"]], capture_output=True, text=True, timeout=30)
    assert second.returncode == 0, second.stderr
    resumed = json.loads(second.stdout)
    assert resumed["status"] == "completed", resumed
    assert "cli-final-needle" in resumed["preview"]["matches"][0]["text"]
    assert min(offset for offset, _ in calls) > 0


def test_gui_file_selection_history_validation_and_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from log_tools.desktop import Window
    from log_tools.desktop_config import DesktopSettings
    app = widgets.QApplication.instance() or widgets.QApplication([])
    settings = DesktopSettings(tmp_path / "gui")
    settings.save_project("project", {"protocol": "sftp", "host": "test", "username": "reader", "root": "/logs"})
    original = settings.read()
    settings.save({**original, "fragment_network_bytes": 8 * MIB, "fragment_output_bytes": MIB // 2},
                  settings.path.read_bytes())
    view = Window(tmp_path / "gui")
    view.show_result({"fragment_listing": True, "server": "project", "files": [{"name": "web-error-2026-10-28.0.log", "relative_path": "error/web-error-2026-10-28.0.log", "remote_path": "/logs/error/web-error-2026-10-28.0.log", "size": 1000, "modified_at": None}], "incomplete": False, "warnings": [], "skipped_directories": []})
    view.check_fragment_files(True)
    view.fragment_mode.setCurrentIndex(1)
    with pytest.raises(ValueError, match="历史时段"):
        view.fragment_arguments()
    view.fragment_start.setText("2026-10-28 00:00:00")
    view.fragment_end.setText("2026-10-28 23:59:59")
    alias, args = view.fragment_arguments()
    assert alias == "project" and args["mode"] == "history" and args["paths"] == ["/logs/error/web-error-2026-10-28.0.log"]
    assert view.fragment_network.maximum() == 8
    assert args["max_network_bytes"] == 8 * MIB and args["max_output_bytes"] == MIB // 2
    view.last_fragment_result, view.last_fragment_alias = {"resume_dataset_id": "previous-id"}, alias
    original = settings.read()
    settings.save({**original, "fragment_network_bytes": 4 * MIB}, settings.path.read_bytes())
    _, args = view.fragment_arguments(True)
    assert args["resume_dataset_id"] == "previous-id" and "paths" not in args
    assert args["max_network_bytes"] == 4 * MIB
    view.close()
    view.deleteLater()
    app.processEvents()
