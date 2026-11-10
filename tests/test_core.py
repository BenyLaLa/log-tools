import gzip
import hashlib
import json
from contextlib import contextmanager

import pytest

from log_tools.config import ServerConfig, load_settings, remote_path
from log_tools.query import Budget, ScanLimit, lines


def imported(service, text, name="app.log"):
    path = service.settings.local_roots[0] / name
    path.write_bytes(text.encode("utf-8"))
    return service.import_local([str(path)])


@pytest.mark.parametrize(
    "path", ["../secret", "/other/file", "foo/../../secret", "x\r\nRETR y", "x\\y"]
)
def test_remote_traversal(path):
    with pytest.raises(ValueError):
        remote_path(path, "/logs")


def test_config_guards_and_relative_paths(tmp_path):
    with pytest.raises(ValueError):
        ServerConfig(protocol="ftp", host="localhost", username="a", root="/logs")
    assert ServerConfig(protocol="sftp", host="localhost", username="a", root="/logs").known_hosts is None
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"cache_dir": "cache", "local_roots": ["logs"]}))
    settings = load_settings(config)
    assert settings.cache_dir == tmp_path / "cache"
    assert settings.local_roots == [tmp_path / "logs"]


def test_import_search_time_context_redaction(service, sample):
    dataset = imported(service, sample)
    assert dataset["status"] == "completed"
    assert dataset["files"][0]["sha256"] == hashlib.sha256(sample.encode()).hexdigest()
    dataset_id = dataset["dataset_id"]
    result = service.query.search(
        dataset_id,
        trace_id="demo-001",
        level="ERROR",
        start_time="2026-10-22T02:00:00Z",
        end_time="2026-10-22T02:30:00Z",
    )
    assert [item["line"] for item in result["matches"]] == [3, 6]
    stack = service.query.search(dataset_id, query="InventoryClient", level="ERROR")
    assert stack["matches"][0]["line"] == 5
    context = service.query.context(dataset_id, dataset["files"][0]["file_id"], 3, 1, 2)
    assert [line["line"] for line in context["lines"]] == [2, 3, 4, 5]
    assert context["target_found"]
    token = service.query.search(dataset_id, query="token")
    assert "demo-sensitive-value" not in json.dumps(token)
    assert "[REDACTED]" in token["matches"][0]["text"]
    assert service.query.summary(dataset_id)["error_events"] == 2


def test_pagination_json_unknown_time_and_invalid_range(service):
    dataset = imported(
        service,
        "\n".join(
            [
                "orphan text",
                '{"timestamp":"2026-10-22T10:00:00+08:00","level":"error","message":"bad","token":"secret123"}',
                "2026-10-22 10:00:01 INFO ok",
                "2026-10-22 10:00:02 INFO ok",
            ]
        ),
    )
    first = service.query.search(dataset["dataset_id"], query="INFO ok", limit=1)
    assert first["truncated"] and first["next_offset"] == 1
    second = service.query.search(dataset["dataset_id"], query="INFO ok", limit=1, offset=1)
    assert not second["truncated"] and second["matches"][0]["line"] == 4
    filtered = service.query.search(dataset["dataset_id"], start_time="2026-10-22T10:00:00+08:00")
    assert filtered["unknown_time_lines_skipped"] == 1
    assert "secret123" not in json.dumps(filtered)
    with pytest.raises(ValueError):
        service.query.search(
            dataset["dataset_id"],
            start_time="2026-10-22T11:00:00Z",
            end_time="2026-10-22T10:00:00Z",
        )


def test_short_dates_use_log_header_and_roll_across_new_year(service):
    dataset = imported(
        service,
        "body contains a date 2031-01-01 02:00:00\n"
        "12-31 23:59:59.500 ERROR request failed for date=2030-01-01 08:00:00\n"
        "01-01 00:00:01.000 ERROR latest failure\n",
        name="web-error-2027-01-01.0.log",
    )
    result = service.query.search(dataset["dataset_id"], start_time="2026-12-31T23:59:00+08:00")
    assert result["unknown_time_lines_skipped"] == 1
    assert [row["timestamp"] for row in result["matches"]] == [
        "2026-12-31T23:59:59.500000+08:00", "2027-01-01T00:00:01+08:00",
    ]
    latest = service.query.latest_errors(dataset["dataset_id"])
    assert [row["line"] for row in latest["errors"]] == [3, 2]


def test_gzip_encoding_and_limits(service):
    path = service.settings.local_roots[0] / "app.log.gz"
    with gzip.open(path, "wb") as target:
        target.write("2026-10-22 10:00:00 ERROR 中文失败\n".encode("gb18030"))
    service.settings.log_encoding = "gb18030"
    dataset = service.import_local([str(path)])
    result = service.query.search(dataset["dataset_id"], query="中文")
    assert len(result["matches"]) == 1
    service.settings.scan_max_bytes = 10
    result = service.query.search(dataset["dataset_id"])
    assert result["truncated"] and result["warnings"] == ["scan_byte_limit"]


def test_corrupt_gzip_and_huge_line_are_explicit(service):
    path = service.settings.local_roots[0] / "bad.gz"
    path.write_bytes(b"not gzip")
    dataset = service.import_local([str(path)])
    assert service.query.search(dataset["dataset_id"])["warnings"] == ["file_read_failed"]
    huge = imported(service, "x" * 70000)
    assert service.query.search(huge["dataset_id"])["warnings"] == ["line_exceeds_65536_characters"]


def test_local_access_and_ids(service, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    with pytest.raises(ValueError):
        service.import_local([str(outside)])
    with pytest.raises(ValueError):
        service.store.read("../secret")
    with pytest.raises(ValueError):
        service.query.context(imported(service, "x")["dataset_id"], "../file", 1)


@pytest.mark.parametrize("mode", ["grows", "changes", "fails", "limit"])
def test_download_failure_does_not_publish_partial_file(service, mode):
    class FakeRemote:
        calls = 0

        def metadata(self, path):
            self.calls += 1
            return {"size": 4, "mtime": 2 if mode == "changes" and self.calls > 1 else 1}

        def download(self, path, consume):
            consume(b"12345" if mode == "grows" else b"1234")
            if mode == "fails":
                raise ConnectionError("secret password must not be returned")

    @contextmanager
    def connector(config):
        yield FakeRemote()

    service.connector = connector
    service.settings.servers["fake"] = ServerConfig(
        protocol="ftp", host="fake", username="u", root="/logs", allow_plain_ftp=True
    )
    job = service.download("fake", ["/logs/app.log"], max_bytes=3 if mode == "limit" else 10)
    service.jobs[job["job_id"]].result(timeout=5)
    result = service.status(job["job_id"])
    assert result["status"] == "failed" and not result["files"]
    assert "secret password" not in json.dumps(result)
    assert not list(service.store.directory(job["job_id"]).glob("*.part"))
    assert not list(service.store.directory(job["job_id"]).glob("*.log"))


def test_restart_recovers_status(service):
    dataset = service.store.new("test", "utf-8")
    assert service.status(dataset["dataset_id"])["status"] == "interrupted"


def test_expired_scan_budget(tmp_path):
    path = tmp_path / "log"
    path.write_text("hello")
    budget = Budget(100, 1)
    budget.deadline = 0
    with pytest.raises(ScanLimit, match="timeout"):
        list(lines(path, False, "utf-8", budget))
