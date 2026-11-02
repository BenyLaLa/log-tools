import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from log_tools import client


def cli(config, *args):
    return subprocess.run(
        [sys.executable, "-m", "log_tools.client", "--config", str(config), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
    )


def test_windows_credentials_fallback_without_exposing_values(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"servers": {"dev": {"password_env": "UNIT_LOG_SECRET"}}}))
    monkeypatch.delenv("UNIT_LOG_SECRET", raising=False)
    looked_up = []

    def lookup(name):
        looked_up.append(name)
        return "private-test-value"

    monkeypatch.setattr(client, "windows_value", lookup)
    env, statuses, secrets = client.child_environment(config)
    assert env["UNIT_LOG_SECRET"] == "private-test-value"
    assert looked_up == ["UNIT_LOG_SECRET"]
    assert "private-test-value" not in json.dumps(statuses)
    assert statuses[0]["source"] == "windows_user_or_system"
    assert secrets == ["private-test-value"]
    monkeypatch.setenv("UNIT_LOG_SECRET", "process-value")
    assert client.child_environment(config)[0]["UNIT_LOG_SECRET"] == "process-value"
    assert (
        client.child_environment(config, {"UNIT_LOG_SECRET": "explicit"})[0]["UNIT_LOG_SECRET"]
        == "explicit"
    )
    monkeypatch.delenv("UNIT_LOG_SECRET")
    assert "UNIT_LOG_SECRET" not in client.child_environment(config, inherit_only=True)[0]


def test_clean_errors_do_not_echo_secrets():
    error = ExceptionGroup("failed", [ValueError("server says my-private-password")])
    assert "my-private-password" not in client.clean_error(error, ["my-private-password"])
    assert "bad-secret-name" not in client.clean_error(
        ValueError("Required credential environment variable is missing: bad-secret-name")
    )


def test_client_commands_against_real_mcp(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "test.log"
    log.write_text(
        "2026-10-27 10:00:00 ERROR traceId=trial old\n"
        "    at Old.run(Old.java:1)\n"
        "2026-10-27 10:02:00 ERROR traceId=trial newest\n"
        "    at New.run(New.java:2)\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"cache_dir": str(tmp_path / "cache"), "local_roots": [str(logs)]})
    )
    result = cli(config, "check")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["tool_count"] == 10
    result = cli(config, "import", str(log))
    assert result.returncode == 0, result.stderr
    dataset = json.loads(result.stdout)
    dataset_id = dataset["dataset_id"]
    result = cli(config, "latest", "--dataset", dataset_id, "--after", "2")
    assert result.returncode == 0, result.stderr
    latest = json.loads(result.stdout)
    assert latest["errors"][0]["line"] == 3
    assert latest["contexts"][0]["lines"][-1]["line"] == 4
    assert not latest["incomplete"]
    result = cli(config, "search", "--dataset", dataset_id, "--trace-id", "trial", "--limit", "1")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["next_offset"] == 1
    result = cli(config, "summary", "--dataset", dataset_id)
    assert json.loads(result.stdout)["error_events"] == 2
    report = tmp_path / "report.json"
    result = cli(config, "--output", str(report), "datasets")
    assert result.returncode == 0 and report.is_file(), result.stderr
    old = report.read_bytes()
    result = cli(config, "--output", str(report), "datasets")
    assert result.returncode == 1 and report.read_bytes() == old
    # No investigation scripts or diagnostics are created beside the config.
    assert {p.name for p in tmp_path.iterdir()} == {"logs", "cache", "config.json", "report.json"}


def test_existing_client_config_launch(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"cache_dir": str(tmp_path / "cache")}))
    launch = tmp_path / "mcp-client.json"
    launch.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "log-tools": {
                        "command": sys.executable,
                        "args": ["-m", "log_tools", "--config", str(config)],
                    }
                }
            }
        )
    )
    result = cli(config, "--client-config", str(launch), "check")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["connected"]


def test_latest_event_order_across_files_and_stack(service):
    first = service.settings.local_roots[0] / "a.log"
    second = service.settings.local_roots[0] / "b.log"
    first.write_text(
        "2026-10-27 11:00:00 ERROR latest\n"
        "    at newest.Stack(Stack.java:10)\n"
        "Caused by: inner ERROR without timestamp\n",
        encoding="utf-8",
    )
    second.write_text(
        "2026-10-27T01:00:00Z ERROR older\n"
        '{"timestamp":"2026-10-27T02:00:00Z","level":"FATAL","message":"middle"}\n',
        encoding="utf-8",
    )
    dataset = service.import_local([str(first), str(second)])
    result = service.query.latest_errors(dataset["dataset_id"], 3)
    assert result["error_events_scanned"] == 3
    assert [e["filename"] for e in result["errors"]] == ["a.log", "b.log", "b.log"]
    assert result["errors"][0]["line"] == 1
    assert result["errors"][1]["level"] == "FATAL"


def test_latest_undated_partial_and_budget(service):
    path = service.settings.local_roots[0] / "a.log"
    path.write_text("ERROR unknown time\n2026-10-27 10:00:00 ERROR known\n")
    dataset = service.import_local([str(path)])
    result = service.query.latest_errors(dataset["dataset_id"])
    assert result["undated_error_events"] == 1 and result["truncated"]
    assert result["errors"][1]["timestamp"] is None
    dataset["status"] = "partial"
    service.store.save(dataset)
    assert (
        "dataset_is_not_complete" in service.query.latest_errors(dataset["dataset_id"])["warnings"]
    )
    service.settings.scan_max_bytes = 1
    assert "scan_byte_limit" in service.query.latest_errors(dataset["dataset_id"])["warnings"]
    with pytest.raises(ValueError):
        service.query.latest_errors(dataset["dataset_id"], 0)


def test_remote_latest_refuses_incomplete_listing_or_download(monkeypatch):
    args = SimpleNamespace(
        action="latest",
        dataset=None,
        server="dev",
        directory=".",
        pattern="*error*",
        recursive=False,
        max_depth=None,
        max_files=10,
        max_bytes=1000,
        wait_timeout=2,
    )

    async def truncated(session, name, arguments):
        return {"files": [], "truncated": True}

    monkeypatch.setattr(client, "call", truncated)
    with pytest.raises(client.ClientError, match="Too many"):
        asyncio.run(client.dispatch(None, args, []))

    async def partial(session, name, arguments):
        if name == "list_remote_logs":
            return {"files": [{"size": 10, "remote_path": "/log"}], "truncated": False}
        if name == "download_logs":
            return {"job_id": "test"}
        if name == "get_download_status":
            return {"status": "partial"}
        raise AssertionError("Incomplete downloads must not be diagnosed as latest")

    monkeypatch.setattr(client, "call", partial)
    assert asyncio.run(client.dispatch(None, args, []))["ok"] is False


def test_files_collects_discovery_pages_in_one_session(monkeypatch):
    args = client.parser().parse_args(["files", "--server", "dev", "--limit", "1"])
    calls = []

    async def pages(session, name, arguments):
        calls.append(arguments)
        if arguments.get("cursor"):
            return {"files": [{"name": "old.log.gz"}], "next_cursor": None,
                    "has_more": False, "truncated": False}
        return {"files": [{"name": "current.log"}], "next_cursor": "cursor",
                "has_more": True, "truncated": True, "incomplete": False}

    monkeypatch.setattr(client, "call", pages)
    result = asyncio.run(client.dispatch(None, args, []))
    assert len(result["files"]) == 2 and not result["truncated"]
    assert calls[1] == {"server": "dev", "cursor": "cursor", "limit": 1}


def test_latest_refuses_incomplete_discovery_before_downloading(monkeypatch):
    args = client.parser().parse_args(["latest", "--server", "dev", "--recursive"])

    async def incomplete(session, name, arguments):
        assert name == "list_remote_logs"
        assert arguments["recursive"] is True
        return {"files": [{"remote_path": "/error.log"}], "incomplete": True}

    monkeypatch.setattr(client, "call", incomplete)
    with pytest.raises(client.ClientError, match="Discovery incomplete"):
        asyncio.run(client.dispatch(None, args, []))
