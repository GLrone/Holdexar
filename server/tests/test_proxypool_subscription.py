"""P1.2 / P1.2.1 订阅抓取 / 快照 / diff 测试。

HTTP 用 `httpx.MockTransport` 按通道注入 —— 不出网、不起 Mihomo。
DB 用例用 tmp sqlite（monkeypatch + 清 lru_cache），不碰开发库。

锁住的回归点：失败不毁池（不改 Registry、不覆盖最后一次健康快照）、
REMOVED ≠ DELETE、UPDATED ∩ REMOVED = ∅、快照不可变、事务归调用方、
非法 UTF-8 明确失败、上一份快照可恢复、UA 伪装、通道链、格式边界、
跨订阅节点身份（一节点多来源）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
import yaml
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.core.database import get_engine, get_session_factory, init_db  # noqa: E402
from app.domains.proxies.kernel_release import MIHOMO_VERSION  # noqa: E402
from app.domains.proxies.models import ProxySubscription  # noqa: E402
from app.domains.proxypool.models import (  # noqa: E402
    ProxyNode, ProxyNodeSource, SubscriptionSnapshot, make_runtime_name,
    node_fingerprint,
)
from app.domains.proxypool.subscription import (  # noqa: E402
    CHANNEL_DIRECT, CHANNEL_KERNEL, CHANNEL_POOL, FORMAT_AGE, FORMAT_BASE64,
    FORMAT_HTML, FORMAT_PROVIDER_YAML, FORMAT_URI, FORMAT_YAML, SUB_USER_AGENT,
    EmptyNodeSetError, FetchFailedError, InvalidEncodingError, Snapshot,
    SnapshotRecoveryError, UnsupportedFormatError,
    build_channels, build_snapshot, compute_diff, decode_utf8, detect_format,
    fetch_subscription, latest_snapshot, node_key, parse_nodes, persist_snapshot,
    snapshot_dir,
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
        SUB_URL, build_channels(kernel_proxy="http://127.0.0.1:1", local_proxies=[]),
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
                                    pool_proxy="http://127.0.0.1:2",
                                    local_proxies=[]),
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
        await s.commit()
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
        await s.commit()

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
        await s.commit()
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
    assert diff.updated[0].node["uuid"] == "new"
    assert not diff.added
    # 端点键没变，才可能被识别成 updated
    assert node_key(diff.updated[0].node) == node_key(prev.nodes[0])


@pytest.mark.asyncio
async def test_changed_attributes_is_updated_not_removed():
    """回归：被取代的旧指纹绝不能同时落进 REMOVED（UPDATED ∩ REMOVED = ∅）。

    旧实现按 `上一份指纹集 - 当前指纹集` 算 removed，端点没变、凭据变了的节点
    会同时出现在 UPDATED 与 REMOVED 里；P1.3 先更新再标 STALE，注册表当场就错。
    这里连同「真消失的节点」一起验，确保修 removed 没有把该报的消失吞掉。
    """
    gone = _node(name="GONE", server="gone.example.net")
    prev = _snap_of([
        _node(name="ROTATED", server="rot.example.net", port=443, uuid="old"),
        gone,
    ])
    nodes = [_node(name="ROTATED", server="rot.example.net", port=443, uuid="new")]
    _, diff = await _refresh(
        1, build_channels(),
        _scripted({CHANNEL_DIRECT: (200, _yaml_bytes(nodes))}, [], []),
        previous=prev,
    )

    superseded = node_fingerprint(prev.nodes[0])
    assert [u.previous_fingerprint for u in diff.updated] == [superseded]
    assert diff.updated[0].node["uuid"] == "new"
    # 被取代 ≠ 消失：只有真不见了的那个端点该进 REMOVED
    assert diff.removed == [node_fingerprint(gone)]
    assert superseded not in diff.removed
    assert set(u.previous_fingerprint for u in diff.updated).isdisjoint(diff.removed)
    assert set(diff.unchanged).isdisjoint(diff.removed)
    assert not diff.added


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
            fp = node_fingerprint(n)
            s.add(ProxyNode(
                node_id=fp[:20], fingerprint=fp,
                runtime_name=make_runtime_name(1, n["name"], set()),
                proxy_type=str(n.get("type")), state="ACTIVE",
            ))
            # 来源归属进独立表：节点身份与「谁在提供它」分开记
            s.add(ProxyNodeSource(node_id=fp[:20], subscription_id=1,
                                  original_name=n["name"]))
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
        # 事务归调用方：persist_snapshot 只 flush，这里由调用方提交
        await s.commit()

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


# ── P1.2.1 ①：非法 UTF-8 明确失败，绝不 replace 兜过去 ─────────────
_BAD_BYTES = b"proxies:\n  - {name: \xff\xfe, type: vmess}\n"


@pytest.mark.asyncio
async def test_invalid_utf8_is_channel_level_and_falls_through():
    """200 + 非法字节＝通道级问题：换下一通道，而不是拿替换字符硬解。"""
    calls: list[str] = []
    result = await fetch_subscription(
        SUB_URL, build_channels(kernel_proxy="http://127.0.0.1:1", local_proxies=[]),
        client_factory=_scripted({CHANNEL_DIRECT: (200, _BAD_BYTES),
                                  CHANNEL_KERNEL: (200, _yaml_bytes([_node()]))},
                                 calls, []),
    )
    assert calls == [CHANNEL_DIRECT, CHANNEL_KERNEL]
    assert result.channel == CHANNEL_KERNEL
    assert result.attempts[0].ok is False
    assert "UTF-8" in (result.attempts[0].error or "")


@pytest.mark.asyncio
async def test_all_channels_invalid_utf8_raises_invalid_encoding():
    calls: list[str] = []
    with pytest.raises(InvalidEncodingError) as exc:
        await fetch_subscription(
            SUB_URL, build_channels(kernel_proxy="http://127.0.0.1:1", local_proxies=[]),
            client_factory=_scripted({CHANNEL_DIRECT: (200, _BAD_BYTES),
                                      CHANNEL_KERNEL: (200, b"\xff\xfe\x00")},
                                     calls, []),
        )
    assert calls == [CHANNEL_DIRECT, CHANNEL_KERNEL]
    assert len(exc.value.attempts) == 2


@pytest.mark.asyncio
async def test_unsupported_fallback_takes_precedence_over_encoding_error():
    """异常优先级：拿到过「可解码但不支持的正文」时报格式错（更有指向性）。"""
    with pytest.raises(UnsupportedFormatError) as exc:
        await fetch_subscription(
            SUB_URL, build_channels(kernel_proxy="http://127.0.0.1:1",
                                    pool_proxy="http://127.0.0.1:2"),
            client_factory=_scripted({CHANNEL_DIRECT: (200, b"<html>x</html>"),
                                      CHANNEL_KERNEL: (200, _BAD_BYTES),
                                      CHANNEL_POOL: (500, b"")}, [], []),
        )
    assert exc.value.format == FORMAT_HTML


def test_decode_utf8_rejects_invalid_bytes():
    assert decode_utf8("香港".encode("utf-8")) == "香港"
    with pytest.raises(InvalidEncodingError):
        decode_utf8(b"\xff\xfe")
    with pytest.raises(InvalidEncodingError):
        parse_nodes(_BAD_BYTES, FORMAT_YAML)
    with pytest.raises(InvalidEncodingError):
        detect_format(_BAD_BYTES)


@pytest.mark.asyncio
async def test_invalid_utf8_does_not_overwrite_snapshot(tmp_data_dir):
    """非法字节 → 无新快照、无 Registry 变更、最后一次健康快照不被覆盖。"""
    await init_db()
    snap, _ = await _refresh(1, build_channels(),
                             _scripted({CHANNEL_DIRECT: (200, _yaml_bytes([_node()]))},
                                       [], []))
    async with get_session_factory()() as s:
        await persist_snapshot(s, snap, data_dir=tmp_data_dir)
        await s.commit()
        before = await s.scalar(select(func.count()).select_from(SubscriptionSnapshot))

    with pytest.raises(InvalidEncodingError):
        await _refresh(1, build_channels(),
                       _scripted({CHANNEL_DIRECT: (200, _BAD_BYTES)}, [], []))

    async with get_session_factory()() as s:
        after = await s.scalar(select(func.count()).select_from(SubscriptionSnapshot))
        row = await s.scalar(select(SubscriptionSnapshot).order_by(
            SubscriptionSnapshot.id.desc()))
        nodes = await s.scalar(select(func.count()).select_from(ProxyNode))
    assert after == before
    assert row.sha256 == snap.sha256
    assert nodes == 0


# ── P1.2.1 ②：事务归调用方（persist_snapshot 只 flush）─────────────
@pytest.mark.asyncio
async def test_persist_snapshot_does_not_commit(tmp_data_dir):
    """回滚必须能把 persist_snapshot 写的行丢掉——它自己 commit 就做不到。"""
    await init_db()
    snap, _ = await _refresh(1, build_channels(),
                             _scripted({CHANNEL_DIRECT: (200, _yaml_bytes([_node()]))},
                                       [], []))
    async with get_session_factory()() as s:
        await persist_snapshot(s, snap, data_dir=tmp_data_dir)
        await s.rollback()

    async with get_session_factory()() as s:
        left = await s.scalar(select(func.count()).select_from(SubscriptionSnapshot))
    assert left == 0
    # 原始字节是幂等落盘、不在事务里：回滚后留孤儿文件，由 P1.9 的 GC 回收。
    # 这比「事务提交了但字节没落盘」安全——后者会让 latest_snapshot 永远恢复不出基线
    blob = snapshot_dir(tmp_data_dir) / "1" / f"{snap.sha256}.bin"
    assert blob.is_file()


@pytest.mark.asyncio
async def test_persist_snapshot_does_not_commit_unrelated_pending_changes(tmp_data_dir):
    """会话里其它 pending 行不得被顺手提交（提交一个自己不知道的写集）。"""
    await init_db()
    snap, _ = await _refresh(1, build_channels(),
                             _scripted({CHANNEL_DIRECT: (200, _yaml_bytes([_node()]))},
                                       [], []))
    async with get_session_factory()() as s:
        s.add(ProxySubscription(kind="clash", url="http://other.invalid/x", label="P"))
        await s.flush()  # 拿到 id，仍在同一个事务里
        await persist_snapshot(s, snap, data_dir=tmp_data_dir)
        await s.rollback()

    async with get_session_factory()() as s:
        subs = await s.scalar(select(func.count()).select_from(ProxySubscription))
        snaps = await s.scalar(select(func.count()).select_from(SubscriptionSnapshot))
    assert subs == 0, "persist_snapshot 把调用方的 pending 订阅行一起提交了"
    assert snaps == 0


# ── P1.2.1 ③：快照不可变（对任何构造路径都成立）──────────────────
def test_snapshot_is_immutable():
    src = [_node()]
    snap = _snap_of(src)
    with pytest.raises(AttributeError):
        snap.nodes.append(_node(name="X"))          # 元组：没有 append
    with pytest.raises(TypeError):
        snap.nodes[0]["uuid"] = "tampered"          # 只读映射：不可赋值
    with pytest.raises(AttributeError):
        snap.sha256 = "tampered"                    # frozen：字段不可重绑
    with pytest.raises(AttributeError):
        snap.attempts.append(None)
    # 深拷贝：外部持有的原始 dict 事后被改，不影响已生成的快照
    src[0]["uuid"] = "mutated-outside"
    assert snap.nodes[0]["uuid"] == "u1"
    # 只封顶层：指纹仍要能走 json.dumps，不能被封成不可序列化对象
    assert node_fingerprint(snap.nodes[0]) == node_fingerprint(_node())


# ── P1.2.1 ④：上一份快照恢复接口 ─────────────────────────────────
@pytest.mark.asyncio
async def test_latest_snapshot_recovers_previous_baseline(tmp_data_dir):
    await init_db()
    nodes = [_node(name="A", server="a.example.net"),
             _node(name="B", server="b.example.net")]
    snap, _ = await _refresh(1, build_channels(),
                             _scripted({CHANNEL_DIRECT: (200, _yaml_bytes(nodes))}, [], []))
    async with get_session_factory()() as s:
        await persist_snapshot(s, snap, data_dir=tmp_data_dir)
        await s.commit()

    async with get_session_factory()() as s:
        back = await latest_snapshot(s, 1, url=SUB_URL)
    assert back is not None
    assert back.sha256 == snap.sha256
    assert back.node_count == 2
    assert [n["name"] for n in back.nodes] == ["A", "B"]
    assert back.fmt == FORMAT_YAML
    assert back.source_channel == CHANNEL_DIRECT
    # 恢复出来的基线必须能直接喂 diff：内容没变 → 不产生任何变更
    d = compute_diff(back, nodes, snap.sha256)
    assert d.unchanged_snapshot is True
    assert not d.added and not d.updated and not d.removed


@pytest.mark.asyncio
async def test_latest_snapshot_returns_none_when_absent(tmp_data_dir):
    """首次订阅：没有行 → None（调用方按全部 ADDED 处理），不是异常。"""
    await init_db()
    async with get_session_factory()() as s:
        assert await latest_snapshot(s, 99) is None


@pytest.mark.asyncio
async def test_latest_snapshot_fails_closed_when_blob_unreadable(tmp_data_dir):
    """行在、字节没了或对不上 sha → 必须抛，不许静默当成「没有上一份」。"""
    await init_db()
    snap, _ = await _refresh(1, build_channels(),
                             _scripted({CHANNEL_DIRECT: (200, _yaml_bytes([_node()]))},
                                       [], []))
    async with get_session_factory()() as s:
        row = await persist_snapshot(s, snap, data_dir=tmp_data_dir)
        await s.commit()
        path = Path(row.raw_path)

    path.unlink()
    async with get_session_factory()() as s:
        with pytest.raises(SnapshotRecoveryError):
            await latest_snapshot(s, 1)

    path.write_bytes(_yaml_bytes([_node(name="别的", server="other.example.net")]))
    async with get_session_factory()() as s:
        with pytest.raises(SnapshotRecoveryError) as exc:
            await latest_snapshot(s, 1)
    assert "sha" in str(exc.value)


# ── 跨订阅节点身份（锁定模型）────────────────────────────────────
def test_rename_is_unchanged_not_updated():
    """改名不进指纹：订阅侧改名不得让身份或池内名漂移。"""
    prev = _snap_of([_node(name="香港01")])
    nodes = [_node(name="香港01-改名")]
    d = compute_diff(prev, nodes, "sha-different")  # 强制走慢路径
    assert d.unchanged == [node_fingerprint(nodes[0])]
    assert not d.updated and not d.added and not d.removed


@pytest.mark.asyncio
async def test_same_config_from_two_subscriptions_is_one_node_two_sources(tmp_data_dir):
    """同一份配置被两个订阅提供 = 一行节点 + 两行来源；A 撤掉不影响 B。"""
    await init_db()
    cfg = _node(name="HK", server="hk.example.net")
    fp = node_fingerprint(cfg)
    async with get_session_factory()() as s:
        s.add(ProxyNode(node_id=fp[:20], fingerprint=fp, runtime_name="1|HK",
                        proxy_type="vmess", state="ACTIVE"))
        s.add(ProxyNodeSource(node_id=fp[:20], subscription_id=1, original_name="HK"))
        s.add(ProxyNodeSource(node_id=fp[:20], subscription_id=2,
                              original_name="香港-HK"))
        await s.commit()

    async with get_session_factory()() as s:
        assert await s.scalar(select(func.count()).select_from(ProxyNode)) == 1
        assert await s.scalar(select(func.count()).select_from(ProxyNodeSource)) == 2
        # 订阅 A 不再提供它：只删 A 的来源行，节点行必须还在（B 还在提供）
        await s.execute(delete(ProxyNodeSource).where(ProxyNodeSource.subscription_id == 1))
        await s.commit()
        assert await s.scalar(select(func.count()).select_from(ProxyNode)) == 1
        remaining = (await s.execute(
            select(ProxyNodeSource.subscription_id))).scalars().all()
    assert remaining == [2], "A 撤掉后节点应只由 B 提供，而不是整体消失"

    # 指纹全局唯一：同一份配置不允许登记第二行节点
    async with get_session_factory()() as s:
        s.add(ProxyNode(node_id="other", fingerprint=fp, runtime_name="2|HK",
                        proxy_type="vmess"))
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
async def test_fresh_db_matches_locked_identity_model(tmp_data_dir):
    """新库结构 = 锁定的身份模型：节点表只有身份，来源单独成表且指纹真被约束。"""
    await init_db()
    async with get_engine().connect() as conn:
        cols = {r[1] for r in (await conn.exec_driver_sql(
            "PRAGMA table_info(proxy_nodes)")).fetchall()}
        src_cols = {r[1] for r in (await conn.exec_driver_sql(
            "PRAGMA table_info(proxy_node_sources)")).fetchall()}
    assert "fingerprint" in cols and "runtime_name" in cols
    assert not {"subscription_id", "original_name"} & cols, "节点表不得再有单列归属"
    assert {"node_id", "subscription_id", "original_name",
            "first_seen", "last_seen"} <= src_cols

    async with get_session_factory()() as s:
        s.add(ProxyNode(node_id="a" * 20, fingerprint="f" * 64,
                        runtime_name="1|A", proxy_type="vmess"))
        s.add(ProxyNode(node_id="b" * 20, fingerprint="f" * 64,
                        runtime_name="1|B", proxy_type="vmess"))
        with pytest.raises(IntegrityError):
            await s.commit()
