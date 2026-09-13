"""桌面壳健康探活身份校验测试（端口撞车防御，不出网）。

背景：默认服务端口族曾与维护者机器上另一个本地应用（同一默认端口）
撞车——本地服务对任意路径应答 200 很常见。探活只看状态码就会把外来
服务误判成"本应用已在运行"，唤醒分支随即置前/用浏览器打开**对方的
页面**。health 载荷自带 {"app": APP_NAME, ...}，探活须比对身份字段：
异名 / 非 JSON / 非 200 一律视为外来占坑，不得唤醒。
"""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop"))

import main as desktop_main  # noqa: E402  （desktop/main.py，自带 sys.path 注入）
from app.core.app_info import APP_NAME  # noqa: E402


def _resp(status: int, body) -> httpx.Response:
    if isinstance(body, str):
        return httpx.Response(status, text=body)
    return httpx.Response(status, json=body)


def test_health_payload_with_own_app_name_is_ours():
    """200 + app=本应用名 → 本应用实例（真实 health 载荷形态）。"""
    r = _resp(200, {"status": "ok", "app": APP_NAME, "version": "0.1.0"})
    assert desktop_main._health_is_ours(r) is True


def test_health_payload_with_foreign_app_name_rejected():
    """200 但 app 异名（或占坑服务仿造的 200）→ 外来占坑，不唤醒。"""
    r = _resp(200, {"status": "ok", "app": "another-local-app"})
    assert desktop_main._health_is_ours(r) is False


def test_health_payload_missing_app_field_rejected():
    """200 但载荷无 app 字段 → 无法锚定身份，视为外来。"""
    r = _resp(200, {"status": "ok"})
    assert desktop_main._health_is_ours(r) is False


def test_non_json_200_rejected():
    """200 但响应体不是 JSON（如裸 HTML 欢迎页）→ 外来。"""
    assert desktop_main._health_is_ours(_resp(200, "<html>hello</html>")) is False


def test_non_200_rejected():
    """404（对方无此路由）→ 外来。"""
    r = _resp(404, {"detail": "Not Found"})
    assert desktop_main._health_is_ours(r) is False


def test_probe_quick_uses_identity(monkeypatch):
    """quick 探活走身份校验：异名 200 不算"已在运行"。"""
    seen: list[str] = []

    def fake_get(url, **kw):
        seen.append(url)
        return _resp(200, {"status": "ok", "app": "another-local-app"})

    monkeypatch.setattr(desktop_main.httpx, "get", fake_get)
    assert desktop_main._probe_running(quick=True) is False
    assert seen and seen[0].endswith("/api/v1/health")

    monkeypatch.setattr(
        desktop_main.httpx,
        "get",
        lambda url, **kw: _resp(200, {"status": "ok", "app": APP_NAME}),
    )
    assert desktop_main._probe_running(quick=True) is True


def test_wait_health_rejects_foreign_200(monkeypatch):
    """--server 启动等待同样校验身份：外来 200 不算就绪（超时返回 False）。"""

    def fake_get(url, **kw):
        return _resp(200, {"status": "ok", "app": "another-local-app"})

    monkeypatch.setattr(desktop_main.httpx, "get", fake_get)
    assert desktop_main._wait_health(0.2) is False


def test_port_family_migrated_off_legacy():
    """默认端口族已整体迁离历史段（28765/28965），旧值不得回潮。"""
    import os

    if os.environ.get("HOLDEXAR_PORT") or os.environ.get("HOLDEXAR_LOCK_PORT"):
        pytest.skip("环境变量覆盖了默认端口，跳过默认值断言")
    assert desktop_main.SERVER_PORT == 28765
    assert desktop_main.LOCK_PORT == 28965
    assert 18765 not in (desktop_main.SERVER_PORT, desktop_main.LOCK_PORT)
    assert 18765 not in desktop_main._LOCK_FALLBACKS
