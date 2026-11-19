"""proxies 域模型：代理池 + 走线日志。"""
from __future__ import annotations

from datetime import datetime
import time

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

ADMISSION_ACTIVE = "ACTIVE"
ADMISSION_CANDIDATE = "CANDIDATE"


class Proxy(Base):
    __tablename__ = "proxies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str | None] = mapped_column(String(100))
    scheme: Mapped[str] = mapped_column(String(10), default="http")
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    # 代理账号口令属长期凭据：与 Steam Cookie 同级，落库为 `enc1:` 密文。
    # 列宽按密文长度放宽（AES-GCM 密文 + base64 后远大于原口令）。
    username: Mapped[str | None] = mapped_column(String(255))
    password: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class ProxyEvent(Base):
    """走线日志：爬取/测试请求经过哪个出口（M1 粒度：任务级 + 测试级）。"""

    __tablename__ = "proxy_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime | None] = mapped_column(DateTime)
    kind: Mapped[str] = mapped_column(String(20), default="crawl")
    target: Mapped[str | None] = mapped_column(String(255))
    proxy_label: Mapped[str | None] = mapped_column(String(100))
    status_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class ProxySubscription(Base):
    """持久化订阅链接，按方式区分：clash（机场 yaml）/ plain（明文代理协议）。

    长期保存在本地库；clash 启动与节点导入都从这里取 URL。
    订阅无可用节点或资源失效时停用；资源与节点恢复后自动解除。
    """

    __tablename__ = "proxy_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(10))
    url: Mapped[str] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_imported_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_stats: Mapped[dict | None] = mapped_column(JSON)
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
    last_fetch_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_fetch_status: Mapped[str | None] = mapped_column(String(32))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(500))
    cache_url_identity: Mapped[str | None] = mapped_column(String(64))
    auto_refresh: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1"
    )

    def resource_block_reason(self, *, now: float | None = None) -> str | None:
        traffic = (self.last_stats or {}).get("traffic")
        values: dict[str, int] = {}
        for part in str(traffic or "").split(";"):
            key, sep, value = part.strip().partition("=")
            if sep:
                try:
                    values[key] = int(value.strip())
                except ValueError:
                    continue
        expiry = values.get("expire", 0)
        if expiry > 0 and expiry <= (time.time() if now is None else now):
            return "订阅已到期"
        total = values.get("total", 0)
        if total > 0 and values.get("upload", 0) + values.get("download", 0) >= total:
            return "订阅流量已用尽"
        return None
