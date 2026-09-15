"""updater 行为验收：版本比较 / 换装状态 / 下载（HTTP 全 mock）。

不碰真实网络与开发库：httpx 走 mock transport（对齐项目内 httpx 依赖，
不起本地服务）；staging 落 tmp（monkeypatch get_settings.data_dir）。
"""
from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import app_info, updater  # noqa: E402


def _mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _mock_client(handler):
    """造 AsyncClient 替身：替换 updater.httpx.AsyncClient。

    必须先持原引用 real_client——直接在 lambda 里写 httpx.AsyncClient 会在
    setattr 生效后递归调用替身自身（kw 里已带 transport，报 multiple values）。
    """
    real_client = updater.httpx.AsyncClient

    def factory(**kw):
        return real_client(transport=_mock_transport(handler), **kw)

    return factory


@pytest.mark.parametrize(
    ("latest", "current", "expect"),
    [
        ("0.2.0", "0.1.0", True),
        ("0.1.0", "0.1.0", False),
        ("0.1.0", "0.2.0", False),
        ("0.10.0", "0.9.0", True),  # 数字比较非字符串
        ("0.2.0", "0.2", False),    # 长度不齐补 0
        ("1.0.0", "0.99.99", True),
        ("v0.3.0", "0.2.5", True),  # v 前缀
    ],
)
def test_version_gt(latest: str, current: str, expect: bool) -> None:
    assert updater._version_gt(latest, current) is expect


@pytest.mark.asyncio
async def test_check_update_prefers_manifest(tmp_path: Path, monkeypatch) -> None:
    """清单可达：一次请求拿全（版本/资产/校验值/说明），不打 GitHub API。"""
    manifest = {
        "schema": 1,
        "app": "Holdexar",
        "version": "9.9.9",
        "tag": "v9.9.9",
        "publishedAt": "2026-09-10T12:00:00Z",
        "notes": "- **新增** 更新清单",
        "assets": [
            {"role": "app", "name": "Holdexar-win64-v9.9.9.zip", "size": 4242,
             "sha256": "a" * 64},
            {"role": "seed", "name": "holdexar_seed.db", "size": 99, "sha256": "b" * 64},
        ],
    }
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "releases/download" in str(request.url):
            assert updater.MANIFEST_TAG in str(request.url)
            return httpx.Response(200, json=manifest)
        # 清单已命中，API 不该被调用
        raise AssertionError(f"不应请求 GitHub API：{request.url}")

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    result = await updater.check_update()
    assert result["source"] == "manifest"
    assert result["available"] is True
    assert result["latest"] == "9.9.9"
    assert result["tag"] == "v9.9.9"
    assert result["sizeBytes"] == 4242
    assert result["sha256"] == "a" * 64
    assert result["asset"] == "Holdexar-win64-v9.9.9.zip"
    assert "新增" in result["notes"]
    assert all("api.github.com" not in u for u in seen)


@pytest.mark.asyncio
async def test_check_update_manifest_falls_back_to_api(tmp_path: Path, monkeypatch) -> None:
    """清单 404（仓库还没发清单）：回落到 GitHub API，行为与老版本一致。"""
    release = {
        "tag_name": "v9.9.9",
        "body": "# 更新内容\n\n- **新增** X\n- [链接](https://example.com)\n\nSHA256: abc123",
        "assets": [
            {"name": "Holdexar-win64-v9.9.9-20260910-1200.zip", "size": 12345,
             "digest": "sha256:" + "c" * 64},
            {"name": "source.zip", "size": 1},
        ],
        "published_at": "2026-09-10T12:00:00Z",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "releases/download" in url:
            return httpx.Response(404)
        assert "releases/latest" in url
        return httpx.Response(200, json=release)

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    result = await updater.check_update()
    assert result["source"] == "api"
    assert result["available"] is True
    assert result["current"] == app_info.APP_VERSION
    assert result["latest"] == "9.9.9"
    assert "新增 X" in result["notes"]
    assert "[链接]" not in result["notes"]
    assert result["sizeBytes"] == 12345
    # 资产 digest 顺带成了校验值（API 路径也能拿到）
    assert result["sha256"] == "c" * 64


@pytest.mark.asyncio
async def test_check_update_bad_manifest_is_not_available(monkeypatch) -> None:
    """清单有版本但无可用资产：available=False + reason=no_asset，不误报有更新。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"schema": 1, "version": "0.9.0", "assets": []})

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    result = await updater.check_update()
    assert result == {"available": False, "reason": "no_asset"}


@pytest.mark.asyncio
async def test_check_update_network_fail_soft(tmp_path: Path, monkeypatch) -> None:
    """网络失败：清单与 API 皆不可达 → available=False + reason=network，绝不抛。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("blocked")

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    result = await updater.check_update()
    assert result == {"available": False, "reason": "network", "error": "blocked"}


@pytest.mark.asyncio
async def test_download_and_pending_flow(tmp_path: Path, monkeypatch) -> None:
    """下载 → 解包 → manifest：pending 状态翻转 + sha256 校验拒绝坏档。

    下载通道链 mock 成直连单通道（不依赖 clash_manager 网络发现）。
    """
    # 造一个合法 onedir 结构 zip（根目录 Holdexar/）
    payload = tmp_path / "fake-release.zip"
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("Holdexar/Holdexar.exe", "MZ-fake")
        zf.writestr("Holdexar/web/index.html", "<html/>")

    def handler(request: httpx.Request) -> httpx.Response:
        if "releases/tags" in str(request.url):
            return httpx.Response(200, json={
                "assets": [{"name": payload.name, "browser_download_url": "https://x/fake.zip"}]
            })
        return httpx.Response(200, content=payload.read_bytes())

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    monkeypatch.setattr(
        updater, "staging_dir", lambda: tmp_path / "staging"
    )
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "app.domains.proxies.clash_manager._download_attempts",
        lambda *_a, **_k: [(None, "直连")],
    )

    # 无期望 sha256：跳过校验，正常暂存
    result = await updater.download_update("v0.2.0", expected_sha256=None)
    assert result["ok"] is True
    status = updater.pending_status()
    assert status["pending"] is True and status["tag"] == "v0.2.0"
    assert (tmp_path / "staging" / "Holdexar" / "Holdexar.exe").is_file()

    # clear 后 pending 复位
    updater.clear_staging()
    assert updater.pending_status() == {"pending": False}

    # 期望值不匹配：staging 清空 + 抛错
    import hashlib

    real_sha = hashlib.sha256(payload.read_bytes()).hexdigest()
    await updater.download_update("v0.2.0", expected_sha256=None)  # 重新暂存
    with pytest.raises(RuntimeError, match="SHA256"):
        await updater.download_update("v0.2.0", expected_sha256="dead" * 16)
    assert updater.pending_status() == {"pending": False}
    # 期望值正确：通过
    await updater.download_update("v0.2.0", expected_sha256=real_sha)
    assert updater.pending_status()["pending"] is True
    updater.clear_staging()


@pytest.mark.asyncio
async def test_download_uses_manifest_asset_name(tmp_path: Path, monkeypatch) -> None:
    """清单给了确切资产名：按名直下，不做 release 资产列表反查（省一次 API 调用）。"""
    payload = tmp_path / "asset.zip"
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("Holdexar/Holdexar.exe", "MZ-fake")

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        # 有确切资产名时不该再去打 releases/tags 反查
        assert "releases/tags" not in url, f"不应反查资产列表：{url}"
        return httpx.Response(200, content=payload.read_bytes())

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    monkeypatch.setattr(updater, "staging_dir", lambda: tmp_path / "staging")
    monkeypatch.setattr(
        "app.domains.proxies.clash_manager._download_attempts",
        lambda *_a, **_k: [(None, "直连")],
    )

    await updater.download_update("v0.2.0", None, "Holdexar-win64-v9.9.9.zip")
    assert updater.pending_status()["pending"] is True
    assert any("Holdexar-win64-v9.9.9.zip" in u for u in seen)
    updater.clear_staging()


@pytest.mark.asyncio
async def test_download_skips_slow_channel_and_sets_total_upfront(
    tmp_path: Path, monkeypatch
) -> None:
    """通道速率考核换道 + 开跑即挂总量（进度条有分母）。

    复现实测场景：首个镜像连上了却几乎不吐数据（40KB/s，整包拖近一小时），
    用户看到的就是「进度条不动、像卡死」。规则：非末位通道有试跑期速率下限，
    低于下限即换道；末位通道不考核（全网都慢也得让它下完）。
    """
    payload = tmp_path / "fake.zip"
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("Holdexar/Holdexar.exe", "MZ-fake")

    calls: list[tuple[str, int, dict]] = []

    async def fake_stream(url, proxy, dest, expected_total=None, min_rate_bps=0):
        calls.append((url, min_rate_bps, dict(updater.download_progress())))
        if len(calls) < 3:
            raise RuntimeError("通道过慢（0 KB/s < 128 KB/s），换下一通道")
        shutil.copyfile(payload, dest)

    monkeypatch.setattr(updater, "_stream_to_file", fake_stream)
    monkeypatch.setattr(updater, "staging_dir", lambda: tmp_path / "staging")
    monkeypatch.setattr(
        "app.domains.proxies.clash_manager._download_attempts",
        lambda *_a, **_k: [(None, "直连")],
    )

    result = await updater.download_update("v0.2.0", None, "x.zip", expected_size=999)

    assert result["ok"] is True
    assert len(calls) == 3, "两条镜像都应被判慢换掉，最终落到直连"
    assert [c[1] for c in calls] == [
        updater._MIN_CHANNEL_RATE, updater._MIN_CHANNEL_RATE, 0,
    ], "末位通道不做速率考核（否则全网都慢时会把所有通道试死）"
    # 第一条通道的试跑期里，进度就必须带总量与 0%——否则进度条无从渲染
    assert calls[0][2]["total"] == 999 and calls[0][2]["percent"] == 0
    updater.clear_staging()
