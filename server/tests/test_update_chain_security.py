"""更新/换装链安全不变量测试（PKG-1/PKG-3/PKG-4 根因修复回归）。

四条不变量：
① 显式 HOLDEXAR_UPDATE_URL 只认回环主机（免验签通道不出本机）；
② 下载完成将经过 Ed25519 验签的发布清单嵌入暂存记录；
③ 交接/换装执行任何暂存载荷前必须复验签名并逐文件核对（fail-closed，
   无记录/篡改/版本不符/清单不一致一律拒绝）；
④ 换装方向硬闸：目标不新于来源即拒绝（降级/平级不得过装）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import threading
import zipfile
import base64
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import layout, update_host, update_signing, updater  # noqa: E402
from app.core.app_info import APP_NAME, GITHUB_REPO, MANIFEST_SCHEMA  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
EXE_NAME = f"{APP_NAME}.exe"
_TEST_SIGNING_KEY = Ed25519PrivateKey.from_private_bytes(bytes(32))
_TEST_KEY_ID = "pentest-fixture"
_TEST_PUBLIC_KEY = _TEST_SIGNING_KEY.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw
).hex()
update_signing.TRUSTED_PUBKEYS[_TEST_KEY_ID] = _TEST_PUBLIC_KEY


def _mock_client(handler):
    real_client = updater.httpx.AsyncClient

    def factory(**kw):
        return real_client(transport=httpx.MockTransport(handler), **kw)

    return factory


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sign_manifest(manifest: dict) -> dict:
    signature = _TEST_SIGNING_KEY.sign(update_signing.canonical_manifest_bytes(manifest))
    return {
        **manifest,
        update_signing.SIGNATURE_FIELD: {
            "alg": update_signing.SIGNATURE_ALG,
            "keyId": _TEST_KEY_ID,
            "sig": base64.b64encode(signature).decode("ascii"),
        },
    }


def _payload_files(payload: Path) -> dict[str, str]:
    return {
        path.relative_to(payload).as_posix(): _sha256_of(path)
        for path in sorted(payload.rglob("*")) if path.is_file()
    }


def _signed_archive_manifest(archive: Path, tag: str) -> dict:
    files = {}
    with zipfile.ZipFile(archive) as zf:
        for item in zf.infolist():
            if item.is_dir():
                continue
            name = item.filename.removeprefix(f"{APP_NAME}/")
            files[name] = hashlib.sha256(zf.read(item)).hexdigest()
    return _sign_manifest({
        "schema": MANIFEST_SCHEMA,
        "app": APP_NAME,
        "version": tag.lstrip("vV"),
        "tag": tag,
        "notes": "test release",
        "assets": [{
            "role": "app",
            "name": f"{APP_NAME}-win64-{tag}.zip",
            "size": archive.stat().st_size,
            "sha256": _sha256_of(archive),
            "files": files,
        }],
    })


async def _return_value(value):
    return value


def _make_staged_payload(staging: Path, *, body: bytes = b"new-payload") -> Path:
    """合成暂存载荷：staging/<APP_NAME>/（主程序 + _internal + 更新宿主）。"""
    payload = staging / APP_NAME
    (payload / "_internal").mkdir(parents=True)
    (payload / EXE_NAME).write_bytes(body)
    (payload / "_internal" / "a.txt").write_text("data", encoding="utf-8")
    host = payload / "updater" / "UpdateHost.exe"
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_bytes(b"update-host")
    return payload


def _seal_staging(staging: Path, data_dir: Path | None, tag: str = "v0.3.7", with_record: bool = True) -> None:
    """Build a staging tree whose payload hashes are covered by a test signature."""
    payload = staging / APP_NAME
    staging.mkdir(parents=True, exist_ok=True)
    archive = staging / "update.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in sorted(payload.rglob("*")):
            if path.is_file():
                zf.write(path, f"{APP_NAME}/{path.relative_to(payload).as_posix()}")
    release = _sign_manifest({
        "schema": MANIFEST_SCHEMA,
        "app": APP_NAME,
        "version": tag.lstrip("vV"),
        "tag": tag,
        "assets": [{
            "role": "app",
            "name": f"{APP_NAME}-win64-{tag}.zip",
            "size": archive.stat().st_size,
            "sha256": _sha256_of(archive),
            "files": _payload_files(payload),
        }],
    })
    (staging / "manifest.json").write_text(json.dumps({
        "tag": tag,
        "ready": True,
        "attempt_id": "att-sec",
        "files": _payload_files(payload),
        "release_manifest": release if with_record else {},
    }), encoding="utf-8")


def _seed_installation(root: Path, version: str = "0.3.6") -> None:
    vdir = layout.get_version_dir(root, version)
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / EXE_NAME).write_bytes(b"old-binary")
    layout.write_current_state(root, version)


# ── ① env 更新源收容（PKG-3：免验签通道不出本机）────────────────────────


@pytest.mark.parametrize(
    "base",
    ["http://127.0.0.1:8888", "http://localhost:8888", "http://[::1]:8888"],
)
def test_env_update_url_loopback_is_honored(monkeypatch, base: str) -> None:
    monkeypatch.setenv("HOLDEXAR_UPDATE_URL", base)
    assert updater._env_update_base() == base
    assert updater.manifest_url() == f"{base}/latest.json"


def test_env_update_url_non_loopback_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv("HOLDEXAR_UPDATE_URL", "http://evil.example.com:9999")
    assert updater._env_update_base() == ""
    assert "github.com" in updater.manifest_url()


@pytest.mark.asyncio
async def test_env_update_url_non_loopback_download_uses_mirror_chain(monkeypatch) -> None:
    """非回环覆盖不进资产地址：下载仍走 GitHub 链，env 主机收不到任何请求。"""
    monkeypatch.setenv("HOLDEXAR_UPDATE_URL", "http://evil.example.com:9999")
    url = await updater._resolve_asset_url("v0.3.7", "x.zip")
    assert url == f"https://github.com/{GITHUB_REPO}/releases/download/v0.3.7/x.zip"


@pytest.mark.asyncio
async def test_fetch_manifest_ignores_remote_env_override(monkeypatch) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404)

    monkeypatch.setenv("HOLDEXAR_UPDATE_URL", "http://evil.example.com:9999")
    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    assert await updater._fetch_manifest() is None
    assert seen, "镜像链应照常发起"
    assert all("evil.example.com" not in u for u in seen)


@pytest.mark.asyncio
async def test_loopback_update_manifest_must_also_be_signed(monkeypatch) -> None:
    """回环调试源仍是运输通道，不能绕过版本清单签名。"""
    monkeypatch.setenv("HOLDEXAR_UPDATE_URL", "http://127.0.0.1:8888")
    payload = {"version": "0.3.7", "tag": "v0.3.7", "assets": []}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    assert await updater._fetch_manifest() is None


def test_staged_trust_rejects_attacker_forged_json_record(tmp_path) -> None:
    """匹配的普通 JSON 记录不能替代发布者签名。"""
    staging = tmp_path / "staging"
    data_dir = tmp_path / "data"
    _make_staged_payload(staging)
    _seal_staging(staging, data_dir, with_record=False)
    files = _payload_files(staging / APP_NAME)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "update-trust.json").write_text(json.dumps({
        "version": "v0.3.7", "zip_sha256": "a" * 64, "files": files,
    }), encoding="utf-8")
    error = update_host.verify_staged_trust(staging, data_dir, "0.3.7")
    assert error
    assert "签名" in error or "发布清单" in error


# ── ② 下载完成写信任锚（PKG-1 前半：Ed25519 结论持久化）──────────────────


@pytest.mark.asyncio
async def test_download_persists_signed_manifest_for_handoff(tmp_path, monkeypatch) -> None:
    payload = tmp_path / "release.zip"
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr(f"{APP_NAME}/{EXE_NAME}", "MZ-fake")
        zf.writestr(f"{APP_NAME}/web/index.html", "<html/>")
    sha = hashlib.sha256(payload.read_bytes()).hexdigest()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload.read_bytes())

    staging = tmp_path / "staging"
    data_dir = tmp_path / "data"
    release = _signed_archive_manifest(payload, "v0.3.7")
    monkeypatch.setattr(updater.httpx, "AsyncClient", _mock_client(handler))
    monkeypatch.setattr(updater, "_fetch_manifest", lambda: _return_value(release))
    monkeypatch.setattr(updater, "staging_dir", lambda: staging)
    monkeypatch.setattr("app.core.paths.resolve_data_dir", lambda: data_dir)
    monkeypatch.setattr(updater, "_MIN_PACKAGE_BYTES", 0)
    monkeypatch.setattr(
        "app.domains.proxies.clash_manager._download_attempts",
        lambda *_a, **_k: [(None, "直连")],
    )

    asset = release["assets"][0]
    result = await updater.download_update(
        "v0.3.7", expected_sha256=sha, asset_name=asset["name"], expected_size=asset["size"]
    )
    assert result["ok"] is True

    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["release_manifest"] == release
    assert manifest["release_manifest"]["assets"][0]["files"][EXE_NAME] == hashlib.sha256(b"MZ-fake").hexdigest()
    assert update_host.verify_staged_trust(staging, data_dir, "0.3.7") == ""
    assert not (data_dir / "update-trust.json").exists()

    with pytest.raises(RuntimeError, match="签名清单"):
        await updater.download_update("v0.3.8", expected_sha256=sha, asset_name=asset["name"])


# ── ③ 交接/换装信任复核（PKG-1 后半：执行前唯一锚）───────────────────────


def test_verify_staged_trust_passes_consistent_staging(tmp_path) -> None:
    staging = tmp_path / "staging"
    data_dir = tmp_path / "data"
    _make_staged_payload(staging)
    _seal_staging(staging, data_dir)
    assert update_host.verify_staged_trust(staging, data_dir, "0.3.7") == ""


def test_verify_staged_trust_missing_record_fails_closed(tmp_path) -> None:
    staging = tmp_path / "staging"
    data_dir = tmp_path / "data"
    _make_staged_payload(staging)
    _seal_staging(staging, data_dir, with_record=False)
    error = update_host.verify_staged_trust(staging, data_dir, "0.3.7")
    assert error


def test_verify_staged_trust_rejects_tampered_payload(tmp_path) -> None:
    staging = tmp_path / "staging"
    data_dir = tmp_path / "data"
    _make_staged_payload(staging)
    _seal_staging(staging, data_dir)
    (staging / APP_NAME / "_internal" / "a.txt").write_text("evil", encoding="utf-8")
    assert "校验不符" in update_host.verify_staged_trust(staging, data_dir, "0.3.7")


def test_verify_staged_trust_rejects_unlisted_payload_file(tmp_path) -> None:
    staging = tmp_path / "staging"
    data_dir = tmp_path / "data"
    _make_staged_payload(staging)
    _seal_staging(staging, data_dir)
    (staging / APP_NAME / "unexpected.dll").write_bytes(b"untrusted")
    assert "发布清单之外" in update_host.verify_staged_trust(staging, data_dir, "0.3.7")


def test_verify_staged_trust_rejects_version_mismatch(tmp_path) -> None:
    staging = tmp_path / "staging"
    data_dir = tmp_path / "data"
    _make_staged_payload(staging)
    _seal_staging(staging, data_dir, tag="v0.3.7")
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    manifest["tag"] = "v0.3.8"
    (staging / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert "版本不符" in update_host.verify_staged_trust(staging, data_dir, "0.3.8")


def test_verify_staged_trust_rejects_manifest_files_mismatch(tmp_path) -> None:
    staging = tmp_path / "staging"
    data_dir = tmp_path / "data"
    _make_staged_payload(staging)
    _seal_staging(staging, data_dir)
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    manifest["release_manifest"]["assets"][0]["files"]["different.exe"] = "0" * 64
    (staging / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert "签名" in update_host.verify_staged_trust(staging, data_dir, "0.3.7")


def test_verify_staged_trust_rejects_missing_payload(tmp_path) -> None:
    staging = tmp_path / "staging"
    data_dir = tmp_path / "data"
    _make_staged_payload(staging)
    _seal_staging(staging, data_dir)
    shutil.rmtree(staging / APP_NAME)  # 清单与记录在场，载荷目录被清
    assert "载荷缺失" in update_host.verify_staged_trust(staging, data_dir, "0.3.7")


# ── ③b 换装宿主入口复核（任何调用方执行 update_host 都过同一道闸）─────────


def test_execute_update_rejects_staging_without_trust_record(tmp_path) -> None:
    root = tmp_path / "app"
    _seed_installation(root, "0.3.6")
    staging = tmp_path / "staging"
    _make_staged_payload(staging)
    _seal_staging(staging, None, with_record=False)

    ok = update_host.execute_update(
        root=root, staging_dir=staging, target_version="0.3.7",
        source_version="0.3.6", skip_health_check=True, launch_on_finish=False,
    )
    assert ok is False
    assert layout.read_current_state(root)["version"] == "0.3.6"
    assert not layout.get_version_dir(root, "0.3.7").exists()


def test_execute_update_rejects_tampered_staging_after_seal(tmp_path) -> None:
    root = tmp_path / "app"
    _seed_installation(root, "0.3.6")
    staging = tmp_path / "staging"
    data_dir = root / "data"
    _make_staged_payload(staging)
    _seal_staging(staging, data_dir)
    (staging / APP_NAME / "_internal" / "a.txt").write_text("evil", encoding="utf-8")

    ok = update_host.execute_update(
        root=root, staging_dir=staging, target_version="0.3.7",
        source_version="0.3.6", skip_health_check=True, launch_on_finish=False,
        data_dir=data_dir,
    )
    assert ok is False
    assert layout.read_current_state(root)["version"] == "0.3.6"
    assert not layout.get_version_dir(root, "0.3.7").exists()


def test_execute_update_succeeds_with_consistent_trust_record(tmp_path) -> None:
    root = tmp_path / "app"
    _seed_installation(root, "0.3.6")
    staging = tmp_path / "staging"
    data_dir = root / "data"
    _make_staged_payload(staging)
    _seal_staging(staging, data_dir)

    ok = update_host.execute_update(
        root=root, staging_dir=staging, target_version="0.3.7",
        source_version="0.3.6", skip_health_check=True, launch_on_finish=False,
        data_dir=data_dir,
    )
    assert ok is True
    assert layout.read_current_state(root)["version"] == "0.3.7"


# ── ④ 换装方向硬闸（PKG-4：降级/平级不得过装）───────────────────────────


def test_execute_update_rejects_downgrade(tmp_path) -> None:
    root = tmp_path / "app"
    _seed_installation(root, "0.3.6")
    staging = tmp_path / "staging"
    data_dir = root / "data"
    _make_staged_payload(staging, body=b"older-payload")
    _seal_staging(staging, data_dir, tag="v0.3.5")

    ok = update_host.execute_update(
        root=root, staging_dir=staging, target_version="0.3.5",
        source_version="0.3.6", skip_health_check=True, launch_on_finish=False,
        data_dir=data_dir,
    )
    assert ok is False
    assert layout.read_current_state(root)["version"] == "0.3.6"
    assert not layout.get_version_dir(root, "0.3.5").exists()
    assert layout.read_transaction(root) is None


def test_execute_update_rejects_same_version_swap(tmp_path) -> None:
    root = tmp_path / "app"
    _seed_installation(root, "0.3.6")
    staging = tmp_path / "staging"
    data_dir = root / "data"
    _make_staged_payload(staging)
    _seal_staging(staging, data_dir, tag="v0.3.6")

    ok = update_host.execute_update(
        root=root, staging_dir=staging, target_version="0.3.6",
        source_version="0.3.6", skip_health_check=True, launch_on_finish=False,
        data_dir=data_dir,
    )
    assert ok is False
    assert layout.read_current_state(root)["version"] == "0.3.6"


# ── 桌面壳交接闸与看门狗（先复核后执行；探测固定回环）─────────────────────


def _load_desktop_main():
    spec = importlib.util.spec_from_file_location(
        "holdexar_desktop_main_sec", ROOT / "desktop" / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


desktop = _load_desktop_main()


def _desktop_env(tmp_path: Path, monkeypatch, data_dir: Path, spawns: list) -> None:
    install = tmp_path / "install"
    install.mkdir(exist_ok=True)
    (install / EXE_NAME).write_bytes(b"old-exe")
    monkeypatch.setattr(desktop, "is_frozen", lambda: True)
    monkeypatch.setattr(desktop, "_data_dir", lambda: data_dir)
    monkeypatch.setattr(desktop, "_app_root", lambda: install)
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda cmd, **kw: spawns.append(cmd))
    monkeypatch.setattr("app.core.update_host.supports_data_protocol", lambda _exe: True)


def test_desktop_handoff_clears_staging_without_trust_record(tmp_path, monkeypatch) -> None:
    """伪造三件套（manifest+载荷，无信任记录）→ 不探测不执行，清暂存放弃交接。"""
    data_dir = tmp_path / "data"
    staging = data_dir / "update-staging"
    _make_staged_payload(staging)
    _seal_staging(staging, None, with_record=False)
    spawns: list = []
    _desktop_env(tmp_path, monkeypatch, data_dir, spawns)

    assert desktop._handoff_pending_update() is False
    assert spawns == []
    assert not staging.exists()


def test_desktop_handoff_clears_tampered_staging(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    staging = data_dir / "update-staging"
    _make_staged_payload(staging)
    _seal_staging(staging, data_dir)
    (staging / APP_NAME / "_internal" / "a.txt").write_text("evil", encoding="utf-8")
    spawns: list = []
    _desktop_env(tmp_path, monkeypatch, data_dir, spawns)

    assert desktop._handoff_pending_update() is False
    assert spawns == []
    assert not staging.exists()


def test_desktop_handoff_proceeds_when_trust_record_consistent(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    staging = data_dir / "update-staging"
    _make_staged_payload(staging)
    _seal_staging(staging, data_dir)
    spawns: list = []
    _desktop_env(tmp_path, monkeypatch, data_dir, spawns)
    monkeypatch.setattr(desktop.os, "getpid", lambda: 4242)

    assert desktop._handoff_pending_update() is True
    assert len(spawns) == 1
    assert spawns[0][0] == str(staging / APP_NAME / "updater" / "UpdateHost.exe")
    assert "--staging" in spawns[0] and str(staging) in spawns[0]


@pytest.mark.parametrize(
    ("server_host", "expected"),
    [
        ("0.0.0.0", "127.0.0.1"),
        ("", "127.0.0.1"),
        ("localhost", "127.0.0.1"),
        ("::1", "127.0.0.1"),
        ("127.0.0.1", "127.0.0.1"),
        ("192.168.5.10", "192.168.5.10"),
    ],
)
def test_desktop_probe_host_forces_loopback_for_wildcard(monkeypatch, server_host, expected) -> None:
    monkeypatch.setattr(desktop, "SERVER_HOST", server_host)
    assert desktop._probe_host() == expected


def test_wait_health_probes_loopback_when_host_wildcard(monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(desktop, "SERVER_HOST", "0.0.0.0")
    monkeypatch.setattr(
        desktop, "_http_get_json",
        lambda host, port, path, **kw: seen.append((host, port, path)) or (0, None),
    )
    assert desktop._wait_health(0.25) is False
    assert seen and {h for h, _, _ in seen} == {"127.0.0.1"}


def test_start_server_disables_proxy_headers(monkeypatch) -> None:
    """桌面形态无可信反代：uvicorn 必须关掉 XFF 解析面。"""
    import uvicorn

    calls: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.update(kw))
    ready = threading.Event()
    desktop._start_server(ready, [])
    assert ready.is_set()
    assert calls.get("proxy_headers") is False
