"""订阅抓取 → 快照 → diff。

职责边界（刻意收窄）：
- 只回答「这次订阅抓到了什么」，**不消费 Registry**（那是 P1.3 的事）。
- 只输出 ADDED / UPDATED / UNCHANGED / REMOVED。
- **REMOVED ≠ DELETE**：P1.2 只告诉 Registry「这个节点已不在最新快照里」，由 Registry
  把它转成 STALE。绝不在这里删除节点。
- 任何一步失败：不改 Registry、不覆盖最后一次健康快照。

抓取链沿用 `clash_manager._download_attempts` 的思想（直连 → 内核 → 池），但**不依赖
尚未完成的 P1.3（池）**：池还没有可用节点时第三通道由调用方不传入而自然跳过。
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import yaml

from app.domains.proxies.kernel_release import MIHOMO_VERSION
from app.domains.proxypool.models import SubscriptionSnapshot, node_fingerprint

# UA 必须与内核拉取 provider 时同款：面板按 UA 分流订阅格式，不含 clash 关键字会
# 回落 base64 节点表，个别机场（已验证）对非 clash UA 直接 404。
# 版本从 kernel_release 单一来源取，本模块不得重写版本号。
SUB_USER_AGENT = f"clash.meta/{MIHOMO_VERSION.lstrip('v')}"

DEFAULT_FETCH_TIMEOUT = 30.0

# ── 格式 ──────────────────────────────────────────────────────────
FORMAT_YAML = "yaml"                  # ✅ inline proxies —— P1.2 唯一端到端承诺
FORMAT_PROVIDER_YAML = "provider_yaml"  # ❌ proxy-providers 形态，明确拒绝
FORMAT_BASE64 = "base64"              # ⚠ 识别 + 扩展点
FORMAT_URI = "uri"                    # ⚠ 识别 + 扩展点
FORMAT_AGE = "age"                    # ❌ 明确拒绝
FORMAT_HTML = "html"
FORMAT_JSON = "json"
FORMAT_EMPTY = "empty"
FORMAT_UNKNOWN = "unknown"

PARSEABLE_FORMATS = frozenset({FORMAT_YAML})
# 由订阅源决定的格式：换通道拿到的也是同一份，不需要再试下一个通道
SOURCE_DETERMINED_FORMATS = frozenset({
    FORMAT_YAML, FORMAT_PROVIDER_YAML, FORMAT_BASE64, FORMAT_URI, FORMAT_AGE, FORMAT_JSON,
})

CHANNEL_DIRECT = "direct"
CHANNEL_KERNEL = "mihomo"
CHANNEL_POOL = "proxypool"


# ── 错误类型（稳定、可断言）──────────────────────────────────────
class SnapshotError(Exception):
    """订阅快照失败基类。任何一种都意味着：不改 Registry、不覆盖旧快照。"""


class FetchFailedError(SnapshotError):
    """所有通道都没拿到可判定内容。"""

    def __init__(self, url: str, attempts: list["FetchAttempt"]) -> None:
        self.url = url
        self.attempts = attempts
        shown = "、".join(f"{a.channel}:{a.http_status or a.error}" for a in attempts)
        super().__init__(f"订阅抓取失败（{len(attempts)} 通道）：{shown}")


class UnsupportedFormatError(SnapshotError):
    """内容拿到了但格式不归本模块处理。"""

    def __init__(self, fmt: str, attempts: list["FetchAttempt"] | None = None) -> None:
        self.format = fmt
        self.attempts = attempts or []
        super().__init__(f"不支持的订阅格式：{fmt}")


class EmptyNodeSetError(SnapshotError):
    """格式合法但解析出 0 个节点——按失败处理，不覆盖旧快照。"""

    def __init__(self, fmt: str) -> None:
        self.format = fmt
        super().__init__(f"订阅解析出 0 个节点（{fmt}），按失败处理")


# ── 数据对象 ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class Channel:
    """一条抓取通道。proxy=None 即直连。"""

    name: str
    proxy: str | None = None


@dataclass
class FetchAttempt:
    channel: str
    ok: bool
    http_status: int | None
    fmt: str
    error: str | None = None


@dataclass
class FetchResult:
    raw: bytes
    http_status: int
    content_type: str
    channel: str
    fmt: str
    attempts: list[FetchAttempt] = field(default_factory=list)


@dataclass
class Snapshot:
    """一次成功抓取的不可变产物。"""

    subscription_id: int
    url: str
    fetched_at: datetime
    http_status: int
    content_type: str
    sha256: str
    raw_content: bytes
    fmt: str
    node_count: int
    source_channel: str
    nodes: list[dict]
    attempts: list[FetchAttempt] = field(default_factory=list)


@dataclass
class Diff:
    """相对上一份快照的差异。**removed 是 STALE 候选，不是删除指令。**"""

    added: list[dict] = field(default_factory=list)      # 新增节点配置
    updated: list[dict] = field(default_factory=list)    # 端点相同、指纹变化
    unchanged: list[str] = field(default_factory=list)   # 指纹
    removed: list[str] = field(default_factory=list)     # 指纹 → Registry 转 STALE
    unchanged_snapshot: bool = False                     # 与上份快照字节相同
    node_count: int = 0


# ── 格式判定 ──────────────────────────────────────────────────────
def detect_format(raw: bytes) -> str:
    """判定订阅正文格式。只做判定，不做解析。"""
    if not raw:
        return FORMAT_EMPTY
    text = raw.decode("utf-8", "replace")
    if "-----BEGIN AGE ENCRYPTED FILE-----" in text[:2000]:
        return FORMAT_AGE
    head = text.lstrip()[:400]
    if head.startswith("<"):
        return FORMAT_HTML
    if head.startswith("{") or head.startswith("["):
        return FORMAT_JSON
    if re.search(r"^proxies\s*:", text, re.MULTILINE):
        return FORMAT_YAML
    if re.search(r"^proxy-providers\s*:", text, re.MULTILINE):
        return FORMAT_PROVIDER_YAML
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:5]
    if lines and all(re.match(r"^[a-z0-9+.\-]+://", ln) for ln in lines):
        return FORMAT_URI
    body = re.sub(r"\s+", "", text)
    if len(body) > 40 and re.fullmatch(r"[A-Za-z0-9+/=]+", body):
        return FORMAT_BASE64
    return FORMAT_UNKNOWN


# ── 抓取 ──────────────────────────────────────────────────────────
def build_channels(
    *,
    kernel_proxy: str | None = None,
    pool_proxy: str | None = None,
) -> list[Channel]:
    """直连 → 现有 Mihomo 内核 → proxypool 已可用代理。

    后两通道未就绪时**自然跳过**：P1.2 不得依赖尚未落地的 P1.3 池。
    """
    channels = [Channel(CHANNEL_DIRECT, None)]
    if kernel_proxy:
        channels.append(Channel(CHANNEL_KERNEL, kernel_proxy))
    if pool_proxy:
        channels.append(Channel(CHANNEL_POOL, pool_proxy))
    return channels


def default_client_factory(channel: Channel, *, timeout: float = DEFAULT_FETCH_TIMEOUT
                           ) -> httpx.AsyncClient:
    """生产默认：按通道的 proxy 建客户端。"""
    return httpx.AsyncClient(timeout=timeout, proxy=channel.proxy, follow_redirects=True)


async def fetch_subscription(
    url: str,
    channels: list[Channel],
    *,
    client_factory=default_client_factory,
) -> FetchResult:
    """按通道顺序抓取，返回第一份**可判定**的内容。

    通道级失败（非 200 / 传输错误）→ 换下一通道。
    拿到了 200 但格式由通道决定（如 html 劫持页）→ 也换下一通道，因为那多半是这条
    通道被劫持，另一条通道可能拿到真内容。
    拿到了 200 且格式由源决定（yaml / base64 / age …）→ 停，交调用方判定。
    """
    attempts: list[FetchAttempt] = []
    fallback: FetchResult | None = None

    for ch in channels:
        try:
            async with client_factory(ch) as client:
                resp = await client.get(url, headers={"User-Agent": SUB_USER_AGENT})
        except Exception as e:  # noqa: BLE001
            attempts.append(FetchAttempt(ch.name, False, None, FORMAT_UNKNOWN,
                                         type(e).__name__))
            continue
        raw = resp.content
        fmt = detect_format(raw)
        if resp.status_code == 200 and raw:
            result = FetchResult(
                raw=raw,
                http_status=resp.status_code,
                content_type=resp.headers.get("content-type", ""),
                channel=ch.name,
                fmt=fmt,
                attempts=attempts + [FetchAttempt(ch.name, fmt == FORMAT_YAML,
                                                  resp.status_code, fmt)],
            )
            if fmt in SOURCE_DETERMINED_FORMATS:
                return result
            fallback = result
            attempts.append(FetchAttempt(ch.name, False, resp.status_code, fmt,
                                         "格式由通道决定且不可用，换下一通道"))
            continue
        attempts.append(FetchAttempt(ch.name, False, resp.status_code, fmt))

    if fallback is not None:
        raise UnsupportedFormatError(fallback.fmt, attempts)
    raise FetchFailedError(url, attempts)


# ── 解析 ──────────────────────────────────────────────────────────
def parse_nodes(raw: bytes, fmt: str) -> list[dict]:
    """解析节点列表。P1.2 只处理 inline YAML；其余格式一律抛 UnsupportedFormatError。"""
    if fmt != FORMAT_YAML:
        raise UnsupportedFormatError(fmt)
    doc = yaml.safe_load(raw.decode("utf-8", "replace"))
    if not isinstance(doc, dict):
        raise UnsupportedFormatError(fmt)
    if isinstance(doc.get("proxy-providers"), dict) and not doc.get("proxies"):
        raise UnsupportedFormatError(FORMAT_PROVIDER_YAML)
    px = doc.get("proxies")
    nodes = [p for p in px if isinstance(p, dict) and p.get("name")] if isinstance(px, list) else []
    if not nodes:
        raise EmptyNodeSetError(fmt)
    return nodes


def node_key(config: dict) -> str:
    """端点身份 `type|server|port`。

    与 fingerprint（全配置哈希、Registry 主键）分工：凭据/参数轮换时指纹变、端点键
    不变，据此把它识别为 UPDATED 而不是「删一条 + 加一条」。
    """
    return f"{config.get('type', '')}|{config.get('server', '')}|{config.get('port', '')}"


def _key_map(nodes: list[dict]) -> dict[str, dict]:
    """端点键 → 节点。同一快照内端点键撞车时退回指纹，避免互相覆盖。"""
    counts = Counter(node_key(n) for n in nodes)
    return {
        (node_fingerprint(n) if counts[node_key(n)] > 1 else node_key(n)): n
        for n in nodes
    }


# ── 快照 ──────────────────────────────────────────────────────────
def snapshot_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "proxypool" / "snapshots"


def _write_raw(data_dir: Path, subscription_id: int, sha: str, raw: bytes) -> Path:
    """内容寻址落盘：相同内容只存一份，天然去重且可回溯历史版本。"""
    d = snapshot_dir(data_dir) / str(subscription_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sha}.bin"
    if not path.is_file():
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(raw)
        tmp.replace(path)
    return path


def build_snapshot(subscription_id: int, url: str, result: FetchResult,
                   nodes: list[dict], *, now: datetime | None = None) -> Snapshot:
    return Snapshot(
        subscription_id=subscription_id,
        url=url,
        fetched_at=now or datetime.now(),
        http_status=result.http_status,
        content_type=result.content_type,
        sha256=hashlib.sha256(result.raw).hexdigest(),
        raw_content=result.raw,
        fmt=result.fmt,
        node_count=len(nodes),
        source_channel=result.channel,
        nodes=nodes,
        attempts=result.attempts,
    )


async def persist_snapshot(session, snap: Snapshot, *, data_dir: Path
                           ) -> SubscriptionSnapshot:
    """把快照落盘 + 写库。调用方负责事务；本函数不碰 Registry。"""
    path = _write_raw(data_dir, snap.subscription_id, snap.sha256, snap.raw_content)
    row = SubscriptionSnapshot(
        subscription_id=snap.subscription_id,
        sha256=snap.sha256,
        format=snap.fmt,
        raw_path=str(path),
        node_count=snap.node_count,
        status="OK",
        http_status=snap.http_status,
        content_type=snap.content_type,
        source_channel=snap.source_channel,
        fetched_at=snap.fetched_at,
    )
    session.add(row)
    await session.commit()
    return row


# ── Diff ──────────────────────────────────────────────────────────
def compute_diff(previous: Snapshot | None, nodes: list[dict], sha256: str) -> Diff:
    """与上一份快照比对。

    previous 为 None（首次）→ 全部 ADDED。
    字节完全一致 → 全部 UNCHANGED，不产生 UPDATED（避免无意义的写放大）。
    """
    if previous is not None and previous.sha256 == sha256 and previous.node_count == len(nodes):
        return Diff(unchanged=[node_fingerprint(n) for n in nodes],
                    unchanged_snapshot=True, node_count=len(nodes))

    prev_fp = {node_fingerprint(n) for n in previous.nodes} if previous else set()
    cur_map = _key_map(nodes)
    prev_map = _key_map(previous.nodes) if previous else {}

    added: list[dict] = []
    updated: list[dict] = []
    unchanged: list[str] = []
    for key, node in cur_map.items():
        fp = node_fingerprint(node)
        if fp in prev_fp:
            unchanged.append(fp)
        elif key in prev_map:
            updated.append(node)
        else:
            added.append(node)

    removed = sorted(prev_fp - {node_fingerprint(n) for n in nodes})
    return Diff(added=added, updated=updated, unchanged=unchanged, removed=removed,
                unchanged_snapshot=False, node_count=len(nodes))
