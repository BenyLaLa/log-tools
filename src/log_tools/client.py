"""Reusable stdio MCP client. No per-investigation scripts or result files required."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .query import redact


class ClientError(Exception):
    pass


def windows_value(name):
    """Read only a requested credential, not the entire Windows environment."""
    if os.name != "nt":
        return None
    import winreg

    for hive, path in (
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    ):
        try:
            with winreg.OpenKey(hive, path) as key:
                value, kind = winreg.QueryValueEx(key, name)
                if isinstance(value, str) and value:
                    return os.path.expandvars(value) if kind == winreg.REG_EXPAND_SZ else value
        except OSError:
            continue
    return None


def child_environment(config_path, overrides=None, inherit_only=False):
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    env = dict(os.environ)
    overrides = overrides or {}
    env.update(overrides)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    statuses, secrets = [], []
    for alias, server in config.get("servers", {}).items():
        for kind in ("password_env", "key_passphrase_env"):
            name = server.get(kind)
            if not name:
                continue
            if not isinstance(name, str):
                raise ClientError("Credential environment references must be strings")
            source = "client_config" if name in overrides else "process"
            if name not in overrides and not env.get(name) and not inherit_only:
                value = windows_value(name)
                if value:
                    env[name] = value
                    source = "windows_user_or_system"
            present = bool(env.get(name))
            statuses.append(
                {
                    "server": alias,
                    "credential": kind,
                    "present": present,
                    "source": source if present else "missing",
                }
            )
            if present:
                secrets.append(env[name])
    return env, statuses, secrets


def connection(args):
    config = Path(args.config).resolve()
    command, command_args, overrides, cwd = (
        sys.executable,
        ["-m", "log_tools", "--config", str(config)],
        {},
        None,
    )
    if getattr(sys, "frozen", False):
        command = str(Path(sys.executable).with_name("log-tools-mcp.exe"))
        command_args = ["--config", str(config)]
    if args.client_config:
        client_path = Path(args.client_config).resolve()
        document = json.loads(client_path.read_text(encoding="utf-8-sig"))
        entry = document.get("mcpServers", {}).get(args.client_name)
        if not entry:
            raise ClientError("MCP client entry not found")
        command, command_args = entry["command"], entry.get("args", [])
        overrides, cwd = entry.get("env", {}), entry.get("cwd")
        if cwd:
            cwd = str((client_path.parent / cwd).resolve())
        if "--config" in command_args:
            config = Path(command_args[command_args.index("--config") + 1])
            if not config.is_absolute():
                config = (Path(cwd or os.getcwd()) / config).resolve()
    env, statuses, secrets = child_environment(config, overrides, args.inherit_env_only)
    return (
        StdioServerParameters(command=command, args=command_args, env=env, cwd=cwd),
        statuses,
        secrets,
    )


def clean_error(error, secrets=()):
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(clean_error(item, secrets) for item in error.exceptions)
    text = str(error) or type(error).__name__
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    if "Required credential environment variable is missing:" in text:
        return "Credential variable missing. Run check; set the configured password_env in the launching user's environment."
    return redact(text)[:2000]


async def call(session, name, arguments):
    result = await session.call_tool(name, arguments)
    if result.isError:
        text = " ".join(item.text for item in result.content if hasattr(item, "text"))
        raise ClientError(text or f"{name} failed")
    if result.structuredContent is not None:
        return result.structuredContent
    for item in result.content:
        if hasattr(item, "text"):
            return json.loads(item.text)
    raise ClientError(f"{name} returned no structured result")


async def fetch(session, server, paths, max_bytes, wait_timeout):
    job = await call(
        session, "download_logs", {"server": server, "paths": paths, "max_bytes": max_bytes}
    )
    deadline = time.monotonic() + wait_timeout
    while True:
        status = await call(session, "get_download_status", {"job_id": job["job_id"]})
        if status["status"] not in ("queued", "downloading"):
            return status
        if time.monotonic() >= deadline:
            raise ClientError(
                f"Download wait timed out; dataset_id={job['dataset_id']}. This one-shot client's shutdown stops its worker; inspect status, then retry if interrupted."
            )
        await asyncio.sleep(0.5)


async def dispatch(session, args, statuses):
    action = args.action
    if action == "check":
        tools = await session.list_tools()
        servers = await call(session, "list_servers", {})
        return {
            "connected": True,
            "tool_count": len(tools.tools),
            "tools": [tool.name for tool in tools.tools],
            "credentials": statuses,
            **servers,
            "remote_connection_tested": False,
        }
    if action == "collect":
        arguments = {"server": args.server, "paths": args.paths or None, "mode": args.mode,
                     "query": args.query, "start_time": args.start, "end_time": args.end,
                     "level": args.level, "trace_id": args.trace_id, "before": args.before,
                     "after": args.after, "tail_bytes": args.tail_mib * 1024**2,
                     "timeout_seconds": args.seconds, "limit": args.limit,
                     "resume_dataset_id": args.resume}
        for name, field in (("network_mib", "max_network_bytes"), ("file_mib", "max_file_bytes"),
                            ("save_mib", "max_output_bytes")):
            value = getattr(args, name)
            arguments[field] = value * 1024**2 if value is not None else None
        return await call(session, "collect_log_fragments", arguments)
    if action == "servers":
        return await call(session, "list_servers", {})
    if action == "files":
        result = await call(
            session,
            "list_remote_logs",
            {
                "server": args.server,
                "directory": args.directory,
                "pattern": args.pattern,
                "recursive": args.recursive, "max_depth": args.max_depth,
                "limit": args.limit,
            },
        )
        while result.get("next_cursor"):
            page = await call(session, "list_remote_logs", {"server": args.server, "cursor": result["next_cursor"], "limit": args.limit})
            result["files"].extend(page["files"])
            result.update(next_cursor=page["next_cursor"], has_more=page["has_more"], truncated=page["truncated"])
        return result
    if action == "datasets":
        return await call(session, "list_datasets", {"limit": args.limit})
    if action == "status":
        return await call(session, "get_download_status", {"job_id": args.dataset})
    if action == "fetch":
        status = await fetch(session, args.server, args.paths, args.max_bytes, args.wait_timeout)
        return {"ok": status["status"] == "completed", **status}
    if action == "import":
        return await call(
            session, "import_local_logs", {"paths": [str(Path(p).resolve()) for p in args.paths]}
        )
    if action == "search":
        return await call(
            session,
            "search_logs",
            {
                "dataset_id": args.dataset,
                "query": args.query,
                "start_time": args.start,
                "end_time": args.end,
                "level": args.level,
                "trace_id": args.trace_id,
                "limit": args.limit,
                "offset": args.offset,
            },
        )
    if action == "context":
        return await call(
            session,
            "read_log_context",
            {
                "dataset_id": args.dataset,
                "file_id": args.file,
                "line": args.line,
                "before": args.before,
                "after": args.after,
            },
        )
    if action == "summary":
        return await call(session, "summarize_errors", {"dataset_id": args.dataset})
    if action == "latest":
        dataset = args.dataset
        download = None
        if args.server:
            listing = await call(
                session,
                "list_remote_logs",
                {
                    "server": args.server,
                    "directory": args.directory,
                    "pattern": args.pattern,
                    "recursive": args.recursive, "max_depth": args.max_depth,
                    "limit": 1000,
                },
            )
            files = listing["files"]
            if listing.get("incomplete"):
                raise ClientError("Discovery incomplete; inspect files output/warnings, adjust scan scope or limits, then retry")
            if listing.get("total_files", len(files)) > args.max_files:
                raise ClientError("Too many matching files; narrow --pattern/--directory or raise --max-files")
            if listing.get("truncated") or listing.get("next_cursor") or len(files) > args.max_files:
                raise ClientError(
                    "Too many matching files; narrow --pattern/--directory or raise --max-files"
                )
            if not files:
                raise ClientError("No files match --pattern in this directory")
            if args.max_bytes and sum(item["size"] for item in files) > args.max_bytes:
                raise ClientError("Selected files exceed --max-bytes; narrow the file selection")
            download = await fetch(
                session,
                args.server,
                [item["remote_path"] for item in files],
                args.max_bytes,
                args.wait_timeout,
            )
            if download["status"] != "completed":
                return {
                    "ok": False,
                    "message": "Download incomplete; not claiming the latest error",
                    "download": download,
                }
            dataset = download["dataset_id"]
        result = await call(session, "latest_errors", {"dataset_id": dataset, "limit": args.limit})
        contexts = []
        for event in result["errors"]:
            contexts.append(
                await call(
                    session,
                    "read_log_context",
                    {
                        "dataset_id": dataset,
                        "file_id": event["file_id"],
                        "line": event["line"],
                        "before": args.before,
                        "after": args.after,
                    },
                )
            )
        result["contexts"] = contexts
        result["incomplete"] = bool(result.get("warnings")) or any(
            c.get("truncated") for c in contexts
        )
        if download:
            result["download"] = download
        return result
    raise ClientError("Unknown action")


def bounded(low, high):
    def parse(value):
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"Must be between {low} and {high}")
        return number

    return parse


def parser():
    root = Path(__file__).resolve().parents[2]
    result = argparse.ArgumentParser(
        description="Reusable log MCP queries; JSON on stdout, no report files by default"
    )
    result.add_argument(
        "--config", default=str(root / "config.json"), help="Log server configuration"
    )
    result.add_argument(
        "--client-config", help="Optional existing mcpServers JSON; use its command/args/env"
    )
    result.add_argument("--client-name", default="log-tools")
    result.add_argument(
        "--inherit-env-only",
        action="store_true",
        help="Do not fill missing credentials from Windows user/system variables",
    )
    result.add_argument(
        "--output", help="Also save JSON to a NEW file; existing files are never overwritten"
    )
    sub = result.add_subparsers(dest="action", required=True)
    sub.add_parser(
        "check", help="MCP handshake, tool inventory and credential presence (never values)"
    )
    sub.add_parser("servers", help="Configured server aliases")
    files = sub.add_parser("files", help="List remote logs; this also checks remote authentication")
    files.add_argument("--server", required=True)
    files.add_argument("--directory", default=".")
    files.add_argument("--pattern", default=None)
    files.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=None)
    files.add_argument("--max-depth", type=bounded(0, 10))
    files.add_argument("--limit", type=bounded(1, 1000), default=100)
    collect = sub.add_parser("collect", help="SFTP range/stream search; save matching context only, no original-file copies")
    collect.add_argument("--server", required=True)
    collect.add_argument("paths", nargs="*")
    collect.add_argument("--mode", choices=["tail", "history", "full"], default="tail")
    collect.add_argument("--query", default="")
    collect.add_argument("--trace-id")
    collect.add_argument("--start")
    collect.add_argument("--end")
    collect.add_argument("--level", choices=["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL", "CRITICAL"])
    collect.add_argument("--before", type=bounded(0, 50), default=20)
    collect.add_argument("--after", type=bounded(0, 100), default=60)
    collect.add_argument("--tail-mib", type=bounded(1, 32), default=8)
    collect.add_argument("--network-mib", type=bounded(1, 2048))
    collect.add_argument("--file-mib", type=bounded(1, 2048))
    collect.add_argument("--save-mib", type=bounded(1, 16))
    collect.add_argument("--seconds", type=bounded(1, 120))
    collect.add_argument("--limit", type=bounded(1, 100), default=100)
    collect.add_argument("--resume", help="Previous fragment dataset ID; keeps its paths, mode and filters")
    datasets = sub.add_parser("datasets", help="List cached snapshots")
    datasets.add_argument("--limit", type=bounded(1, 200), default=50)
    for name in ("status", "summary"):
        item = sub.add_parser(name)
        item.add_argument("--dataset", required=True)
    download = sub.add_parser(
        "fetch", help="Download explicit remote paths and wait for completion"
    )
    download.add_argument("--server", required=True)
    download.add_argument("paths", nargs="+")
    imported = sub.add_parser("import", help="Import files from allowlisted local roots")
    imported.add_argument("paths", nargs="+")
    search = sub.add_parser(
        "search", help="Search a cached snapshot; use next_offset for the next page"
    )
    search.add_argument("--dataset", required=True)
    search.add_argument("--query", default="")
    search.add_argument("--trace-id")
    search.add_argument("--start", help="ISO timestamp; explicit timezone recommended")
    search.add_argument("--end")
    search.add_argument(
        "--level",
        choices=["TRACE", "DEBUG", "INFO", "WARN", "WARNING", "ERROR", "FATAL", "CRITICAL"],
    )
    search.add_argument("--limit", type=bounded(1, 100), default=50)
    search.add_argument("--offset", type=bounded(0, 100000), default=0)
    context = sub.add_parser("context", help="Read lines around an evidence location")
    context.add_argument("--dataset", required=True)
    context.add_argument("--file", required=True)
    context.add_argument("--line", required=True, type=bounded(1, 10**12))
    latest = sub.add_parser(
        "latest", help="Newest error events plus context; optionally fetch a fresh snapshot"
    )
    source = latest.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset")
    source.add_argument("--server")
    latest.add_argument("--directory", default=".")
    latest.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=False,
                        help="Include historical directories; default checks current logs only")
    latest.add_argument("--max-depth", type=bounded(0, 10))
    latest.add_argument("--pattern", default="*error*.log*")
    latest.add_argument("--max-files", type=bounded(1, 200), default=10)
    latest.add_argument("--limit", type=bounded(1, 20), default=1)
    for item in (context, latest):
        item.add_argument("--before", type=bounded(0, 50), default=3)
        item.add_argument("--after", type=bounded(0, 50), default=30)
    for item in (download, latest):
        item.add_argument(
            "--max-bytes",
            type=bounded(1, 100 * 1024**3),
            default=None,
            help="Defaults to the MCP server's configured download limit",
        )
        item.add_argument("--wait-timeout", type=bounded(1, 900), default=120)
    return result


async def execute(args, params, statuses):
    # MCP library diagnostics are not query results; discard them rather than writing scratch logs.
    with open(os.devnull, "w") as diagnostics:
        async with stdio_client(params, errlog=diagnostics) as (reader, writer):
            async with ClientSession(
                reader, writer, read_timeout_seconds=timedelta(seconds=180)
            ) as session:
                await session.initialize()
                return await dispatch(session, args, statuses)


def main(argv=None):
    args = parser().parse_args(argv)
    secrets = []
    try:
        if args.output and Path(args.output).exists():
            raise ClientError("Output file already exists; choose a new --output path")
        params, statuses, secrets = connection(args)
        result = asyncio.run(execute(args, params, statuses))
        text = json.dumps(result, ensure_ascii=False, indent=2)
        for secret in secrets:
            text = text.replace(secret, "[REDACTED]")
        if args.output:
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as output:
                output.write(text + "\n")
        print(text)
        return 1 if result.get("ok") is False else 0
    except KeyboardInterrupt:
        print('{"ok":false,"error":"Interrupted"}', file=sys.stderr)
        return 130
    except Exception as error:
        print(
            json.dumps({"ok": False, "error": clean_error(error, secrets)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
