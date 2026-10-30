"""摘要邮件渲染验收：digest 与连通性测试邮件共用同一深色主题骨架。

触发与投递的行为验收在 test_alert_rule_contract.py 与 test_notification_contract.py；
安全转义与 URL 防护在 test_mail_rendering.py。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.alerts import notify
from app.domains.notifications import mail


def _digest(**kw):
    base = dict(counts=[("价格变化", 2)], details=["价格变化 · 国区 · ¥42.00 → ¥12.60 · Portal 2"],
                total=2, games=1, deferred=0, when=datetime(2026, 10, 30, 12, 0))
    base.update(kw)
    return mail.digest_mail_html(**base)


@pytest.mark.parametrize(
    "builder",
    [
        lambda: _digest(),
        lambda: notify._test_mail_html(host="smtp.qq.com", port=465, user="me@qq.com",
                                       to_addr="to@qq.com", use_ssl=True),
    ],
    ids=["digest", "test"],
)
def test_all_mails_share_theme(builder):
    """两类邮件同一深色主题骨架：品牌头 + 主卡 + 页脚 + 关键色。"""
    html = builder()
    for needle in ("Holdexar", "STEAM 多区价格监控终端",
                   "#66c0f4", "#1b2838", "#0e1a27", "border-radius:14px"):
        assert needle in html, f"{needle} 缺失"


def test_digest_renders_counts_details_and_overflow():
    html = _digest(details=["行一", "行二", "行三"], total=30, games=12, deferred=5)
    assert "本轮价格更新" in html and "30 条变化" in html
    assert "行一" in html and "行三" in html
    assert "5 条来自更早的轮次" in html
    assert "不再逐条列出" in html  # 超出明细上限的余量提示

    empty = _digest(counts=[], details=[], total=0, games=0)
    assert "0 条变化" in empty


def test_digest_plain_text_keeps_lines_without_html():
    plain = notify.mail_text(_digest())
    assert "价格变化 · 国区 · ¥42.00 → ¥12.60 · Portal 2" in plain
    assert "<table" not in plain and "<style" not in plain and "<html" not in plain
