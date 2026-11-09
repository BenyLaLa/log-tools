import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_real_stdio_tool_calls(tmp_path):
    log = tmp_path / "sample.log"
    log.write_text("2026-10-22 10:00:00 ERROR traceId=smoke connection timed out\n")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"cache_dir": str(tmp_path / "cache"), "local_roots": [str(tmp_path)]})
    )

    async def scenario():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "log_tools", "--config", str(config)],
            env=dict(os.environ),
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 11
                assert "latest_errors" in {tool.name for tool in tools.tools}
                imported = await session.call_tool("import_local_logs", {"paths": [str(log)]})
                assert not imported.isError
                assert imported.structuredContent
                dataset_id = imported.structuredContent["dataset_id"]
                searched = await session.call_tool(
                    "search_logs", {"dataset_id": dataset_id, "trace_id": "smoke"}
                )
                assert not searched.isError
                assert searched.structuredContent["matches"][0]["line"] == 1
                latest = await session.call_tool("latest_errors", {"dataset_id": dataset_id})
                assert not latest.isError
                assert latest.structuredContent["errors"][0]["line"] == 1
                invalid = await session.call_tool("search_logs", {"dataset_id": "../bad"})
                assert invalid.isError

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))
