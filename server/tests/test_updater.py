"""updater 行为验收：版本比较 / 换装状态 / 下载（HTTP 全 mock）。

不碰真实网络与开发库：httpx 走 mock transport（对齐项目内 httpx 依赖，
不起本地服务）；staging 落 tmp（monkeypatch get_settings.data_dir）。
"""
from __future__ import annotations

import hashlib
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
from app.core import update_signing  # noqa: E402


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


@pytest.fixture
def sign_manifest(monkeypatch):
    """测试签名工厂：生成一次性 Ed25519 密钥并注入信任表（不碰真实公钥）。"""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pub_hex = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()
    monkeypatch.setattr(update_signing, "TRUSTED_PUBKEYS", {"test": pub_hex})

    def _sign(manifest: dict) -> dict:
        signature = key.sign(update_signing.canonical_manifest_bytes(manifest))
        signed = dict(manifest)
        signed["signature"] = {
            "alg": "ed25519", "keyId": "test",
            "sig": base64.b64encode(signature).decode("ascii"),
        }
        return signed

    return _sign


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
        ("0.1.0-beta.2", "0.1.0", False),  # 预发布段更旧于同号正式版
        ("0.1.0", "0.1.0-beta.2", True),
        ("1.0.0-rc.1", "1.0.0", False),
        ("0.1.0-rc.1", "0.1.0-beta.9", True),  # 字母标识按字典序
        ("0.1.0-beta.2", "0.1.0-beta.1", True),
        ("0.1.0-alpha", "0.1.0-alpha.1", False),  # 同前缀标识少的更旧
        ("0.2.0-beta.1", "0.1.0", True),  # 主段先于预发布段
        ("0.1.0+build.5", "0.1.0", False),  # 构建元数据不参与比较
    ],
)
def test_version_gt(latest: str, current: str, expect: bool) -> None:
    assert updater._version_gt(latest, current) is expect


@pytest.mark.asyncio
async def test_new_staging_clears_handoff_marks(tmp_path: Path, monkeypatch, sign_manifest) -> None:
    """新解出的暂存包清掉上一包的交接标记：重下同一版本仍可自动换装。"""
    payload = tmp_path / "fake-release.zip"
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("Holdexar/Holdexar.exe", "MZ-fake")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload.read_bytes())

    staging = tmp_path / "staging"
    staging.mkdir(parents=True)
    (staging / updater.HANDOFF_UNSUPPORTED_MARK).write_text("stale", encoding="utf-8")
    # 换装失败标记的真实写入侧是数据目录（桌面壳落点），不是安装根
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / updater.SWAP_FAILED_FLAG).write_text("stale", encoding="utf-8")
    monkeypatch.setattr("app.core.paths.resolve_data_dir", lambda: data_dir)

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    monkeypatch.setattr(updater, "staging_dir", lambda: staging)
    monkeypatch.setattr(updater, "_MIN_PACKAGE_BYTES", 0)
    monkeypatch.setattr(
        "app.domains.proxies.clash_manager._download_attempts",
        lambda *_a, **_k: [(None, "直连")],
    )

    await updater.download_update(
        "v0.2.0",
        expected_sha256=hashlib.sha256(payload.read_bytes()).hexdigest(),
        asset_name="fake-release.zip",
    )

    assert not (staging / updater.HANDOFF_UNSUPPORTED_MARK).exists()
    assert not (data_dir / updater.SWAP_FAILED_FLAG).exists()
    assert updater.pending_status()["pending"] is True


@pytest.mark.asyncio
async def test_check_update_prefers_manifest(tmp_path: Path, monkeypatch, sign_manifest) -> None:
    """验签清单可达：一次请求拿全（版本/资产/校验值/说明），不打 GitHub API。"""
    manifest = sign_manifest({
        "schema": 2,
        "app": "Holdexar",
        "version": "9.9.9",
        "tag": "v9.9.9",
        "publishedAt": "2026-09-10T12:00:00Z",
        "notes": "- **新增** 更新清单",
        "assets": [
            {"role": "app", "name": "Holdexar-win64-v9.9.9.zip", "size": 4242,
             "sha256": "a" * 64},
        ],
    })
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
async def test_check_update_rejects_unsigned_manifest(monkeypatch) -> None:
    """镜像回未签名清单：按通道失败处理，回落 GitHub API（不拿它当可信数据）。"""
    unsigned = {
        "schema": 2, "version": "9.9.9", "tag": "v9.9.9",
        "assets": [{"role": "app", "name": "x.zip", "size": 1, "sha256": "a" * 64}],
    }
    release = {
        "tag_name": "v8.8.8",
        "body": "",
        "assets": [{"name": "Holdexar-win64-v8.8.8.zip", "size": 2, "digest": "sha256:" + "b" * 64}],
        "published_at": "2026-09-10T12:00:00Z",
    }
    api_hit: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "releases/download" in url:
            return httpx.Response(200, json=unsigned)
        api_hit.append(url)
        assert "releases/latest" in url
        return httpx.Response(200, json=release)

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    result = await updater.check_update()
    assert api_hit, "未签名清单必须被拒并回落 API"
    assert result["source"] == "api"
    assert result["latest"] == "8.8.8"


@pytest.mark.asyncio
async def test_check_update_rejects_tampered_manifest(monkeypatch, sign_manifest) -> None:
    """清单被镜像篡改（签名后改版本号）：验签不过，等同通道失败。"""
    manifest = sign_manifest({
        "schema": 2, "version": "9.9.9", "tag": "v9.9.9",
        "assets": [{"role": "app", "name": "x.zip", "size": 1, "sha256": "a" * 64}],
    })
    manifest["version"] = "99.99.99"  # 签名后篡改

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "releases/download" in url:
            return httpx.Response(200, json=manifest)
        raise httpx.ConnectError("no api fallback")

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    result = await updater.check_update()
    assert result == {"available": False, "reason": "network", "error": "no api fallback"}


@pytest.mark.asyncio
async def test_check_update_picks_verified_among_racing_mirors(monkeypatch, sign_manifest) -> None:
    """镜像竞速只在验签通过者中取：先到的未签名清单不挡后面的可信清单。"""
    signed = sign_manifest({
        "schema": 2, "version": "9.9.9", "tag": "v9.9.9", "notes": "signed",
        "assets": [{"role": "app", "name": "good.zip", "size": 1, "sha256": "a" * 64}],
    })
    unsigned = {
        "schema": 2, "version": "8.8.8", "tag": "v8.8.8",
        "assets": [{"role": "app", "name": "bad.zip", "size": 1, "sha256": "b" * 64}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # ghfast.top 是镜像链第一条（竞速结果列表的第一位）
        if "ghfast.top" in url:
            return httpx.Response(200, json=unsigned)
        if "releases/download" in url:
            return httpx.Response(200, json=signed)
        raise AssertionError(f"不应请求 GitHub API：{url}")

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    result = await updater.check_update()
    assert result["source"] == "manifest"
    assert result["latest"] == "9.9.9"
    assert result["asset"] == "good.zip"


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
async def test_check_update_bad_manifest_is_not_available(monkeypatch, sign_manifest) -> None:
    """验签清单有版本但无可用资产：available=False + reason=no_asset，不误报有更新。"""
    manifest = sign_manifest({"schema": 2, "version": "0.9.0", "assets": []})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=manifest)

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    result = await updater.check_update()
    assert result == {"available": False, "reason": "no_asset"}


@pytest.mark.asyncio
async def test_check_update_signed_manifest_without_sha256_refused(monkeypatch, sign_manifest) -> None:
    """验签清单的资产缺 sha256：按残缺清单拒绝（available=False），不给无校验下载留口。"""
    manifest = sign_manifest({
        "schema": 2, "version": "9.9.9", "tag": "v9.9.9",
        "assets": [{"role": "app", "name": "x.zip", "size": 10}],
    })

    def handler(request: httpx.Request) -> httpx.Response:
        if "releases/download" in str(request.url):
            return httpx.Response(200, json=manifest)
        raise httpx.ConnectError("no api")

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    result = await updater.check_update()
    assert result == {"available": False, "reason": "bad_manifest"}


@pytest.mark.asyncio
async def test_check_update_network_fail_soft(tmp_path: Path, monkeypatch) -> None:
    """网络失败：清单与 API 皆不可达 → available=False + reason=network，绝不抛。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("blocked")

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    result = await updater.check_update()
    assert result == {"available": False, "reason": "network", "error": "blocked"}


@pytest.mark.asyncio
async def test_download_rejects_missing_sha256(tmp_path: Path, monkeypatch) -> None:
    """校验值缺失即拒绝下载：不存在「无校验放行」的口子（护栏先于一切暂存动作）。"""
    monkeypatch.setattr(updater, "staging_dir", lambda: tmp_path / "staging")
    with pytest.raises(RuntimeError, match="校验值"):
        await updater.download_update("v0.2.0", expected_sha256=None)
    with pytest.raises(RuntimeError, match="校验值"):
        await updater.download_update("v0.2.0", expected_sha256="   ")


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
    real_sha = hashlib.sha256(payload.read_bytes()).hexdigest()

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
    # 用例用的是几百字节的假包：体积下限（防镜像错误页）在测试里归零
    monkeypatch.setattr(updater, "_MIN_PACKAGE_BYTES", 0)

    monkeypatch.setattr(
        "app.domains.proxies.clash_manager._download_attempts",
        lambda *_a, **_k: [(None, "直连")],
    )

    result = await updater.download_update("v0.2.0", expected_sha256=real_sha)
    assert result["ok"] is True
    status = updater.pending_status()
    assert status["pending"] is True and status["tag"] == "v0.2.0"
    assert (tmp_path / "staging" / "Holdexar" / "Holdexar.exe").is_file()
    manifest_data = json.loads((tmp_path / "staging" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_data.get("source_version") == updater.APP_VERSION

    # clear 后 pending 复位
    updater.clear_staging()
    cleared = updater.pending_status()
    assert cleared["pending"] is False and cleared["swapFailed"] is False

    # 期望值不匹配：staging 清空 + 抛错
    with pytest.raises(RuntimeError, match="SHA256"):
        await updater.download_update("v0.2.0", expected_sha256="dead" * 16)
    assert updater.pending_status()["pending"] is False
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
    # 用例用的是几百字节的假包：体积下限（防镜像错误页）在测试里归零
    monkeypatch.setattr(updater, "_MIN_PACKAGE_BYTES", 0)
    monkeypatch.setattr(
        "app.domains.proxies.clash_manager._download_attempts",
        lambda *_a, **_k: [(None, "直连")],
    )

    await updater.download_update(
        "v0.2.0", hashlib.sha256(payload.read_bytes()).hexdigest(),
        "Holdexar-win64-v9.9.9.zip",
    )
    assert updater.pending_status()["pending"] is True
    assert any("Holdexar-win64-v9.9.9.zip" in u for u in seen)
    updater.clear_staging()


@pytest.mark.asyncio
async def test_download_skips_missing_channel_before_downloading(
    tmp_path: Path, monkeypatch
) -> None:
    """探测阶段就排除「资产不存在」的通道（镜像 404），不浪费一次真实下载。

    镜像 404 有两种成因：镜像没同步、或该版本压根没发。区分不出来，但代价
    极低——探测是 1 字节 Range 请求；判断「全 404」= 版本没发，见下一条用例。
    """
    payload = tmp_path / "fake.zip"
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("Holdexar/Holdexar.exe", "MZ-fake")
    data = payload.read_bytes()

    # (url, 是否带 Range)：探测请求带 Range（1 字节）、正式下载不带
    seen: list[tuple[str, bool]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append((url, bool(request.headers.get("range"))))
        if "ghfast.top" in url:
            return httpx.Response(404)
        return httpx.Response(200, content=data)

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    monkeypatch.setattr(updater, "staging_dir", lambda: tmp_path / "staging")
    # 用例用的是几百字节的假包：体积下限（防镜像错误页）在测试里归零
    monkeypatch.setattr(updater, "_MIN_PACKAGE_BYTES", 0)
    monkeypatch.setattr(
        "app.domains.proxies.clash_manager._download_attempts",
        lambda *_a, **_k: [(None, "直连")],
    )

    result = await updater.download_update(
        "v0.2.0", hashlib.sha256(data).hexdigest(), "x.zip", expected_size=len(data)
    )
    assert result["ok"] is True
    # 404 通道只出现在探测请求里（带 Range），不出现在正式下载里
    assert [u for u, has_range in seen if "ghfast.top" in u and not has_range] == []
    assert any("ghfast.top" in u for u, _ in seen), "该镜像也应被探测（而非凭空跳过）"
    updater.clear_staging()


@pytest.mark.asyncio
async def test_download_asset_missing_fails_fast(tmp_path: Path, monkeypatch) -> None:
    """全通道 404（release 还是草稿 / 资产被撤）→ AssetMissing + code=asset_missing。

    探测一轮（各通道并发、8s 上限）即定性；串行试跑整条通道链（每通道最长 20s）
    才报错会让用户看到「下载永远下不动」。
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    monkeypatch.setattr(updater, "staging_dir", lambda: tmp_path / "staging")
    # 用例用的是几百字节的假包：体积下限（防镜像错误页）在测试里归零
    monkeypatch.setattr(updater, "_MIN_PACKAGE_BYTES", 0)
    monkeypatch.setattr(
        "app.domains.proxies.clash_manager._download_attempts",
        lambda *_a, **_k: [(None, "直连")],
    )

    with pytest.raises(updater.AssetMissing):
        await updater.download_update("v0.2.0", "a" * 64, "x.zip", expected_size=1234)
    progress = updater.download_progress()
    assert progress["code"] == "asset_missing"
    assert progress["running"] is False


@pytest.mark.asyncio
async def test_stream_to_file_resumes_with_range(tmp_path: Path, monkeypatch) -> None:
    """换通道不丢已下部分：第二次请求带 `Range: bytes=N-` 续传。

    125MB 的包每次换道都从 0 开始 = 永远下不完。
    """
    dest = tmp_path / "update.zip.part"
    dest.write_bytes(b"HEAD")
    ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ranges.append(request.headers.get("range") or "")
        return httpx.Response(206, content=b"TAIL", headers={
            "content-range": "bytes 4-7/8",
        })

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    await updater._stream_to_file("https://x/y.zip", None, dest, expected_total=8)

    assert ranges == ["bytes=4-"]
    assert dest.read_bytes() == b"HEADTAIL"


@pytest.mark.asyncio
async def test_stream_to_file_restarts_when_range_ignored(
    tmp_path: Path, monkeypatch
) -> None:
    """服务端不认 Range（回 200 整包）→ 必须从头写，否则追加出坏包。"""
    dest = tmp_path / "update.zip.part"
    dest.write_bytes(b"HEAD")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"FULL")

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    await updater._stream_to_file("https://x/y.zip", None, dest, expected_total=4)

    assert dest.read_bytes() == b"FULL"


@pytest.mark.asyncio
async def test_download_respects_cancel(tmp_path: Path, monkeypatch) -> None:
    """用户取消：下载循环在分片边界退出，返回 cancelled 而不是抛故障。"""
    payload = tmp_path / "fake.zip"
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("Holdexar/Holdexar.exe", "MZ-fake")
    data = payload.read_bytes()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=data)

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    monkeypatch.setattr(updater, "staging_dir", lambda: tmp_path / "staging")
    # 用例用的是几百字节的假包：体积下限（防镜像错误页）在测试里归零
    monkeypatch.setattr(updater, "_MIN_PACKAGE_BYTES", 0)
    monkeypatch.setattr(
        "app.domains.proxies.clash_manager._download_attempts",
        lambda *_a, **_k: [(None, "直连")],
    )

    # 探测刚落定即取消（download_update 开头会清旗标，必须在这之后置位）
    real_probe = updater._probe_channels

    async def probe_then_cancel(channels, fallback_total):
        probes = await real_probe(channels, fallback_total)
        updater._CANCEL["flag"] = True
        return probes

    monkeypatch.setattr(updater, "_probe_channels", probe_then_cancel)
    result = await updater.download_update(
        "v0.2.0", hashlib.sha256(data).hexdigest(), "x.zip", expected_size=len(data)
    )
    assert result["ok"] is False and result["cancelled"] is True
    assert updater.download_progress()["code"] == "cancelled"
    # 取消下载后，保留已下的 .part 供后续续传
    assert (tmp_path / "staging" / updater._PART_NAME).is_file()
    updater.clear_staging()


@pytest.mark.asyncio
async def test_download_sha256_mismatch_falls_back_to_next_channel(
    tmp_path: Path, monkeypatch
) -> None:
    """镜像返回错误内容（SHA256 不符）时，通道循环内即刻换道下一镜像，最终下载成功。"""
    payload = tmp_path / "real.zip"
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("Holdexar/Holdexar.exe", "MZ-valid")
    good_bytes = payload.read_bytes()
    good_sha = hashlib.sha256(good_bytes).hexdigest()
    bad_bytes = b"corrupt payload from bad mirror"

    called_urls: list[str] = []

    async def fake_stream(url, proxy, target_file: Path, *args, **kwargs):
        called_urls.append(url)
        if "bad_mirror" in url:
            target_file.write_bytes(bad_bytes)
        else:
            target_file.write_bytes(good_bytes)

    async def fake_probe(channels, fallback_total):
        return [
            {"label": "镜像1", "url": "https://bad_mirror/x.zip", "proxy": None, "missing": False, "bogus": False, "total": len(bad_bytes)},
            {"label": "镜像2", "url": "https://good_mirror/x.zip", "proxy": None, "missing": False, "bogus": False, "total": len(good_bytes)},
        ]

    monkeypatch.setattr(updater, "staging_dir", lambda: tmp_path / "staging")
    monkeypatch.setattr(updater, "_MIN_PACKAGE_BYTES", 0)
    monkeypatch.setattr(updater, "_probe_channels", fake_probe)
    monkeypatch.setattr(updater, "_stream_to_file", fake_stream)
    monkeypatch.setattr(
        "app.domains.proxies.clash_manager._download_attempts",
        lambda *_a, **_k: [(None, "直连")],
    )

    result = await updater.download_update("v0.2.0", expected_sha256=good_sha, asset_name="x.zip")
    assert result["ok"] is True
    assert called_urls == ["https://bad_mirror/x.zip", "https://good_mirror/x.zip"]
    assert updater.pending_status()["pending"] is True
    assert (tmp_path / "staging" / "Holdexar" / "Holdexar.exe").read_bytes() == b"MZ-valid"
    updater.clear_staging()


def test_download_meta_binding_discards_different_version(tmp_path: Path, monkeypatch) -> None:
    """旧 .part 文件若属于不同版本或资产，启动下载时必须清除重来，防止损坏拼接。"""
    staging = tmp_path / "staging"
    staging.mkdir(parents=True)
    part = staging / updater._PART_NAME
    part.write_bytes(b"old-partial-bytes")
    meta = staging / updater.DOWNLOAD_META_NAME
    meta.write_text(
        json.dumps({"tag": "v0.1.0", "asset": "x.zip", "size": 100, "sha256": "oldsha"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(updater, "staging_dir", lambda: staging)

    import asyncio

    async def fake_probe(channels, fallback_total):
        # 抛异常终止后续执行，只检验开头的元数据校验与清理行为
        raise updater._Cancelled()

    monkeypatch.setattr(updater, "_probe_channels", fake_probe)
    monkeypatch.setattr(
        "app.domains.proxies.clash_manager._download_attempts",
        lambda *_a, **_k: [(None, "直连")],
    )

    asyncio.run(updater.download_update("v0.2.0", expected_sha256="newsha", asset_name="x.zip", expected_size=100))
    # 因为 tag 不同，旧的 .part 必须已被清空
    assert not part.exists()


def test_mark_download_starting_prevents_race() -> None:
    """原子化置位标记：第一次标记成功，第二次并发调用被拦截。"""
    updater._PROGRESS.clear()
    assert updater.mark_download_starting("v0.2.0", 100) is True
    assert updater.mark_download_starting("v0.2.0", 100) is False
    updater._PROGRESS["running"] = False


@pytest.mark.asyncio
async def test_update_url_custom_override(monkeypatch) -> None:
    """测试指定 HOLDEXAR_UPDATE_URL 时走本地测试更新源。"""
    monkeypatch.setenv("HOLDEXAR_UPDATE_URL", "http://127.0.0.1:8888")
    assert updater.manifest_url() == "http://127.0.0.1:8888/latest.json"
    url = await updater._resolve_asset_url("v0.3.2-beta", "app.zip")
    assert url == "http://127.0.0.1:8888/app.zip"
    plan = updater._channel_plan("http://127.0.0.1:8888/app.zip", [])
    assert len(plan) == 1
    assert plan[0]["direct"] is True
    assert plan[0]["proxy"] is None


def test_extract_sanitizes_malicious_zip_members(tmp_path: Path) -> None:
    """全量包解包的 zip-slip 防线：`../`、盘符绝对路径成员不得写出暂存目录之外。"""
    malicious = tmp_path / "evil.zip"
    outside_marker = tmp_path / "escaped.txt"
    with zipfile.ZipFile(malicious, "w") as zf:
        zf.writestr("Holdexar/normal.txt", "ok")
        zf.writestr("../escaped.txt", "should-not-land")
        zf.writestr("Holdexar/../../escaped2.txt", "should-not-land")
        zf.writestr("C:/absolute/escaped3.txt", "should-not-land")

    staging = tmp_path / "staging"
    staging.mkdir()
    updater._extract(malicious, staging)

    assert (staging / "Holdexar" / "normal.txt").is_file()
    assert not outside_marker.exists()
    assert not (tmp_path / "escaped2.txt").exists()
    assert not Path("C:/absolute/escaped3.txt").exists()


def test_truncate_to_never_null_pads(tmp_path: Path) -> None:
    """_truncate_to 当文件小于期望大小时绝不能截断放大补零。"""
    p = tmp_path / "test.part"
    p.write_bytes(b"12345")
    updater._truncate_to(p, 10)
    # 小于 10 字节时不能补零到 10 字节，应该直接清理
    assert not p.exists() or p.stat().st_size <= 5


@pytest.mark.asyncio
async def test_stream_to_file_preserves_partial_on_200_failure(tmp_path: Path, monkeypatch) -> None:
    """若服务端不认 Range 返回 200 且下载中断，已有的续传文件不应被抹除或补零破坏。"""
    part = tmp_path / "test.part"
    part.write_bytes(b"initial-valid-bytes")
    initial_size = part.stat().st_size

    def handler(request: httpx.Request) -> httpx.Response:
        class AsyncStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"small"
                raise RuntimeError("stream broke")

        return httpx.Response(200, stream=AsyncStream())

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    with pytest.raises(RuntimeError, match="stream broke"):
        await updater._stream_to_file("https://mirror/file.zip", None, part, expected_total=100)

    # 初始的续传文件依然完好，没有被 200 的失败下载覆盖或冲掉
    assert part.is_file()
    assert part.read_bytes() == b"initial-valid-bytes"
    assert not (tmp_path / "test.part.full").exists()


@pytest.mark.asyncio
async def test_download_all_channels_fail_preserves_part_file(tmp_path: Path, monkeypatch) -> None:
    """所有通道失败时不应清空暂存区，需保留 .part 文件以便下次续传。"""
    staging = tmp_path / "update-staging"
    staging.mkdir()
    part = staging / "update.zip.part"
    part.write_bytes(b"downloaded-50MB-chunk")
    expected_sha = "f" * 64
    meta = staging / "download_meta.json"
    meta.write_text(json.dumps({"tag": "v0.3.6", "asset": "", "size": 0, "sha256": expected_sha}), encoding="utf-8")

    def handler(_request: httpx.Request):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    monkeypatch.setattr(updater, "staging_dir", lambda: staging)
    monkeypatch.setattr(
        "app.domains.proxies.clash_manager._download_attempts",
        lambda *_a, **_k: [(None, "直连")],
    )

    with pytest.raises(RuntimeError, match="所有下载通道均失败"):
        await updater.download_update("v0.3.6", expected_sha256=expected_sha)

    # .part 文件依然存在，未被清空
    assert part.is_file()
    assert part.read_bytes() == b"downloaded-50MB-chunk"


def test_just_updated_status_and_acknowledge(tmp_path: Path, monkeypatch) -> None:
    """换装升级成功状态判定与账本确认闭合。"""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.core.paths.logs_dir", lambda: logs_dir)
    monkeypatch.setattr("app.core.paths.install_root", lambda: tmp_path)

    # 1. 无诊断文件返回 None
    assert updater.just_updated_status() is None

    # 2. 诊断文件标记换装成功但未确认
    diag = logs_dir / "update-diagnosis.json"
    diag.write_text(
        json.dumps({
            "success": True,
            "stage": "committed",
            "sourceVersion": "0.3.1-beta",
            "targetVersion": updater.APP_VERSION,
            "notes": "Feature 1\nFeature 2",
            "acknowledged": False,
        }),
        encoding="utf-8",
    )

    status = updater.just_updated_status()
    assert status == {
        "previousVersion": "0.3.1-beta",
        "currentVersion": updater.APP_VERSION,
        "notes": "Feature 1\nFeature 2",
    }
    assert updater.pending_status()["justUpdated"] == status

    # 3. 确认后不再重复汇报
    ack = updater.acknowledge_update()
    assert ack["acknowledged"] is True
    assert updater.just_updated_status() is None
    assert updater.pending_status()["justUpdated"] is None


def test_just_updated_status_ignores_mismatched_version(tmp_path: Path, monkeypatch) -> None:
    """目标版本与当前运行版本不匹配时不汇报成功。"""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.core.paths.logs_dir", lambda: logs_dir)
    monkeypatch.setattr("app.core.paths.install_root", lambda: tmp_path)

    diag = logs_dir / "update-diagnosis.json"
    diag.write_text(
        json.dumps({
            "success": True,
            "stage": "committed",
            "currentVersion": "0.3.1-beta",
            "targetVersion": "99.99.99",
            "acknowledged": False,
        }),
        encoding="utf-8",
    )

    assert updater.just_updated_status() is None


@pytest.mark.parametrize(
    ("stage", "diagnosis_stage", "target", "attempt", "installing", "pending"),
    [
        ("verifying", "switching", None, "current", True, False),
        ("committed", "switching", None, "current", True, False),
        ("committed", "committed", None, "current", False, False),
        ("rolled_back", "rolled_back", None, "current", False, True),
        ("failed", "switching", None, "current", False, True),
        ("verifying", "accepted_failure", None, "current", False, True),
        ("verifying", "switching", "99.99.99", "current", False, True),
        ("verifying", "switching", None, "other", False, True),
        ("unknown", "switching", None, "current", False, True),
    ],
)
def test_pending_status_projects_current_installation(
    tmp_path: Path, monkeypatch, stage, diagnosis_stage, target, attempt, installing, pending,
) -> None:
    from app.core import layout

    staging = tmp_path / "data" / "update-staging"
    staging.mkdir(parents=True)
    manifest = staging / "manifest.json"
    manifest.write_text(json.dumps({"ready": True, "tag": f"v{updater.APP_VERSION}"}))
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(updater, "staging_dir", lambda: staging)
    monkeypatch.setattr("app.core.paths.install_root", lambda: tmp_path)
    monkeypatch.setattr("app.core.paths.logs_dir", lambda: logs)
    layout.start_transaction(tmp_path, target or updater.APP_VERSION, "previous", attempt_id="current")
    layout.update_transaction(tmp_path, stage)
    (logs / "update-diagnosis.json").write_text(json.dumps({
        "attemptId": attempt, "targetVersion": target or updater.APP_VERSION,
        "stage": diagnosis_stage, "success": diagnosis_stage == "committed", "acknowledged": True,
    }))

    status = updater.pending_status()
    assert status["installing"] is installing
    assert status["pending"] is pending
    assert status["justUpdated"] is None
    manifest.write_text(json.dumps({"ready": True, "tag": "v99.99.99"}))
    assert updater.pending_status()["pending"] is True


def test_pick_asset_ignores_patch_role() -> None:
    """差量链已退役：role=patch 资产不再被择优，一律取全量应用包。"""
    assets = [
        {"role": "app", "name": "Holdexar-win64-v0.3.6.zip", "size": 50000000, "sha256": "app-sha"},
        {"role": "patch", "name": "Holdexar-patch-v0.3.5-to-v0.3.6.zip", "size": 2500000, "sha256": "patch-sha", "fromVersion": "0.3.5"},
    ]

    picked = updater._pick_asset(assets)
    assert picked is not None
    assert picked["role"] == "app"
    assert picked["name"] == "Holdexar-win64-v0.3.6.zip"

    explicit = updater._pick_asset(assets, name="Holdexar-win64-v0.3.6.zip")
    assert explicit is not None
    assert explicit["name"] == "Holdexar-win64-v0.3.6.zip"


def test_payload_from_manifest_has_no_patch_fields(monkeypatch) -> None:
    """清单解析不再产出差量字段（isPatch/patchFromVersion/fullSizeBytes 已随差量链退役）。"""
    manifest_data = {
        "version": "0.3.6",
        "tag": "v0.3.6",
        "notes": "Feature notes",
        "assets": [
            {"role": "app", "name": "Holdexar-win64-v0.3.6.zip", "size": 50000000, "sha256": "app-sha"},
        ],
    }

    class DummySettings:
        version = "0.3.5"

    monkeypatch.setattr(updater, "get_settings", lambda: DummySettings())

    payload = updater._payload_from_manifest(manifest_data)
    assert payload["available"] is True
    assert payload["sha256"] == "app-sha"
    assert "isPatch" not in payload
    assert "patchFromVersion" not in payload
    assert "fullSizeBytes" not in payload







@pytest.mark.parametrize("diagnosis", ["switching", "missing", "unrelated"])
def test_committed_transaction_closes_display_when_diagnosis_write_failed(tmp_path, monkeypatch, diagnosis):
    from app.core import layout, update_host

    monkeypatch.setattr("app.core.paths.install_root", lambda: tmp_path)
    monkeypatch.setattr("app.core.paths.logs_dir", lambda: tmp_path / "data/logs")
    staging = tmp_path / "update-staging"
    staging.mkdir()
    (staging / "manifest.json").write_text(json.dumps({"ready": True, "tag": f"v{updater.APP_VERSION}"}))
    monkeypatch.setattr(updater, "staging_dir", lambda: staging)
    layout.start_transaction(tmp_path, updater.APP_VERSION, "old", attempt_id="current")
    layout.update_transaction(tmp_path, layout.STAGE_COMMITTED, currentVersion=updater.APP_VERSION, dataAccepted=True)
    if diagnosis != "missing":
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "update-diagnosis.json").write_text(json.dumps({
            "attemptId": "other" if diagnosis == "unrelated" else "current",
            "targetVersion": updater.APP_VERSION, "stage": "switching", "acknowledged": True,
        }))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    status = updater.pending_status()
    assert status["pending"] is False and status["installing"] is False
    assert status["justUpdated"]["currentVersion"] == updater.APP_VERSION
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.json")} == before
    (staging / "manifest.json").write_text(json.dumps({"ready": True, "tag": "v99.99.99"}))
    assert updater.pending_status()["pending"] is True
    writer = update_host._write_diagnosis

    def fail(*args, **kwargs):
        raise OSError("diagnosis storage unavailable")

    monkeypatch.setattr(update_host, "_write_diagnosis", fail)
    with pytest.raises(OSError, match="storage unavailable"):
        updater.acknowledge_update()
    assert updater.pending_status()["justUpdated"] is not None
    monkeypatch.setattr(update_host, "_write_diagnosis", writer)
    assert updater.acknowledge_update()["acknowledged"] is True
    assert updater.pending_status()["justUpdated"] is None
    assert updater.acknowledge_update()["acknowledged"] is True


def test_unaccepted_commit_cannot_report_success_without_diagnosis(tmp_path, monkeypatch):
    from app.core import layout

    monkeypatch.setattr("app.core.paths.install_root", lambda: tmp_path)
    monkeypatch.setattr("app.core.paths.logs_dir", lambda: tmp_path / "logs")
    layout.start_transaction(tmp_path, updater.APP_VERSION, "old", attempt_id="unaccepted")
    layout.update_transaction(tmp_path, layout.STAGE_COMMITTED, currentVersion=updater.APP_VERSION, dataPrepared=True)
    assert updater.just_updated_status() is None


def test_committed_without_previous_database_is_complete(tmp_path, monkeypatch):
    from app.core import layout

    monkeypatch.setattr("app.core.paths.install_root", lambda: tmp_path)
    monkeypatch.setattr("app.core.paths.logs_dir", lambda: tmp_path / "logs")
    layout.start_transaction(tmp_path, updater.APP_VERSION, "old", attempt_id="empty-data")
    layout.update_transaction(tmp_path, layout.STAGE_COMMITTED, currentVersion=updater.APP_VERSION,
                              dataHadDatabase=False, dataPrepared=False)
    assert updater.just_updated_status()["currentVersion"] == updater.APP_VERSION
