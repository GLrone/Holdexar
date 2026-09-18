"""proxypool 域模型：节点注册表、出口组、lane 租约与运行台账。

Registry 是事实源，pool 文件只是它的运行时产物：节点身份、健康证据与调度
台账都落在这里，下游只读不反倒写。

节点身份的两个工具函数放模型层而非服务层，因为它们决定台账主键：
`node_fingerprint` 判定「两个配置是否同一节点」，`make_runtime_name`
判定「进池后叫什么」。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.domains.proxypool.state import LANE_EMPTY, NODE_NEW

# 不参与节点身份的键：命名必然随来源而变，开关由消费侧自行覆盖，
# 两者都不改变「这个节点是谁」。
_IGNORE_FOR_FP = frozenset({
    "name", "display-name",
    "udp", "tfo", "mptcp", "smux",
    "uot", "xudp",
    "dialer-proxy", "interface-name", "routing-mark",
})


def node_fingerprint(config: dict) -> str:
    """节点指纹：剔除消费侧噪声后规范化 JSON 的 sha256。

    稳定性契约：键顺序不影响结果；未知键参与身份（来源新增可选字段会换指纹，
    这是预期——宁可重登记一条，不可把两个节点并成一条台账）。
    """
    payload = {k: v for k, v in config.items() if k not in _IGNORE_FOR_FP}
    blob = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def make_runtime_name(subscription_id: int | str, original_name: str, taken: set[str]) -> str:
    """池内唯一名：`<订阅>|原名`，同订阅撞名追加 `#2` `#3`。

    原始名绝不直接进池：执行器按 name 去重，重名节点会被静默丢弃，池文件
    写盘数与内核可见数之间会出现无从自察的差额。订阅前缀同时保证跨订阅
    同名节点互不遮蔽。首次登记时算一次即持久化，后续不重算——否则 lane
    绑定会漂移。
    """
    base = f"{subscription_id}|{original_name}"
    candidate, suffix = base, 1
    while candidate in taken:
        suffix += 1
        candidate = f"{base}#{suffix}"
    return candidate


class ProxyNode(Base):
    """节点注册表行：一个唯一出口实现一条台账，跨订阅刷新不删节点、不丢历史。"""

    __tablename__ = "proxy_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(64), unique=True)
    subscription_id: Mapped[int] = mapped_column(Integer, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    runtime_name: Mapped[str] = mapped_column(String(400), unique=True)
    original_name: Mapped[str] = mapped_column(String(255))
    proxy_type: Mapped[str] = mapped_column(String(32))
    server: Mapped[str | None] = mapped_column(String(255))
    normalized_config: Mapped[dict | None] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), default=NODE_NEW)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime)
    # 最后一次出现在订阅快照里的时刻 —— STALE 判定的来源证据
    last_source_seen: Mapped[datetime | None] = mapped_column(DateTime)
    last_l0_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_l1_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_l2_at: Mapped[datetime | None] = mapped_column(DateTime)
    exit_ip: Mapped[str | None] = mapped_column(String(64), index=True)
    exit_group_id: Mapped[int | None] = mapped_column(Integer)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    # 业务容量：由 L2 真实批次测得；L0 的小负载延迟不参与（NULL=未测）
    capacity_score: Mapped[float | None] = mapped_column(Float)


class ExitGroup(Base):
    """出口组：以出口 IP 为身份聚合共享出口的节点，调度按组而非按节点记账。"""

    __tablename__ = "exit_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exit_id: Mapped[str] = mapped_column(String(64), unique=True)
    exit_ip: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    primary_node_id: Mapped[str | None] = mapped_column(String(64))
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    l2_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    capacity_score: Mapped[float | None] = mapped_column(Float)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime)


class Lane(Base):
    """爬虫 worker 的稳定代理工位：一个 listener + 当前出口组 / 节点绑定。"""

    __tablename__ = "lanes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lane_index: Mapped[int] = mapped_column(Integer, unique=True)
    listener_port: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default=LANE_EMPTY)
    exit_group_id: Mapped[int | None] = mapped_column(Integer)
    node_id: Mapped[str | None] = mapped_column(String(64))
    generation: Mapped[int | None] = mapped_column(Integer)
    last_switch_at: Mapped[datetime | None] = mapped_column(DateTime)


class PoolGeneration(Base):
    """池发布的版本号：写盘前落 PENDING，内核对账数相符才置 COMMITTED。"""

    __tablename__ = "pool_generations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generation: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    pool_sha256: Mapped[str] = mapped_column(String(64))
    expected_node_count: Mapped[int] = mapped_column(Integer)
    observed_node_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime)


class WorkerLease(Base):
    """Worker 租约：worker 只见 lease，不直接操纵节点与内核。"""

    __tablename__ = "worker_leases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lease_id: Mapped[str] = mapped_column(String(64), unique=True)
    worker_id: Mapped[str] = mapped_column(String(64), index=True)
    lane_index: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer)
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime)
    released_at: Mapped[datetime | None] = mapped_column(DateTime)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)


class HealthObservation(Base):
    """健康观测流水：L0 传输 / L1 出口身份 / L2 真实业务，三层不可替代。"""

    __tablename__ = "health_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(64), index=True)
    level: Mapped[str] = mapped_column(String(2))
    ok: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime)


class SubscriptionSnapshot(Base):
    """订阅快照：一次抓取产出一份不可变记录，失败也留痕（不改变 Registry）。"""

    __tablename__ = "subscription_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int] = mapped_column(Integer, index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    format: Mapped[str] = mapped_column(String(16))
    # 原始字节按 sha 内容寻址落盘（订阅动辄数十 KB，不进库），这里只存路径
    raw_path: Mapped[str | None] = mapped_column(String(500))
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16))
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(100))
    # 抓取成功的通道名（direct / mihomo / proxypool）
    source_channel: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)


class OrchestrationEvent(Base):
    """编排事件流水：订阅失败、对账拒绝、lane 切换等一律留痕可回溯。"""

    __tablename__ = "orchestration_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime | None] = mapped_column(DateTime)
    kind: Mapped[str] = mapped_column(String(50), index=True)
    level: Mapped[str] = mapped_column(String(8))
    message: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[dict | None] = mapped_column(JSON)
