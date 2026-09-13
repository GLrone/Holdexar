"""连通性测试邮件（notify.send_test_mail）：打桩 smtplib，零真实发信。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.alerts import notify  # noqa: E402


class _FakeSMTP:
    """捕获 login/sendmail 调用；记录 MIMEText 内容供断言。"""

    last_instance: "_FakeSMTP | None" = None

    def __init__(self, host, port, timeout=0):
        self.host = host
        self.port = port
        self.logged_in = None
        self.sent = None
        _FakeSMTP.last_instance = self

    def login(self, user, password):
        self.logged_in = (user, password)

    def sendmail(self, from_addr, to_addrs, msg_string):
        self.sent = (from_addr, to_addrs, msg_string)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def fake_smtp(monkeypatch):
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP", _FakeSMTP)
    _FakeSMTP.last_instance = None
    return _FakeSMTP


@pytest.fixture()
def stored_password(monkeypatch):
    """存量密码 smtp.password = saved-pass。"""

    async def fake_get_value(key, default=None):
        return {"smtp.password": "saved-pass"}.get(key, default)

    monkeypatch.setattr(notify, "get_value", fake_get_value)


FORM = dict(host="smtp.qq.com", port=465, user="me@qq.com", password="", to_addr="to@qq.com", use_ssl=True)


@pytest.mark.asyncio
async def test_subject_and_themed_body(fake_smtp, stored_password):
    """主题含「欢迎使用 Holdexar」，正文为主题化 HTML（深色卡 + 连接成功 + 详情行）。"""
    import email
    import email.policy

    result = await notify.send_test_mail(**FORM)
    assert result == {"ok": True}

    _from, _to, msg = fake_smtp.last_instance.sent
    parsed = email.message_from_string(msg, policy=email.policy.default)
    assert "欢迎使用 Holdexar" in parsed["Subject"]
    body = parsed.get_content()
    # 主题化正文关键要素
    for needle in ("连接成功", "#66c0f4", "#1b2838", "SMTP 服务器", "smtp.qq.com:465", "SSL/TLS"):
        assert needle in body, f"正文缺少 {needle}"


@pytest.mark.asyncio
async def test_empty_password_falls_back_to_stored(fake_smtp, stored_password):
    """表单密码留空 → 回退存量密码登录（不要求用户重填授权码）。"""
    await notify.send_test_mail(**FORM)
    assert fake_smtp.last_instance.logged_in == ("me@qq.com", "saved-pass")


@pytest.mark.asyncio
async def test_missing_config_raises_with_field_names(fake_smtp, monkeypatch):
    """配置缺失：ValueError 列出全部缺失项（不发起 SMTP 连接）。"""

    async def no_password(key, default=None):
        return default

    monkeypatch.setattr(notify, "get_value", no_password)
    with pytest.raises(ValueError) as ei:
        await notify.send_test_mail(**{**FORM, "host": "", "to_addr": ""})
    msg = str(ei.value)
    assert "SMTP 服务器" in msg and "收件邮箱" in msg
    assert fake_smtp.last_instance is None  # 未走到连接


@pytest.mark.asyncio
async def test_smtp_error_wrapped_as_chinese_valueerror(fake_smtp, stored_password, monkeypatch):
    """SMTP 失败 → ValueError「连接失败：<原因>」，前端可直接展示。"""
    import smtplib

    class _FailSMTP(_FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"(535) auth failed")

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _FailSMTP)
    with pytest.raises(ValueError) as ei:
        await notify.send_test_mail(**FORM)
    assert "连接失败" in str(ei.value)
    assert "535" in str(ei.value)
