"""account 域单元测试：Cookie 解析 / 币种映射 / 地区派生（不触真实网络）。

隔离：涉及 DB 的用例通过 monkeypatch 把 get_session_factory 换成指向
tmp 目录独立 SQLite 的工厂——不读写开发库 data/holdexar.db，也不动
全局 lru_cache（否则同进程后续测试文件会拿到被换掉的引擎）。
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base, init_db as _real_init_db
from app.domains.account import service as account_service
from app.domains.account.steam_wallet import (
    NO_DIVIDE_CURRENCY_IDS,
    STEAM_CURRENCY_IDS,
    WalletInfo,
    _from_raw_amounts,
    filter_login_cookies,
    has_login_cookie,
    parse_cookie_str,
    steam_id_from_cookies,
)
from app.domains.settings import service as settings_service


@pytest.fixture
def db(tmp_path, monkeypatch):
    """独立临时库：临时替换 session 工厂与 settings 键值层落点。

    settings_service.get_value/set_value 走同一个 session factory，
    替换 factory 即可把全部 DB 读写隔离到 tmp 库。
    """
    import app.core.database as database_module

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    # service/settings 模块以 from-import 持有函数引用，需同步替换
    monkeypatch.setattr(account_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(settings_service, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    """在临时库上建表 + 种子区服。"""
    import app.domains.alerts.models  # noqa: F401 注册模型
    import app.domains.crawl.models  # noqa: F401
    import app.domains.account.models  # noqa: F401
    import app.domains.games.models  # noqa: F401
    import app.domains.proxies.models  # noqa: F401
    import app.domains.rates.models  # noqa: F401
    import app.domains.regions.models  # noqa: F401
    import app.domains.settings.models  # noqa: F401
    import app.domains.wishlist.models  # noqa: F401
    from app.domains.regions import service as regions_service

    async with engine_begin(db):
        pass
    from sqlalchemy import text

    from app.crawler.config import CC_LIST
    from app.crawler.utils import get_beijing_time_obj

    now = get_beijing_time_obj().replace(tzinfo=None)
    async with db() as session:
        for idx, (code, name, currency) in enumerate(CC_LIST):
            await session.execute(
                text(
                    "INSERT OR REPLACE INTO crawl_regions"
                    " (code, name, currency, enabled, sort, updated_at)"
                    " VALUES (:c, :n, :cu, 1, :s, :u)"
                ),
                {"c": code, "n": name, "cu": currency, "s": idx, "u": now},
            )
        await session.commit()


def engine_begin(factory):
    """create_all 到临时库（复用 Base.metadata）。"""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx():
        async with factory.kw["bind"].begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield

    return _ctx()


# ── Cookie 解析 ──────────────────────────────────────────

def test_parse_cookie_str_is_generic_parser():
    # 通用解析器保留全部键；收窄由 filter_login_cookies 负责
    jar = parse_cookie_str("sessionid=abc; steamLoginSecure=76561%7C%7Cxyz; junk=1")
    assert jar["sessionid"] == "abc"
    assert jar["steamLoginSecure"] == "76561%7C%7Cxyz"
    assert jar["junk"] == "1"
    assert "junk" not in filter_login_cookies("sessionid=abc; steamLoginSecure=76561%7C%7Cxyz; junk=1")


def test_parse_cookie_str_tolerates_paste_variants():
    # F12 请求标头整行复制（带 Cookie: 前缀）
    header = "Cookie: sessionid=abc; steamLoginSecure=76561%7C%7Ctok"
    assert parse_cookie_str(header)["steamLoginSecure"] == "76561%7C%7Ctok"
    # Application 面板逐条复制的换行格式
    multiline = "sessionid=abc\nsteamCountry=CN%7C\nsteamLoginSecure=76561%7C%7Ctok"
    narrowed = filter_login_cookies(multiline)
    assert narrowed == "sessionid=abc; steamCountry=CN%7C; steamLoginSecure=76561%7C%7Ctok"


def test_filter_login_cookies_keeps_login_set_and_refresh_credential():
    """收窄口径 = 登录态 Cookie + 续期凭据；浏览器偏好类字段一律不入库。"""
    raw = (
        "sessionid=abc; steamCountry=CN%7C; steamLoginSecure=76561%7C%7Ctok; "
        "steamRefresh_steam=76561%7C%7Cref; steamRememberLogin=true; "
        "browserid=x; Steam_Language=schinese"
    )
    kept = filter_login_cookies(raw)
    assert kept == (
        "sessionid=abc; steamCountry=CN%7C; steamLoginSecure=76561%7C%7Ctok; "
        "steamRefresh_steam=76561%7C%7Cref; steamRememberLogin=true"
    )
    # 续期凭据缺席时收窄结果仍是合法登录态（只是寿命止于访问令牌到期）
    assert "steamRefresh_steam" not in filter_login_cookies(
        "sessionid=abc; steamLoginSecure=76561%7C%7Ctok"
    )


def test_has_login_cookie():
    assert has_login_cookie("a=1; steamLoginSecure=76561||t")
    assert not has_login_cookie("sessionid=abc")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("steamLoginSecure=76561198123456789%7C%7Ctoken", "76561198123456789"),
        ("steamLoginSecure=76561198123456789||token", "76561198123456789"),
        ("steamLoginSecure=76561198123456789", "76561198123456789"),
        ("steamLoginSecure=not-numeric", ""),
        ("sessionid=abc", ""),
    ],
)
def test_steam_id_from_cookies(raw, expected):
    assert steam_id_from_cookies(raw) == expected


# ── 币种映射 / 格式化 ────────────────────────────────────

def test_currency_ids_cover_cc_list_unique_currencies():
    from app.crawler.config import CC_LIST

    wallet_codes = {code for code, _symbol in STEAM_CURRENCY_IDS.values()}
    # CC_LIST 中 Steam 钱包确实承载的币种（UYU/CRC/CHF 无钱包 ID，属爬价专属区）
    crawl_only = {"UYU", "CRC", "CHF"}
    for _cc, _name, currency in CC_LIST:
        if currency in crawl_only:
            continue
        assert currency in wallet_codes, f"CC_LIST 币种 {currency} 缺少钱包 ID 映射"


def test_from_raw_amounts_divides_by_100():
    info = _from_raw_amounts(12345, 500, 23, "CN")
    assert info.currency_code == "CNY"
    assert info.balance == 128.45
    assert info.balance_display == "¥128.45"
    assert info.country_code == "CN"


@pytest.mark.parametrize("currency_id", sorted(NO_DIVIDE_CURRENCY_IDS))
def test_from_raw_amounts_no_divide_currencies(currency_id):
    code, symbol = STEAM_CURRENCY_IDS[currency_id]
    info = _from_raw_amounts(1000, 0, currency_id, "JP")
    assert info.balance == 1000.0
    assert info.balance_display == f"{symbol}1,000"
    assert code in {"JPY", "KRW", "VND"}


def test_from_raw_amounts_rejects_unknown_id():
    with pytest.raises(Exception, match="未知 Steam 钱包币种"):
        _from_raw_amounts(100, 0, 9999, "XX")


def test_from_raw_amounts_normalizes_bad_country():
    info = _from_raw_amounts(100, 0, 1, "usa")
    assert info.country_code == ""


# ── 地区派生（反查 crawl_regions）─────────────────────────

@pytest.mark.asyncio
async def test_derive_region_by_currency_reverse_lookup():
    # CNY → cn；EUR 欧元区基准 pt；USD 多区固定 us
    assert await account_service._derive_region("CNY") == "cn"
    assert await account_service._derive_region("EUR") == "pt"
    assert await account_service._derive_region("USD") == "us"
    assert await account_service._derive_region("KZT") == "kz"


@pytest.mark.asyncio
async def test_derive_region_unknown_currency_returns_empty():
    assert await account_service._derive_region("XXX") == ""
    assert await account_service._derive_region("") == ""


# ── 服务编排（mock 网络层）─────────────────────────────────

@pytest.mark.asyncio
async def test_sync_wallet_persists_snapshot(monkeypatch):
    async def fake_fetch(cookies, *, verify=None, proxy_url=None):
        return _from_raw_amounts(20000, 0, 23, "CN")

    monkeypatch.setattr(account_service, "fetch_wallet", fake_fetch)
    # 多账号表：经 save_cookies 绑定（upsert + 置 active），不再种 KV
    await account_service.save_cookies(
        "steamLoginSecure=76561198123456789%7C%7Ct"
    )
    result = await account_service.sync_wallet(force=True)
    assert result["ok"] is True
    wallet = result["wallet"]
    assert wallet["balance"] == 200.0
    assert wallet["balance_display"] == "¥200.00"
    assert wallet["currency_code"] == "CNY"
    assert wallet["region_code"] == "cn"
    assert wallet["check_ok"] is True


@pytest.mark.asyncio
async def test_sync_wallet_without_cookie_is_soft_skip():
    result = await account_service.sync_wallet(force=True)
    assert result["ok"] is False
    assert result["status"] == "no_cookie"


@pytest.mark.asyncio
async def test_save_cookies_flags_mismatch(monkeypatch):
    async def fake_fetch(cookies, *, verify=None, proxy_url=None):
        return _from_raw_amounts(1, 0, 1, "US")

    monkeypatch.setattr(account_service, "fetch_wallet", fake_fetch)
    await settings_service.set_value("account.steam_id", "76561198000000000")

    result = await service_save("steamLoginSecure=76561198123456789%7C%7Ct")
    assert result["mismatch"] is True

    result = await service_save("steamLoginSecure=76561198000000000%7C%7Ct")
    assert result["mismatch"] is False


async def service_save(cookies: str) -> dict:
    return await account_service.save_cookies(cookies)


# ── SSL 降级（本机自签证书加速器场景）──────────────────────

@pytest.mark.asyncio
async def test_fetch_wallet_falls_back_on_ssl_error(monkeypatch):
    from app.domains.account import steam_wallet

    calls: list[bool] = []

    async def fake_try(verify_flag, cookies, proxy_url=None):
        calls.append(verify_flag)
        if verify_flag:
            raise steam_wallet.WalletFetchError(
                "无法获取 Steam 钱包余额：_fetch_market_wallet: SSLError: certificate verify failed"
            )
        return _from_raw_amounts(100, 0, 1, "US")

    monkeypatch.setattr(steam_wallet, "_try_channels", fake_try)
    info = await steam_wallet.fetch_wallet("steamLoginSecure=76561198000000000%7C%7Ct")
    assert calls == [True, False]  # 严格 → 证书失败降级
    assert info.currency_code == "USD"


@pytest.mark.asyncio
async def test_fetch_wallet_no_fallback_on_plain_network_error(monkeypatch):
    from app.domains.account import steam_wallet

    calls: list[bool] = []

    async def fake_try(verify_flag, cookies, proxy_url=None):
        calls.append(verify_flag)
        raise steam_wallet.WalletFetchError(
            "无法获取 Steam 钱包余额：_fetch_market_wallet: ConnectTimeout: timed out"
        )

    monkeypatch.setattr(steam_wallet, "_try_channels", fake_try)
    with pytest.raises(steam_wallet.WalletFetchError):
        await steam_wallet.fetch_wallet("steamLoginSecure=76561198000000000%7C%7Ct")
    assert calls == [True]  # 非 SSL 错误不降级，避免掩盖 Cookie 失效


@pytest.mark.asyncio
async def test_fetch_wallet_explicit_verify_skips_fallback(monkeypatch):
    from app.domains.account import steam_wallet

    calls: list[bool] = []

    async def fake_try(verify_flag, cookies, proxy_url=None):
        calls.append(verify_flag)
        raise steam_wallet.WalletFetchError(
            "无法获取 Steam 钱包余额：_fetch_market_wallet: SSLError: self-signed certificate"
        )

    monkeypatch.setattr(steam_wallet, "_try_channels", fake_try)
    with pytest.raises(steam_wallet.WalletFetchError):
        await steam_wallet.fetch_wallet(
            "steamLoginSecure=76561198000000000%7C%7Ct", verify=False
        )
    assert calls == [False]  # 显式指定时只跑一次


# ── store account 通道（2026-09 新增，fixture 取自真实页面结构）───

from app.domains.account.steam_wallet import (  # noqa: E402
    _ACCOUNT_BALANCE_RE,
    _ACCOUNT_CURRENCY_RE,
    _ACCOUNT_STEAMID_RE,
    _REGION_NOTE_RE,
    _parse_money,
    _fetch_store_account,
)

# 实网 store/account 页关键片段（INR 账户）：
_ACCOUNT_PAGE_FIXTURE = """
<a class="global_action_link" id="header_wallet_balance"
   href="https://store.steampowered.com/account/store_transactions/">₹ 137.30</a>
<a href="https://steamcommunity.com/profiles/76561198454168600/"
   class="user_avatar playerAvatar offline" aria-label="查看您的个人资料">
&quot;subtotal&quot;:{&quot;amount_in_cents&quot;:&quot;0&quot;,&quot;currency_code&quot;:24,&quot;formatted_amount&quot;:&quot;\u20b90.00&quot;},&quot;is_valid&quot;:1
"""


class _FakeResp:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """通道 HTTP 依赖的最小替身：account 页命中 fixture，history 抓不到即静默。"""

    def __init__(self, pages: dict[str, str]):
        self._pages = pages

    async def get(self, url, **_kw):
        return _FakeResp(self._pages.get(url, ""))


def test_account_page_regexes_against_real_structure():
    text = _ACCOUNT_PAGE_FIXTURE
    bal = _ACCOUNT_BALANCE_RE.search(text)
    cur = _ACCOUNT_CURRENCY_RE.search(text)
    sid = _ACCOUNT_STEAMID_RE.search(text)
    assert bal and cur and sid
    assert bal.group(1).strip() == "₹ 137.30"
    assert cur.group(1) == "24"
    assert sid.group(1) == "76561198454168600"


def test_region_note_regex_matches_history_line():
    m = _REGION_NOTE_RE.search("货币转换至 INR（印度、29）")
    assert m and m.group(1) == "印度" and m.group(2) == "29"


def test_parse_money_locale_variants():
    assert _parse_money("₹ 137.30") == 137.30
    assert _parse_money("¥1,234.56") == 1234.56
    assert _parse_money("₸ 3 810,21") == 3810.21
    assert _parse_money("$0.00") == 0.0
    assert _parse_money("R$ 12.345,67") == 12345.67
    assert _parse_money("--") is None


@pytest.mark.asyncio
async def test_fetch_store_account_parses_inr_wallet():
    from app.domains.account import steam_wallet as w

    client = _FakeClient({w.ACCOUNT_URL: _ACCOUNT_PAGE_FIXTURE})
    cookies = {
        "sessionid": "x",
        "steamLoginSecure": "76561198454168600%7C%7Ctok",
        "steamCountry": "US%7Cdeadbeef",  # 代理出口污染，通道应无视
    }
    info = await _fetch_store_account(client, cookies)
    assert info is not None
    assert info.currency_code == "INR"
    assert info.currency_id == 24
    assert info.balance == 137.30
    assert info.balance_display == "₹137.30"
    assert info.steam_id == "76561198454168600"


@pytest.mark.asyncio
async def test_fetch_store_account_symbol_mismatch_raises():
    from app.domains.account import steam_wallet as w

    # currency_code=1 (USD $) 但余额符号是 ₹ → 交叉验证应炸（防误配）
    broken = _ACCOUNT_PAGE_FIXTURE.replace("currency_code&quot;:24", "currency_code&quot;:1")
    client = _FakeClient({w.ACCOUNT_URL: broken})
    with pytest.raises(Exception, match="不一致"):
        await _fetch_store_account(client, {})


@pytest.mark.asyncio
async def test_fetch_store_account_logged_out_returns_none():
    from app.domains.account import steam_wallet as w

    client = _FakeClient({w.ACCOUNT_URL: "<html>登录 Steam</html>"})
    assert await _fetch_store_account(client, {}) is None


@pytest.mark.asyncio
async def test_derive_region_multi_currency_disambiguates_by_name():
    # USD 多区（us/pk/tr/ar/az…）+ history 页地区名「美国」→ us；名字不匹配回落基准区
    assert await account_service._derive_region("USD", "美国") == "us"
    assert await account_service._derive_region("USD", "南亚") == "pk"
    assert await account_service._derive_region("USD", "") == "us"
    # 名字与币种不符时以币种为准（name 来自过期转换记录）
    assert await account_service._derive_region("KZT", "印度") == "kz"
    # 唯一币种区直接反查
    assert await account_service._derive_region("INR", "印度") == "in"


# ── 头像 URL 归一（2026-09 CDN 域名轮换治理）──────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://avatars.akamaized.net/abc_full.jpg",
         "https://avatars.fastly.steamstatic.com/abc_full.jpg"),
        ("https://avatars.cdn.queniuqe.com/abc_full.jpg",
         "https://avatars.fastly.steamstatic.com/abc_full.jpg"),
        ("https://avatars.st.dl.eccdnx.com/abc.jpg",
         "https://avatars.fastly.steamstatic.com/abc.jpg"),
        ("https://avatars.steamstatic.com/abc.jpg",
         "https://avatars.fastly.steamstatic.com/abc.jpg"),
        # 现行规范域原样保留
        ("https://avatars.fastly.steamstatic.com/abc_full.jpg",
         "https://avatars.fastly.steamstatic.com/abc_full.jpg"),
        # 非头像 URL / data URI / 空值原样
        ("https://steamcdn-a.akamaihd.net/steamcommunity/public/images/xxx",
         "https://steamcdn-a.akamaihd.net/steamcommunity/public/images/xxx"),
        ("data:image/png;base64,iVBOR", "data:image/png;base64,iVBOR"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_avatar_url(raw, expected):
    from app.domains.account.steam_wallet import normalize_avatar_url

    assert normalize_avatar_url(raw) == expected


@pytest.mark.asyncio
async def test_sync_profile_row_self_heals_avatar_domain(db, monkeypatch):
    """资料轮转时头像 URL 归一化覆盖写：存量 queniuqe 旧域 → 现行 fastly。

    昵称保持「只在缺失时补写」语义不变；头像抓到非空且不同才覆盖
    （网络抖动抓空不清空存量）。
    """
    from app.crawler.utils import get_beijing_time_obj
    from app.domains.account.models import SteamAccount

    sid = "76561198454168600"
    async with db() as session:
        session.add(SteamAccount(
            steam_id=sid,
            persona_name="旧昵称",
            avatar_url="https://avatars.cdn.queniuqe.com/abc_full.jpg",
            bound_at=get_beijing_time_obj(),
        ))
        await session.commit()

    async def _fake_profile(steam_id, *, verify=None, proxy_url=None):
        return {
            "persona_name": "新昵称",
            "avatar_url": "https://avatars.fastly.steamstatic.com/abc_full.jpg",
            "online": False,
            "in_game_name": "",
        }

    monkeypatch.setattr(account_service, "fetch_profile", _fake_profile)

    async def _proxy():
        return None

    monkeypatch.setattr(account_service, "_strategy_proxy", _proxy)

    await account_service._sync_profile_row(sid, cookies="", now=get_beijing_time_obj())

    async with db() as session:
        row = await session.get(SteamAccount, sid)
        assert row.avatar_url == "https://avatars.fastly.steamstatic.com/abc_full.jpg"
        assert row.persona_name == "旧昵称"  # 只在缺失时补写，不覆盖


@pytest.mark.asyncio
async def test_sync_profile_row_keeps_avatar_when_fetch_empty(db, monkeypatch):
    """抓取失败（空 avatar_url）不清空存量头像。"""
    from app.crawler.utils import get_beijing_time_obj
    from app.domains.account.models import SteamAccount

    sid = "76561198454168667"
    async with db() as session:
        session.add(SteamAccount(
            steam_id=sid,
            persona_name="甲",
            avatar_url="https://avatars.fastly.steamstatic.com/keep.jpg",
            bound_at=get_beijing_time_obj(),
        ))
        await session.commit()

    async def _empty_profile(steam_id, *, verify=None, proxy_url=None):
        return {"persona_name": "", "avatar_url": "", "online": False, "in_game_name": ""}

    monkeypatch.setattr(account_service, "fetch_profile", _empty_profile)

    async def _proxy():
        return None

    monkeypatch.setattr(account_service, "_strategy_proxy", _proxy)

    await account_service._sync_profile_row(sid, cookies="", now=get_beijing_time_obj())

    async with db() as session:
        row = await session.get(SteamAccount, sid)
        assert row.avatar_url == "https://avatars.fastly.steamstatic.com/keep.jpg"
