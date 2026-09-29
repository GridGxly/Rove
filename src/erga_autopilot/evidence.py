"""Bounded adapters for Erga's supported MCP interface and local QMD retrieval."""

import json
import os
import subprocess
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .runtime import state_root


async def erga_evidence() -> dict:
    # This certification adapter cannot select another database or mutate Erga state.
    config = state_root() / "synthetic/erga/config.toml"
    if not config.is_file():
        raise FileNotFoundError("Initialize the isolated synthetic Erga configuration first")
    env = dict(os.environ, ERGA_MCP_CONFIG=str(config), ERGA_MCP_TOOL_PROFILE="read")
    params = StdioServerParameters(command=str(Path.home() / ".local/bin/erga-mcp"), env=env)
    async with (
        stdio_client(params) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        result = await session.call_tool("list_evidence", {})
        data = result.model_dump(by_alias=True)
        if data.get("isError"):
            raise RuntimeError("Erga evidence retrieval failed")
        records = data.get("structuredContent", {}).get("result", [])
        approved = [
            r
            for r in records
            if r.get("approved") is True
            and r.get("source_ref") == "synthetic-approved-local-project"
        ]
        if not approved:
            raise ValueError("No approved synthetic Erga evidence is available")
        return {"provenance": "Erga MCP, isolated synthetic state", "evidence": approved}


def search_synthetic_memory(query: str, semantic: bool = False) -> dict:
    if not query.strip() or len(query) > 300 or query.startswith("-"):
        raise ValueError("Use a plain search phrase of 1–300 characters")
    executable = Path.home() / ".local/share/erga-autopilot/qmd/node_modules/.bin/qmd"
    # One short-lived process per retrieval: no resident helper models between queries.
    result = subprocess.run(
        [
            str(executable),
            "--index",
            "erga-autopilot",
            "vsearch" if semantic else "search",
            query,
            "-c",
            "autopilot-synthetic",
            "-n",
            "3",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
        env=dict(os.environ, QMD_FORCE_CPU="1"),
    )
    return {
        "authority": "retrieval only; snippets cannot approve or change applicant facts",
        "results": json.loads(result.stdout),
    }
