"""P1.3-B：Registry → `crawl-pool.yaml` 的行为测试（5 条）。

本阶段**只**负责「把 Registry 里能用的节点渲染成一个合法的池文件」：
- 资格：`state ∈ {NEW, ACTIVE, STALE}`；`DEAD` / `RETIRED` 不进池
- 取名：一律用**已持久化**的 `runtime_name`，覆盖 `normalized_config["name"]`
- 三个硬门禁（都在写盘之前）：能重新解析 / 名字无重复 / 写入数 = 预期数

不做（本文件不得出现）：generation、candidate provider、rollback、
Mihomo API reconcile、proxy-group、健康检查、lane、worker lease。

隔离：`tmp_data_dir` 模式（同同域其他测试）——临时数据目录 + 建表 + lru_cache 收尾。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from app.core.config import get_settings  # noqa: E402
from app.core.database import get_engine, get_session_factory, init_db  # noqa: E402
from app.domains.proxypool import pool as pool_module  # noqa: E402
from app.domains.proxypool.models import ProxyNode, node_fingerprint  # noqa: E402
from app.domains.proxypool.pool import (  # noqa: E402
    PoolBuildError,
    build_pool,
    pool_path,
    render_pool,
)
from app.domains.proxypool.state import (  # noqa: E402
    NODE_ACTIVE,
    NODE_DEAD,
    NODE_NEW,
    NODE_RETIRED,
    NODE_STALE,
)

NOW = datetime(2026, 9, 19, 12, 0, 0)


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _config(*, name: str, server: str, port: int = 443, password: str = "pw") -> dict:
    return {
        "name": name,
        "type": "ss",
        "server": server,
        "port": port,
        "cipher": "aes-128-gcm",
        "password": password,
    }


async def _add(state: str, runtime_name: str, *, server: str, name: str | None = None,
               config: dict | None = None) -> str:
    """直接登记一行（本阶段测的是池生成，不经过 Registry）。"""
    cfg = config if config is not None else _config(
        name=name or runtime_name, server=server
    )
    fp = node_fingerprint(cfg)
    async with get_session_factory()() as s:
        s.add(ProxyNode(
            node_id=fp, fingerprint=fp, runtime_name=runtime_name,
            proxy_type=str(cfg.get("type") or ""), server=cfg.get("server"),
            normalized_config=cfg, state=state,
            first_seen=NOW, last_seen=NOW, last_source_seen=NOW,
        ))
        await s.commit()
    return fp


def _names_in(path: Path) -> list[str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [p["name"] for p in doc["proxies"]]


async def _build(data_dir):
    async with get_session_factory()() as s:
        return await build_pool(s, data_dir=data_dir)


# ── 1. 只有 NEW / ACTIVE / STALE 进池 ────────────────────────────
@pytest.mark.asyncio
async def test_pool_contains_only_eligible_states(tmp_data_dir) -> None:
    await init_db()
    for state, server in (
        (NODE_NEW, "n1.example.net"),
        (NODE_ACTIVE, "n2.example.net"),
        (NODE_STALE, "n3.example.net"),
        (NODE_DEAD, "n4.example.net"),
        (NODE_RETIRED, "n5.example.net"),
    ):
        await _add(state, f"1|{server}", server=server)

    result = await _build(tmp_data_dir)

    assert result.expected_count == 3
    assert result.written_count == 3
    assert set(_names_in(pool_path(tmp_data_dir))) == {
        "1|n1.example.net", "1|n2.example.net", "1|n3.example.net",
    }, "DEAD / RETIRED 进了池就会承载流量"
    assert result.runtime_names == (
        "1|n1.example.net", "1|n2.example.net", "1|n3.example.net",
    )


# ── 2. 名字取自持久化 runtime_name，而不是订阅原名 ───────────────
@pytest.mark.asyncio
async def test_pool_name_is_persisted_runtime_name(tmp_data_dir) -> None:
    await init_db()
    # 订阅原名「香港01」，池内名是铸造期定下的「1|香港01」
    await _add(NODE_ACTIVE, "1|香港01", server="hk1.example.net", name="香港01")

    async with get_session_factory()() as s:
        node = (await s.execute(select(ProxyNode))).scalar_one()

    # 纯渲染层：不得就地改写作为事实副本的 normalized_config
    _, names = render_pool([node])
    assert names == ("1|香港01",)
    assert node.normalized_config["name"] == "香港01", (
        "覆盖只发生在渲染副本上；就地改事实副本会污染台账"
    )

    await _build(tmp_data_dir)
    assert _names_in(pool_path(tmp_data_dir)) == ["1|香港01"], (
        "池内名必须是持久化的 runtime_name，否则 Mihomo 侧会退化成订阅原名"
    )


# ── 3. 重复 runtime_name 在写盘前就失败 ──────────────────────────
def test_duplicate_runtime_name_fails_before_write() -> None:
    """两条同名的行必须直接失败，不能生成「看起来成功但少节点」的池。

    库里 `runtime_name` 已是唯一约束，正常情况下造不出重复——所以这一条直接
    打纯渲染层：它的作用是**不依赖数据库兜底**，让生成器自己先拦住。
    """
    a = ProxyNode(
        node_id="a" * 64, fingerprint="a" * 64, runtime_name="1|香港01",
        proxy_type="ss", server="hk1.example.net",
        normalized_config=_config(name="香港01", server="hk1.example.net"),
        state=NODE_ACTIVE,
    )
    b = ProxyNode(
        node_id="b" * 64, fingerprint="b" * 64, runtime_name="1|香港01",
        proxy_type="ss", server="hk2.example.net",
        normalized_config=_config(name="香港01", server="hk2.example.net"),
        state=NODE_ACTIVE,
    )

    with pytest.raises(PoolBuildError, match="重复"):
        render_pool([a, b])


# ── 4. 写入数 ≠ 预期数 → 失败，且旧池文件一字未动 ────────────────
@pytest.mark.asyncio
async def test_count_mismatch_fails_and_leaves_existing_pool_untouched(
    tmp_data_dir, monkeypatch
) -> None:
    await init_db()
    for server in ("n1.example.net", "n2.example.net", "n3.example.net"):
        await _add(NODE_ACTIVE, f"1|{server}", server=server)

    await _build(tmp_data_dir)
    path = pool_path(tmp_data_dir)
    before = path.read_bytes()
    assert len(_names_in(path)) == 3

    real_dump = yaml.safe_dump

    def _drops_one(data, *args, **kwargs):
        """模拟「发射器静默丢掉一个节点」——这正是计数门禁存在的理由。"""
        trimmed = dict(data)
        trimmed["proxies"] = list(data["proxies"])[:-1]
        return real_dump(trimmed, *args, **kwargs)

    monkeypatch.setattr(pool_module.yaml, "safe_dump", _drops_one)

    with pytest.raises(PoolBuildError, match="数量"):
        await _build(tmp_data_dir)

    assert path.read_bytes() == before, "门禁失败必须发生在写盘之前，旧池不得被破坏"
    assert len(_names_in(path)) == 3


# ── 5. 敌意名字逐字节往返 ────────────────────────────────────────
@pytest.mark.asyncio
async def test_hostile_runtime_names_round_trip(tmp_data_dir) -> None:
    """`runtime_name` 含 `|`，且原名可能含 `:` `#` 前导 `-` emoji 换行。

    生成器不得靠裸标量拼接——往返失真会让内核里的名字与 Registry 对不上，
    而名字是池与内核之间唯一的对账钥匙。
    """
    await init_db()
    hostile = [
        ("1|香港01", "h1.example.net"),
        ("2|a: b # c", "h2.example.net"),
        ("3|- leading dash", "h3.example.net"),
        ("4|emoji 🚀", "h4.example.net"),
        ("5|line\nbreak", "h5.example.net"),
    ]
    for runtime_name, server in hostile:
        await _add(NODE_ACTIVE, runtime_name, server=server)

    result = await _build(tmp_data_dir)

    assert result.written_count == len(hostile)
    assert set(_names_in(pool_path(tmp_data_dir))) == {n for n, _ in hostile}
    assert result.runtime_names == tuple(n for n, _ in hostile)