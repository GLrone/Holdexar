"""proxypool 域模型：节点注册表、出口组、lane 租约与运行台账。

Registry 是事实源，pool 文件只是它的运行时产物：节点身份、健康证据与调度
台账都落在这里，下游只读不反倒写。

节点身份的两个工具函数放模型层而非服务层，因为它们决定台账主键：
`node_fingerprint` 判定「两个配置是否同一节点」，`make_runtime_name`
判定「进池后叫什么」。

身份模型（锁定，改它等于改主键语义）：
- **节点身份 = `node_fingerprint`（整份配置的规范化哈希），全局唯一**；
- **来源归属 = `ProxyNodeSource`**（订阅↔节点，多对多），一个节点可被多个订阅
  同时提供，全部来源都不再提供时节点才允许离开运行集；
- 端点（type/server/port）只是 diff 分类与检索线索，不参与身份判定。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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
    绑定会漂移。`subscription_id` 传的是**首次引入该节点的来源**，此后即使
    该来源撤掉这个节点、改名或它在别的订阅里叫别的名字，名字都不变。
    """
    base = f"{subscription_id}|{original_name}"
    candidate, suffix = base, 1
    while candidate in taken:
        suffix += 1
        candidate = f"{base}#{suffix}"
    return candidate


class ProxyNode(Base):
    """节点注册表行：一个唯一出口实现一条台账，跨订阅刷新不删节点、不丢历史。

    **身份**：`fingerprint`（整份配置的规范化哈希）就是全局节点身份，故它
    **全局唯一**——同一份配置被订阅 A/B/C 同时提供，账上只有这一行。
    「谁在提供它」不在本表，见 `ProxyNodeSource`。

    早期版本在本表挂了单列 `subscription_id`（NOT NULL）+ 全局唯一 fingerprint，
    是一套自相矛盾的模型：同一配置的第二条订阅插不进来（撞唯一约束），而
    「订阅 A 撤掉、订阅 B 仍在提供」又会被读成节点消失（把来源当身份）。身份
    与来源必须分表，这正是 `ProxyNodeSource` 存在的理由。

    节点离开运行集的条件是**所有来源都不再提供它**，不是某个订阅不再提供它。
    `normalized_config` 是这条台账的事实副本；端点（type/server/port）只是检索
    线索与 diff 分类依据，不参与身份判定——同端点换凭据/参数＝另一个节点
    （另起一行；旧行靠来源流失 + 健康证据自然退休，不做跨血缘的凭据轮换推定）。
    """

    __tablename__ = "proxy_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(64), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    # 首次登记时铸造（<来源订阅>|<原名>，撞名加 #N），此后绝不重算：
    # 重算会让已绑定的 lane / 池文件条目指向一个不存在的名字
    runtime_name: Mapped[str] = mapped_column(String(400), unique=True)
    proxy_type: Mapped[str] = mapped_column(String(32))
    server: Mapped[str | None] = mapped_column(String(255))
    normalized_config: Mapped[dict | None] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), default=NODE_NEW)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime)
    # legacy / 暂不使用：登记时赋值，此后**不再推进**。来源时间的事实源是
    # ProxyNodeSource.last_seen，来源总览是 last_source_seen，健康时间在
    # last_l0/l1/l2_at——本列若也定义成「最近见到」，就会造出第二套时间真相。
    last_seen: Mapped[datetime | None] = mapped_column(DateTime)
    # 最后一次出现在**任一**订阅快照里的时刻（= 该节点全部来源 last_seen 的最大
    # 值，冗余缓存，便于按单表筛 STALE 候选）——来源证据的事实源仍是 NodeSource
    last_source_seen: Mapped[datetime | None] = mapped_column(DateTime)
    last_l0_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_l1_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_l2_at: Mapped[datetime | None] = mapped_column(DateTime)
    exit_ip: Mapped[str | None] = mapped_column(String(64), index=True)
    exit_group_id: Mapped[int | None] = mapped_column(Integer)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    # 业务容量：由 L2 真实批次测得；L0 的小负载延迟不参与（NULL=未测）
    capacity_score: Mapped[float | None] = mapped_column(Float)


class ProxyNodeSource(Base):
    """订阅↔节点 的来源关联（多对多）：节点生命周期的来源证据。

    订阅刷新成功后，用「本订阅本次快照解析出的指纹集合」**整体替换**本订阅的
    行：新指纹插入、仍在的刷新 `last_seen` / `original_name`、本订阅已不再提供
    的删除。节点行本身不因此消失——只要还有别的来源提供它，它就是活的（`state`
    由状态机按健康证据决定），这正是「A 订阅撤掉、B 订阅仍在提供」不会被误判成
    节点消失的地方。

    `original_name` 挂这里而不是 `ProxyNode`：同一个配置在不同订阅里叫什么都不
    影响身份，但「订阅侧改名了」的观测与审计要按来源分别留痕。
    `node_id` 不单建索引——下面的唯一约束索引以 node_id 为前导列，够用。
    """

    __tablename__ = "proxy_node_sources"
    __table_args__ = (
        UniqueConstraint("node_id", "subscription_id", name="ux_pns_node_sub"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(64))
    subscription_id: Mapped[int] = mapped_column(Integer, index=True)
    original_name: Mapped[str] = mapped_column(String(255))
    first_seen: Mapped[datetime | None] = mapped_column(DateTime)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime)


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


class ProxyRunExit(Base):
    """出口账本：一次作业 × 一个出口 IP × 一个端点 = 一行聚合。

    回答的是「本次作业用了哪些出口、每个出口发了多少、怎么失败的」——这是判断限流归属
    （按出口 IP / 按 endpoint / 别的维度）的唯一生产证据来源。

    边界：**不按 HTTP 请求写库**。累计先发生在内存（`crawler/exit_stats.py`），作业收尾
    一次性落成聚合行；`outcome` 是固定枚举，不放自由文本（异常串可能带 URL 与凭据，
    且长度无界）。新表由 `create_all` 缺表即建，不产生迁移、不抬 `SCHEMA_VERSION`。
    """

    __tablename__ = "proxy_run_exits"
    __table_args__ = (
        Index("ix_pre_run_exit", "run_id", "exit_ip"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)  # proxy_job_runs.id
    exit_ip: Mapped[str] = mapped_column(String(64))
    endpoint: Mapped[str] = mapped_column(String(32))
    node: Mapped[str | None] = mapped_column(String(400))  # 当时绑在该出口上的节点
    requests: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[int] = mapped_column(Integer, default=0)
    e429: Mapped[int] = mapped_column(Integer, default=0)
    e4xx: Mapped[int] = mapped_column(Integer, default=0)
    e5xx: Mapped[int] = mapped_column(Integer, default=0)
    timeout: Mapped[int] = mapped_column(Integer, default=0)
    connect_error: Mapped[int] = mapped_column(Integer, default=0)
    other: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


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
    # 这份内容是从哪个 URL 抓下来的（provenance）：订阅换链接后，旧链接的成功快照
    # 不得被 `latest_snapshot(url=当前URL)` 选中。历史行无此列为 NULL。
    url: Mapped[str | None] = mapped_column(String(500))
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16))
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(100))
    # 抓取成功的通道名（direct / mihomo / proxypool / local）
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


class ProxyJobRun(Base):
    """生产作业台账：一次真实 `run_crawl` = 一行，**只记事实，不下健康结论**。

    边界（定稿见 `docs/PROXYPOOL_HANDOVER_P1.7_NEXT.md` §13/§14）：

    - **不按 HTTP 请求记账**。爬虫的计量单位是「任务 = 一区 × 一批 ≤400 appid」
      （`CrawlerScheduler.counts()`），所以字段是 `task_count`，不是 request_count。
      `task_count` 的口径是**计划任务数**（`scheduler.total_target`）：中断的作业里
      它可以大于 `success_count + error_count`（计划 2、完成 1 就记 2/1/0）。
    - 本轮运行时的身份是 `proxy_url`（`http://127.0.0.1:<mixed-port>`）+ `pool_sha256`
      （池文件内容哈希）——**没有 runtime_name，也没有写进库的 pool_generation**
      （`pool_generations` 至今无写入方），照实记能记到的。
    - `selected_node` / `node_exit_ip` 是**一等列而非 mapping**：运行时是
      `mode: global` + 单个 mixed-port，一轮作业全程只有一个 GLOBAL 选中节点，
      `error_summary` 里放 `by_node` 字典反而会让人以为一轮跑过很多节点。
    - `active_subscription_id` 目前**恒为 NULL**：Active/Candidate 尚未实现，
      当下的事实是「池内合格节点的全部来源订阅」→ `subscription_ids_json`。
    - `error_summary` 只放**固定枚举**计数（见 `jobruns.ERROR_KINDS`），不放自由文本：
      异常串可能带 URL/凭据，且长度无界。
    - 主键 `id` 就是作业身份，**不另造 `run_id`**（两套身份是本项目明令禁止的形态）。
    """

    __tablename__ = "proxy_job_runs"
    __table_args__ = (
        Index("ix_pjr_node_started", "selected_node", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # running | success | partial | failed | interrupted（由 jobruns 一处定义）
    status: Mapped[str] = mapped_column(String(16), index=True)
    # 预留触发来源（manual|scheduled|bundles|cli）；v1 一律 'crawl'（run_crawl 是唯一入口）
    kind: Mapped[str] = mapped_column(String(16), default="crawl")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    regions_json: Mapped[list | None] = mapped_column(JSON)
    workers: Mapped[int | None] = mapped_column(Integer)
    # 本轮固定使用的池入口（run 内不换端口）
    proxy_url: Mapped[str | None] = mapped_column(String(120))
    # 池文件内容哈希：回答「这一轮用的是哪一版池」，无需 generation 概念
    pool_sha256: Mapped[str | None] = mapped_column(String(64))
    pool_node_count: Mapped[int | None] = mapped_column(Integer)
    # 池内**已知**出口 IP 去重数；NULL = 尚未探测（与「0 个出口」不是一回事）
    pool_exit_ip_count: Mapped[int | None] = mapped_column(Integer)
    # GLOBAL 选中节点的 runtime_name（run 开始时读控制器取得；读不到为 NULL）
    selected_node: Mapped[str | None] = mapped_column(String(400))
    node_exit_ip: Mapped[str | None] = mapped_column(String(64))
    # 池内合格节点的全部来源订阅 + 各订阅最新 OK 快照（事实，不是「生产订阅」）
    subscription_ids_json: Mapped[list | None] = mapped_column(JSON)
    snapshot_ids_json: Mapped[list | None] = mapped_column(JSON)
    # **v1 恒 NULL**：留给 Active/Candidate 落地后回填（不预留 pool_generation——
    # 那个概念连事实源都还没有，等发布协议真落地再加列）
    active_subscription_id: Mapped[int | None] = mapped_column(Integer)
    task_count: Mapped[int | None] = mapped_column(Integer)
    success_count: Mapped[int | None] = mapped_column(Integer)
    error_count: Mapped[int | None] = mapped_column(Integer)
    error_summary: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
