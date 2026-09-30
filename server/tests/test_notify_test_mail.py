"""连通性测试邮件（notify.send_test_mail）：打桩 smtplib，零真实发信。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.alerts import notify  # noqa: E402


class _FakeSMTP:
    """捕获 auth/sendmail 调用；记录 MIMEText 内容供断言。"""

    last_instance: "_FakeSMTP | None" = None

    def __init__(self, host, port, timeout=0):
        self.host = host
        self.port = port
        self.authenticated = None
        self.auth_attempts: list[str] = []
        self.sent = None
        self.esmtp_features = {"auth": "LOGIN PLAIN"}
        _FakeSMTP.last_instance = self

    def ehlo_or_helo_if_needed(self):
        return None

    def has_extn(self, name):
        return name in self.esmtp_features

    def auth(self, mechanism, authobject, *, initial_response_ok=True):
        self.auth_attempts.append(mechanism)
        self.authenticated = (self.user, self.password)

    def auth_plain(self, challenge=None):
        return f"\0{self.user}\0{self.password}"

    def auth_login(self, challenge=None):
        return self.user

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
    """表单密码留空 → 回退存量密码认证（不要求用户重填授权码）。"""
    await notify.send_test_mail(**FORM)
    assert fake_smtp.last_instance.authenticated == ("me@qq.com", "saved-pass")


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
    """非认证类 SMTP 失败 → ValueError「连接失败：<原因>」，前端可直接展示。"""
    import smtplib

    class _FailSMTP(_FakeSMTP):
        def sendmail(self, from_addr, to_addrs, msg_string):
            raise smtplib.SMTPConnectError(421, "service not available")

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _FailSMTP)
    with pytest.raises(ValueError) as ei:
        await notify.send_test_mail(**FORM)
    assert "连接失败" in str(ei.value)
    assert "421" in str(ei.value)


@pytest.mark.asyncio
async def test_auth_rejection_surfaces_first_535(fake_smtp, stored_password, monkeypatch):
    """认证被拒：首个 535 即作为用户可读原因上抛，不在被拒连接上换机制重试。

    smtplib.login 会在同一条连接上继续尝试下一机制，部分服务商会直接断开
    连接，调用方只能看到「连接被断」——真实拒绝原因丢失。
    """
    import smtplib

    class _QQLikeSMTP(_FakeSMTP):
        def auth(self, mechanism, authobject, *, initial_response_ok=True):
            self.auth_attempts.append(mechanism)
            raise smtplib.SMTPAuthenticationError(
                535, b"Login fail. service is not open, password is incorrect.")

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _QQLikeSMTP)
    with pytest.raises(ValueError) as ei:
        await notify.send_test_mail(**FORM)
    msg = str(ei.value)
    assert "邮箱拒绝登录（535）" in msg
    assert "IMAP/SMTP" in msg
    assert "连接失败" not in msg
    assert _QQLikeSMTP.last_instance.auth_attempts == ["PLAIN"]
