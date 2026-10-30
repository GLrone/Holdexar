"""摘要邮件安全验收：外部字段转义、链接协议校验、缺价不编造、纯文本一致。

渲染入口 = notifications/mail.py 的摘要模板与 alerts/notify.py 的共用组件。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest
from html.parser import HTMLParser

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.alerts import notify
from app.domains.notifications import mail


class _Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def _digest(details):
    return mail.digest_mail_html(
        counts=[("价格变化", 1)], details=details, total=1, games=1,
        deferred=0, when=datetime(2026, 10, 30, 12, 0),
    )


def test_digest_details_cannot_inject_markup():
    value = '<img src="https://evil.invalid/x" onerror="bad"><script>bad</script>'
    html = _digest([f"价格变化 · {value}"])
    elements = _Elements(html).elements
    assert not any(tag in ("img", "script") for tag, _ in elements)
    assert "<script" not in html and "onerror=\"bad\"" not in html
    assert value in notify.mail_text(_digest([f"价格变化 · {value}"]))


def test_plain_text_keeps_prices_reasons_without_html():
    details = ["指定价格 · 美国 · $15.00 · Portal 2", "连接通道提醒 · 全部代理通道不可用"]
    plain = notify.mail_text(_digest(details))
    for value in ("指定价格 · 美国 · $15.00 · Portal 2", "全部代理通道不可用"):
        assert value in plain
    assert "<table" not in plain and "<style" not in plain and "<html" not in plain


def test_text_extraction_ignores_scripts_and_styles():
    assert notify.mail_text('<head><style>bad</style></head><p>USD 12.34</p>'
                            '<script>bad</script>') == "USD 12.34"


# ── 链接协议校验（mail_text 提取链接时的 _safe_url 防线）──────────


@pytest.mark.parametrize("url", [
    "https://store.steampowered.com/app/620/?cc=us&l=english",
    "https://store.epicgames.com/en-US/p/example",
])
def test_known_store_links_survive_extraction(url):
    html = f'<a href="{url.replace("&", "&amp;")}">查看</a>'
    assert notify._safe_url(url) == url


@pytest.mark.parametrize("url", [
    "data:text/html,<b>bad</b>", "javascript:alert(1)",
    "https://store.steampowered.com.evil.invalid/",
    "https://user:pass@store.steampowered.com/", "https://evil.invalid/",
])
def test_invalid_destinations_are_rejected(url):
    assert notify._safe_url(url) == ""


# ── 价格事实边界：缺价不编造、未知币种不编符号 ───────────────────


def test_missing_price_renders_nothing_and_unknown_currency_has_no_symbol():
    from app.domains.notifications.mail import _price_text

    assert _price_text(None, "US") == ""
    assert _price_text(1234, "XX") == ""  # 区服表外无可靠币种，宁可空着
    assert _price_text(1234, "US") == "$12.34"


def test_digest_missing_details_do_not_invent_cny():
    html = _digest(["价格变化 · 美国 · G100"])
    assert "¥" not in html
