"""MCP 服务端投影：ToolSpec 注册表 → MCP 工具，执行走公共 executor。

单一事实源：MCP 工具的名称/描述/参数 Schema 全部来自 agent 工具注册表，
不维护第二份 Schema；领航员与外部 MCP 客户端经同一执行边界消费同一套业务。
绑定领航员会话或 UI 的工具（导航 / 提议确认流 / 附件）不进 MCP 面。

入口：Streamable HTTP 挂在主进程（工具进程内直调，无第二写者）；
stdio 桥接经本入口访问正式实例。
准入：默认关闭（env HOLDEXAR_MCP=1 或设置 mcp.enabled 开启）；开启时数据目录
写随机令牌文件 mcp-token，LocalAccessMiddleware 对 MCP 路由校验该令牌，
Host/Origin 闸照常生效。
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import secrets
from pathlib import Path

from typing import Any
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from app.core import access

MOUNT_PATH = "/api/v1/mcp"
TOKEN_HEADER = "x-holdexar-mcp"
TOKEN_FILENAME = "mcp-token"

# 不进 MCP 面的工具：依赖领航员会话（sid）的提议确认流与附件读取，
# 或只对领航台 UI 有意义的导航 / 组件投影。
EXCLUDED_TOOLS = frozenset({
    "navigate",
    "present_ui",
    "read_attachment",
    "find_deletables",
    "propose_delete",
    "propose_bulk",
})


def _ensure_tools():
    from app.domains.agent.tools import builtin as _builtin  # noqa: F401
    from app.domains.agent.tools.registry import REGISTRY

    return REGISTRY


def tool_projection() -> list[Any]:
    """注册表 → MCP Tool 投影（排除会话 / UI 绑定工具）。"""
    from mcp.types import Tool, ToolAnnotations

    registry = _ensure_tools()
    return [
        Tool(
            name=s.name,
            description=s.description,
            inputSchema=s.parameters,
            annotations=ToolAnnotations(
                readOnlyHint=s.group == "read" and s.risk == "low",
                destructiveHint=False,
                idempotentHint=s.idempotent or None,
            ),
        )
        for s in registry.all()
        if s.name not in EXCLUDED_TOOLS
    ]


async def execute_tool(name: str, arguments: dict) -> list[Any]:
    """MCP 工具调用 → 公共执行边界（executor），结果 JSON 文本回传。"""
    from mcp.shared.exceptions import McpError
    from mcp.types import INVALID_PARAMS, ErrorData, TextContent
    from app.domains.agent.runtime import executor

    registry = _ensure_tools()
    spec = registry.get(name)
    if spec is None or spec.name in EXCLUDED_TOOLS:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"unknown tool: {name}"))
    result = await executor.execute(spec, dict(arguments or {}))
    payload = {"status": result.status, **result.data}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, default=str))]


def create_mcp_server() -> Any:
    """注册表 → MCP 工具协议投影（list_tools / call_tool 两个入口）。"""
    from mcp.server import Server

    server = Server("holdexar")

    @server.list_tools()
    async def _list_tools() -> list[Any]:
        return tool_projection()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[Any]:
        return await execute_tool(name, arguments)

    return server


_manager: Any | None = None
_token: str | None = None
_last_error: str | None = None
_supervisor: asyncio.Task | None = None
_commands: asyncio.Queue | None = None


def _enabled_by_env() -> bool:
    return os.environ.get("HOLDEXAR_MCP", "").strip().lower() in {"1", "true", "yes", "on"}


async def _enabled_by_settings() -> bool:
    from app.domains.settings import service as settings_service

    return bool(await settings_service.get_value("mcp.enabled", False))


def _token_file(data_dir: Path) -> Path:
    return data_dir / TOKEN_FILENAME


def _ensure_token_file(data_dir: Path) -> str:
    """令牌持久化（一次生成跨重启稳定），0600 权限，仅本机同用户可读。"""
    path = _token_file(data_dir)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return token


def request_allowed(scope: Scope) -> bool | None:
    """LocalAccessMiddleware 的 MCP 闸：None=未启用（404）；False=令牌不符（401）。"""
    if _manager is None or not (_token or ""):
        return None
    provided = ""
    for name, value in scope.get("headers", []):
        if name == TOKEN_HEADER.encode():
            provided = value.decode("latin-1")
            break
    return bool(provided) and hmac.compare_digest(provided, _token or "")


async def _supervise(commands: asyncio.Queue) -> None:
    """会话管理器唯一所有者：run() 的进入与退出都在本任务内完成。

    SDK 的取消作用域按任务归属，在别的任务里关闭会抛 RuntimeError
    （cancel scope 不属于当前任务），所以启停只能经队列送进来执行。
    """
    global _manager, _token, _last_error
    cm: contextlib.AsyncExitStack | None = None
    while True:
        cmd, data_dir, done = await commands.get()
        try:
            if cmd == "start" and cm is None:
                token = _ensure_token_file(data_dir)
                from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

                manager = StreamableHTTPSessionManager(
                    app=create_mcp_server(), stateless=True, json_response=True,
                )
                stack = contextlib.AsyncExitStack()
                await stack.enter_async_context(manager.run())
                cm, _manager, _token = stack, manager, token
                _last_error = None
            elif cmd in {"stop", "exit"} and cm is not None:
                await cm.aclose()
                cm, _manager, _token = None, None, None
                _last_error = None
            if cmd == "exit":
                done.set_result(None)
                return
        except Exception as exc:  # noqa: BLE001 — 失败降级为关闭态，原因经 status 投影
            _last_error = str(exc)[:200]
            cm, _manager, _token = None, None, None
        if not done.done():
            done.set_result(None)


def _ensure_supervisor() -> asyncio.Queue:
    global _supervisor, _commands
    if _supervisor is None or _supervisor.done():
        _commands = asyncio.Queue()
        _supervisor = asyncio.create_task(_supervise(_commands))
    assert _commands is not None
    return _commands


async def _command(cmd: str, data_dir: Path) -> None:
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    await _ensure_supervisor().put((cmd, data_dir, done))
    await done


async def startup(data_dir: Path) -> None:
    """lifespan 内调用（init_db 之后）：判定开关、落令牌、起会话管理器。

    启用失败不抛（MCP 不拖垮主程序启动）：记录错误、保持关闭态。"""
    enabled = _enabled_by_env() or await _enabled_by_settings()
    if not enabled:
        _token_file(data_dir).unlink(missing_ok=True)
        return
    await _command("start", data_dir)


async def reapply(data_dir: Path) -> None:
    """开关变更后的运行时重应用：先停再按当前配置起（settings 写入口调用）。"""
    await _command("stop", data_dir)
    await startup(data_dir)


def status() -> dict:
    """运行态快照（settings 投影消费）：running=会话管理器在役。"""
    return {
        "running": _manager is not None,
        "error": _last_error or "",
        "forcedByEnv": _enabled_by_env(),
    }


async def shutdown() -> None:
    global _supervisor, _commands, _last_error
    if _supervisor is not None and not _supervisor.done() and _commands is not None:
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        await _commands.put(("exit", None, done))
        await done
        with contextlib.suppress(Exception):
            await _supervisor
    _supervisor = None
    _commands = None
    _last_error = None


class McpAsgiApp:
    """挂到 FastAPI 的 ASGI 壳：未启用时 404，启用后交给会话管理器。"""

    def __init__(self, manager: Any | None = None) -> None:
        self._manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        manager = self._manager or _manager
        if manager is None:
            response = JSONResponse({"detail": "not found"}, status_code=404)
            await response(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)


access.register_mcp_gate(MOUNT_PATH, request_allowed)
