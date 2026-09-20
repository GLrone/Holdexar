"""订阅抓取 → 快照 → diff。

职责边界（刻意收窄）：
- 只回答「这次订阅抓到了什么」，**不消费 Registry**（那是 P1.3 的事）。
- 只输出 ADDED / UPDATED / UNCHANGED / REMOVED。
- **REMOVED ≠ DELETE**：P1.2 只告诉 Registry「这个节点已不在最新快照里」，由 Registry
  把它转成 STALE。绝不在这里删除节点。
- 任何一步失败：不改 Registry、不覆盖最后一次健康快照。
- 正文必须是**合法 UTF-8**：非法字节按失败处理（绝不 `errors="replace"` 兜过去，
  否则二进制垃圾会被洗成一段看起来合法的 YAML 静默流进 Registry）。
- `Snapshot` 是不可变承诺（frozen + 只读映射 + 元组），不是修辞：P1.3 会把上一份
  快照当基线反复读，任何一处就地改写都会让「diff 的依据」与「落库的事实」分叉。

抓取链沿用 `clash_manager._download_attempts` 的思想（直连 → 内核 → 池），但**不依赖
尚未完成的 P1.3（池）**：池还没有可用节点时第三通道由调用方不传入而自然跳过。
"""
from __future__ import annotations

import copy
import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import httpx
import yaml
from sqlalchemy import select

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


class InvalidEncodingError(SnapshotError):
    """正文不是合法 UTF-8：明确失败，不生成快照、不碰 Registry。

    为什么不能 `decode("utf-8", "replace")` 兜过去：替换字符（U+FFFD）会把「通道
    塞了二进制垃圾 / 编码换了」洗成一段看起来合法的 YAML，非法字节于是静默流进
    Registry，事后无从追溯是哪一步引入的。这里刻意与 html 劫持**同档**处理：
    属通道级问题 → 换下一通道；全部通道都拿不到合法 UTF-8 才失败。
    """

    def __init__(self, attempts: list["FetchAttempt"] | None = None, *, detail: str = "") -> None:
        self.attempts = attempts or []
        self.detail = detail
        super().__init__(f"订阅正文不是合法 UTF-8：{detail or '全部通道均未取得可解码内容'}")


class SnapshotRecoveryError(SnapshotError):
    """历史快照行在库里、原始字节却读不回或已不可解析。

    fail-closed：调用方拿到这个异常就该中止本轮刷新，**不能**把它当作「没有上一份
    快照」——那会让整批节点被判成新增、并把 REMOVED 判空，本该退休的节点永远不退休。
    """

    def __init__(self, subscription_id: int, detail: str) -> None:
        self.subscription_id = subscription_id
        self.detail = detail
        super().__init__(f"订阅 {subscription_id} 的上一份快照无法恢复：{detail}")


# ── 数据对象 ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class Channel:
    """一条抓取通道。proxy=None 即直连。"""

    name: str
    proxy: str | None = None


@dataclass(frozen=True)
class FetchAttempt:
    """一次通道尝试的留痕。frozen：它是失败报告的证物，事后被改写就失去意义。"""

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


@dataclass(frozen=True)
class Snapshot:
    """一次成功抓取的**不可变**产物。

    `nodes` / `attempts` 在 `__post_init__` 里就地归一成元组 + 只读映射，所以
    「不可变」对**任何**构造路径都成立，不靠调用方自觉传只读容器：
    `snap.nodes.append(...)` 与 `snap.nodes[0]["uuid"] = ...` 都会当场报错，
    而不是「悄悄改了基线，diff 结果与库里的事实对不上」。

    只读映射只封顶层：`node_fingerprint` 要走 `json.dumps`，嵌套值保持普通 dict
    / list（深拷贝进来的私有副本，外部持有的原对象改了也不影响本快照）。
    """

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
    nodes: tuple[Mapping[str, Any], ...]
    attempts: tuple[FetchAttempt, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "nodes",
            tuple(MappingProxyType(copy.deepcopy(dict(n))) for n in self.nodes),
        )
        object.__setattr__(self, "attempts", tuple(self.attempts))


@dataclass(frozen=True)
class NodeUpdate:
    """端点未变、配置已变的节点：旧指纹被这条新配置**取代**了。

    为什么必须把旧指纹显式带出来：`removed` 从 P1.2.1 起不再包含「被取代」的旧
    指纹（见 `compute_diff`），旧指纹只能从这里取。P1.3 需要它把来源关联与健康
    /容量历史从旧行搬到新行——没有它就只剩「新登记一条 + 旧行靠来源流失自然
    退休」，同一逻辑节点的历史会被腰斩。

    `previous_fingerprint` 是**上一份快照里**的指纹，不是 Registry 里那一行的
    现值；两者在连续多次变更后可能不同，调用方按指纹查行、查不到就当新增处理。
    """

    previous_fingerprint: str
    node: dict


@dataclass
class Diff:
    """相对上一份快照的差异。**removed 是 STALE 候选，不是删除指令。**

    不变量：`updated` 携带的 previous_fingerprint 与 `removed` **交集为空**，
    且 `unchanged` 与 `removed` 交集为空——同一个逻辑节点不允许既被更新又被
    判为消失（P1.3 会先按 UPDATED 更新、再按 REMOVED 标 STALE，注册表当场就错）。

    注意本对象是**报告**而非指令集：P1.3 对来源集合要按当前快照整体替换
    （sources(S) = {fingerprint(n) for n in N}），不要按 diff 逐条重放。
    """

    added: list[dict] = field(default_factory=list)      # 新增节点配置
    updated: list[NodeUpdate] = field(default_factory=list)  # 端点相同、指纹变化
    unchanged: list[str] = field(default_factory=list)   # 指纹
    removed: list[str] = field(default_factory=list)     # 指纹 → Registry 转 STALE
    unchanged_snapshot: bool = False                     # 与上份快照字节相同
    node_count: int = 0


# ── 格式判定 ──────────────────────────────────────────────────────
def decode_utf8(raw: bytes) -> str:
    """严格 UTF-8 解码：非法字节一律抛 `InvalidEncodingError`。

    订阅正文是文本协议（YAML/base64/URI），拿到的字节解不出 UTF-8 只可能是通道
    问题（劫持页被塞了二进制、编码非 UTF-8）。此处不提供任何降级通道——
    `errors="replace"` 正是把这类错误洗白的入口。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise InvalidEncodingError(detail=f"偏移 {e.start}：{e.reason}") from e


def detect_format(raw: bytes) -> str:
    """判定订阅正文格式。只做判定，不做解析。正文非合法 UTF-8 即抛异常。"""
    if not raw:
        return FORMAT_EMPTY
    text = decode_utf8(raw)
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

    通道级失败（非 200 / 传输错误 / **正文非合法 UTF-8**）→ 换下一通道。
    拿到了 200 但格式由通道决定（如 html 劫持页）→ 也换下一通道，因为那多半是这条
    通道被劫持，另一条通道可能拿到真内容。
    拿到了 200 且格式由源决定（yaml / base64 / age …）→ 停，交调用方判定。

    全部通道失败时的异常优先级：有「可解码但本模块不处理」的兜底内容 →
    `UnsupportedFormatError`（内容是真的，只是不支持，报这个更有指向性）；
    否则曾遇到非法 UTF-8 → `InvalidEncodingError`；否则 `FetchFailedError`。
    """
    attempts: list[FetchAttempt] = []
    fallback: FetchResult | None = None
    bad_encoding = False

    for ch in channels:
        try:
            async with client_factory(ch) as client:
                resp = await client.get(url, headers={"User-Agent": SUB_USER_AGENT})
        except Exception as e:  # noqa: BLE001
            attempts.append(FetchAttempt(ch.name, False, None, FORMAT_UNKNOWN,
                                         type(e).__name__))
            continue
        raw = resp.content
        if not (resp.status_code == 200 and raw):
            fmt = FORMAT_UNKNOWN
            try:
                fmt = detect_format(raw)
            except InvalidEncodingError:
                pass  # 非 200 的响应体是什么都无所谓，按通道失败记
            attempts.append(FetchAttempt(ch.name, False, resp.status_code, fmt))
            continue

        try:
            fmt = detect_format(raw)
        except InvalidEncodingError as e:
            # 200 却拿不到合法 UTF-8：与 html 劫持同档的通道级问题，换下一通道
            attempts.append(FetchAttempt(ch.name, False, resp.status_code, FORMAT_UNKNOWN,
                                         f"非法 UTF-8（{e.detail}），换下一通道"))
            bad_encoding = True
            continue

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

    if fallback is not None:
        raise UnsupportedFormatError(fallback.fmt, attempts)
    if bad_encoding:
        raise InvalidEncodingError(attempts)
    raise FetchFailedError(url, attempts)


# ── 解析 ──────────────────────────────────────────────────────────
def parse_nodes(raw: bytes, fmt: str) -> list[dict]:
    """解析节点列表。P1.2 只处理 inline YAML；其余格式一律抛 UnsupportedFormatError。

    与 `detect_format` 同一套严格解码：解析入口不得成为「非法字节洗白」的第二条路。
    """
    if fmt != FORMAT_YAML:
        raise UnsupportedFormatError(fmt)
    doc = yaml.safe_load(decode_utf8(raw))
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
    """把快照落盘 + 写库。**调用方负责事务边界**：这里只 flush，绝不 commit。

    为什么底层不吃事务：P1.3 要求「快照元数据 + Registry 变更 + generation 推进」
    在同一个事务里原子落地，helper 自己 commit 会让后面两者无法纳入边界；更糟的
    是它会把调用方会话里**其它** pending 行一起提交掉——提交一个自己都不知道
    存在的写集，回滚时那部分永远回不来。

    落盘排在 flush 之前：文件是幂等的内容寻址写入，事务回滚后留个孤儿文件（由
    P1.9 的 GC 规则按 raw_path 引用回收）远好过「事务提交了但原始字节没落盘」
    ——后者会让 `latest_snapshot()` 永远恢复不出基线。
    本函数不碰 Registry。
    """
    path = _write_raw(data_dir, snap.subscription_id, snap.sha256, snap.raw_content)
    row = SubscriptionSnapshot(
        subscription_id=snap.subscription_id,
        sha256=snap.sha256,
        format=snap.fmt,
        raw_path=str(path),
        # URL 是快照的 provenance：换链接后靠它把"旧链接的成功快照"排除掉
        url=snap.url,
        node_count=snap.node_count,
        status="OK",
        http_status=snap.http_status,
        content_type=snap.content_type,
        source_channel=snap.source_channel,
        fetched_at=snap.fetched_at,
    )
    session.add(row)
    await session.flush()
    return row


async def latest_snapshot(
    session, subscription_id: int, *, url: str = "",
) -> Snapshot | None:
    """从最近一条成功快照行恢复出 `Snapshot`——P1.3「上一份」的唯一入口。

    P1.2 只把元数据写库、原始字节按 sha 落在磁盘上，所以恢复要三步：取最新 OK
    行 → 读回 `raw_path` → 严格解码并重新解析节点。少了这个出口，每个调用方都会
    自己重拼一遍（并很容易拼丢严格解码、`node_count` 口径、sha 校验）。

    - `url` 非空时**只认从该 URL 抓下来的成功快照**：订阅换链接等于换了一个机场
      （见 `update_subscription`），旧链接的成功快照不得被当成当前订阅的事实——
      否则会出现「订阅 url=B、Registry 却来自 Snapshot(A)」，而且返回的对象还会
      谎报 `url=B`。当前 URL 没有成功快照 → `None`（调用方按「还需重新成功抓取」处理）；
    - 没有行 → `None`（首次订阅，调用方按「全部 ADDED」处理）；
    - 行在、字节读不回 / sha 对不上 / 已不可解析 → `SnapshotRecoveryError`
      （fail-closed，见该异常说明：静默当成没有上一份会让 REMOVED 判空）。
    """
    stmt = (
        select(SubscriptionSnapshot)
        .where(SubscriptionSnapshot.subscription_id == subscription_id)
        .where(SubscriptionSnapshot.status == "OK")
        .order_by(SubscriptionSnapshot.id.desc())
        .limit(1)
    )
    if url:
        stmt = stmt.where(SubscriptionSnapshot.url == url)
    row = await session.scalar(stmt)
    if row is None:
        return None
    if not row.raw_path:
        raise SnapshotRecoveryError(subscription_id, "快照行没有 raw_path")

    path = Path(row.raw_path)
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise SnapshotRecoveryError(
            subscription_id, f"原始字节读不回：{path}（{type(e).__name__}）"
        ) from e

    sha = hashlib.sha256(raw).hexdigest()
    if sha != row.sha256:
        # 内容寻址的意义就在这一步：文件被覆盖/截断时，diff 的「字节相同」快路径
        # 会拿一个错误的基线判定 unchanged，必须当场拦住
        raise SnapshotRecoveryError(
            subscription_id,
            f"原始字节与快照 sha 不符：文件 {sha[:12]} != 记录 {row.sha256[:12]}",
        )
    try:
        fmt = detect_format(raw)
        nodes = parse_nodes(raw, row.format or fmt)
    except SnapshotError as e:
        raise SnapshotRecoveryError(subscription_id, f"原始字节已不可解析：{e}") from e

    return Snapshot(
        subscription_id=row.subscription_id,
        # 真实的来源 URL 来自快照行本身——不能回显调用方的入参，否则换链接后
        # 会把旧链接的内容标成新链接（调用方无从察觉）
        url=row.url or url,
        fetched_at=row.fetched_at or datetime.now(),
        http_status=row.http_status or 0,
        content_type=row.content_type or "",
        sha256=sha,
        raw_content=raw,
        fmt=row.format or fmt,
        node_count=len(nodes),
        source_channel=row.source_channel or "",
        nodes=nodes,
    )


# ── Diff ──────────────────────────────────────────────────────────
def compute_diff(previous: Snapshot | None, nodes: list[dict], sha256: str) -> Diff:
    """与上一份快照比对。

    previous 为 None（首次）→ 全部 ADDED。
    字节完全一致 → 全部 UNCHANGED，不产生 UPDATED（避免无意义的写放大）。

    互斥性不变量：`updated` 的 previous_fingerprint 与 `removed` 交集为空。
    旧实现用 `prev_fp - 当前指纹集` 算 removed，于是「端点没变、凭据/参数变了」
    的节点会**同时**落进 UPDATED 与 REMOVED——P1.3 先按 UPDATED 更新注册表、再
    按 REMOVED 把同一个节点标 STALE，注册表当场就是错的。故 removed 重定义为
    「上一份里**没有被任何当前节点接管**的指纹」，被取代的旧指纹只出现在
    `updated[].previous_fingerprint` 里。
    """
    if previous is not None and previous.sha256 == sha256 and previous.node_count == len(nodes):
        return Diff(unchanged=[node_fingerprint(n) for n in nodes],
                    unchanged_snapshot=True, node_count=len(nodes))

    prev_fp = {node_fingerprint(n) for n in previous.nodes} if previous else set()
    cur_map = _key_map(nodes)
    prev_map = _key_map(previous.nodes) if previous else {}

    added: list[dict] = []
    updated: list[NodeUpdate] = []
    unchanged: list[str] = []
    matched: set[str] = set()  # 已被当前快照接管的上一份指纹（含被取代的旧指纹）
    for key, node in cur_map.items():
        fp = node_fingerprint(node)
        if fp in prev_fp:
            unchanged.append(fp)
            matched.add(fp)
        elif key in prev_map:
            superseded = node_fingerprint(prev_map[key])
            updated.append(NodeUpdate(previous_fingerprint=superseded, node=node))
            matched.add(superseded)
        else:
            added.append(node)

    removed = sorted(prev_fp - matched)
    return Diff(added=added, updated=updated, unchanged=unchanged, removed=removed,
                unchanged_snapshot=False, node_count=len(nodes))
