#!/usr/bin/env python3
"""Local MCP caller for testing the server from the command line.

Examples:
  python call_local_mcp.py
  python call_local_mcp.py --tool browse_extract --args-json '{"url":"https://example.com"}'
  python call_local_mcp.py --tool cerca_sentenze_wrapper --args-json '{"parole":"12345/2024","pagina":1}'
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return to_jsonable(model_dump(mode="json"))
    if hasattr(value, "__dict__"):
        return {k: to_jsonable(v) for k, v in vars(value).items()}
    return str(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Call the local MCP server via stdio")
    parser.add_argument(
        "--tool",
        help="Tool name to call. If omitted, the script only lists available tools.",
    )
    parser.add_argument(
        "--args-json",
        default="{}",
        help='JSON object with tool arguments, for example: {"parole":"responsabilita medica","pagina":1}',
    )
    parser.add_argument(
        "--server-command",
        default=sys.executable,
        help="Executable used to launch the local server process.",
    )
    parser.add_argument(
        "--server-args",
        nargs="*",
        default=["-m", "general_mcp.server"],
        help="Arguments passed to --server-command.",
    )
    parser.add_argument(
        "--cwd",
        default=str(Path(__file__).resolve().parent),
        help="Working directory where the MCP server process will be started.",
    )
    return parser


async def run(tool: str | None, args_json: str, server_command: str, server_args: list[str], cwd: str) -> int:
    try:
        tool_args = json.loads(args_json)
        if not isinstance(tool_args, dict):
            raise ValueError("--args-json must decode to a JSON object")
    except Exception as exc:
        print(f"Invalid --args-json: {exc}", file=sys.stderr)
        return 2

    server_env = os.environ.copy()
    server_env["MCP_TRANSPORT"] = "stdio"

    server = StdioServerParameters(
        command=server_command,
        args=server_args,
        cwd=cwd,
        env=server_env,
    )

    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init_result = await session.initialize()
            print("Connected to server:", init_result.serverInfo.name)

            tools_result = await session.list_tools()
            tools = [t.name for t in tools_result.tools]
            print("Available tools:", ", ".join(tools) if tools else "<none>")

            if not tool:
                return 0

            if tool not in tools:
                print(f"Tool '{tool}' not found on server", file=sys.stderr)
                return 3

            result = await session.call_tool(tool, tool_args)
            print(json.dumps(to_jsonable(result), indent=2, ensure_ascii=True))
            return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return anyio.run(
        run,
        args.tool,
        args.args_json,
        args.server_command,
        args.server_args,
        args.cwd,
    )


if __name__ == "__main__":
    raise SystemExit(main())
