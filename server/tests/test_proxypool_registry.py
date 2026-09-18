"""P1.3-A：Snapshot → Registry 登记事实的行为测试（6 条）。

Registry 只回答一个问题：**「谁还在提供这个节点？」**
「这个节点还能不能用？」归健康模块，「该给 Mihomo 哪些节点」归池生成——
本文件的断言不得越界到后两者（不探测、不计失败、不退休）。

隔离：`tmp_data_dir` 模式（同 `test_proxypool_subscription.py`）——临时数据目录
+ 建表 + 三个 lru_cache 收尾清理，绝不触生产库。
"""
from __future__ import annotations

import hashlib
from datetime import datetime

import pytest
import yaml
from sqlalchemy import func, select

from app.core.config import get_settings  # noqa: E402
from app.core.database import get_engine, get_session_factory, init_db  # noqa: E402
from app.domains.proxypool.models import (  # noqa: E402
    ProxyNode,
    ProxyNodeSource,
    node_fingerprint,
)
from app.domains.proxypool.registry import apply_snapshot  # noqa: E402
from app.domains.proxypool.state import (  # noqa: E402
    NODE_ACTIVE,
    NODE_DEAD,
    NODE_NEW,
    NODE_RETIRED,
    NODE_STALE,
)
from app.domains.proxypool.subscription import (  # noqa: E402
    CHANNEL_DIRECT,
    FORMAT_YAML,
    Snapshot,
)

NOW = datetime(2026, 9, 19, 12, 0, 0)


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """临时数据目录 + 建表（收尾必须清 `get_settings` 的 lru_cache，见同域测试注）。"""
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _node(name: str, *, server: str | None = None, port: int = 443,
          password: str = "pw") -> dict:
    return {
        "name": name,
        "type": "ss",
        "server": server or f"{name}.example.net",
        "port": port,
        "cipher": "aes-128-gcm",
        "password": password,
    }


def _snap(nodes: list[dict], *, subscription_id: int = 1,
          url: str = "https://example.test/sub") -> Snapshot:
    raw = yaml.safe_dump({"proxies": list(nodes)}, allow_unicode=True).encode("utf-8")
    return Snapshot(
        subscription_id=subscription_id,
        url=url,
        fetched_at=NOW,
        http_status=200,
        content_type="text/yaml",
        sha256=hashlib.sha256(raw).hexdigest(),
        raw_content=raw,
        fmt=FORMAT_YAML,
        node_count=len(nodes),
        source_channel=CHANNEL_DIRECT,
        nodes=tuple(nodes),
    )


async def _count(model) -> int:
    async with get_session_factory()() as s:
        return await s.scalar(select(func.count()).select_from(model))


async def _apply(snapshot: Snapshot, *, now: datetime = NOW):
    async with get_session_factory()() as s:
        result = await apply_snapshot(
            s, subscription_id=snapshot.subscription_id, snapshot=snapshot, now=now
        )
        await s.commit()
        return result


async def _node_row(fingerprint: str) -> ProxyNode:
    async with get_session_factory()() as s:
        return (await s.execute(
            select(ProxyNode).where(ProxyNode.fingerprint == fingerprint)
        )).scalar_one()


async def _sources_of(subscription_id: int) -> list[ProxyNodeSource]:
    async with get_session_factory()() as s:
        return list((await s.execute(
            select(ProxyNodeSource).where(
                ProxyNodeSource.subscription_id == subscription_id
            ).order_by(ProxyNodeSource.id)
        )).scalars())


async def _set_state(fingerprint: str, state: str) -> None:
    """模拟健康模块此前的判定结果（Registry 自己不产生 ACTIVE/DEAD/RETIRED）。"""
    async with get_session_factory()() as s:
        row = (await s.execute(
            select(ProxyNode).where(ProxyNode.fingerprint == fingerprint)
        )).scalar_one()
        row.state = state
        await s.commit()


# ── 1. 首次登记 ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_first_snapshot_registers_one_node_and_one_source(tmp_data_dir) -> None:
    await init_db()
    x = _node("香港01", server="hk1.example.net")

    result = await _apply(_snap([x]))

    assert await _count(ProxyNode) == 1
    assert await _count(ProxyNodeSource) == 1
    fp = node_fingerprint(x)
    assert result.created_nodes == (fp,)

    row = await _node_row(fp)
    assert row.state == NODE_NEW, "Registry 登记事实，不代替健康模块判可用"
    assert row.runtime_name == "1|香港01"
    assert row.proxy_type == "ss"
    assert row.server == "hk1.example.net"

    (src,) = await _sources_of(1)
    assert src.node_id == row.node_id
    assert src.original_name == "香港01"
    assert src.last_seen == NOW and src.first_seen == NOW


# ── 2. 重复刷新不产生重复 ────────────────────────────────────────
@pytest.mark.asyncio
async def test_repeat_identical_refresh_creates_no_duplicates(tmp_data_dir) -> None:
    await init_db()
    x = _node("香港01", server="hk1.example.net")

    await _apply(_snap([x]))
    result = await _apply(_snap([x]), now=datetime(2026, 9, 19, 13, 0, 0))

    assert await _count(ProxyNode) == 1
    assert await _count(ProxyNodeSource) == 1
    assert result.created_nodes == ()

    (src,) = await _sources_of(1)
    assert src.last_seen == datetime(2026, 9, 19, 13, 0, 0), "续见的来源要刷新 last_seen"
    assert src.first_seen == NOW, "first_seen 是首次见到，不随刷新前移"


# ── 3. 跨订阅共享：Model A 的核心证明 ────────────────────────────
@pytest.mark.asyncio
async def test_same_config_from_two_subscriptions_shares_one_node(tmp_data_dir) -> None:
    await init_db()
    x = _node("香港01", server="hk1.example.net")

    await _apply(_snap([x], subscription_id=1))
    await _apply(_snap([x], subscription_id=2))

    assert await _count(ProxyNode) == 1, "同一份配置只占一行身份"
    assert await _count(ProxyNodeSource) == 2, "两个订阅各留一条来源证据"

    row = await _node_row(node_fingerprint(x))
    assert row.runtime_name == "1|香港01", "身份与名字由首个登记它的订阅铸造，第二名不得重铸"


# ── 4. A 撤掉、B 仍提供 → 节点不消失 ─────────────────────────────
@pytest.mark.asyncio
async def test_node_survives_when_one_of_two_subscriptions_drops_it(tmp_data_dir) -> None:
    await init_db()
    x = _node("香港01", server="hk1.example.net")
    await _apply(_snap([x], subscription_id=1))
    await _apply(_snap([x], subscription_id=2))

    await _apply(_snap([_node("别的")], subscription_id=1))

    assert await _count(ProxyNode) == 2, "只该多出「别的」，X 不得被删"
    row = await _node_row(node_fingerprint(x))
    assert row.state == NODE_NEW, "B 仍在提供它，来源流失不成立，状态不得被判死"

    srcs = await _sources_of(1)
    assert [s.original_name for s in srcs] == ["别的"]
    assert len(await _sources_of(2)) == 1
    assert (await _sources_of(2))[0].node_id == row.node_id


# ── 5. 所有来源消失 → 按已封板的 D1 语义落状态 ───────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial,expected",
    [
        (NODE_NEW, NODE_DEAD),        # 从未通过体检，失去来源不得进池
        (NODE_ACTIVE, NODE_STALE),    # 曾通过体检，降权保留
        (NODE_STALE, NODE_STALE),     # 已是 STALE，原地不动
        (NODE_DEAD, NODE_DEAD),       # 已判死，不得被「来源消失」洗白成 STALE
        (NODE_RETIRED, NODE_RETIRED),  # 退休是终态，除非健康证据
    ],
)
async def test_last_source_gone_follows_locked_state_semantics(
    tmp_data_dir, initial: str, expected: str
) -> None:
    await init_db()
    x = _node("香港01", server="hk1.example.net")
    await _apply(_snap([x]))
    fp = node_fingerprint(x)
    await _set_state(fp, initial)

    result = await _apply(_snap([_node("别的")]))

    row = await _node_row(fp)
    assert row.state == expected
    assert await _count(ProxyNodeSource) == 1, "订阅已不再提供的来源必须被替换掉"
    assert result.removed_sources == (fp,)


# ── 6. 订阅侧改名：只有来源的 original_name 变 ───────────────────
@pytest.mark.asyncio
async def test_rename_updates_source_name_only(tmp_data_dir) -> None:
    await init_db()
    before = _node("香港01", server="hk1.example.net")
    after = _node("香港主节点", server="hk1.example.net")

    await _apply(_snap([before]))
    row_before = await _node_row(node_fingerprint(before))

    await _apply(_snap([after]))

    assert await _count(ProxyNode) == 1, "改名不是新节点"
    row_after = await _node_row(node_fingerprint(after))
    assert row_after.node_id == row_before.node_id
    assert row_after.runtime_name == row_before.runtime_name == "1|香港01", (
        "runtime_name 铸造一次即持久化：重铸会让已绑定的池条目指向不存在的名字"
    )
    (src,) = await _sources_of(1)
    assert src.original_name == "香港主节点", "订阅侧的名称变化按来源分别留痕"
    assert src.last_seen == NOW