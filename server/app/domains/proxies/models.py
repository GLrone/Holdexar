"""proxies 域模型：代理池 + 走线日志。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# ── 订阅级「生产准入」────────────────────────────────────────────────
# 它只回答一个问题：**这条订阅的节点是否获准进入 Registry / 生产池**。
# **不是**「当前内核在跑哪条订阅」，**不是** Runtime GLOBAL 的选择，也**不是**
# 节点级健康（那是 `ProxyNode.state`）。
# - `ACTIVE`   ：获准进入生产——订阅同步会把它 apply 进 Registry；
# - `CANDIDATE`：新来源尚未获准——只抓取 + 落快照，不进 Registry
#                （见 `domains/proxypool/admission.py`）。
# 与 `deprecated`（旧来源的可用性弃用）**完全独立**：两者互不转换、互不跟随。
ADMISSION_ACTIVE = "ACTIVE"
ADMISSION_CANDIDATE = "CANDIDATE"
ADMISSION_STATUSES = (ADMISSION_ACTIVE, ADMISSION_CANDIDATE)


class Proxy(Base):
    __tablename__ = "proxies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str | None] = mapped_column(String(100))
    scheme: Mapped[str] = mapped_column(String(10), default="http")  # http | socks5
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    username: Mapped[str | None] = mapped_column(String(100))
    password: Mapped[str | None] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="unknown")  # ok|failed|unknown
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)

    def url(self) -> str:
        auth = f"{self.username}:{self.password}@" if self.username else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"


class ProxyEvent(Base):
    """走线日志：爬取/测试请求经过哪个出口（M1 粒度：任务级 + 测试级）。"""

    __tablename__ = "proxy_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime | None] = mapped_column(DateTime)
    kind: Mapped[str] = mapped_column(String(20), default="crawl")  # crawl | test | clash
    target: Mapped[str | None] = mapped_column(String(255))
    proxy_label: Mapped[str | None] = mapped_column(String(100))  # direct 或代理标签
    status_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class ProxySubscription(Base):
    """持久化订阅链接，按方式区分：clash（机场 yaml）/ plain（明文代理协议）。

    长期保存在本地库；clash 启动与节点导入都从这里取 URL。
    废弃终态（deprecated）：不可用节点 >95% 时标记，后端选订阅时跳过
    （不删除——用户手动删）；下次检测恢复达标自动解除。
    """

    __tablename__ = "proxy_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(10))  # clash | plain
    url: Mapped[str] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_imported_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_stats: Mapped[dict | None] = mapped_column(JSON)  # 最近一次导入/检测结果
    deprecated: Mapped[bool] = mapped_column(Boolean, default=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime)
    deprecated_reason: Mapped[str | None] = mapped_column(String(200))
    # 生产准入（见文件头常量说明）。**模型默认 CANDIDATE**：新行一律先当候选，
    # 显式晋升才进生产——产品新增订阅走 `add_subscription`（显式 CANDIDATE）。
    # 库层默认 `'ACTIVE'` 只服务历史行兼容（`_TABLE_EXTRA_COLUMNS` 的 ALTER 会把
    # 已存在的行填成 ACTIVE）；旧 KV 迁移路径显式写 ACTIVE（用户原有在用订阅）。
    admission_status: Mapped[str] = mapped_column(
        String(16), default=ADMISSION_CANDIDATE, server_default=ADMISSION_ACTIVE
    )
    # ── proxypool 抓取 / 快照元数据（仅增列，不影响既有代理链路）──
    last_fetch_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_fetch_status: Mapped[str | None] = mapped_column(String(32))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(500))
    snapshot_sha256: Mapped[str | None] = mapped_column(String(64))
    snapshot_version: Mapped[int] = mapped_column(Integer, default=0)


class ClashNode(Base):
    """Clash 节点级状态账本（2026-09 节点状态存储）。

    每次 test_clash_nodes 的逐节点结果 UPSERT 到这里；跨重启持久，
    体检门槛（6h）、冷却递增、dead 三连复活都以此表为唯一事实源。
    status: ok（通 Steam）| dead（累计失败转终态，3-of-3 连测通过才复活）
    """

    __tablename__ = "clash_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int] = mapped_column(Integer)  # proxy_subscriptions.id
    name: Mapped[str] = mapped_column(String(255))
    exit_ip: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(10), default="unknown")  # ok|dead|unknown
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)  # 累计失败（10 → dead）
    revive_passes: Mapped[int] = mapped_column(Integer, default=0)  # dead 后连续通过计数（3 → 复活）
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime)  # 冷却期内跳过检测
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
