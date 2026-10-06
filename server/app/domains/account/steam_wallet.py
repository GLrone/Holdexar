"""Steam 钱包余额/结算币种抓取（账户页单通道）。

数据源：登录态商店账户页 https://store.steampowered.com/account/——
余额渲染在 `div.accountBalance` 的 `div.accountData.price`（带币种符号），
同页 data-store_user_config 内嵌购物车小计（currency_code 数字 ID +
formatted_amount），与余额符号交叉验证防误配。

抓取目标页为 Steam 公开页面结构，此处仅消费客观字段。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

from app.core.logging import log_event

logger = logging.getLogger(__name__)

ACCOUNT_URL = "https://store.steampowered.com/account/"
HISTORY_URL = "https://store.steampowered.com/account/history/"

FETCH_TIMEOUT = 15.0

# Steam 钱包币种 ID（wallet_currency 整数）→ ISO 代号与符号。
# 清单来自 Steam 官方页面实际返回的枚举值，与本项目 crawl_regions 的
# currency 字段（ISO 代号）对齐使用。
STEAM_CURRENCY_IDS: dict[int, tuple[str, str]] = {
    1: ("USD", "$"),
    2: ("GBP", "£"),
    3: ("EUR", "€"),
    5: ("RUB", "₽"),
    6: ("PLN", "zł"),
    7: ("BRL", "R$"),
    8: ("JPY", "¥"),
    9: ("NOK", "kr"),
    10: ("IDR", "Rp"),
    11: ("MYR", "RM"),
    12: ("PHP", "₱"),
    13: ("SGD", "S$"),
    14: ("THB", "฿"),
    15: ("VND", "₫"),
    16: ("KRW", "₩"),
    17: ("TRY", "₺"),
    18: ("UAH", "₴"),
    19: ("MXN", "Mex$"),
    20: ("CAD", "C$"),
    21: ("AUD", "A$"),
    22: ("NZD", "NZ$"),
    23: ("CNY", "¥"),
    24: ("INR", "₹"),
    25: ("CLP", "CLP$"),
    26: ("PEN", "S/"),
    27: ("COP", "COL$"),
    28: ("ZAR", "R"),
    29: ("HKD", "HK$"),
    30: ("TWD", "NT$"),
    31: ("SAR", "SR"),
    32: ("AED", "AED"),
    34: ("ARS", "ARS$"),
    35: ("ILS", "₪"),
    37: ("KZT", "₸"),
    38: ("KWD", "KD"),
    39: ("QAR", "QR"),
}

# Steam 原值即主单位（不做 /100）的币种：JPY / KRW / VND
NO_DIVIDE_CURRENCY_IDS = frozenset({8, 16, 15})

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 账户页余额：accountBalance 区块的 price 元素。页面存在两种渲染变体
# （纯文本 "S$0.00" / 链接包裹 "<a>$0.00 USD</a>"、price 元素可带前置属性），
# 故按区块定位后剥内层标签取文本。
_ACCOUNT_BALANCE_RE = re.compile(
    r'class="accountRow accountBalance">\s*<div[^>]*class="accountData price"[^>]*>(.*?)</div>',
    re.DOTALL,
)
_ACCOUNT_CURRENCY_RE = re.compile(r"&quot;currency_code&quot;:(\d+),&quot;formatted_amount&quot;:&quot;([^&]+)&quot;")
_ACCOUNT_STEAMID_RE = re.compile(
    r'steamcommunity\.com/profiles/(\d{17,})/"[^>]*user_avatar'
)
# 账户历史页货币行「货币转换至 INR（印度、29）」：括号内地区名+地区数字码。
# 这是账户真实商店区的权威信号（cookie 的 steamCountry 会被代理出口 IP 污染）。
_REGION_NOTE_RE = re.compile(r"（([^，）]{2,6})、(\d{1,2})）")

# 登录态 Steam Cookie 的最小集合（其余字段对余额查询无意义）。
# steamRefresh_steam 是续期凭据：访问令牌（steamLoginSecure）寿命约 24 小时，
# 丢掉它登录态就只能在一天内可用（续期见 session.py）；steamRememberLogin
# 记录登录时是否勾选「记住我」，用于判断续期凭据为何缺席。
_STEAM_COOKIE_KEYS = (
    "sessionid",
    "steamCountry",
    "steamLoginSecure",
    "steamRefresh_steam",
    "steamRememberLogin",
)


class WalletFetchError(RuntimeError):
    """余额抓取失败（账户页未取到有效数据）。"""


class StoreSessionExpiredError(WalletFetchError):
    """store 会话被服务端衰减：页面被弹回登录页。

    访问令牌（JWT）本身未过期，但 finalize 注册的商店会话数十分钟不用即
    失效。消费方应强制换发新会话
    （account_service.ensure_live_session(force=True)）后重试一次。
    """


def is_login_page(resp: httpx.Response) -> bool:
    """重定向链终点是否落在登录页（store 会话失效的服务端表现）。"""
    return "/login/" in str(resp.url)


class WalletRateLimitedError(WalletFetchError):
    """Steam 节点级风控（429 限流 / 403 拒绝）：调用方应进长冷却，
    不适用普通指数退避——这是出口 IP 被盯上的信号，短期重试只会
    加深风控印象（redeem 域 429 同语义：换节点才有效）。"""


class UnknownCurrencyError(WalletFetchError):
    """Steam 返回了映射表之外的币种 ID。"""


@dataclass(slots=True)
class WalletInfo:
    """规范化后的钱包信息。金额单位为主单位数值（如 CNY 123.45）。"""

    balance: float
    currency_code: str
    currency_symbol: str
    currency_id: int
    country_code: str
    steam_id: str = ""  # 页面归属账户（store account 通道填充；其余通道留空）

    @property
    def balance_display(self) -> str:
        if self.currency_id in NO_DIVIDE_CURRENCY_IDS:
            return f"{self.currency_symbol}{int(self.balance):,}"
        return f"{self.currency_symbol}{self.balance:,.2f}"


def parse_cookie_str(cookies_raw: str) -> dict[str, str]:
    """把浏览器复制的 Cookie 串解析为 dict。

    容忍三种常见粘贴形态：整行带 "Cookie:" 前缀（F12 请求标头整行复制）、
    换行分隔（Application → Cookies 逐条复制）、多余空白。
    """
    jar: dict[str, str] = {}
    text = re.sub(r"^\s*cookie\s*:\s*", "", cookies_raw or "", flags=re.IGNORECASE)
    for part in re.split(r"[;\r\n]+", text):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        jar[key.strip()] = value.strip()
    return jar


def filter_login_cookies(cookies_raw: str) -> str:
    """收窄为登录态 Cookie（sessionid / steamCountry / steamLoginSecure
    / steamRefresh_steam / steamRememberLogin）。

    面积最小化：既避免无关 Cookie（浏览偏好等）入库，也便于用户粘贴整段
    浏览器 Cookie 后自动规整。续期凭据必须留下——只存访问令牌的登录态
    24 小时后即失效，且无法自动恢复。
    """
    jar = parse_cookie_str(cookies_raw)
    keep = [f"{k}={jar[k]}" for k in _STEAM_COOKIE_KEYS if jar.get(k)]
    return "; ".join(keep)


def steam_id_from_cookies(cookies_raw: str) -> str:
    """从 steamLoginSecure 提取 SteamID64（`<steamid>||<token>` 前段）。

    URL 编码与明文两种 `||` 分隔（%7C%7C）都处理；纯数字值直接判定。
    """
    value = parse_cookie_str(cookies_raw).get("steamLoginSecure", "")
    for sep in ("%7C%7C", "||"):
        if sep in value:
            head = value.split(sep, 1)[0].strip()
            return head if head.isdigit() else ""
    return value if value.isdigit() else ""


def has_login_cookie(cookies_raw: str) -> bool:
    return "steamLoginSecure" in parse_cookie_str(cookies_raw).keys()


def _normalize_country(value: object) -> str:
    if not isinstance(value, str):
        return ""
    code = value.strip().upper()
    return code if re.fullmatch(r"[A-Z]{2}", code) else ""


def _from_raw_amounts(
    balance_raw: int, delayed_raw: int, currency_id: int, country: str
) -> WalletInfo:
    """Steam 返回的分单位原值 → WalletInfo（含 /100 规则）。"""
    info = STEAM_CURRENCY_IDS.get(currency_id)
    if info is None:
        raise UnknownCurrencyError(f"未知 Steam 钱包币种 ID: {currency_id}")
    code, symbol = info
    total_fen = balance_raw + delayed_raw
    amount = float(total_fen) if currency_id in NO_DIVIDE_CURRENCY_IDS else total_fen / 100.0
    return WalletInfo(
        balance=round(amount, 2),
        currency_code=code,
        currency_symbol=symbol,
        currency_id=currency_id,
        country_code=_normalize_country(country),
    )


async def _region_from_history(
    client: httpx.AsyncClient, cookies: dict[str, str]
) -> str:
    """history 页「货币转换至 INR（印度、29）」→ 地区名（中文）。

    返回地区名原串（如 "印度"），由服务层对 crawl_regions.name 精确匹配；
    抓取失败或未登录返回空串。
    """
    try:
        resp = await client.get(HISTORY_URL, headers=_HEADERS, cookies=cookies)
        if resp.status_code != 200:
            return ""
        match = _REGION_NOTE_RE.search(resp.text)
        return match.group(1).strip() if match else ""
    except Exception as exc:  # noqa: BLE001 地区名是增强信号，失败不阻断余额
        log_event(
            logger,
            "账号历史页地区名抓取失败",
            level=logging.DEBUG,
            detail={"原因": str(exc)},
        )
        return ""


async def _fetch_store_account(
    client: httpx.AsyncClient, cookies: dict[str, str]
) -> WalletInfo | None:
    """登录态商店账户页三合一：余额 + 币种 + 页面归属账户。

    余额渲染在 `div.accountBalance` 的 `div.accountData.price`（带币种符号）；
    同页 data-store_user_config 内嵌购物车小计含
    `"currency_code":<id>,"formatted_amount":"S$0.00"`。符号与币种 id
    双源交叉验证，避免仅凭符号硬映射。
    未登录页面无余额元素 → 返回 None 交给上层按失败处理。
    """
    resp = await client.get(ACCOUNT_URL, headers=_HEADERS, cookies=cookies)
    resp.raise_for_status()
    if is_login_page(resp):
        raise StoreSessionExpiredError("商店账户页把我们送回了登录页")
    text = resp.text
    bal = _ACCOUNT_BALANCE_RE.search(text)
    cur = _ACCOUNT_CURRENCY_RE.search(text)
    if not bal or not cur:
        return None
    currency_id = int(cur.group(1))
    info = STEAM_CURRENCY_IDS.get(currency_id)
    if info is None:
        raise UnknownCurrencyError(f"未知 Steam 钱包币种 ID: {currency_id}")
    # formatted_amount（如 "₹0.00"）与余额符号须一致，防误配；
    # 页面里符号是 unicode 转义（\u20b9）：latin-1 编码回字节后按
    # unicode_escape 解（直接 encode().decode() 会把 UTF-8 字节当 latin-1 出 mojibake）
    raw_balance = re.sub(r"<[^>]+>", "", bal.group(1)).strip()
    amount = _parse_money(raw_balance)
    if amount is None:
        return None
    code, symbol = info
    fmt_amount = cur.group(2).encode("latin-1", "backslashreplace").decode(
        "unicode_escape"
    )
    if symbol not in fmt_amount or symbol not in raw_balance:
        raise WalletFetchError(
            f"余额符号与币种不一致：{raw_balance!r} vs {fmt_amount!r}（id={currency_id}）"
        )
    # 账户商店区：steamCountry cookie 会被代理出口 IP 污染（如 US 出口），
    # 不可信。改从 history 页「货币转换至 INR（印度、29）」取地区名（权威）；
    # history 抓取失败时回退 cookie。
    country = await _region_from_history(client, cookies) or _normalize_country(
        cookies.get("steamCountry", "").split("|")[0]
    )
    sid_match = _ACCOUNT_STEAMID_RE.search(text)
    return WalletInfo(
        balance=round(amount, 2),
        currency_code=code,
        currency_symbol=symbol,
        currency_id=currency_id,
        country_code=country,
        steam_id=sid_match.group(1) if sid_match else "",
    )


def _parse_money(text: str) -> float | None:
    """从 Steam 余额串（₹ 137.30 / ¥1,234.56 / ₸ 3 810,21）取数值。

    不同 locale 千分位/小数点符号不同：按「最后出现的分隔符」判定——
    逗号在末段为小数点，否则按千分位剔除。
    """
    cleaned = re.sub(r"[^\d.,\s]", "", text).strip()
    match = re.search(r"[\d.,\s]+", cleaned)
    if not match:
        return None
    s = re.sub(r"\s", "", match.group(0))
    if "," in s and "." in s:
        # 两者都有：靠后的当小数点，靠前的当千分位剔除
        # （1,234.56 → 1234.56；3.810,21 → 3810.21）
        dec, sep = (".", ",") if s.rfind(".") > s.rfind(",") else (",", ".")
        head, _, tail = s.rpartition(dec)
        s = head.replace(sep, "") + "." + tail
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = head.replace(",", "") + "." + tail if len(tail) in (1, 2) else s.replace(",", "")
    elif "." in s:
        head, _, tail = s.rpartition(".")
        s = head.replace(".", "") + "." + tail if len(tail) in (1, 2) else s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


# 进程级客户端缓存（键 = 出口代理 × 证书校验 × 超时）：代理连接的建立
# （CONNECT + TLS 握手）是单次开销，复用后请求走已建隧道，延迟与失败率
# 都从「每轮握手」级降为「偶发建连」级。客户端随进程存活；池中连接失效
# 时由连接类失败路径丢弃重建（_try_channels 的自愈重试）。
# 响应 Set-Cookie 一律不采用——每次取用前清空 jar，登录态始终以调用方
# 显式传入的 Cookie 为准，不与 Steam 响应下发的会话字段串味。
_client_cache: dict[tuple[str | None, bool, float], httpx.AsyncClient] = {}


def _new_client(
    proxy_url: str | None, verify: bool, timeout: float
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout, verify=verify, follow_redirects=True, proxy=proxy_url
    )


async def _borrow_client(
    proxy_url: str | None, verify: bool, timeout: float, *, fresh: bool = False
) -> httpx.AsyncClient:
    """取缓存客户端；fresh=True 丢弃旧实例重建（内核重启/出口切换后
    池中连接全死，由连接类失败路径触发）。"""
    key = (proxy_url, verify, timeout)
    client = _client_cache.get(key)
    if client is not None and not fresh:
        client.cookies.clear()
        return client
    if client is not None:
        _client_cache.pop(key, None)
        await client.aclose()
    client = _new_client(proxy_url, verify, timeout)
    _client_cache[key] = client
    return client


async def _try_channels(
    verify_flag: bool, cookies: dict[str, str], proxy_url: str | None = None
) -> WalletInfo:
    """跑钱包抓取；verify=False 用于本机自签证书加速器场景。

    客户端经 _borrow_client 复用；连接层错误（建连失败/读超时/断链）丢弃
    缓存、重建连接后整轮重试一次——隧道级故障重连即可恢复。HTTP 状态
    错误不属连接层：命中 429/403 上抛 WalletRateLimitedError——节点被
    风控时连环硬打只会加深印象（redeem 域 429 同语义：换节点才有效）。
    """
    errors: list[str] = []
    retried = False
    for attempt in (0, 1):
        if attempt > 0 and not retried:
            break
        client = await _borrow_client(
            proxy_url, verify_flag, FETCH_TIMEOUT, fresh=attempt > 0
        )
        for channel in (_fetch_store_account,):
            try:
                info = await channel(client, cookies)
            except (UnknownCurrencyError, StoreSessionExpiredError):
                raise  # 专型错误直穿：币种缺失 / 会话衰减各自有上层处置
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (429, 403):
                    raise WalletRateLimitedError(
                        f"Steam 节点风控（HTTP {exc.response.status_code}，"
                        f"{channel.__name__}）——请切换 Clash 节点或稍后再试"
                    ) from exc
                errors.append(f"{channel.__name__}: {type(exc).__name__}: {exc}")
                log_event(
                    logger,
                    "钱包通道抓取失败",
                    level=logging.DEBUG,
                    detail={"证书校验": verify_flag, "错误": errors[-1]},
                )
            except httpx.TransportError as exc:
                errors.append(f"{channel.__name__}: {type(exc).__name__}: {exc}")
                log_event(
                    logger,
                    "钱包通道抓取失败",
                    level=logging.DEBUG,
                    detail={"证书校验": verify_flag, "错误": errors[-1]},
                )
                retried = attempt == 0
            except Exception as exc:  # noqa: BLE001 通道间容错，最后统一抛
                errors.append(f"{channel.__name__}: {type(exc).__name__}: {exc}")
                log_event(
                    logger,
                    "钱包通道抓取失败",
                    level=logging.DEBUG,
                    detail={"证书校验": verify_flag, "错误": errors[-1]},
                )
            else:
                if info is not None:
                    return info
    raise WalletFetchError(
        "无法获取 Steam 钱包余额（Cookie 失效或网络不通）：" + "；".join(errors)
    )


async def fetch_wallet(cookies_raw: str, *, verify: bool | None = None, proxy_url: str | None = None) -> WalletInfo:
    """经账户页通道抓取钱包余额与结算币种。

    verify 显式传值时只按该值跑一次；默认先严格校验，遇到 SSL 证书
    错误时降级跳过校验重试一次——本机用 Steam 加速器（hosts 劫持 +
    本地 127.0.0.1 自签反代）时系统信任链必然失败，降级只影响本次
    回环流量，属可接受折中。
    proxy_url 来自代理策略引擎（steamcommunity 直连常被墙，需经 Clash）。
    """
    if not has_login_cookie(cookies_raw):
        raise WalletFetchError("Steam Cookie 未配置或缺少 steamLoginSecure")
    cookies = parse_cookie_str(cookies_raw)
    # 与浏览器一致的两个辅助 Cookie（年龄确认），Steam 页面常规字段
    cookies.setdefault("birthtime", "283993201")
    cookies.setdefault("wants_mature_content", "1")

    if verify is not None:
        return await _try_channels(verify, cookies, proxy_url)

    try:
        return await _try_channels(True, cookies, proxy_url)
    except WalletFetchError as exc:
        if not _has_ssl_error(str(exc)):
            raise
        log_event(
            logger,
            "Steam 证书校验失败（疑似本机加速器），降级跳过校验重试",
            tag="降级",
        )
        return await _try_channels(False, cookies, proxy_url)


def _has_ssl_error(message: str) -> bool:
    clues = ("SSLError", "CERTIFICATE_VERIFY_FAILED", "self-signed",
             "certificate verify failed", "ssl")
    lowered = message.lower()
    return any(clue.lower() in lowered for clue in clues)


# ── 头像 URL 归一（CDN 域名轮换治理）──────────────────
# Valve 头像 CDN 多域并存：akamaized.net（已死，DNS 不解析）、queniuqe.com /
# eccdnx.com（完美世界国服）、fastly.steamstatic.com（现行，miniprofile 返回）。
# 同一 hash 路径在所有域等价——统一归一到现行规范域，旧域 URL 同样归一，
# 全项目头像走同一个 URL。
AVATAR_CANONICAL_HOST = "https://avatars.fastly.steamstatic.com/"
_AVATAR_HOST_SUFFIXES = ("akamaized.net", "steamstatic.com", "queniuqe.com", "eccdnx.com")


def normalize_avatar_url(url: str | None) -> str:
    """Steam 头像 URL 统一出口：历史 CDN 域名 → 现行 fastly.steamstatic.com。

    非头像 URL / data URI / 空值原样返回。
    """
    u = (url or "").strip()
    if not u:
        return ""
    m = re.match(r"(https?://avatars\.[^/?#]+/)(.*)", u)
    if not m:
        return u
    host = m.group(1)[:-1]
    if host.endswith(_AVATAR_HOST_SUFFIXES) and not host.endswith("fastly.steamstatic.com"):
        return AVATAR_CANONICAL_HOST + m.group(2)
    return u


def parse_miniprofile(data: dict) -> dict:
    """miniprofile JSON → 昵称/头像/在线态（Steam 悬停卡同源数据）。

    在线字段为**条件出现**：离线/隐身时无 `online`（只返回等级/徽章/
    背景/头像边框，无任何在线字段）——缺失即离线。
    `in_game` 为对象（Steam 游戏）；非 Steam 游戏走 `in_nonsteam_game`。
    """
    def _in_game_name(d: dict) -> str:
        for key in ("in_game", "in_nonsteam_game"):
            v = d.get(key)
            if isinstance(v, dict):
                name = str(v.get("name") or v.get("game_name") or "").strip()
                if name:
                    return name
            elif isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    return {
        "persona_name": str(data.get("persona_name") or "").strip(),
        "avatar_url": str(data.get("avatar_url") or "").strip(),
        "online": bool(data.get("online")),
        "in_game_name": _in_game_name(data),
    }


async def fetch_player_states(
    steam_ids: list[str], api_key: str, *, proxy_url: str | None = None
) -> dict[str, dict]:
    """官方账号状态批量查询（ISteamUser/GetPlayerSummaries/v2）。

    一次最多 100 个 SteamID；返回 {steamid: {state, in_game, country}}：
    - personastate：0=离线 1=在线 2=忙碌 3=离开 4=打盹 5=想交易 6=想一起玩；
    - 游戏中附 gameextrainfo（游戏名）；
    - country = loccountrycode（资料国家 ISO 两字码；未填写/私有为空串），
      成员地区判定的资料国家来源；
    - 响应中缺失的 steamid = 离线（私密档案也如此）。
    网络/Key 失败返回 {}（调用方回退 miniprofile 的 online 兜底）。
    """
    if not steam_ids or not api_key:
        return {}
    try:
        client = await _borrow_client(proxy_url, True, 15.0)
        resp = await client.get(
            "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/",
            params={"key": api_key, "steamids": ",".join(steam_ids)},
        )
        if resp.status_code != 200:
            return {}
        players = (resp.json().get("response") or {}).get("players") or []
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "官方在线状态批量拉取失败",
            level=logging.DEBUG,
            exc_info=True,
        )
        return {}
    out: dict[str, dict] = {}
    for p in players:
        sid = str(p.get("steamid") or "")
        if not sid:
            continue
        try:
            state = int(p.get("personastate") or 0)
        except (TypeError, ValueError):
            state = 0
        out[sid] = {
            "state": state,
            "in_game": str(p.get("gameextrainfo") or "").strip(),
            "country": str(p.get("loccountrycode") or "").strip().upper(),
        }
    return out


async def fetch_profile(
    steam_id: str, *, verify: bool | None = None, proxy_url: str | None = None
) -> dict:
    """拉取账户展示名/头像/在线态（miniprofile JSON，轻量免 Key）。

    返回 {"persona_name", "avatar_url", "online", "in_game_name"}；失败返回空值字段。
    在线态为 miniprofile 兜底通道（无 API Key 时唯一来源）。
    证书失败时与 fetch_wallet 同规则降级重试（本机加速器场景）。
    """
    empty = {"persona_name": "", "avatar_url": "", "online": False, "in_game_name": ""}
    if not steam_id:
        return empty
    try:
        friend_code = int(steam_id) - 76561197960265728
    except ValueError:
        return empty

    async def _get(verify_flag: bool) -> dict:
        client = await _borrow_client(proxy_url, verify_flag, 10.0)
        resp = await client.get(
            f"https://steamcommunity.com/miniprofile/{friend_code}/json",
            headers=_HEADERS,
        )
        if resp.status_code != 200:
            return {}
        parsed = parse_miniprofile(resp.json())
        avatar = parsed["avatar_url"]
        if avatar and "_medium" in avatar:
            avatar = avatar.replace("_medium", "_full")
        parsed["avatar_url"] = normalize_avatar_url(avatar)
        return parsed

    for verify_flag in ([verify] if verify is not None else [True, False]):
        try:
            result = await _get(verify_flag)
        except Exception as exc:  # noqa: BLE001
            log_event(
                logger,
                "账号资料（昵称/头像/在线态）拉取失败",
                level=logging.DEBUG,
                detail={"证书校验": verify_flag, "原因": str(exc)},
            )
            if verify is None and _has_ssl_error(str(exc)):
                continue  # 证书问题 → 降级重试一轮
            return empty
        if result.get("persona_name") or result.get("avatar_url"):
            return result
    return empty
