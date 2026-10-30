"""MCP 面契约测试：注册表投影 / 执行边界 / 准入闸 / 进程内协议回环（不触网）。"""
import json

import pytest

from app.core import access
from app.domains.agent import mcp
from app.domains.agent.tools.registry import REGISTRY

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_projection_excludes_session_bound_tools():
    tools = mcp.tool_projection()
    names = {t.name for t in tools}
    assert len(names) == len(tools)
    assert len(tools) >= 60  # 注册表非空防线：builtin 未导入时此处暴露
    assert names == {s.name for s in REGISTRY.all()
                     if s.name not in mcp.EXCLUDED_TOOLS and s.group != "write"}
    assert not names & {"navigate", "present_ui", "read_attachment",
                        "propose_bulk", "propose_delete", "find_deletables", "start_task"}


def test_projection_excludes_write_group():
    # 外部客户端没有审批通道：写组工具不进 MCP 面，也不接受直调
    write_names = {s.name for s in REGISTRY.all() if s.group == "write"}
    assert write_names, "注册表写组为空时本测试失去意义"
    names = {t.name for t in mcp.tool_projection()}
    assert not names & write_names


def test_projection_schema_passthrough():
    tools = {t.name: t for t in mcp.tool_projection()}
    spec = REGISTRY.get("search_games")
    assert tools["search_games"].inputSchema == spec.parameters
    assert tools["search_games"].annotations.readOnlyHint is True


def test_projection_readonly_hint_follows_real_side_effects():
    tools = {t.name: t for t in mcp.tool_projection()}
    # 同步 / 刷新类在 read 守卫组但有联网写库副作用 → 不得标只读
    for name in ("sync_wallet", "refresh_game_price", "sync_subscriptions",
                 "notify_test", "ingest_appids"):
        assert tools[name].annotations.readOnlyHint is False, name


async def test_execute_unknown_tool_raises_protocol_error():
    from mcp.shared.exceptions import McpError

    with pytest.raises(McpError):
        await mcp.execute_tool("__nope__", {})
    with pytest.raises(McpError):
        await mcp.execute_tool("navigate", {})  # 排除面不可经 MCP 调用
    with pytest.raises(McpError):
        await mcp.execute_tool("add_follow", {"appid": 1})  # 写组不可经 MCP 直达
    with pytest.raises(McpError):
        await mcp.execute_tool("start_task", {})  # 编排入口不进外部面


async def test_execute_known_tool_returns_status_payload():
    out = await mcp.execute_tool("list_tools", {})
    assert len(out) == 1 and out[0].type == "text"
    payload = json.loads(out[0].text)
    assert payload["status"] == "ok"


async def test_acknowledge_update_rejected_over_mcp():
    from mcp.shared.exceptions import McpError

    with pytest.raises(McpError):
        await mcp.execute_tool("acknowledge_update", {})


async def test_acknowledge_update_write_path_intact_outside_mcp(monkeypatch):
    # 写组工具只是不进外部面：领航台侧执行边界与共享写者行为保持不变
    from app.core import updater
    from app.domains.agent.runtime import executor

    calls = []
    def acknowledge():
        calls.append(True)
        return {"acknowledged": True, "updated": len(calls) == 1}

    monkeypatch.setattr(updater, "acknowledge_update", acknowledge)
    spec = REGISTRY.get("acknowledge_update")
    assert spec is not None
    for updated in (True, False):
        result = await executor.execute(spec, {})
        assert result.status == "ok"
        assert result.data == {"acknowledged": True, "updated": updated}
    assert len(calls) == 2

    def fail():
        raise OSError("confirmation write failed")

    monkeypatch.setattr(updater, "acknowledge_update", fail)
    result = await executor.execute(spec, {})
    assert result.status == "error"
    assert "acknowledged" not in result.data


async def test_update_status_projects_completion_without_acknowledging(monkeypatch):
    from app.core import updater

    report = {"previousVersion": "old", "currentVersion": "new", "notes": "done"}
    monkeypatch.setattr(updater, "pending_status", lambda: {
        "pending": False, "installing": False, "justUpdated": report,
    })
    monkeypatch.setattr(updater, "download_progress", lambda: {})
    def forbidden():
        pytest.fail("read-only status must not acknowledge")
    monkeypatch.setattr(updater, "acknowledge_update", forbidden)
    out = await mcp.execute_tool("update_status", {})
    payload = json.loads(out[0].text)
    assert payload["justUpdated"] is True
    assert payload["updateReport"] == report
    assert payload["installing"] is False


# ---- 准入闸（LocalAccessMiddleware 的 MCP 分支）----


def _scope(path: str, headers: list[tuple[str, str]], method: str = "POST"):
    return {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": [(k.encode(), v.encode()) for k, v in headers],
        "server": ("127.0.0.1", 28765), "client": ("127.0.0.1", 1),
    }


async def _run(app, scope):
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await app(scope, receive, send)
    return sent


async def test_gate_disabled_rejects_with_404(monkeypatch):
    monkeypatch.setattr(mcp, "_manager", None)
    reached = []

    async def inner(scope, receive, send):
        reached.append(True)

    sent = await _run(access.LocalAccessMiddleware(inner),
                      _scope("/api/v1/mcp", [("host", "127.0.0.1:28765")]))
    assert not reached
    assert sent[0]["status"] == 404


async def test_gate_wrong_token_rejects_with_401(monkeypatch):
    monkeypatch.setattr(mcp, "_manager", object())
    monkeypatch.setattr(mcp, "_token", "expected-token")
    sent = await _run(access.LocalAccessMiddleware(lambda s, r, w: None),
                      _scope("/api/v1/mcp", [("host", "127.0.0.1:28765"),
                                             (mcp.TOKEN_HEADER, "wrong")]))
    assert sent[0]["status"] == 401


async def test_gate_valid_token_passes_host_then_reaches_app(monkeypatch):
    monkeypatch.setattr(mcp, "_manager", object())
    monkeypatch.setattr(mcp, "_token", "expected-token")
    reached = []

    async def inner(scope, receive, send):
        reached.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent = await _run(access.LocalAccessMiddleware(inner),
                      _scope("/api/v1/mcp", [("host", "127.0.0.1:28765"),
                                             (mcp.TOKEN_HEADER, "expected-token")]))
    assert reached
    assert sent[-1]["body"] == b"ok"


async def test_gate_foreign_host_still_blocked_before_token(monkeypatch):
    monkeypatch.setattr(mcp, "_manager", object())
    monkeypatch.setattr(mcp, "_token", "expected-token")
    sent = await _run(access.LocalAccessMiddleware(lambda s, r, w: None),
                      _scope("/api/v1/mcp", [("host", "evil.example:28765"),
                                             (mcp.TOKEN_HEADER, "expected-token")]))
    assert sent[0]["status"] == 421


# ---- 进程内协议回环：stateless JSON 模式下 initialize / tools/list / tools/call ----


async def _rpc(asgi_app, method: str, params: dict, id_: int = 1, path: str = "/"):
    body = json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params}).encode()
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "path": path, "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json"),
                    (b"accept", b"application/json, text/event-stream"),
                    (b"content-length", str(len(body)).encode()),
                    (b"host", b"127.0.0.1:28765")],
        "server": ("127.0.0.1", 28765), "client": ("127.0.0.1", 1),
    }
    await asgi_app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(payload) if payload else None


async def test_stateless_protocol_round_trip():
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    manager = StreamableHTTPSessionManager(
        app=mcp.create_mcp_server(), stateless=True, json_response=True,
    )
    app = mcp.McpAsgiApp(manager)
    async with manager.run():
        status, init = await _rpc(app, "initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"},
        })
        assert status == 200 and init["result"]["serverInfo"]["name"] == "holdexar"

        status, listed = await _rpc(app, "tools/list", {})
        names = {t["name"] for t in listed["result"]["tools"]}
        assert status == 200
        assert "search_games" in names
        assert not names & mcp.EXCLUDED_TOOLS

        status, called = await _rpc(app, "tools/call", {
            "name": "list_tools", "arguments": {},
        })
        assert status == 200
        payload = json.loads(called["result"]["content"][0]["text"])
        assert payload["status"] == "ok"

        status, err = await _rpc(app, "tools/call", {"name": "__nope__", "arguments": {}})
        assert status == 200
        assert err["result"]["isError"] is True
        assert "not available over MCP" in err["result"]["content"][0]["text"]


@pytest.mark.parametrize("suffix", ["", "/"])
async def test_full_application_mcp_paths_reach_protocol(monkeypatch, suffix):
    from httpx import ASGITransport, AsyncClient
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from app import main

    manager = StreamableHTTPSessionManager(app=mcp.create_mcp_server(), stateless=True, json_response=True)
    monkeypatch.setattr(mcp, "_manager", manager)
    monkeypatch.setattr(mcp, "_token", "path-test-token")
    async with manager.run():
        async with AsyncClient(transport=ASGITransport(app=main.create_app()), base_url="http://127.0.0.1") as client:
            response = await client.post(mcp.MOUNT_PATH + suffix,
                headers={mcp.TOKEN_HEADER: "path-test-token", "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "route-test", "version": "1"},
                }})
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "holdexar"
