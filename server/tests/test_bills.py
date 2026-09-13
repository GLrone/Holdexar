"""bills 域测试：解析器纯函数 + 真实样本端到端对账。

对账基准来自维护者本人的前作脚本（汇率导出脚本，非第三方代码）
在两份 Steam_Report 真实账单转储上的实测输出，本模块的解析行为必须与
之逐项一致。样本文件名含个人昵称，落在 汇率档案_DIR（环境变量，默认
E:\\汇率档案），目录不存在时相关用例自动 skip。

DB 用例隔离方式对齐 test_account_wallet.py：monkeypatch 临时库 session factory。
"""
import json
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.bills.parser import (
    classify_type,
    get_rate_from_rows,
    parse_date_cn,
    parse_money,
    parse_report,
    strip_gift_marks,
)
from app.domains.bills import service as bills_service

# 源样本目录（个人外部数据源，路径由环境变量 汇率档案_DIR 提供；未设置时对账用例 skip）
汇率档案_DIR = Path(os.environ.get("汇率档案_DIR", ""))
# 样本文件名按昵称区分（样本A/tree），真实文件名含个人昵称——
# 由环境变量提供映射，未设且默认文件缺失时相关用例 skip
_SAMPLE_FILENAMES = {
    "样本A": os.environ.get("汇率档案_SAMPLE_样本A", "bills_sample_2026-03-17.json"),
    "tree": os.environ.get("汇率档案_SAMPLE_TREE", "bills_sample_2026-03-19.json"),
}
SAMPLES = {k: 汇率档案_DIR / f for k, f in _SAMPLE_FILENAMES.items()}


def _load_sample(name: str) -> dict:
    path = SAMPLES[name]
    if not path.is_file():
        pytest.skip(f"源样本不存在：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ── 纯函数 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "value", "code"),
    [
        ("790₸", 790.0, "KZT"),
        ("960,70₸ 资金", 960.70, "KZT"),      # 德式小数 + 「资金」尾缀
        ("$5.99 USD", 5.99, "USD"),
        ("$0.02 USD Credit", 0.02, "USD"),
        ("20,--€", 20.0, "EUR"),                # 德式 20,--
        ("A$ 35.00", 35.0, "AUD"),
        ("NT$ 650", 650.0, "TWD"),
        ("¥ 60.00", 60.0, "CNY"),
        ("-790₸", -790.0, "KZT"),
        ("CDN$ 49.99", 49.99, "CAD"),          # 长符号优先于 $
        ("1 365₸", 1365.0, "KZT"),
    ],
)
def test_parse_money(text, value, code):
    got_value, got_code = parse_money(text)
    assert got_code == code
    assert got_value == pytest.approx(value, abs=0.001)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026 年 3 月 17 日", "2026-03-17"),
        ("23 Feb, 2026", "2026-02-23"),
        ("2026-03-17", "2026-03-17"),
    ],
)
def test_parse_date_cn(text, expected):
    assert parse_date_cn(text) == expected


def test_get_rate_backtrack():
    """精确日缺失 → 回溯 15 天内取最近可用日。"""
    rows = [("2026-01-01", 0.15), ("2026-01-08", 0.16)]
    assert get_rate_from_rows(rows, "KZT", "2026-01-08") == 0.16
    assert get_rate_from_rows(rows, "KZT", "2026-01-10") == 0.16   # 回溯 2 天
    assert get_rate_from_rows(rows, "KZT", "2026-01-30") is None    # 超出 15 天
    assert get_rate_from_rows(rows, "CNY", "2026-01-30") == 1.0


def test_wallet_and_refund_classification():
    """钱包充值/充值退款识别 + 退款继承 orig_is_gift。"""
    data = {
        "account": {"nickname": "t", "avatar_base64": ""},
        "history": [
            # 普通购买（CNY）
            {"date": "2026 年 1 月 1 日", "item": "Game A", "type": "购买",
             "payment": "钱包", "total": "¥30", "wallet_change": "-¥30"},
            # 送出的礼物
            {"date": "2026 年 1 月 2 日", "item": "Game B", "type": "礼物购买",
             "payment": "钱包", "total": "¥50", "wallet_change": "-¥50"},
            # 该礼物的退款 → orig_is_gift 应为 True
            {"date": "2026 年 1 月 3 日", "item": "Game B", "type": "退款",
             "payment": "", "total": "¥50", "wallet_change": "+¥50"},
            # 钱包充值
            {"date": "2026 年 1 月 4 日", "item": "已购买 ¥60.00 钱包资金 退款", "type": "退款",
             "payment": "支付宝", "total": "¥ 60.00", "wallet_change": "+¥60"},
            # 市场交易（过滤）
            {"date": "2026 年 1 月 5 日", "item": "Case", "type": "市场交易",
             "payment": "", "total": "¥1", "wallet_change": ""},
            # 转换（过滤）
            {"date": "2026 年 1 月 5 日", "item": "货币转换至 KZT", "type": "转换",
             "payment": "钱包", "total": "", "wallet_change": ""},
        ],
        "licenses": [
            {"date": "2026 年 1 月 6 日", "item": "Retail Game", "method": "零售"},
            {"date": "2026 年 1 月 6 日", "item": "Free Game", "method": "免费赠送"},
        ],
    }
    parsed = parse_report(data)
    # 游戏交易 3 笔：购买 + 礼物 + 退款
    assert len(parsed["game_txs"]) == 3
    gift_refund = next(t for t in parsed["game_txs"] if t["is_refund"])
    assert gift_refund["orig_is_gift"] is True
    # 充值退款单独流水
    assert len(parsed["topup_txs"]) == 1
    assert parsed["topup_txs"][0]["is_refund"] is True
    # CDK 落库；免费赠送一并落库（只展示不计价）
    assert [g["acq"] for g in parsed["cdk_games"]] == ["cdk", "free"]


# ── 2026-09 新 UI：礼物标记剥离 + 正则模糊分类 ────────────────


@pytest.mark.parametrize(
    ("text", "expected_clean", "expected_recipients"),
    [
        # 退款行：退款尾缀在末位受赠人名之后
        ("Zefyr: A Thief's Melody 礼物已发送给 recipient_a 退款",
         "Zefyr: A Thief's Melody", ["recipient_a"]),
        # 受赠人名含空格（俄文名）
        ("Euro Truck Simulator 2 礼物已发送给 тест имя получателя",
         "Euro Truck Simulator 2", ["тест имя получателя"]),
        # 旧格式（无标记）原样返回
        ("Raging Loop", "Raging Loop", []),
    ],
)
def test_strip_gift_marks(text, expected_clean, expected_recipients):
    clean, recipients = strip_gift_marks(text)
    assert clean == expected_clean
    assert recipients == expected_recipients


def test_newformat_gift_recipients_field_priority():
    """新导出格式：受赠人走独立 gift_recipients 字段（DOM 层提取），
    item 内嵌标记仅作兜底剥离，不污染 summary_key。"""
    data = {
        "account": {"nickname": "t", "avatar_base64": ""},
        "history": [
            {
                "date": "2026 年 3 月 21 日",
                "item": "Wandering Sword - Secrets of the Eastern Sea",
                "type": "礼物购买",
                "payment": "钱包",
                "total": "₹ 264",
                "wallet_change": "-₹ 264",
                "wallet_balance": "₹ 2,357.30",
                "base_price": "₹ 345",
                "original_price": "₹ 345",
                "discount": "-23%",
                "gift_recipients": ["тест имя получателя"],
            },
            # 退款行：item 带退款尾缀（兜底口径）→ 剥净不进 items
            {
                "date": "2026 年 3 月 20 日",
                "item": "Dead Estate 退款",
                "type": "退款",
                "payment": "万事达卡 **52",
                "total": "₹ 170",
                "wallet_change": "+₹ 170",
                "gift_recipients": None,
            },
        ],
        "licenses": [],
    }
    parsed = parse_report(data)
    assert len(parsed["game_txs"]) == 2
    gift = next(t for t in parsed["game_txs"] if t["is_gift"])
    assert gift["gift_recipients"] == ["тест имя получателя"]
    assert gift["items"] == ["Wandering Sword - Secrets of the Eastern Sea"]
    assert gift["wallet_balance"] == "₹ 2,357.30"
    assert gift["base_price"] == "₹ 345"
    refund = next(t for t in parsed["game_txs"] if t["is_refund"])
    assert refund["items"] == ["Dead Estate"]
    assert refund["gift_recipients"] == []


@pytest.mark.parametrize(
    ("tx_type", "item", "wallet_change", "expected"),
    [
        # 英文/中文变体 → 分类器同源正则兜底
        ("Refund", "Game A", "", "refund"),
        ("Purchase", "Game A", "", "store"),
        ("Gift Purchase", "Game A", "", "gift"),
        ("In-Game Purchase", "DLC X", "", "ingame"),
        ("游戏内物品购买", "DLC X", "", "ingame"),       # Steam UI 文案变体
        ("Market Transaction", "Skin", "-¥10", "market_buy"),
        ("Market Transaction", "Skin", "+¥10", "market_sell"),
        ("Purchase", "Wallet Credit", "", "convert"),     # 物品含钱包资金
    ],
)
def test_classify_type(tx_type, item, wallet_change, expected):
    assert classify_type(tx_type, item, wallet_change) == expected


def test_newformat_recharge_base_price_fallback():
    """新 UI 充值行：total/wallet_change 空，金额在 base_price 列（后端抓取实测形态）。"""
    data = {
        "account": {"nickname": "t", "avatar_base64": ""},
        "history": [
            {
                "date": "2025 年 8 月 10 日",
                "item": "已购买 A$ 35.00 钱包资金",
                "type": "购买",
                "payment": "零售",
                "total": "",
                "wallet_change": "",
                "base_price": "A$ 35.00",
            },
            {
                "date": "2025 年 8 月 10 日",
                "item": "已购买 NT$ 650 钱包资金",
                "type": "购买",
                "payment": "零售",
                "total": "",
                "wallet_change": "",
                "base_price": "NT$ 650",
            },
        ],
        "licenses": [],
    }
    parsed = parse_report(data)
    assert len(parsed["topup_txs"]) == 2
    amounts = sorted(t["amount"] for t in parsed["topup_txs"])
    assert amounts == [35.0, 650.0]
    currencies = sorted(t["currency"] for t in parsed["topup_txs"])
    assert currencies == ["AUD", "TWD"]
    assert all(t["tx_type"] == "钱包充值" for t in parsed["topup_txs"])


def test_english_report():
    """英文导出（Purchase/Refund/Conversion/批量市场交易）同等处理。"""
    data = {
        "account": {"nickname": "en", "avatar_base64": ""},
        "history": [
            {"date": "2 Mar, 2026", "item": "Game A", "type": "Purchase",
             "payment": "Wallet", "total": "$5.99 USD", "wallet_change": "-$5.99 USD"},
            {"date": "3 Mar, 2026", "item": "Game B", "type": "Gift Purchase",
             "payment": "Wallet", "total": "$7.99 USD", "wallet_change": "-$7.99 USD"},
            {"date": "4 Mar, 2026", "item": "Game B", "type": "Refund",
             "payment": "", "total": "$7.99 USD", "wallet_change": "+$7.99 USD"},
            {"date": "5 Mar, 2026", "item": "Wallet Credit", "type": "Conversion",
             "payment": "", "total": "$20,--", "wallet_change": "+$20"},
            {"date": "6 Mar, 2026", "item": "6 items", "type": "6 Market Transactions",
             "payment": "", "total": "", "wallet_change": ""},
        ],
        "licenses": [
            {"date": "7 Mar, 2026", "item": "Retail G", "method": "Retail"},
            {"date": "7 Mar, 2026", "item": "Gifted G", "method": "Gift"},
        ],
    }
    parsed = parse_report(data)
    assert len(parsed["game_txs"]) == 3
    assert all(t["currency"] == "USD" for t in parsed["game_txs"])
    # Conversion 行 type 规范化后为「转换」→ 过滤；但这行 item 无钱包关键词且 total 可解析
    # 源逻辑：type 为空的行才是钱包充值；type=Conversion 直接过滤。
    assert len(parsed["topup_txs"]) == 0
    assert len(parsed["cdk_games"]) == 2
    assert parsed["cdk_games"][1]["acq"] == "gift"


# ── 许可分类（《Steam 许可分类器》MATCH_CFG 四类同源）─────────


@pytest.mark.parametrize(
    ("method", "expected_cat"),
    [
        ("Steam 商店", "store"),
        ("Steam Store", "store"),
        ("零售", "retail"),
        ("Retail", "retail"),
        ("免费赠送", "free"),
        ("Complimentary", "free"),
        ("贈品", "free"),
        ("礼物/玩家通行证", "gift"),
        ("Guest Pass", "gift"),
        ("禮物", "gift"),
        ("招待券", "gift"),
        ("未知方式", "other"),
    ],
)
def test_classify_license(method, expected_cat):
    from app.domains.bills.parser import classify_license

    cat, _label = classify_license(method)
    assert cat == expected_cat


def test_license_stats_and_cdk_scope():
    """license_stats 全量四类统计；cdk_games 收 retail/gift/free（store/other 不落库）。"""
    data = {
        "account": {"nickname": "t", "avatar_base64": ""},
        "history": [],
        "licenses": [
            {"date": "2026 年 1 月 1 日", "item": "Store Game", "method": "Steam 商店"},
            {"date": "2026 年 1 月 2 日", "item": "Free Game", "method": "免费赠送"},
            {"date": "2026 年 1 月 3 日", "item": "Retail Game", "method": "零售"},
            {"date": "2026 年 1 月 4 日", "item": "Gift Game", "method": "Gift"},
            {"date": "2026 年 1 月 5 日", "item": "Odd Game", "method": "Steam 商店"},
        ],
    }
    parsed = parse_report(data)
    assert parsed["license_stats"]["store"] == 2
    assert parsed["license_stats"]["free"] == 1
    assert parsed["license_stats"]["retail"] == 1
    assert parsed["license_stats"]["gift"] == 1
    assert parsed["license_stats"]["other"] == 0
    # cdk_games 收 retail/gift/free；store 类本身有账单流水，不重复建行
    assert [g["name"] for g in parsed["cdk_games"]] == ["Free Game", "Retail Game", "Gift Game"]
    assert all(g["acq"] in ("cdk", "gift", "free") for g in parsed["cdk_games"])


def test_license_rows_appid_extraction():
    """steam_fetch 许可行解析：名称列商店链接提取 appid（无链接行 → None）。"""
    from app.domains.bills.steam_fetch import parse_license_rows_from_html

    html = """
    <table class="account_table"><tbody>
    <tr>
      <td class="license_date_col">2026 年 1 月 6 日</td>
      <td><a href="https://store.steampowered.com/app/440/">Team Fortress 2</a>
          <a class="free_license_remove_link" href="#">移除</a></td>
      <td class="license_acquisition_col">免费赠送</td>
    </tr>
    <tr>
      <td class="license_date_col">2026 年 1 月 7 日</td>
      <td>Some Retail Key</td>
      <td class="license_acquisition_col">零售</td>
    </tr>
    </tbody></table>
    """
    rows = parse_license_rows_from_html(html)
    assert [r["appid"] for r in rows] == [440, None]
    assert rows[0]["item"] == "Team Fortress 2"
    assert rows[1]["method"] == "零售"


def test_parse_report_license_appid_passthrough():
    """parse_report 透传许可 appid；缺失/非整数字段 → None（不炸）。"""
    data = {
        "account": {"nickname": "t", "avatar_base64": ""},
        "history": [],
        "licenses": [
            {"date": "2026 年 1 月 1 日", "item": "Linked Game", "method": "零售", "appid": 440},
            {"date": "2026 年 1 月 2 日", "item": "Bare Game", "method": "礼物"},
            {"date": "2026 年 1 月 3 日", "item": "Bad Game", "method": "免费赠送", "appid": "xyz"},
        ],
    }
    parsed = parse_report(data)
    by_name = {g["name"]: g["appid"] for g in parsed["cdk_games"]}
    assert by_name == {"Linked Game": 440, "Bare Game": None, "Bad Game": None}


# ── steam_fetch：真实 HTML 样本解析（history 行提取器）────────

# 样本为本机 scripts/ 下的 gitignored 转储（_history_*.html，含真实消费记录）；
# 项目相对定位（仓库根/scripts），不存在则 skip
_FETCH_SAMPLES = Path(__file__).resolve().parents[2] / "scripts"


def _load_fetch_sample(name: str) -> str:
    path = _FETCH_SAMPLES / name
    if not path.is_file():
        pytest.skip(f"抓取样本不存在：{path}")
    return path.read_text(encoding="utf-8")


def test_steam_fetch_history_parser_real_html():
    """后端爬虫的 history 行提取器对 2026-09 实网样本的口径验证。

    与 parseHistoryRows 同构：受赠人独立字段、item 不含礼物标记/退款尾缀、
    批量市场行带 type_count、余额/基准价列透传。
    """
    from app.domains.bills.steam_fetch import parse_history_rows_from_html

    html = _load_fetch_sample("_history_page_sample.html")
    rows = parse_history_rows_from_html(html)
    assert len(rows) > 400
    # 受赠人独立提取（DOM 层 a[data-miniprofile]）
    gift_rows = [r for r in rows if r.get("gift_recipients")]
    assert gift_rows, "样本中应有礼物行"
    assert all(
        "礼物已发送给" not in (r["item"] or "") for r in rows
    ), "item 不应残留礼物标记"
    assert all(
        not r["item"].endswith("退款") for r in rows if r["item"]
    ), "item 不应残留退款尾缀"
    # 礼物行受赠人独立提取（真实样本中的实名人）
    all_recipients = {name for r in gift_rows for name in r["gift_recipients"]}
    assert all_recipients & {"Sea", "recipient_a", "тест имя получателя"}
    # 批量市场行 type_count 提取
    market_rows = [r for r in rows if (r.get("type_count") or 1) > 1]
    assert market_rows and all(r["type_count"] >= 2 for r in market_rows)


def test_steam_fetch_history_parser_first_screen():
    """首屏样本（81 行）同口径解析。"""
    from app.domains.bills.steam_fetch import parse_history_rows_from_html

    html = _load_fetch_sample("_history_first_screen.html")
    rows = parse_history_rows_from_html(html)
    assert len(rows) >= 75
    assert all(r["date"] for r in rows)


def test_steam_fetch_full_chain_matches_exporter_format():
    """爬虫提取 → parse_report 全链：与导出 JSON 同构可解析。"""
    from app.domains.bills.steam_fetch import parse_history_rows_from_html

    rows = []
    for name in ("_history_first_screen.html", "_history_page_sample.html"):
        rows.extend(parse_history_rows_from_html(_load_fetch_sample(name)))
    # 同款去重
    seen: set = set()
    deduped = []
    for r in rows:
        key = (r["date"], r["item"], r["total"], r["type"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    parsed = parse_report(
        {
            "account": {"nickname": "sample", "avatar_base64": ""},
            "history": deduped,
            "licenses": [],
        }
    )
    assert len(parsed["game_txs"]) > 150
    years = {t["date"][:4] for t in parsed["game_txs"]}
    assert "2021" in years and "2026" in years  # 早期记录全在（根因修复验证）


# ── 真实样本：解析产物对账（不触 DB）────────────────────────


@pytest.mark.parametrize(
    ("name", "game", "topup", "cdk", "spend", "refund"),
    [
        # 基准：源脚本实测输出
        ("样本A", 143, 108, 140, 8442.51, 1435.42),
        ("tree", 87, 41, 9, 4386.28, 1372.09),
    ],
)
def test_real_sample_parse(name, game, topup, cdk, spend, refund):
    parsed = parse_report(_load_sample(name))
    assert len(parsed["game_txs"]) == game
    assert len(parsed["topup_txs"]) == topup
    # 基线数字是「零售 + 礼物」口径——免费入库一并落库后不进基线，单独排除
    assert sum(1 for g in parsed["cdk_games"] if g["acq"] != "free") == cdk
    # 币种与数量粗校验（总口径依赖汇率库，见端到端用例）
    assert parsed["game_txs"], "解析产物为空"
    spend_any = sum(t["amount"] for t in parsed["game_txs"] if not t["is_refund"])
    assert spend_any > 0
    if name == "样本A":
        assert {t["currency"] for t in parsed["game_txs"]} == {"KZT", "CNY", "HKD"}
    else:
        assert {t["currency"] for t in parsed["game_txs"]} == {"USD", "RUB"}


# ── 端到端（临时库 + fx 种子）：与源脚本总口径对账 ──────────


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.core.database as database_module

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test_bills.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class _Factory:
        """factory(engine 双引用：service 只认 get_session_factory()。"""

        def __call__(self):
            return factory()

        @property
        def bind(self):
            return engine

    wrapped = _Factory()
    monkeypatch.setattr(database_module, "get_session_factory", lambda: wrapped)
    monkeypatch.setattr(bills_service, "get_session_factory", lambda: wrapped)
    return wrapped


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.bills.models  # noqa: F401
    import app.domains.rates.models  # noqa: F401

    from app.core.database import Base

    async with db.bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


async def _seed_fx(db, rows: list[tuple[str, float, str]]):
    """种子汇率：rows = (currency, date, rate_cny) 写入 fx_rate_history。"""
    from app.domains.rates.models import FxRateHistory

    from datetime import datetime

    async with db() as session:
        for cur, day, rate in rows:
            session.add(
                FxRateHistory(
                    currency_code=cur,
                    rate_to_cny=rate,
                    source="test",
                    fetched_at=datetime.fromisoformat(f"{day} 12:00:00"),
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_import_overview_roundtrip(db):
    """导入 → 总览：净支出/赠礼额度/充值净额口径与手工计算一致。"""
    data = {
        "account": {"nickname": "测试", "avatar_base64": ""},
        "history": [
            {"date": "2026 年 1 月 1 日", "item": "Game A", "type": "购买",
             "payment": "钱包", "total": "¥30", "wallet_change": "-¥30",
             "original_price": "¥60", "discount": "-50%"},
            {"date": "2026 年 1 月 2 日", "item": "Game B", "type": "礼物购买",
             "payment": "钱包", "total": "¥50", "wallet_change": "-¥50",
             "original_price": None, "discount": None},
            {"date": "2026 年 1 月 3 日", "item": "Game B", "type": "退款",
             "payment": "", "total": "¥50", "wallet_change": "+¥50"},
            {"date": "2026 年 1 月 4 日", "item": "已购买 ¥60.00 钱包资金", "type": "购买",
             "payment": "支付宝", "total": "¥ 60.00", "wallet_change": "+¥60"},
        ],
        "licenses": [
            {"date": "2026 年 1 月 6 日", "item": "CDK Game", "method": "零售", "appid": 1234},
            {"date": "2026 年 1 月 7 日", "item": "Free Game", "method": "免费赠送"},
        ],
    }
    res = await bills_service.import_report(data, "test.json")
    assert res["gameTxs"] == 3
    assert res["topupTxs"] == 1
    assert res["cdkGames"] == 2

    ov = await bills_service.overview(res["importId"])
    s = ov["summary"]
    assert ov["counts"]["free"] == 1
    # 净支出 = 30（Game A）；退款 50 冲回礼物
    assert s["netFen"] == 3000
    assert s["spendFen"] == 8000
    assert s["refundFen"] == 5000
    # 赠礼额度 = 自购净 30 - 礼物净 (50-50) = 30
    assert s["quotaFen"] == 3000
    assert s["selfNetFen"] == 3000
    assert s["giftNetFen"] == 0
    # 充值净额 60
    assert s["topupNetFen"] == 6000
    # 账户价值 = 自购净额（CDK 未计价）
    assert s["accountValueFen"] == 3000

    # CDK 计价 → 账户价值联动（free 行不计价、不进合计）
    cdk_list = await bills_service.cdk_games(res["importId"])
    cdk_row = next(r for r in cdk_list["rows"] if r["acq"] == "cdk")
    assert cdk_row["appid"] == 1234
    assert next(r for r in cdk_list["rows"] if r["acq"] == "free")["manualFen"] is None
    await bills_service.set_cdk_price(cdk_row["id"], 1500)
    ov2 = await bills_service.overview(res["importId"])
    assert ov2["summary"]["accountValueFen"] == 4500
    assert ov2["summary"]["cdkTotalFen"] == 1500

    # 明细过滤：年/类型/搜索
    rows = await bills_service.game_txs(res["importId"], year="2026")
    assert rows["total"] == 3
    rows = await bills_service.game_txs(res["importId"], tx_type="退款")
    assert rows["total"] == 1
    rows = await bills_service.game_txs(res["importId"], search="Game A")
    assert rows["total"] == 1

    # 删除
    await bills_service.delete_import(res["importId"])
    imports = await bills_service.list_imports()
    assert imports == []


@pytest.mark.asyncio
async def test_fx_multi_year(db):
    """回归：同币种跨年交易各自用对应年份的档案汇率（缓存曾按币种键控，
    2026 首笔加载后 2025 交易错落当前快照）。"""
    await _seed_fx(
        db,
        [
            ("KZT", "2026-01-05", 0.010),  # 2026 档案
            ("KZT", "2025-08-01", 0.020),  # 2025 档案
        ],
    )
    data = {
        "account": {"nickname": "multi-year", "avatar_base64": ""},
        "history": [
            # 2026 交易（先出现 → 触发 KZT 2026 窗口加载）
            {"date": "2026 年 1 月 6 日", "item": "Game 26", "type": "购买",
             "payment": "钱包", "total": "1000₸", "wallet_change": "-1000₸"},
            # 2025 交易（若缓存未按年键控，会错用快照/置空）
            {"date": "2025 年 8 月 2 日", "item": "Game 25", "type": "购买",
             "payment": "钱包", "total": "1000₸", "wallet_change": "-1000₸"},
        ],
        "licenses": [],
    }
    res = await bills_service.import_report(data, "multi.json")
    ov = await bills_service.overview(res["importId"])
    # 1000×0.010 + 1000×0.020 = 30 元
    assert ov["summary"]["spendFen"] == 3000
    assert ov["summary"]["fxMissing"] == 0
    # 两笔各自的汇率
    rows = await bills_service.game_txs(res["importId"])
    by_year = {r["date"][:4]: r["fxRate"] for r in rows["rows"]}
    assert by_year["2026"] == 0.010
    assert by_year["2025"] == 0.020


@pytest.mark.asyncio
async def test_fx_backtrack_and_missing(db, monkeypatch):
    """外币按当日汇率折算；档案缺失 15 天回溯；全缺则 cny_fen 置空不入总额。

    回归：fx_rates 当前快照**不**兜底历史交易（隐性错误）——补快照后重导，
    汇率缺失行依旧缺失。
    """
    await _seed_fx(db, [("KZT", "2026-01-08", 0.015)])
    data = {
        "account": {"nickname": "fx", "avatar_base64": ""},
        "history": [
            # 1月10日 KZT：无当日档案 → 回溯到 1月8日 0.015
            {"date": "2026 年 1 月 10 日", "item": "Game A", "type": "购买",
             "payment": "钱包", "total": "1000₸", "wallet_change": "-1000₸"},
            # 2月1日 KZT：档案窗口外（>15天）→ cny_fen 置空
            {"date": "2026 年 2 月 1 日", "item": "Game B", "type": "购买",
             "payment": "钱包", "total": "2000₸", "wallet_change": "-2000₸"},
        ],
        "licenses": [],
    }
    res = await bills_service.import_report(data, "fx.json")
    ov = await bills_service.overview(res["importId"])
    s = ov["summary"]
    # Game A: 1000 × 0.015 = 15 元；Game B 汇率缺失不入总额
    assert s["spendFen"] == 1500
    assert s["fxMissing"] == 1
    assert ov["monthSeries"][0]["month"] == "2026-01"

    # 补 fx_rates 当前快照后重新导入（同昵称覆盖）：快照不参与折算，缺失行依旧缺失
    from app.domains.rates.models import FxRate

    from datetime import datetime

    async with db() as session:
        session.add(FxRate(currency_code="KZT", rate_to_cny=0.012, fetched_at=datetime(2026, 2, 1)))
        await session.commit()
    res2 = await bills_service.import_report(data, "fx2.json")
    assert res2["replaced"] == 1  # 同昵称「fx」旧账被覆盖
    ov2 = await bills_service.overview(res2["importId"])
    assert ov2["summary"]["spendFen"] == 1500
    assert ov2["summary"]["fxMissing"] == 1
    imports = await bills_service.list_imports()
    assert len(imports) == 1  # 只剩覆盖后的新账


# ── sync_bills 调度语义（30min 周期 / 陈旧锁解死 / 失败等下一轮）──────────


def _patch_sync_env(monkeypatch, store: dict, fetch) -> list:
    """sync_bills 测试桩：settings 内存化 + 账号 Cookie + 抓取层假报告。

    settings 层打内存 dict（不触任何 session factory）——sync_bills 经
    `from app.domains.settings import service` 拿到的是同一模块对象，
    打属性即生效，DB 全程零接触。
    """
    from app.domains.settings import service as settings_service
    from app.domains.account import service as account_service
    from app.domains.bills import steam_fetch

    async def fake_get(key, default=None):
        return store.get(key, default)

    async def fake_set(key, value):
        store[key] = value

    monkeypatch.setattr(settings_service, "get_value", fake_get)
    monkeypatch.setattr(settings_service, "set_value", fake_set)

    async def fake_primary():
        return "steamLoginSecure=fake||12345"

    async def fake_strategy_proxy():
        return None

    monkeypatch.setattr(account_service, "get_primary_cookies", fake_primary)
    monkeypatch.setattr(account_service, "_strategy_proxy", fake_strategy_proxy)

    calls: list = []

    async def fake_fetch(cookies, proxy_url=None, on_progress=None):
        calls.append({"cookies": cookies, "proxy_url": proxy_url})
        return await fetch()

    monkeypatch.setattr(steam_fetch, "fetch_full_report", fake_fetch)

    # 首屏探测桩：恒"有新交易"（走全量分支——探测分流逻辑另有专项用例）。
    # mock 掉避免既有 sync_bills 用例碰真实网络（探测默认会建 httpx client）。
    async def fake_probe(cookies, *, known_latest_date=None, proxy_url=None):
        return True

    monkeypatch.setattr(steam_fetch, "probe_new_transactions", fake_probe)

    async def fake_import(report_json, source_file=""):
        return {
            "importId": 1, "nickname": "t", "gameTxs": 0, "topupTxs": 0,
            "cdkGames": 0, "fxMissing": 0, "warnings": [], "replaced": 0,
        }

    monkeypatch.setattr(bills_service, "import_report", fake_import)
    return calls


class _FakeReport:
    """抓取层假报告：单行占位（过空报告门禁；import_report 已打桩，不落库）。"""

    history: list = [{"date": "2026-09-01", "type": "购买", "item": "Game", "total": "$5.99 USD"}]
    licenses: list = [{"date": "2026-09-01", "item": "Game", "method": "Steam 商店"}]

    def to_report_json(self):
        return {"account": {"nickname": "t", "avatar_base64": ""}, "history": [], "topup": [], "licenses": []}


@pytest.mark.asyncio
async def test_sync_bills_busy_within_cycle(db, monkeypatch):
    """周期内 running=True → busy 跳过，不叠加第二份全量翻页。"""
    from app.crawler.utils import get_beijing_time_obj

    async def fetch():
        return _FakeReport()

    store: dict = {}
    calls = _patch_sync_env(monkeypatch, store, fetch)

    now = get_beijing_time_obj().replace(tzinfo=None)
    store[bills_service.KEY_BILLS_SYNC] = {"running": True, "since": now.isoformat()}

    res = await bills_service.sync_bills(force=False)
    assert res == {"ok": False, "status": "busy"}
    assert calls == []


@pytest.mark.asyncio
async def test_sync_bills_stale_lock_released(db, monkeypatch):
    """running 卡死超过一个同步周期（进程中途被杀）→ 死锁放行，本轮重新拉起。"""
    from datetime import timedelta

    from app.crawler.utils import get_beijing_time_obj

    async def fetch():
        return _FakeReport()

    store: dict = {}
    calls = _patch_sync_env(monkeypatch, store, fetch)

    now = get_beijing_time_obj().replace(tzinfo=None)
    store[bills_service.KEY_BILLS_SYNC] = {"running": True, "since": (now - timedelta(minutes=35)).isoformat()}

    res = await bills_service.sync_bills(force=False)
    assert res.get("ok") is True
    assert len(calls) == 1
    # 快照落回非 running：死锁解除后不再阻塞后续周期
    assert store[bills_service.KEY_BILLS_SYNC]["running"] is False


@pytest.mark.asyncio
async def test_sync_bills_failure_waits_next_cycle(db, monkeypatch):
    """单次失败不重试：错误写回快照即返回；下一次调用正常再拉（等下一轮语义）。"""
    from app.crawler.utils import get_beijing_time_obj

    state = {"fail": True}

    async def fetch():
        if state["fail"]:
            raise RuntimeError("proxy dead")
        return _FakeReport()

    store: dict = {}
    calls = _patch_sync_env(monkeypatch, store, fetch)

    res = await bills_service.sync_bills(force=False)
    assert res["ok"] is False and res["status"] == "error" and "proxy dead" in res["error"]
    assert len(calls) == 1
    assert store[bills_service.KEY_BILLS_SYNC]["running"] is False

    # 下一轮：失败态不阻塞，再次发起同步并成功
    state["fail"] = False
    res2 = await bills_service.sync_bills(force=False)
    assert res2.get("ok") is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_sync_bills_empty_report_rejected(db, monkeypatch):
    """空报告门禁：0 账单 + 0 许可 → 拒落库（不生成遮住旧快照的空 import）。"""

    class _EmptyReport:
        history: list = []
        licenses: list = []

        def to_report_json(self):
            return {"account": {"nickname": "t", "avatar_base64": ""}, "history": [], "topup": [], "licenses": []}

    async def fetch():
        return _EmptyReport()

    store: dict = {}
    calls = _patch_sync_env(monkeypatch, store, fetch)

    import_calls: list = []

    async def counting_import(report_json, source_file=""):
        import_calls.append(report_json)
        return {}

    monkeypatch.setattr(bills_service, "import_report", counting_import)

    res = await bills_service.sync_bills(force=False)
    assert res["ok"] is False and res["status"] == "error"
    assert "拒落空快照" in res["error"]
    assert import_calls == []  # DB 零接触：旧快照不被空 import 遮蔽
    assert len(calls) == 1
    assert store[bills_service.KEY_BILLS_SYNC]["running"] is False


# ── sync_bills 首屏探测分流（降频机制：无新交易不全量翻页）──────────


@pytest.mark.asyncio
async def test_sync_bills_probe_skip_when_no_new(db, monkeypatch):
    """探测无新交易：整轮跳过全量翻页（calls 零次），快照标 probedSkip。"""

    async def fetch():
        raise AssertionError("不应走到全量拉取")  # pragma: no cover

    store: dict = {}
    calls = _patch_sync_env(monkeypatch, store, fetch)

    # 探测桩改为"无新交易"（覆盖 _patch_sync_env 的默认 True）
    from app.domains.bills import steam_fetch as sf

    async def fake_probe(cookies, *, known_latest_date=None, proxy_url=None):
        return False

    monkeypatch.setattr(sf, "probe_new_transactions", fake_probe)

    res = await bills_service.sync_bills(force=False)
    assert res["ok"] is True and res["status"] == "probed_skip"
    assert calls == []  # 全量抓取零调用——整轮只有探测 1 个 GET
    assert store[bills_service.KEY_BILLS_SYNC]["probedSkip"] is True


@pytest.mark.asyncio
async def test_sync_bills_probe_force_bypasses(db, monkeypatch):
    """force=True（手动同步/绑定首拉）：探测被绕过，直接全量。"""

    async def fetch():
        return _FakeReport()

    store: dict = {}
    calls = _patch_sync_env(monkeypatch, store, fetch)

    probe_calls: list = []

    async def tracking_probe(cookies, *, known_latest_date=None, proxy_url=None):
        probe_calls.append(cookies)
        return False

    from app.domains.bills import steam_fetch as sf

    monkeypatch.setattr(sf, "probe_new_transactions", tracking_probe)

    res = await bills_service.sync_bills(force=True)
    assert res.get("ok") is True
    assert probe_calls == []  # force 路径不探测
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_sync_bills_probe_cookie_error_writes_error_snapshot(db, monkeypatch):
    """探测遇 Cookie 失效（SteamFetchError）：错误写回快照，等下一轮再探。"""

    async def fetch():
        raise AssertionError("不应走到全量拉取")  # pragma: no cover

    store: dict = {}
    calls = _patch_sync_env(monkeypatch, store, fetch)

    from app.domains.bills import steam_fetch as sf

    async def failing_probe(cookies, *, known_latest_date=None, proxy_url=None):
        raise sf.SteamFetchError("消费明细页返回登录页：Cookie 已过期/失效")

    monkeypatch.setattr(sf, "probe_new_transactions", failing_probe)

    res = await bills_service.sync_bills(force=False)
    assert res["ok"] is False and res["status"] == "error"
    assert "登录页" in res["error"]
    assert calls == []
    snap = store[bills_service.KEY_BILLS_SYNC]
    assert snap["running"] is False and "error" in snap


@pytest.mark.asyncio
async def test_probe_first_screen_logic(db):
    """探测判定核心逻辑：首行日期 > 库内最新 → True；≤ → False；无法解析 → 保守 True。"""
    from app.domains.bills import steam_fetch as sf

    class _ProbeClient:
        def __init__(self, first_date_html: str):
            self._html = first_date_html

        async def get(self, url, headers=None, **kwargs):
            return _FakeResp(text=self._html)

    def _page(first_date: str) -> str:
        return (
            '<table class="wallet_history_table">'
            f'<tr class="wallet_table_row"><td class="wht_date">{first_date}</td></tr>'
            "</table>"
        )

    # 首行 2026-09-10 > 库内 09-01 → 有新交易
    assert await sf.probe_history_first_screen(_ProbeClient(_page("2026年9月10日")), "2026-09-01") is True
    # 首行 2026-09-01 ≤ 库内 09-01 → 无新交易（同日新交易场景保守跳过：
    # 同日增量少见，且下轮探测/手动 force 可兜）
    assert await sf.probe_history_first_screen(_ProbeClient(_page("2026年9月1日")), "2026-09-01") is False
    # 库内无日期（首拉语义）→ True
    assert await sf.probe_history_first_screen(_ProbeClient(_page("2020年1月1日")), None) is True
    # 页面无表格（结构异常）→ 保守 True
    assert await sf.probe_history_first_screen(_ProbeClient("<html>empty</html>"), "2026-09-01") is True


# ── steam_fetch：账号身份提取（头像域名兼容）──────────────────


class _FakeResp:
    def __init__(self, text="", content=b"", status_code=200, url="https://store.steampowered.com/account/history/"):
        self.text = text
        self.content = content
        self.status_code = status_code
        self.url = url

    def raise_for_status(self):
        pass


class _FakeClient:
    """_fetch_account_identity 依赖的最小 client 桩：账号页 HTML + 头像字节。"""

    def __init__(self, page_html: str, img_bytes: bytes = b"img-bytes", img_status: int = 200):
        self.page_html = page_html
        self.img_bytes = img_bytes
        self.img_status = img_status
        self.fetched_urls: list[str] = []

    async def get(self, url, headers=None, **kwargs):
        self.fetched_urls.append(url)
        if "store.steampowered.com/account" in url:
            return _FakeResp(text=self.page_html)
        return _FakeResp(content=self.img_bytes, status_code=self.img_status)


_PAGE = (
    '<div id="account_pulldown">测试昵称A</div>'
    '<img class="userAvatar" src="{url}">'
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url,mime",
    [
        ("https://avatars.fastly.steamstatic.com/abc_full.jpg", "image/jpeg"),
        ("https://avatars.akamaized.net/abc_full.jpg", "image/jpeg"),
        ("https://avatars.steamstatic.com/abc.png", "image/png"),
    ],
)
async def test_fetch_account_identity_avatar_domains(url, mime):
    """新旧头像域名都要能抓到（2026-09 账号页迁移 fastly.steamstatic 后头像恒空的修复）。"""
    from app.domains.bills.steam_fetch import _fetch_account_identity

    client = _FakeClient(_PAGE.format(url=url))
    nickname, avatar = await _fetch_account_identity(client)
    assert nickname == "测试昵称A"
    assert avatar.startswith(f"data:{mime};base64,")
    import base64

    assert base64.b64decode(avatar.split(",", 1)[1]) == b"img-bytes"
    assert client.fetched_urls[-1] == url


@pytest.mark.asyncio
async def test_fetch_account_identity_no_avatar_nonblocking():
    """页面无头像 img / 下载失败都不阻断：返回空串，昵称照常。"""
    from app.domains.bills.steam_fetch import _fetch_account_identity

    client = _FakeClient(_PAGE.format(url=""))
    nickname, avatar = await _fetch_account_identity(client)
    assert nickname == "测试昵称A"
    assert avatar == ""

    client2 = _FakeClient(_PAGE.format(url="https://avatars.fastly.steamstatic.com/x.jpg"), img_status=404)
    nickname2, avatar2 = await _fetch_account_identity(client2)
    assert nickname2 == "测试昵称A"
    assert avatar2 == ""


# ── steam_fetch：登录页守门（Cookie 过期拒产空报告）──────────────


def test_login_page_gate_by_url():
    """302 终点是登录 URL（login/signin 子串）→ 判登录页。"""
    from app.domains.bills.steam_fetch import _is_login_page

    resp = _FakeResp(text="<html><body>whatever</body></html>",
                     url="https://store.steampowered.com/login/?redir=account%2Fhistory%2F")
    assert _is_login_page(resp) is True


def test_login_page_gate_by_title():
    """终点 URL 无线索时看 HTML 标题（Sign In / 登录）。"""
    from app.domains.bills.steam_fetch import _is_login_page

    assert _is_login_page(_FakeResp(text="<html><title>Sign In</title></html>")) is True
    assert _is_login_page(_FakeResp(text="<html><title>欢迎登录 Steam</title></html>")) is True


def test_login_page_gate_normal_page_passes():
    """正常账号页（含表格）不误伤。"""
    from app.domains.bills.steam_fetch import _is_login_page

    resp = _FakeResp(
        text='<html><title>Welcome to Steam</title><table class="account_table">...</table></html>',
    )
    assert _is_login_page(resp) is False


@pytest.mark.asyncio
async def test_fetch_history_login_page_raises():
    """Cookie 过期 → Steam 返回登录页 → 抛 SteamFetchError（不再静默产空报告）。"""
    from app.domains.bills import steam_fetch

    class _LoginClient:
        async def get(self, url, headers=None, **kwargs):
            return _FakeResp(
                text="<html><title>Sign In</title></html>",
                url="https://store.steampowered.com/login/?redir=account%2Fhistory%2F",
            )

    with pytest.raises(steam_fetch.SteamFetchError, match="登录页"):
        await steam_fetch._fetch_history(_LoginClient())
