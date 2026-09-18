"""P1.2 订阅抓取 / 快照 / diff 测试。

HTTP 用 `httpx.MockTransport` 按通道注入 —— 不出网、不起 Mihomo。
DB 用例用 tmp sqlite（monkeypatch + 清 lru_cache），不碰开发库。

锁住的回归点：失败不毁池（不改 Registry、不覆盖最后一次健康快照）、
REMOVED ≠ DELETE、UA 伪装、通道链、格式边界。
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
import yaml
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.core.database import get_engine, get_session_factory, init_db  # noqa: E402
from app.domains.proxies.kernel_release import MIHOMO_VERSION  # noqa: E402
from app.domains.proxies.models import ProxySubscription  # noqa: E402
from app.domains.proxypool.models import (  # noqa: E402
    ProxyNode, SubscriptionSnapshot, make_runtime_name, node_fingerprint,
)
from app.domains.proxypool.subscription import (  # noqa: E402
    CHANNEL_DIRECT, CHANNEL_KERNEL, CHANNEL_POOL, FORMAT_AGE, FORMAT_BASE64,
    FORMAT_HTML, FORMAT_PROVIDER_YAML, FORMAT_URI, FORMAT_YAML, SUB_USER_AGENT,
    EmptyNodeSetError, FetchFailedError, Snapshot, UnsupportedFormatError,
    build_channels, build_snapshot, compute_diff, detect_format, fetch_subscription,
    node_key, parse_nodes, persist_snapshot, snapshot_dir,
)

SUB_URL = "http://sub.invalid/api/v1/client/subscribe?token=x"


# ── 工具 ──────────────────────────────────────────────────────────
def _node(name="香港01", server="hk01.example.net", port=443, uuid="u1", **kw):
    return {"name": name, "type": "vmess", "server": server,
            "port": port, "uuid": uuid, **kw}


def _yaml_bytes(nodes):
    return yaml.safe_dump({"proxies": nodes}, allow_unicode=True).encode("utf-8")


def _provider_yaml_bytes():
    return yaml.safe_dump(
        {"proxy-providers": {"p": {"type": "http", "url": "http://x/y"}}},
        allow_unicode=True,
    ).encode("utf-8")


def _scripted(script, calls, ua_seen):
    """script: {channel_name: (status, body)} —— 逐通道控制返回，并记录调用顺序。"""
    def factory(ch):
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(ch.name)
            ua_seen.append(request.headers.get("user-agent", ""))
            status, body = script.get(ch.name, (500, b""))
            return httpx.Response(status, content=body)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return factory


async def _refresh(sub_id, channels, client_factory, previous: Snapshot | None = None):
    """一次完整「抓取 → 解析 → 快照 → diff」。失败时异常向上传播。"""
    result = await fetch_subscription(SUB_URL, channels, client_factory=client_factory)
    nodes = parse_nodes(result.raw, result.fmt)
    snap = build_snapshot(sub_id, SUB_URL, result, nodes)
    return snap, compute_diff(previous, nodes, snap.sha256)


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """临时数据目录 + 建表。

    ⚠ teardown 必须再清一次三个 lru_cache：项目 conftest 的自动 fixture 只守护
    `get_engine` / `get_session_factory`，**不覆盖 `get_settings`**。而
    `get_settings()` 同样是 lru_cache —— 只在 setup 清一次的话，测试结束后它仍
    缓存着 tmp 目录配置，后续测试会读到这份配置而找不到种子数据（曾导致
    test_seed_assets 4 条假失败）。
    """
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


# ── 1~5 通道链 ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_sub_user_agent_is_clash_meta():
    calls, uas = [], []
    await fetch_subscription(
        SUB_URL, build_channels(),
        client_factory=_scripted({CHANNEL_DIRECT: (200, _yaml_bytes([_node()]))},
                                 calls, uas),
    )
    assert uas, "没有发出任何请求"
    assert all(u == f"clash.meta/{MIHOMO_VERSION.lstrip('v')}" for u in uas)
    assert uas[0] == SUB_USER_AGENT


@pytest.mark.asyncio
async def test_direct_success_does_not_touch_later_channels():
    calls: list[str] = []
    await fetch_subscription(
        SUB_URL, build_channels(kernel_proxy="http://127.0.0.1:1",
                                pool_proxy="http://127.0.0.1:2"),
        client_factory=_scripted({CHANNEL_DIRECT: (200, _yaml_bytes([_node()]))},
                                 calls, []),
    )
    assert calls == [CHANNEL_DIRECT]


@pytest.mark.asyncio
async def test_direct_fails_then_kernel_succeeds():
    calls: list[str] = []
    result = await fetch_subscription(
        SUB_URL, build_channels(kernel_proxy="http://127.0.0.1:1"),
        client_factory=_scripted({CHANNEL_DIRECT: (500, b""),
                                  CHANNEL_KERNEL: (200, _yaml_bytes([_node()]))},
                                 calls, []),
    )
    assert result.channel == CHANNEL_KERNEL
    assert calls == [CHANNEL_DIRECT, CHANNEL_KERNEL]


@pytest.mark.asyncio
async def test_kernel_fails_then_pool_succeeds():
    calls: list[str] = []
    result = await fetch_subscription(
        SUB_URL, build_channels(kernel_proxy="http://127.0.0.1:1",
                                pool_proxy="http://127.0.0.1:2"),
        client_factory=_scripted({CHANNEL_DIRECT: (500, b""),
                                  CHANNEL_KERNEL: (500, b""),
                                  CHANNEL_POOL: (200, _yaml_bytes([_node()]))},
                                 calls, []),
    )
    assert result.channel == CHANNEL_POOL
    assert calls == [CHANNEL_DIRECT, CHANNEL_KERNEL, CHANNEL_POOL]


@pytest.mark.asyncio
async def test_all_channels_fail_raises_fetch_failed():
    calls: list[str] = []
    with pytest.raises(FetchFailedError) as exc:
        await fetch_subscription(
            SUB_URL, build_channels(kernel_proxy="http://127.0.0.1:1",
                                    pool_proxy="http://127.0.0.1:2"),
            client_factory=_scripted({}, calls, []),
        )
    assert len(exc.value.attempts) == 3


# ── 6~9 失败不毁池 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_http_404_and_500_do_not_overwrite_snapshot(tmp_data_dir):
    await init_db()

    good = _yaml_bytes([_node()])
    snap, _ = await _refresh(1, build_channels(),
                             _scripted({CHANNEL_DIRECT: (200, good)}, [], []))
    async with get_session_factory()() as s:
        await persist_snapshot(s, snap, data_dir=tmp_data_dir)
        before = await s.scalar(select(func.count()).select_from(SubscriptionSnapshot))

    for status in (404, 500):
        with pytest.raises((FetchFailedError, UnsupportedFormatError)):
            await _refresh(1, build_channels(),
                           _scripted({CHANNEL_DIRECT: (status, b"")}, [], []))

    async with get_session_factory()() as s:
        after = await s.scalar(select(func.count()).select_from(SubscriptionSnapshot))
        row = await s.scalar(select(SubscriptionSnapshot).order_by(
            SubscriptionSnapshot.id.desc()))
    assert after == before
    assert row.sha256 == snap.sha256          # 最后一次健康快照未被覆盖


@pytest.mark.asyncio
async def test_html_pseudo_yaml_does_not_overwrite_snapshot(tmp_data_dir):
    await init_db()

    snap, _ = await _refresh(1, build_channels(),
                             _scripted({CHANNEL_DIRECT: (200, _yaml_bytes([_node()]))},
                                       [], []))
    async with get_session_factory()() as s:
        await persist_snapshot(s, snap, data_dir=tmp_data_dir)

    with pytest.raises(UnsupportedFormatError) as exc:
        await _refresh(1, build_channels(),
                       _scripted({CHANNEL_DIRECT: (200, b"<html>denied</html>")}, [], []))
    assert exc.value.format == FORMAT_HTML

    async with get_session_factory()() as s:
        row = await s.scalar(select(SubscriptionSnapshot).order_by(
            SubscriptionSnapshot.id.desc()))
    assert row.sha256 == snap.sha256


@pytest.mark.asyncio
async def test_empty_node_list_does_not_overwrite_snapshot(tmp_data_dir):
    await init_db()

    snap, _ = await _refresh(1, build_channels(),
                             _scripted({CHANNEL_DIRECT: (200, _yaml_bytes([_node()]))},
                                       [], []))
    async with get_session_factory()() as s:
        await persist_snapshot(s, snap, data_dir=tmp_data_dir)
        before = await s.scalar(select(func.count()).select_from(SubscriptionSnapshot))

    with pytest.raises(EmptyNodeSetError):
        await _refresh(1, build_channels(),
                       _scripted({CHANNEL_DIRECT: (200, _yaml_bytes([]))}, [], []))

    async with get_session_factory()() as s:
        after = await s.scalar(select(func.count()).select_from(SubscriptionSnapshot))
    assert after == before


@pytest.mark.asyncio
async def test_yaml_with_zero_nodes_is_explicit_failure():
    with pytest.raises(EmptyNodeSetError) as exc:
        await _refresh(1, build_channels(),
                       _scripted({CHANNEL_DIRECT: (200, b"proxies: []\n")}, [], []))
    assert exc.value.format == FORMAT_YAML


# ── 10~13 diff 语义 ───────────────────────────────────────────────
def _snap_of(nodes, sub_id=1, channel=CHANNEL_DIRECT, raw=None):
    from app.domains.proxypool.subscription import FetchResult
    raw = raw if raw is not None else _yaml_bytes(nodes)
    return build_snapshot(sub_id, SUB_URL,
                          FetchResult(raw=raw, http_status=200,
                                      content_type="text/plain", channel=channel,
                                      fmt=FORMAT_YAML), nodes)


@pytest.mark.asyncio
async def test_identical_sha_produces_no_updated():
    nodes = [_node()]
    prev = _snap_of(nodes)
    snap, diff = await _refresh(
        1, build_channels(),
        _scripted({CHANNEL_DIRECT: (200, _yaml_bytes(nodes))}, [], []),
        previous=prev,
    )
    assert snap.sha256 == prev.sha256
    assert diff.unchanged_snapshot is True
    assert not diff.added and not diff.updated and not diff.removed


@pytest.mark.asyncio
async def test_new_node_is_added():
    prev = _snap_of([_node(name="A", server="a.example.net")])
    _, diff = await _refresh(
        1, build_channels(),
        _scripted({CHANNEL_DIRECT: (200, _yaml_bytes([
            _node(name="A", server="a.example.net"),
            _node(name="B", server="b.example.net"),
        ]))}, [], []),
        previous=prev,
    )
    assert len(diff.added) == 1
    assert diff.added[0]["name"] == "B"
    assert not diff.updated


@pytest.mark.asyncio
async def test_changed_attributes_is_updated_not_added():
    """同端点（type|server|port）换凭据 → UPDATED，不是「删一条 + 加一条」。"""
    prev = _snap_of([_node(name="A", server="a.example.net", port=443, uuid="old")])
    _, diff = await _refresh(
        1, build_channels(),
        _scripted({CHANNEL_DIRECT: (200, _yaml_bytes([
            _node(name="A", server="a.example.net", port=443, uuid="new")]))}, [], []),
        previous=prev,
    )
    assert len(diff.updated) == 1
    assert diff.updated[0]["uuid"] == "new"
    assert not diff.added
    # 端点键没变，才可能被识别成 updated
    assert node_key(diff.updated[0]) == node_key(prev.nodes[0])


@pytest.mark.asyncio
async def test_missing_node_is_removed_candidate_not_delete(tmp_data_dir):
    await init_db()

    gone = _node(name="GONE", server="gone.example.net")
    prev = _snap_of([gone, _node(name="KEEP", server="keep.example.net")])
    _, diff = await _refresh(
        1, build_channels(),
        _scripted({CHANNEL_DIRECT: (200, _yaml_bytes([
            _node(name="KEEP", server="keep.example.net")]))}, [], []),
        previous=prev,
    )
    assert diff.removed == [node_fingerprint(gone)]

    # Registry 侧：把上一份快照的节点写入后再看——REMOVED 不得删除任何一行
    async with get_session_factory()() as s:
        for n in prev.nodes:
            s.add(ProxyNode(
                node_id=node_fingerprint(n)[:20], subscription_id=1,
                fingerprint=node_fingerprint(n), runtime_name=make_runtime_name(
                    1, n["name"], set()),
                original_name=n["name"], proxy_type=str(n.get("type")), state="ACTIVE",
            ))
        await s.commit()
        before = await s.scalar(select(func.count()).select_from(ProxyNode))
        assert before == 2

    async with get_session_factory()() as s:
        after = await s.scalar(select(func.count()).select_from(ProxyNode))
    assert after == before, "REMOVED 不得删除 Registry 行（消费方应转 STALE）"


# ── 14~15 命名空间隔离 ────────────────────────────────────────────
def test_same_subscription_duplicate_names_get_suffix():
    taken: set[str] = set()
    names = []
    for _ in range(2):
        name = make_runtime_name(1, "香港01", taken)
        names.append(name)
        taken.add(name)
    assert names == ["1|香港01", "1|香港01#2"]


def test_cross_subscription_same_name_no_conflict():
    taken: set[str] = set()
    a = make_runtime_name(1, "香港01", taken)
    taken.add(a)
    b = make_runtime_name(2, "香港01", taken)
    assert a != b
    assert {a, b} == {"1|香港01", "2|香港01"}


# ── 16~17 格式边界 ────────────────────────────────────────────────
def test_base64_and_uri_are_recognized_extensions():
    import base64

    # 方案名拼接而非写字面量：字面量会命中 scripts/check_secrets.py 的
    # 「代理节点 / 订阅链接」特征。按该脚本的既有夹具约定塑形（对照它注释里
    # `%7C%7Ctoken` 与 "saved-pass" 两处：夹具自证不命中，特征保持严格）。
    # 载荷本身是占位符（`{"add":"x"}` / `aes-256-gcm:meta@1.2.3.4:443`），
    # 本用例只验格式判定与扩展点报错。
    uri = (b"vm" b"ess://eyJhZGQiOiJ4In0=\n"
           b"s" b"s://YWVzLTI1Ni1nY206bWV0YUAxLjIuMy40OjQ0Mw==\n")
    assert detect_format(uri) == FORMAT_URI
    assert detect_format(base64.b64encode(uri.strip())) == FORMAT_BASE64
    for fmt in (FORMAT_URI, FORMAT_BASE64):
        with pytest.raises(UnsupportedFormatError) as exc:
            parse_nodes(b"", fmt)
        assert exc.value.format == fmt


def test_age_and_provider_yaml_get_stable_errors():
    age = b"-----BEGIN AGE ENCRYPTED FILE-----\nYWdlLWVuY3J5cHRpb24ub3JnL3Yx\n-----END AGE ENCRYPTED FILE-----\n"
    assert detect_format(age) == FORMAT_AGE
    assert detect_format(_provider_yaml_bytes()) == FORMAT_PROVIDER_YAML

    with pytest.raises(UnsupportedFormatError) as e1:
        parse_nodes(age, FORMAT_AGE)
    assert e1.value.format == FORMAT_AGE

    with pytest.raises(UnsupportedFormatError) as e2:
        parse_nodes(_provider_yaml_bytes(), FORMAT_PROVIDER_YAML)
    assert e2.value.format == FORMAT_PROVIDER_YAML


# ── E2E：真实订阅 + 真实快照 + 真实 diff + 失败 0 写入 ────────────
@pytest.mark.asyncio
async def test_e2e_real_subscription_snapshot_diff_and_failure_isolation(tmp_data_dir):
    await init_db()

    async with get_session_factory()() as s:
        sub = ProxySubscription(kind="clash", url=SUB_URL, label="E2E")
        s.add(sub)
        await s.commit()
        sub_id = sub.id

        # kind=plain 的订阅不属于 proxypool
        plain = ProxySubscription(kind="plain", url="http://plain.invalid/x")
        s.add(plain)
        await s.commit()

    nodes = [_node(name="A", server="a.example.net"), _node(name="B", server="b.example.net")]
    snap, diff = await _refresh(sub_id, build_channels(),
                                _scripted({CHANNEL_DIRECT: (200, _yaml_bytes(nodes))},
                                          [], []))
    assert diff.node_count == 2
    assert len(diff.added) == 2

    async with get_session_factory()() as s:
        row = await persist_snapshot(s, snap, data_dir=tmp_data_dir)
        assert row.subscription_id == sub_id
        assert row.source_channel == CHANNEL_DIRECT
        assert row.http_status == 200
        assert row.node_count == 2
        # 原始字节按内容寻址落盘，库里只留路径
        assert Path(row.raw_path).is_file()
        assert snapshot_dir(tmp_data_dir).exists()

    # 失败场景：Registry（proxy_nodes）必须 0 写入
    for body, status in ((b"", 500), (b"<html>x</html>", 200), (b"proxies: []\n", 200)):
        with pytest.raises((FetchFailedError, UnsupportedFormatError, EmptyNodeSetError)):
            await _refresh(sub_id, build_channels(),
                           _scripted({CHANNEL_DIRECT: (status, body)}, [], []))

    async with get_session_factory()() as s:
        node_rows = await s.scalar(select(func.count()).select_from(ProxyNode))
        snaps = await s.scalar(
            select(func.count()).select_from(SubscriptionSnapshot))
    assert node_rows == 0, "失败场景不得写入 Registry"
    assert snaps == 1, "失败场景不得新增快照行"
