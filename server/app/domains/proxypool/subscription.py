"""统一订阅抓取、内容校验和不可变快照。

网络请求在写入快照或节点注册之前完成。任何失败都保留最后一次成功快照。
"""
from __future__ import annotations

import copy
import hashlib
import asyncio
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import httpx
import yaml
from sqlalchemy import select

from app.domains.proxies.kernel_release import MIHOMO_VERSION
from app.domains.proxies.subscription_secret import open_url, seal_url, url_matches
from app.domains.proxypool.models import (
    SubscriptionSnapshot,
    is_info_placeholder,
)

# UA 必须与内核拉取 provider 时同款：面板按 UA 分流订阅格式，不含 clash 关键字会
# 回落 base64 节点表，个别机场（已验证）对非 clash UA 直接 404。
# 版本从 kernel_release 单一来源取，本模块不得重写版本号。
SUB_USER_AGENT = f"clash.meta/{MIHOMO_VERSION.lstrip('v')}"

DEFAULT_FETCH_TIMEOUT = 12.0
DEFAULT_SUBSCRIPTION_DEADLINE = 24.0
DEFAULT_HEDGE_DELAY = 0.25
DEFAULT_FETCH_PARALLELISM = 2

# ── 格式 ──────────────────────────────────────────────────────────
FORMAT_YAML = "yaml"
FORMAT_PROVIDER_YAML = "provider_yaml"
FORMAT_BASE64 = "base64"
FORMAT_URI = "uri"
FORMAT_AGE = "age"
FORMAT_HTML = "html"
FORMAT_JSON = "json"
FORMAT_EMPTY = "empty"
FORMAT_UNKNOWN = "unknown"

CHANNEL_DIRECT = "direct"
CHANNEL_KERNEL = "mihomo"
CHANNEL_POOL = "proxypool"
CHANNEL_LOCAL = "local"


def saved_proxy_egress_id(proxy: str) -> str:
    """Return a stable opaque identity for a stored proxy URL."""
    return f"saved:{hashlib.sha256(proxy.encode('utf-8')).hexdigest()[:32]}"


def saved_proxy_channels(proxies: list[str]) -> list[Channel]:
    channels: list[Channel] = []
    seen: set[str] = set()
    for proxy in proxies:
        if proxy and proxy not in seen:
            seen.add(proxy)
            channels.append(Channel("saved", proxy, saved_proxy_egress_id(proxy)))
    return channels


# ── 错误类型（稳定、可断言）──────────────────────────────────────
class SnapshotError(Exception):
    """订阅快照失败基类。任何一种都意味着：不改 Registry、不覆盖旧快照。"""


class FetchFailedError(SnapshotError):
    """所有通道都没拿到可判定内容。"""

    def __init__(self, url: str, attempts: list["FetchAttempt"],
                 duration_ms: int = 0) -> None:
        self.url = url
        self.attempts = attempts
        self.duration_ms = max(0, int(duration_ms))
        shown = "、".join(f"{a.channel}:{a.http_status or a.error}" for a in attempts)
        super().__init__(f"订阅抓取失败（{len(attempts)} 通道）：{shown}")


class UnsupportedFormatError(SnapshotError):
    """内容拿到了但格式不归本模块处理。"""

    def __init__(self, fmt: str, attempts: list["FetchAttempt"] | None = None,
                 duration_ms: int = 0) -> None:
        self.format = fmt
        self.attempts = attempts or []
        self.duration_ms = max(0, int(duration_ms))
        messages = {
            "provider_proxy_group_unsupported":
                "订阅节点来源引用了同一配置中的代理组作为下载出口；该出口在节点注册前不可用",
            "provider_proxy_unavailable": "订阅节点来源指定的代理出口当前不可用",
            "provider_direct_route_unavailable": "订阅节点来源要求直连，但当前没有直连通道",
            "provider_proxy_rejected": "订阅节点来源的代理设置拒绝连接",
        }
        super().__init__(messages.get(fmt, f"不支持的订阅格式：{fmt}"))


class EmptyNodeSetError(SnapshotError):
    """格式合法但解析出 0 个节点——按失败处理，不覆盖旧快照。"""

    def __init__(self, fmt: str, attempts: list["FetchAttempt"] | None = None,
                 duration_ms: int = 0) -> None:
        self.format = fmt
        self.attempts = attempts or []
        self.duration_ms = max(0, int(duration_ms))
        super().__init__(f"订阅解析出 0 个节点（{fmt}），按失败处理")


class InvalidEncodingError(SnapshotError):
    """正文不是合法 UTF-8：明确失败，不生成快照、不碰 Registry。

    不能 `decode("utf-8", "replace")` 兜过去：替换字符（U+FFFD）会把「通道
    塞了二进制垃圾 / 编码换了」洗成一段看起来合法的 YAML，非法字节于是静默流进
    Registry，事后无从追溯是哪一步引入的。这里刻意与 html 劫持**同档**处理：
    属通道级问题 → 换下一通道；全部通道都拿不到合法 UTF-8 才失败。
    """

    def __init__(self, attempts: list["FetchAttempt"] | None = None, *, detail: str = "",
                 duration_ms: int = 0) -> None:
        self.attempts = attempts or []
        self.detail = detail
        self.duration_ms = max(0, int(duration_ms))
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


class ProviderResolutionError(SnapshotError):
    """Provider YAML was valid but its referenced node set could not be resolved."""

    def __init__(
        self, code: str, stage: str = "provider",
        attempts: list["FetchAttempt"] | None = None,
    ) -> None:
        self.code = code
        self.stage = stage
        self.attempts = attempts or []
        super().__init__(code)


# ── 数据对象 ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class Channel:
    """一条抓取通道。proxy=None 即直连。"""

    name: str
    proxy: str | None = None
    egress_id: str | None = None


@dataclass(frozen=True)
class FetchAttempt:
    """一次通道尝试的留痕。frozen：它是失败报告的证物，事后被改写就失去意义。"""

    channel: str
    ok: bool
    http_status: int | None
    fmt: str
    error: str | None = None
    stage: str = ""
    elapsed_ms: int = 0
    egress_id: str | None = None


@dataclass
class FetchResult:
    raw: bytes
    http_status: int
    content_type: str
    channel: str
    fmt: str
    attempts: list[FetchAttempt] = field(default_factory=list)
    userinfo: str | None = None
    profile_title: str | None = None
    content_disposition: str | None = None
    egress_id: str | None = None
    duration_ms: int = 0
    resolved_nodes: list[dict] = field(default_factory=list)
    provider_nodes: dict[str, list[dict]] = field(default_factory=dict)


def subscription_url_identity(url: str) -> str:
    """Return a non-reversible identity for a subscription URL cache association."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


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
    source_egress: str | None = None
    profile_title: str | None = None
    content_disposition: str | None = None
    userinfo: str | None = None
    duration_ms: int = 0
    provider_nodes: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "nodes",
            tuple(MappingProxyType(copy.deepcopy(dict(n))) for n in self.nodes),
        )
        object.__setattr__(self, "attempts", tuple(self.attempts))
        object.__setattr__(
            self,
            "provider_nodes",
            MappingProxyType({
                str(name): tuple(MappingProxyType(copy.deepcopy(dict(node))) for node in nodes)
                for name, nodes in self.provider_nodes.items()
            }),
        )


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
    if re.search(r"^proxy-providers\s*:", text, re.MULTILINE):
        return FORMAT_PROVIDER_YAML
    if re.search(r"^proxies\s*:", text, re.MULTILINE):
        return FORMAT_YAML
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
    pool_channels: list[Channel] | None = None,
    local_proxies: list[str] | None = None,
) -> list[Channel]:
    """Build subscription paths from direct, managed, healthy pool, and local exits."""
    channels = [Channel(CHANNEL_DIRECT, None, CHANNEL_DIRECT)]
    if kernel_proxy:
        channels.append(Channel(CHANNEL_KERNEL, kernel_proxy, "runtime"))
    channels.extend(pool_channels or [])
    if local_proxies is None:
        local_proxies = _local_mixed_proxies(kernel_proxy)
    for url in local_proxies:
        if url and url not in [c.proxy for c in channels]:
            port = url.rsplit(":", 1)[-1]
            channels.append(Channel(CHANNEL_LOCAL, url, f"external:{port}"))
    return channels


def _local_mixed_proxies(kernel_proxy: str | None) -> list[str]:
    """本机在听的常见混合端口（借道用户自启的 Clash / Verge）。探活失败即无。"""
    try:
        from app.domains.proxies import clash_manager

        exclude = None
        if kernel_proxy:
            tail = kernel_proxy.rsplit(":", 1)[-1]
            exclude = int(tail) if tail.isdigit() else None
        return clash_manager.local_mixed_channels(exclude)
    except Exception:  # noqa: BLE001 —— 借道口只是加成，探不到就不加
        return []


def _provider_definitions(raw: bytes) -> tuple[dict, dict[str, dict]]:
    """Validate the provider section without exposing provider URLs in diagnostics."""
    doc = yaml.safe_load(decode_utf8(raw))
    if not isinstance(doc, dict):
        raise UnsupportedFormatError(FORMAT_PROVIDER_YAML)
    providers = doc.get("proxy-providers")
    if not isinstance(providers, dict) or not providers or len(providers) > 16:
        raise UnsupportedFormatError(FORMAT_PROVIDER_YAML)
    normalized: dict[str, dict] = {}
    for name, config in providers.items():
        if not isinstance(name, str) or not name or not isinstance(config, dict):
            raise UnsupportedFormatError(FORMAT_PROVIDER_YAML)
        if str(config.get("type", "")).lower() != "http":
            raise UnsupportedFormatError(FORMAT_PROVIDER_YAML)
        provider_url = config.get("url")
        if not isinstance(provider_url, str):
            raise UnsupportedFormatError(FORMAT_PROVIDER_YAML)
        parsed = urlsplit(provider_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise UnsupportedFormatError(FORMAT_PROVIDER_YAML)
        provider_proxy = config.get("proxy")
        if provider_proxy is not None and not isinstance(provider_proxy, str):
            raise UnsupportedFormatError(FORMAT_PROVIDER_YAML)
        headers = config.get("header") or {}
        if not isinstance(headers, Mapping) or any(
            not isinstance(key, str)
            or not isinstance(value, (str, list))
            or isinstance(value, list) and any(not isinstance(item, str) for item in value)
            for key, value in headers.items()
        ):
            raise UnsupportedFormatError(FORMAT_PROVIDER_YAML)
        for field_name in ("filter", "exclude-filter", "exclude-type"):
            if config.get(field_name) is not None and not isinstance(config[field_name], str):
                raise UnsupportedFormatError(FORMAT_PROVIDER_YAML)
        size_limit = config.get("size-limit", 0)
        if not isinstance(size_limit, int) or size_limit < 0:
            raise UnsupportedFormatError(FORMAT_PROVIDER_YAML)
        normalized[name] = config
    return doc, normalized


def default_client_factory(channel: Channel, *, timeout: float = DEFAULT_FETCH_TIMEOUT
                           ) -> httpx.AsyncClient:
    """生产默认：按通道的 proxy 建客户端。"""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
        proxy=channel.proxy,
        follow_redirects=True,
        trust_env=False,
    )


def _validate_fetch_response(
    resp: httpx.Response,
    channel: Channel,
    elapsed_ms: int,
    response_parser: Callable[[bytes, str], list[dict]] | None = None,
) -> tuple[FetchResult | None, FetchAttempt, str | None]:
    raw = resp.content
    egress_id = channel.egress_id or channel.name
    if resp.status_code != 200:
        return None, FetchAttempt(
            channel.name, False, resp.status_code, FORMAT_UNKNOWN,
            error=f"http_{resp.status_code}", stage="http", elapsed_ms=elapsed_ms,
            egress_id=egress_id,
        ), None
    if not raw:
        return None, FetchAttempt(
            channel.name, False, resp.status_code, FORMAT_EMPTY,
            error="empty_body", stage="content", elapsed_ms=elapsed_ms,
            egress_id=egress_id,
        ), FORMAT_EMPTY
    try:
        fmt = detect_format(raw)
    except InvalidEncodingError:
        return None, FetchAttempt(
            channel.name, False, resp.status_code, FORMAT_UNKNOWN,
            error="invalid UTF-8", stage="encoding", elapsed_ms=elapsed_ms,
            egress_id=egress_id,
        ), "invalid_utf8"

    parsed_nodes: list[dict] | None = None
    if fmt == FORMAT_YAML:
        try:
            parse_nodes(raw, fmt)
        except EmptyNodeSetError:
            return None, FetchAttempt(
                channel.name, False, resp.status_code, fmt,
                error="empty_node_set", stage="validation", elapsed_ms=elapsed_ms,
                egress_id=egress_id,
            ), FORMAT_EMPTY
        except (UnsupportedFormatError, yaml.YAMLError, ValueError):
            return None, FetchAttempt(
                channel.name, False, resp.status_code, fmt,
                error="invalid_subscription_yaml", stage="validation",
                elapsed_ms=elapsed_ms, egress_id=egress_id,
            ), fmt
    elif fmt == FORMAT_PROVIDER_YAML:
        try:
            _provider_definitions(raw)
        except (UnsupportedFormatError, yaml.YAMLError, ValueError):
            return None, FetchAttempt(
                channel.name, False, resp.status_code, fmt,
                error="invalid_provider_subscription", stage="validation",
                elapsed_ms=elapsed_ms, egress_id=egress_id,
            ), fmt
    elif response_parser is not None and fmt in {FORMAT_URI, FORMAT_UNKNOWN}:
        try:
            parsed_nodes = response_parser(raw, fmt)
        except (UnicodeError, ValueError, TypeError):
            parsed_nodes = []
        if not parsed_nodes:
            return None, FetchAttempt(
                channel.name, False, resp.status_code, fmt,
                error="invalid_subscription_content", stage="validation",
                elapsed_ms=elapsed_ms, egress_id=egress_id,
            ), "invalid_subscription_content"
    else:
        return None, FetchAttempt(
            channel.name, False, resp.status_code, fmt,
            error="unsupported_subscription_format", stage="validation",
            elapsed_ms=elapsed_ms, egress_id=egress_id,
        ), fmt

    result = FetchResult(
        raw=raw,
        http_status=resp.status_code,
        content_type=resp.headers.get("content-type", ""),
        channel=channel.name,
        fmt=fmt,
        userinfo=resp.headers.get("subscription-userinfo"),
        profile_title=resp.headers.get("profile-title"),
        content_disposition=resp.headers.get("content-disposition"),
        egress_id=egress_id,
        duration_ms=elapsed_ms,
    )
    if fmt == FORMAT_YAML:
        result.resolved_nodes = parse_nodes(raw, fmt)
    elif parsed_nodes is not None:
        result.resolved_nodes = parsed_nodes
    return result, FetchAttempt(
        channel.name, True, resp.status_code, fmt, stage="complete",
        elapsed_ms=elapsed_ms, egress_id=egress_id,
    ), None


async def _fetch_provider_nodes(
    raw: bytes,
    channel: Channel,
    *,
    channels: list[Channel],
    client_factory,
    request_timeout: float,
    deadline: float,
) -> tuple[list[dict], dict[str, list[dict]], list[FetchAttempt]]:
    doc, providers = _provider_definitions(raw)
    semaphore = asyncio.Semaphore(3)

    def _request_channel(config: Mapping[str, Any]) -> Channel:
        target = config.get("proxy")
        if target is None:
            return channel
        groups = doc.get("proxy-groups")
        if isinstance(groups, list) and any(
            isinstance(group, Mapping) and group.get("name") == target
            for group in groups
        ):
            raise ProviderResolutionError(
                "provider_proxy_group_unsupported", "provider_proxy",
            )
        if target == "DIRECT":
            direct = next((item for item in channels if item.name == CHANNEL_DIRECT), None)
            if direct is None:
                raise ProviderResolutionError("provider_direct_route_unavailable", "provider_proxy")
            return direct
        if target in {"REJECT", "REJECT-DROP"}:
            raise ProviderResolutionError("provider_proxy_rejected", "provider_proxy")
        match = next((item for item in channels
                      if target in {item.name, item.egress_id}), None)
        if match is None:
            raise ProviderResolutionError("provider_proxy_unavailable", "provider_proxy")
        return match

    def _filter_nodes(nodes: list[dict], config: Mapping[str, Any]) -> list[dict]:
        filtered = nodes
        pattern = config.get("filter")
        if pattern:
            try:
                matcher = re.compile(pattern)
            except re.error as exc:
                raise ProviderResolutionError(
                    "invalid_provider_filter", "provider_validation"
                ) from exc
            filtered = [node for node in filtered
                        if matcher.search(str(node.get("name") or ""))]
        exclude = config.get("exclude-filter")
        if exclude:
            try:
                matcher = re.compile(exclude)
            except re.error as exc:
                raise ProviderResolutionError(
                    "invalid_provider_filter", "provider_validation"
                ) from exc
            filtered = [node for node in filtered
                        if not matcher.search(str(node.get("name") or ""))]
        excluded_types = {
            item.strip().lower() for item in str(config.get("exclude-type") or "").split("|")
            if item.strip()
        }
        if excluded_types:
            filtered = [node for node in filtered
                        if str(node.get("type") or "").lower() not in excluded_types]
        if not filtered:
            raise ProviderResolutionError("empty_provider_node_set", "provider_validation")
        return filtered

    async def _fetch_one(
        name: str, config: dict,
    ) -> tuple[str, list[dict], FetchAttempt]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderResolutionError("subscription_deadline", "overall_timeout")
        timeout = min(max(0.01, request_timeout), remaining)
        try:
            request_channel = _request_channel(config)
        except ProviderResolutionError as exc:
            attempt = FetchAttempt(
                f"provider:{name}", False, None, FORMAT_UNKNOWN,
                exc.code, exc.stage, 0, None,
            )
            raise ProviderResolutionError(exc.code, exc.stage, [attempt]) from exc
        started = time.monotonic()
        headers: list[tuple[str, str]] = [("User-Agent", SUB_USER_AGENT)]
        for key, value in (config.get("header") or {}).items():
            values = value if isinstance(value, list) else [value]
            headers.extend((key, item) for item in values)
        try:
            async with semaphore:
                async with asyncio.timeout(timeout):
                    async with client_factory(request_channel) as client:
                        response = await client.get(
                            str(config["url"]), headers=headers
                        )
        except (TimeoutError, httpx.TimeoutException) as exc:
            attempt = FetchAttempt(
                f"provider:{name}", False, None, FORMAT_UNKNOWN,
                "provider_timeout", "provider_timeout",
                int((time.monotonic() - started) * 1000),
                request_channel.egress_id or request_channel.name,
            )
            raise ProviderResolutionError(
                "provider_timeout", "provider_timeout", [attempt]
            ) from exc
        except httpx.ProxyError as exc:
            attempt = FetchAttempt(
                f"provider:{name}", False, None, FORMAT_UNKNOWN,
                "provider_proxy_error", "provider_proxy",
                int((time.monotonic() - started) * 1000),
                request_channel.egress_id or request_channel.name,
            )
            raise ProviderResolutionError(
                "provider_proxy_error", "provider_proxy", [attempt]
            ) from exc
        except httpx.ConnectError as exc:
            attempt = FetchAttempt(
                f"provider:{name}", False, None, FORMAT_UNKNOWN,
                "provider_connect_error", "provider_connect",
                int((time.monotonic() - started) * 1000),
                request_channel.egress_id or request_channel.name,
            )
            raise ProviderResolutionError(
                "provider_connect_error", "provider_connect", [attempt]
            ) from exc
        except httpx.HTTPError as exc:
            attempt = FetchAttempt(
                f"provider:{name}", False, None, FORMAT_UNKNOWN,
                "provider_transport_error", "provider_transport",
                int((time.monotonic() - started) * 1000),
                request_channel.egress_id or request_channel.name,
            )
            raise ProviderResolutionError(
                "provider_transport_error", "provider_transport", [attempt]
            ) from exc
        if response.status_code != 200:
            attempt = FetchAttempt(
                f"provider:{name}", False, response.status_code, FORMAT_UNKNOWN,
                f"provider_http_{response.status_code}", "provider_http",
                int((time.monotonic() - started) * 1000),
                request_channel.egress_id or request_channel.name,
            )
            raise ProviderResolutionError(
                f"provider_http_{response.status_code}", "provider_http", [attempt]
            )
        size_limit = int(config.get("size-limit") or 0)
        if size_limit and len(response.content) > size_limit:
            attempt = FetchAttempt(
                f"provider:{name}", False, response.status_code, FORMAT_UNKNOWN,
                "provider_size_limit", "provider_validation",
                int((time.monotonic() - started) * 1000),
                request_channel.egress_id or request_channel.name,
            )
            raise ProviderResolutionError(
                "provider_size_limit", "provider_validation", [attempt]
            )
        try:
            fmt = detect_format(response.content)
            nodes = parse_nodes(response.content, fmt)
        except (SnapshotError, yaml.YAMLError, ValueError) as exc:
            attempt = FetchAttempt(
                f"provider:{name}", False, response.status_code, FORMAT_UNKNOWN,
                "invalid_provider_content", "provider_validation",
                int((time.monotonic() - started) * 1000),
                request_channel.egress_id or request_channel.name,
            )
            raise ProviderResolutionError(
                "invalid_provider_content", "provider_validation", [attempt]
            ) from exc
        try:
            nodes = _filter_nodes(nodes, config)
        except ProviderResolutionError as exc:
            attempt = FetchAttempt(
                f"provider:{name}", False, response.status_code, fmt,
                exc.code, exc.stage, int((time.monotonic() - started) * 1000),
                request_channel.egress_id or request_channel.name,
            )
            raise ProviderResolutionError(exc.code, exc.stage, [attempt]) from exc
        attempt = FetchAttempt(
            f"provider:{name}", True, response.status_code, fmt,
            stage="provider_complete",
            elapsed_ms=int((time.monotonic() - started) * 1000),
            egress_id=request_channel.egress_id or request_channel.name,
        )
        return name, nodes, attempt

    tasks = [asyncio.create_task(_fetch_one(name, config))
             for name, config in providers.items()]
    try:
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    provider_attempts: list[FetchAttempt] = []
    errors: list[BaseException] = []
    resolved: list[tuple[str, list[dict], FetchAttempt]] = []
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            errors.append(outcome)
            if isinstance(outcome, ProviderResolutionError):
                provider_attempts.extend(outcome.attempts)
        else:
            resolved.append(outcome)
            provider_attempts.append(outcome[2])
    if errors:
        error = errors[0]
        if isinstance(error, ProviderResolutionError):
            raise ProviderResolutionError(error.code, error.stage, provider_attempts) from error
        raise ProviderResolutionError("provider_transport_error", "provider_transport",
                                      provider_attempts) from error
    provider_nodes = {name: nodes for name, nodes, _attempt in resolved}
    from app.domains.proxies.runtime_config import apply_provider_overrides

    try:
        effective_provider_nodes = {
            name: apply_provider_overrides(nodes, providers[name], name)
            for name, nodes in provider_nodes.items()
        }
    except ValueError as exc:
        raise ProviderResolutionError(
            "invalid_provider_override", "provider_validation",
        ) from exc
    inline = doc.get("proxies") if isinstance(doc.get("proxies"), list) else []
    inline_nodes = [
        dict(node) for node in inline
        if isinstance(node, dict) and node.get("name")
        and not is_info_placeholder(str(node.get("name") or ""))
    ]
    all_nodes = inline_nodes + [
        node for nodes in effective_provider_nodes.values() for node in nodes
    ]
    if not all_nodes:
        raise ProviderResolutionError("empty_provider_node_set", "provider_validation")
    return all_nodes, provider_nodes, provider_attempts


async def fetch_subscription(
    url: str,
    channels: list[Channel],
    *,
    client_factory=default_client_factory,
    request_timeout: float = DEFAULT_FETCH_TIMEOUT,
    total_timeout: float = DEFAULT_SUBSCRIPTION_DEADLINE,
    hedge_delay: float = DEFAULT_HEDGE_DELAY,
    max_parallel_channels: int = DEFAULT_FETCH_PARALLELISM,
    preferred_egress: str | None = None,
    egress_scores: Mapping[str, float] | None = None,
    response_parser: Callable[[bytes, str], list[dict]] | None = None,
) -> FetchResult:
    """有限竞速，返回第一份格式和节点内容都有效的订阅。"""
    ordered = list(channels)
    scores = egress_scores or {}
    if preferred_egress or scores:
        ordered.sort(key=lambda channel: (
            channel.egress_id != preferred_egress if preferred_egress else False,
            -float(scores.get(channel.egress_id or channel.name, 0.0)),
        ))
    if not ordered:
        raise FetchFailedError(url, [])

    started = time.monotonic()
    deadline = started + max(0.01, total_timeout)
    attempts: list[FetchAttempt] = []
    unsupported: list[str] = []
    invalid_encoding = False
    empty_nodes = False
    next_index = 0
    parallelism = max(1, int(max_parallel_channels))
    active: dict[asyncio.Task, tuple[Channel, float]] = {}

    async def _one(
        channel: Channel,
    ) -> tuple[FetchResult | None, FetchAttempt, str | None, list[FetchAttempt]]:
        channel_started = time.monotonic()
        try:
            async with asyncio.timeout(max(0.01, request_timeout)):
                async with client_factory(channel) as client:
                    response = await client.get(
                        url, headers={"User-Agent": SUB_USER_AGENT}
                    )
        except TimeoutError:
            elapsed = int((time.monotonic() - channel_started) * 1000)
            return None, FetchAttempt(
                channel.name, False, None, FORMAT_UNKNOWN, "request_timeout",
                "timeout", elapsed, channel.egress_id or channel.name,
            ), None, []
        except httpx.TimeoutException as exc:
            elapsed = int((time.monotonic() - channel_started) * 1000)
            return None, FetchAttempt(
                channel.name, False, None, FORMAT_UNKNOWN, type(exc).__name__,
                "timeout", elapsed, channel.egress_id or channel.name,
            ), None, []
        except httpx.ProxyError as exc:
            elapsed = int((time.monotonic() - channel_started) * 1000)
            return None, FetchAttempt(
                channel.name, False, None, FORMAT_UNKNOWN, type(exc).__name__,
                "proxy", elapsed, channel.egress_id or channel.name,
            ), None, []
        except httpx.ConnectError as exc:
            elapsed = int((time.monotonic() - channel_started) * 1000)
            return None, FetchAttempt(
                channel.name, False, None, FORMAT_UNKNOWN, type(exc).__name__,
                "connect", elapsed, channel.egress_id or channel.name,
            ), None, []
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.monotonic() - channel_started) * 1000)
            return None, FetchAttempt(
                channel.name, False, None, FORMAT_UNKNOWN, type(exc).__name__,
                "transport", elapsed, channel.egress_id or channel.name,
            ), None, []
        elapsed = int((time.monotonic() - channel_started) * 1000)
        result, attempt, issue = _validate_fetch_response(
            response, channel, elapsed, response_parser=response_parser,
        )
        if result is None or issue is not None:
            return result, attempt, issue, []
        if result.fmt == FORMAT_PROVIDER_YAML:
            try:
                (result.resolved_nodes, result.provider_nodes,
                 provider_attempts) = await _fetch_provider_nodes(
                    result.raw, channel, channels=ordered, client_factory=client_factory,
                    request_timeout=request_timeout, deadline=deadline,
                )
            except ProviderResolutionError as exc:
                elapsed = int((time.monotonic() - channel_started) * 1000)
                return None, FetchAttempt(
                    channel.name, False, response.status_code, result.fmt,
                    exc.code, exc.stage, elapsed, None,
                ), exc.code, exc.attempts
            attempt = FetchAttempt(
                channel.name, True, response.status_code, result.fmt,
                stage="provider_validation",
                elapsed_ms=int((time.monotonic() - channel_started) * 1000),
                egress_id=channel.egress_id or channel.name,
            )
        else:
            provider_attempts = []
        return result, attempt, None, provider_attempts

    def _launch_one() -> None:
        nonlocal next_index
        channel = ordered[next_index]
        next_index += 1
        task = asyncio.create_task(_one(channel))
        active[task] = (channel, time.monotonic())

    _launch_one()
    try:
        while active:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait_for = remaining
            if next_index < len(ordered) and len(active) < parallelism:
                wait_for = min(wait_for, max(0.0, hedge_delay))
            done, _ = await asyncio.wait(
                active, timeout=wait_for, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                if next_index < len(ordered) and len(active) < parallelism:
                    _launch_one()
                continue

            completed: list[tuple[float, asyncio.Task, FetchResult | None,
                                  FetchAttempt, str | None, list[FetchAttempt]]] = []
            for task in done:
                channel, launched_at = active.pop(task)
                try:
                    result, attempt, issue, provider_attempts = task.result()
                except asyncio.CancelledError:
                    continue
                except Exception as exc:  # noqa: BLE001
                    elapsed = int((time.monotonic() - launched_at) * 1000)
                    result = None
                    issue = None
                    provider_attempts = []
                    attempt = FetchAttempt(
                        channel.name, False, None, FORMAT_UNKNOWN, type(exc).__name__,
                        "transport", elapsed, channel.egress_id or channel.name,
                    )
                completed.append((time.monotonic(), task, result, attempt, issue,
                                  provider_attempts))
            completed.sort(key=lambda item: item[0])
            winner: FetchResult | None = None
            for _finished, _task, result, attempt, issue, provider_attempts in completed:
                attempts.append(attempt)
                attempts.extend(provider_attempts)
                if issue == "invalid_utf8":
                    invalid_encoding = True
                elif issue == FORMAT_EMPTY:
                    empty_nodes = True
                elif issue:
                    unsupported.append(issue)
                if result is not None and winner is None:
                    winner = result
            if winner is not None:
                winner.duration_ms = int((time.monotonic() - started) * 1000)
                winner.attempts = attempts
                return winner

            while next_index < len(ordered) and len(active) < parallelism:
                _launch_one()
    finally:
        pending = list(active)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    if time.monotonic() >= deadline:
        for channel, launched_at in active.values():
            attempts.append(FetchAttempt(
                channel.name, False, None, FORMAT_UNKNOWN, "subscription_deadline",
                "overall_timeout", int((time.monotonic() - launched_at) * 1000),
                channel.egress_id or channel.name,
            ))
    duration_ms = int((time.monotonic() - started) * 1000)
    if unsupported:
        raise UnsupportedFormatError(unsupported[0], attempts, duration_ms)
    if invalid_encoding:
        raise InvalidEncodingError(attempts, duration_ms=duration_ms)
    if empty_nodes:
        raise EmptyNodeSetError(FORMAT_YAML, attempts, duration_ms)
    raise FetchFailedError(url, attempts, duration_ms)


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
    nodes = [
        p for p in px
        if isinstance(p, dict) and p.get("name")
        and not is_info_placeholder(str(p["name"]))
    ] if isinstance(px, list) else []
    if not nodes:
        raise EmptyNodeSetError(fmt)
    return nodes


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
        nodes=result.resolved_nodes or nodes,
        attempts=result.attempts,
        source_egress=result.egress_id,
        profile_title=result.profile_title,
        content_disposition=result.content_disposition,
        userinfo=result.userinfo,
        duration_ms=result.duration_ms,
        provider_nodes=result.provider_nodes,
    )


def _safe_egress_id(value: str | None) -> str | None:
    if not value or not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", value):
        return None
    return value


def _attempts_json(attempts: tuple[FetchAttempt, ...] | list[FetchAttempt]) -> str:
    values = [{
        "channel": attempt.channel[:32],
        "egress_id": _safe_egress_id(attempt.egress_id),
        "ok": bool(attempt.ok),
        "http_status": attempt.http_status,
        "format": attempt.fmt[:16],
        "stage": attempt.stage[:32],
        "elapsed_ms": max(0, int(attempt.elapsed_ms)),
        "error": (attempt.error or "")[:100],
    } for attempt in attempts]
    return json.dumps(values, ensure_ascii=True, separators=(",", ":"))


async def fetch_egress_success_scores(session, *, limit: int = 200) -> dict[str, float]:
    """Score recent egress evidence stored with the canonical subscription attempts."""
    egress_failure_stages = {
        "timeout", "overall_timeout", "proxy", "connect", "transport",
        "provider_timeout", "provider_proxy", "provider_connect",
        "provider_transport",
    }
    rows = await session.scalars(
        select(SubscriptionSnapshot.attempts_json)
        .where(SubscriptionSnapshot.attempts_json.is_not(None))
        .order_by(SubscriptionSnapshot.id.desc())
        .limit(max(1, int(limit)))
    )
    totals: Counter[str] = Counter()
    successes: Counter[str] = Counter()
    for payload in rows:
        try:
            attempts = json.loads(payload or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(attempts, list):
            continue
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            egress_id = _safe_egress_id(attempt.get("egress_id"))
            if egress_id is None:
                continue
            ok = attempt.get("ok") is True
            if not ok and str(attempt.get("stage") or "") not in egress_failure_stages:
                continue
            totals[egress_id] += 1
            if ok:
                successes[egress_id] += 1
    return {
        egress_id: (successes[egress_id] + 1) / (count + 2)
        for egress_id, count in totals.items()
    }


async def preferred_fetch_egresses(session, urls: Mapping[int, str]) -> dict[int, str]:
    """Get each current subscription's last successful egress in one batch read."""
    if not urls:
        return {}
    rows = await session.scalars(
        select(SubscriptionSnapshot)
        .where(SubscriptionSnapshot.subscription_id.in_(tuple(urls)))
        .where(SubscriptionSnapshot.status == "OK")
        .order_by(SubscriptionSnapshot.id.desc())
    )
    preferred: dict[int, str] = {}
    fallback = {CHANNEL_DIRECT: CHANNEL_DIRECT, CHANNEL_KERNEL: "runtime"}
    for row in rows:
        sub_id = int(row.subscription_id)
        if sub_id in preferred or not url_matches(row.url, urls[sub_id]):
            continue
        egress_id = _safe_egress_id(row.source_egress)
        if not egress_id:
            egress_id = fallback.get(str(row.source_channel or ""))
        if egress_id:
            preferred[sub_id] = egress_id
    return preferred


async def persist_snapshot(session, snap: Snapshot, *, data_dir: Path
                           ) -> SubscriptionSnapshot:
    """把快照落盘 + 写库。**调用方负责事务边界**：这里只 flush，绝不 commit。

    底层不吃事务：「快照元数据 + Registry 变更 + generation 推进」
    要在同一个事务里原子落地，helper 自己 commit 会让后面两者无法纳入边界；更糟的
    是它会把调用方会话里**其它** pending 行一起提交掉——提交一个自己都不知道
    存在的写集，回滚时那部分永远回不来。

    落盘排在 flush 之前：文件是幂等的内容寻址写入，事务回滚后留个孤儿文件（由
    GC 规则按 raw_path 引用回收）远好过「事务提交了但原始字节没落盘」
    ——后者会让 `latest_snapshot()` 永远恢复不出基线。
    本函数不碰 Registry。
    """
    path = _write_raw(data_dir, snap.subscription_id, snap.sha256, snap.raw_content)
    row = SubscriptionSnapshot(
        subscription_id=snap.subscription_id,
        sha256=snap.sha256,
        format=snap.fmt,
        raw_path=str(path),
        # URL 是快照的 provenance（落库密封，读取侧开封比对）：换链接后靠它
        # 把"旧链接的成功快照"排除掉
        url=seal_url(snap.url),
        node_count=snap.node_count,
        status="OK",
        http_status=snap.http_status,
        content_type=snap.content_type,
        source_channel=snap.source_channel,
        source_egress=_safe_egress_id(snap.source_egress),
        profile_title=(snap.profile_title or "")[:100] or None,
        content_disposition=(snap.content_disposition or "")[:255] or None,
        userinfo=(snap.userinfo or "")[:500] or None,
        duration_ms=max(0, int(snap.duration_ms)),
        attempts_json=_attempts_json(snap.attempts),
        provider_nodes_json=json.dumps(
            {name: [dict(node) for node in nodes]
             for name, nodes in snap.provider_nodes.items()},
            ensure_ascii=False, separators=(",", ":"),
        ) if snap.provider_nodes else None,
        fetched_at=snap.fetched_at,
    )
    session.add(row)
    await session.flush()
    return row


async def persist_fetch_failure(
    session, *, subscription_id: int, url: str, attempts: list[FetchAttempt],
    error: str, duration_ms: int, now: datetime,
) -> SubscriptionSnapshot:
    """Record a failed refresh beside successes without changing the recoverable snapshot."""
    row = SubscriptionSnapshot(
        subscription_id=subscription_id,
        sha256="",
        format=FORMAT_UNKNOWN,
        raw_path=None,
        url=seal_url(url),
        node_count=0,
        status="FAILED",
        http_status=next((a.http_status for a in reversed(attempts)
                          if a.http_status is not None), None),
        content_type=None,
        source_channel=None,
        source_egress=None,
        duration_ms=max(0, int(duration_ms)),
        attempts_json=_attempts_json(attempts),
        error=(error or "subscription refresh failed")[:500],
        fetched_at=now,
    )
    session.add(row)
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
    )
    if url:
        # 行内存的是密封 URL（provenance 加密），SQL 等值比较不可用——
        # 按新到旧逐行开封比对，取第一条「当前 URL 抓的」成功快照
        matched: SubscriptionSnapshot | None = None
        for row in (await session.scalars(stmt)).all():
            if url_matches(row.url, url):
                matched = row
                break
        if matched is None:
            return None
        row = matched
    else:
        row = await session.scalar(stmt.limit(1))
    if row is None:
        return None
    return _restore_snapshot(row, url=url)


def _restore_snapshot(row: SubscriptionSnapshot, *, url: str = "") -> Snapshot:
    """校验并恢复既有快照的原始内容。"""
    subscription_id = row.subscription_id
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
    provider_nodes: dict[str, list[dict]] = {}
    try:
        fmt = detect_format(raw)
        if row.provider_nodes_json:
            parsed_provider_nodes = json.loads(row.provider_nodes_json)
            if not isinstance(parsed_provider_nodes, dict):
                raise ValueError("provider node metadata is not a mapping")
            provider_nodes = {
                str(name): [dict(node) for node in values if isinstance(node, dict)]
                for name, values in parsed_provider_nodes.items()
                if isinstance(values, list)
            }
            doc = yaml.safe_load(decode_utf8(raw))
            inline = doc.get("proxies") if isinstance(doc, dict) else []
            provider_configs = doc.get("proxy-providers") if isinstance(doc, dict) else {}
            if not isinstance(provider_configs, dict):
                raise ValueError("provider definitions are not a mapping")
            from app.domains.proxies.runtime_config import apply_provider_overrides

            nodes = [dict(node) for node in (inline or []) if isinstance(node, dict)
                     and node.get("name")]
            for name, group in provider_nodes.items():
                config = provider_configs.get(name)
                if not isinstance(config, Mapping):
                    raise ValueError("provider definition is missing")
                nodes.extend(apply_provider_overrides(group, config, name))
            if not nodes:
                raise EmptyNodeSetError(row.format or fmt)
        else:
            nodes = parse_nodes(raw, row.format or fmt)
    except (SnapshotError, yaml.YAMLError, TypeError, ValueError) as e:
        raise SnapshotRecoveryError(subscription_id, f"原始字节已不可解析：{e}") from e

    attempts: list[FetchAttempt] = []
    try:
        stored_attempts = json.loads(row.attempts_json or "[]")
    except (TypeError, ValueError):
        stored_attempts = []
    if isinstance(stored_attempts, list):
        for item in stored_attempts:
            if not isinstance(item, dict):
                continue
            attempts.append(FetchAttempt(
                channel=str(item.get("channel") or "")[:32],
                ok=bool(item.get("ok")),
                http_status=item.get("http_status")
                if isinstance(item.get("http_status"), int) else None,
                fmt=str(item.get("format") or FORMAT_UNKNOWN)[:16],
                error=str(item.get("error") or "")[:100] or None,
                stage=str(item.get("stage") or "")[:32],
                elapsed_ms=max(0, int(item.get("elapsed_ms") or 0)),
                egress_id=_safe_egress_id(item.get("egress_id")),
            ))
    return Snapshot(
        subscription_id=row.subscription_id,
        # 真实的来源 URL 来自快照行本身（开封）——不能回显调用方的入参，否则
        # 换链接后会把旧链接的内容标成新链接（调用方无从察觉）
        url=open_url(row.url) or url,
        fetched_at=row.fetched_at or datetime.now(),
        http_status=row.http_status or 0,
        content_type=row.content_type or "",
        sha256=sha,
        raw_content=raw,
        fmt=row.format or fmt,
        node_count=len(nodes),
        source_channel=row.source_channel or "",
        nodes=nodes,
        attempts=tuple(attempts),
        source_egress=_safe_egress_id(row.source_egress),
        profile_title=(row.profile_title or "")[:100] or None,
        content_disposition=(row.content_disposition or "")[:255] or None,
        userinfo=(row.userinfo or "")[:500] or None,
        duration_ms=max(0, int(row.duration_ms or 0)),
        provider_nodes=provider_nodes,
    )


async def load_source_documents(
    session, subscription_ids, *, data_dir: Path, errors: dict[int, str] | None = None,
) -> dict[int, dict]:
    """批量读取当前订阅快照；尚无快照时复用原始订阅缓存。"""
    from app.domains.proxies.clash_manager import subscription_config_path
    from app.domains.proxies.models import ProxySubscription

    ids = {int(sid) for sid in subscription_ids if sid is not None}
    if not ids:
        return {}
    subscriptions = (await session.scalars(
        select(ProxySubscription).where(ProxySubscription.id.in_(ids))
    )).all()
    urls = {sub.id: open_url(sub.url) or sub.url for sub in subscriptions}
    rows = (await session.scalars(
        select(SubscriptionSnapshot)
        .where(SubscriptionSnapshot.subscription_id.in_(ids))
        .where(SubscriptionSnapshot.status == "OK")
        .order_by(SubscriptionSnapshot.id.desc())
    )).all()
    selected: dict[int, SubscriptionSnapshot] = {}
    for row in rows:
        sid = row.subscription_id
        if sid in selected or (urls.get(sid) and not url_matches(row.url, urls[sid])):
            continue
        selected[sid] = row
    documents: dict[int, dict] = {}
    for sid in ids:
        try:
            row = selected.get(sid)
            if row is not None:
                if row.format and row.format not in (FORMAT_YAML, FORMAT_PROVIDER_YAML):
                    documents[sid] = {}
                    continue
                snapshot = _restore_snapshot(row)
                raw = snapshot.raw_content
                provider_nodes = snapshot.provider_nodes
            else:
                provider_nodes = {}
                path = subscription_config_path(data_dir, sid)
                if not path.is_file():
                    documents[sid] = {}
                    continue
                try:
                    raw = path.read_bytes()
                except OSError as exc:
                    raise SnapshotRecoveryError(sid, "订阅缓存读不回") from exc
                try:
                    fmt = detect_format(raw)
                except SnapshotError as exc:
                    raise SnapshotRecoveryError(sid, "订阅缓存无法解析") from exc
                if fmt not in (FORMAT_YAML, FORMAT_PROVIDER_YAML):
                    documents[sid] = {}
                    continue
            if provider_nodes:
                from app.domains.proxies.runtime_config import expand_provider_nodes

                raw = expand_provider_nodes(
                    decode_utf8(raw), provider_nodes,
                ).encode("utf-8")
            try:
                doc = yaml.safe_load(decode_utf8(raw))
            except (SnapshotError, yaml.YAMLError) as exc:
                raise SnapshotRecoveryError(sid, "订阅原文无法解析") from exc
            if not isinstance(doc, dict):
                raise SnapshotRecoveryError(sid, "订阅原文不是映射")
            documents[sid] = doc
        except SnapshotRecoveryError as exc:
            if errors is None:
                raise
            errors[sid] = str(exc)
    return documents
