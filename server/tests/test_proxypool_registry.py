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
    node_fingerprint,
    subscription_tag,
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
          url: str | None = None) -> Snapshot:
    raw = yaml.safe_dump({"proxies": list(nodes)}, allow_unicode=True).encode("utf-8")
    return Snapshot(
        subscription_id=subscription_id,
        url=url or f"https://sub{subscription_id}.example.test/sub",
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
            s, subscription_id=snapshot.subscription_id, snapshot=snapshot, now=now,
            tag=subscription_tag(snapshot.url),
        )
        await s.commit()
        return result


async def _node_row(fingerprint: str) -> ProxyNode:
    async with get_session_factory()() as s:
        return (await s.execute(
            select(ProxyNode).where(ProxyNode.fingerprint == fingerprint)
        )).scalar_one()


def _fp(config: dict, subscription_id: int = 1) -> str:
    """与生产同源的指纹：节点身份含订阅标识。"""
    return node_fingerprint(config, subscription_tag(f"https://sub{subscription_id}.example.test/sub"))


async def _all_nodes() -> list[ProxyNode]:
    async with get_session_factory()() as s:
        return list((await s.execute(
            select(ProxyNode).order_by(ProxyNode.id)
        )).scalars())


async def _sources_of(subscription_id: int) -> list[ProxyNode]:
    async with get_session_factory()() as s:
        return list((await s.execute(
            select(ProxyNode).where(
                ProxyNode.subscription_id == subscription_id
            ).order_by(ProxyNode.id)
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
    assert await _count(ProxyNode) == 1
    fp = _fp(x)
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
    assert result.created_nodes == ()

    row = await _node_row(_fp(x))
    assert row.last_source_seen == datetime(2026, 9, 19, 13, 0, 0), "续见要推进 last_source_seen"
    assert row.first_seen == NOW, "first_seen 是首次见到，不随刷新前移"


# ── 3. 同配置跨订阅 → 两行独立节点（节点跟着订阅走）──────────────
@pytest.mark.asyncio
async def test_same_config_from_two_subscriptions_makes_two_nodes(tmp_data_dir) -> None:
    await init_db()
    x = _node("香港01", server="hk1.example.net")

    await _apply(_snap([x], subscription_id=1))
    await _apply(_snap([x], subscription_id=2))

    assert await _count(ProxyNode) == 2, "同一份配置由两个订阅提供时是两个节点"

    rows = await _all_nodes()
    assert sorted(r.runtime_name for r in rows) == ["1|香港01", "2|香港01"]
    assert {r.subscription_id for r in rows} == {1, 2}
    assert len({r.fingerprint for r in rows}) == 2, "归属订阅进身份，指纹必须不同"


# ── 4. 一个订阅撤掉它 → 只影响自己那份 ───────────────────────────
@pytest.mark.asyncio
async def test_drop_from_one_subscription_leaves_the_other_node_intact(tmp_data_dir) -> None:
    await init_db()
    x = _node("香港01", server="hk1.example.net")
    await _apply(_snap([x], subscription_id=1))
    await _apply(_snap([x], subscription_id=2))

    await _apply(_snap([_node("别的", server="hk9.example.net")], subscription_id=1))

    assert await _count(ProxyNode) == 3, "订阅1 的 X 解除归属后行仍在，另有订阅2 的 X 与「别的」"
    rows = await _all_nodes()
    sub2 = [r for r in rows if r.subscription_id == 2]
    assert len(sub2) == 1 and sub2[0].state == NODE_NEW, "订阅2 的节点不受订阅1 变动影响"
    assert len([r for r in rows if r.subscription_id is None]) == 1, "订阅1 的 X 归属已解除"


# ── 5. 归属解除 → 按已封板的 D1 语义落状态 ───────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial,expected",
    [
        (NODE_NEW, NODE_DEAD),        # 从未通过体检，失去归属不得进池
        (NODE_ACTIVE, NODE_STALE),    # 曾通过体检，降权保留
        (NODE_STALE, NODE_STALE),     # 已是 STALE，原地不动
        (NODE_DEAD, NODE_DEAD),       # 已判死，不得被「归属消失」洗白成 STALE
        (NODE_RETIRED, NODE_RETIRED),  # 退休是终态，除非健康证据
    ],
)
async def test_last_source_gone_follows_locked_state_semantics(
    tmp_data_dir, initial: str, expected: str
) -> None:
    await init_db()
    x = _node("香港01", server="hk1.example.net")
    await _apply(_snap([x]))
    fp = _fp(x)
    await _set_state(fp, initial)

    result = await _apply(_snap([_node("别的", server="hk9.example.net")]))

    row = await _node_row(fp)
    assert row.state == expected
    assert row.subscription_id is None, "订阅已不再提供的节点必须解除归属"
    assert result.removed_sources == (fp,)


# ── 6. 订阅侧改名：只有 original_name 变 ─────────────────────────
@pytest.mark.asyncio
async def test_rename_updates_source_name_only(tmp_data_dir) -> None:
    await init_db()
    before = _node("香港01", server="hk1.example.net")
    after = _node("香港主节点", server="hk1.example.net")

    await _apply(_snap([before]))
    row_before = await _node_row(_fp(before))

    await _apply(_snap([after]))

    assert await _count(ProxyNode) == 1, "改名不是新节点"
    row_after = await _node_row(_fp(after))
    assert row_after.node_id == row_before.node_id
    assert row_after.runtime_name == row_before.runtime_name == "1|香港01", (
        "runtime_name 铸造一次即持久化：重铸会让已绑定的池条目指向不存在的名字"
    )
    (src,) = await _sources_of(1)
    assert src.original_name == "香港主节点", "订阅侧的名称变化要跟着更新"
    assert src.last_source_seen == NOW


# ── 7. last_source_seen 只在续见时推进 ───────────────────────────
@pytest.mark.asyncio
async def test_last_source_seen_advances_only_when_still_provided(tmp_data_dir) -> None:
    """`last_source_seen` 是「最近一次出现在订阅快照里」的时刻。

    订阅不再提供它时**不得**把它推成当前轮的时刻——那会把节点的「最后见到」
    凭空刷新，STALE 候选按新鲜度筛选会因此误判。
    """
    await init_db()
    x = _node("香港01", server="hk1.example.net")
    t10 = datetime(2026, 9, 19, 10, 0, 0)
    t12 = datetime(2026, 9, 19, 12, 0, 0)
    t13 = datetime(2026, 9, 19, 13, 0, 0)

    await _apply(_snap([x], subscription_id=1), now=t10)
    await _apply(_snap([x], subscription_id=1), now=t12)
    fp = _fp(x)
    assert (await _node_row(fp)).last_source_seen == t12, "续见时推进"

    # 13:00：订阅不再提供 X
    await _apply(_snap([_node("别的", server="hk9.example.net")], subscription_id=1), now=t13)

    row = await _node_row(fp)
    assert row.last_source_seen == t12, "归属已解除，不得把「最后见到」推成 13:00"
    assert row.subscription_id is None


# ── 8. 同一份快照内的重复指纹只登记一条来源 ──────────────────────
@pytest.mark.asyncio
async def test_duplicate_fingerprint_in_one_snapshot_collapses(tmp_data_dir) -> None:
    """同一份快照里的重复配置只算一条来源（`name` 不参与身份）。

    节点身份是**集合**（`ProxyNode(S) == {fingerprint(n) | n ∈ Snapshot(S)}`），
    天然应去重；机场确实会返回重复配置（同线路两名）。逐条处理会撞
    `UNIQUE(fingerprint)`——一个数据事实不该以数据库异常收场。
    """
    await init_db()
    duplicated = [
        _node("香港01", server="hk1.example.net"),
        _node("香港01", server="hk1.example.net"),          # 字面重复
        _node("香港高速01", server="hk1.example.net"),       # 同配置、换名（name 不参与指纹）
    ]

    result = await _apply(_snap(duplicated))

    assert await _count(ProxyNode) == 1
    assert result.created_nodes == (_fp(duplicated[0]),)
    (src,) = await _sources_of(1)
    assert src.original_name == "香港01", "重复时取确定性的首次出现值"


@pytest.mark.asyncio
async def test_dead_node_is_released_when_subscription_still_provides_it(
    tmp_data_dir,
) -> None:
    """订阅仍在提供却停在 DEAD 的节点必须被放出来重参选。

    它掉出池文件后池内核不认识它，体检只能拿到「名字没定位到（404）」，
    而 404 又坐实 DEAD —— 不放出这条来源证据，节点状态永远翻不回来。
    """
    await init_db()
    node = _node("香港01", server="hk1.example.net")
    fp = _fp(node)
    await _apply(_snap([node]))
    await _set_state(fp, NODE_DEAD)

    result = await _apply(_snap([node]))

    row = await _node_row(fp)
    assert row.state == NODE_STALE, "仍在被提供的 DEAD 节点必须放回池（STALE）"
    assert (fp, NODE_DEAD, NODE_STALE) in result.state_changes


@pytest.mark.asyncio
async def test_released_dead_node_resets_failure_count(tmp_data_dir) -> None:
    """放出时清零失败计数：否则下一次体检失败即达退休线，白放一次。"""
    await init_db()
    node = _node("香港01", server="hk1.example.net")
    fp = _fp(node)
    await _apply(_snap([node]))
    async with get_session_factory()() as s:
        row = (await s.execute(
            select(ProxyNode).where(ProxyNode.fingerprint == fp)
        )).scalar_one()
        row.state = NODE_DEAD
        row.consecutive_failures = 2
        await s.commit()

    await _apply(_snap([node]))

    row = await _node_row(fp)
    assert row.state == NODE_STALE
    assert row.consecutive_failures == 0


@pytest.mark.asyncio
async def test_active_node_is_not_disturbed_by_refresh(tmp_data_dir) -> None:
    """健康节点不因一次刷新被改动 —— 放出规则只作用于 DEAD。"""
    await init_db()
    node = _node("香港01", server="hk1.example.net")
    fp = _fp(node)
    await _apply(_snap([node]))
    await _set_state(fp, NODE_ACTIVE)

    result = await _apply(_snap([node]))

    row = await _node_row(fp)
    assert row.state == NODE_ACTIVE
    assert result.state_changes == ()


@pytest.mark.asyncio
async def test_same_identity_refreshes_complete_connection_config(tmp_data_dir) -> None:
    await init_db()
    before = {**_node("endpoint"), "udp": False, "dialer-proxy": "old-hop",
              "ws-opts": {"headers": {"Host": "edge.example.test"}}}
    after = {**before, "name": "renamed-endpoint", "port": 8443,
             "udp": True, "dialer-proxy": "new-hop", "tfo": True}
    assert _fp(before) == _fp(after)
    await _apply(_snap([before]))
    await _set_state(_fp(before), NODE_ACTIVE)
    original = await _node_row(_fp(before))

    await _apply(_snap([after]))

    refreshed = await _node_row(_fp(after))
    assert refreshed.normalized_config == after
    assert refreshed.node_id == original.node_id
    assert refreshed.runtime_name == original.runtime_name
    assert refreshed.state == NODE_ACTIVE
