"""MCP stdio 桥：把本机正式实例的 MCP 端点转发到 stdio，供 Codex 等本机开发 Agent。

子进程只做协议转发，不初始化数据库 / 调度器 / 代理池——业务执行全部在
正式实例进程内，不产生第二写者。准入令牌从数据目录 mcp-token 读取
（数据目录解析与主程序同一规则，尊重 HOLDEXAR_DATA_DIR）。

运行：PYTHONPATH=<仓库>/server python -m app.domains.agent.mcp_stdio
可选环境变量：HOLDEXAR_MCP_URL（默认 http://127.0.0.1:28765/api/v1/mcp）。
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.types import Content, Tool

_MCP_URL_DEFAULT = "http://127.0.0.1:28765/api/v1/mcp"


def _load_token() -> str:
    from app.core.paths import resolve_data_dir

    token = (resolve_data_dir() / "mcp-token").read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("mcp-token 为空：请确认主实例已启用 MCP（HOLDEXAR_MCP=1）")
    return token


async def run() -> None:
    from mcp.server.stdio import stdio_server

    url = os.environ.get("HOLDEXAR_MCP_URL", _MCP_URL_DEFAULT).rstrip("/")
    headers = {"x-holdexar-mcp": _load_token()}
    forward = Server("holdexar-stdio")

    @forward.list_tools()
    async def _list() -> list[Tool]:
        result = await session.list_tools()
        return result.tools

    @forward.call_tool()
    async def _call(name: str, arguments: dict) -> list[Content]:
        result = await session.call_tool(name, arguments or {})
        return list(result.content or [])

    async with httpx.AsyncClient(headers=headers, trust_env=False, follow_redirects=True) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                async with stdio_server() as (in_read, in_write):
                    print(f"holdexar mcp stdio bridge -> {url}", file=sys.stderr)
                    await forward.run(
                        in_read, in_write,
                        forward.create_initialization_options(),
                    )


if __name__ == "__main__":
    asyncio.run(run())
