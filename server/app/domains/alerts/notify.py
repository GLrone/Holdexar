"""邮件通知（SMTP）。

配置存 app_settings：smtp.host / smtp.port / smtp.user / smtp.password /
smtp.to_addr / smtp.use_ssl（默认 True，QQ/163 邮箱 465 端口）。
未配置时静默跳过。
"""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from app.core.app_info import APP_NAME
from app.domains.settings.service import get_value, set_value

logger = logging.getLogger(__name__)


async def smtp_config() -> dict:
    """读取 SMTP 配置（明文返回 password，仅后端内部使用）。"""
    return {
        "host": await get_value("smtp.host", ""),
        "port": int(await get_value("smtp.port", 465) or 465),
        "user": await get_value("smtp.user", ""),
        "password": await get_value("smtp.password", ""),
        "to_addr": await get_value("smtp.to_addr", ""),
        "use_ssl": await get_value("smtp.use_ssl", True),
    }


async def get_smtp_config() -> dict:
    """返回 SMTP 配置（前端用），密码做掩码处理。"""
    cfg = await smtp_config()
    has_password = bool(cfg["password"])
    # 掩码：只显示首尾字符
    pwd = cfg["password"]
    if len(pwd) > 2:
        masked = pwd[0] + "*" * (len(pwd) - 2) + pwd[-1]
    elif len(pwd) > 0:
        masked = "*" * len(pwd)
    else:
        masked = ""
    return {
        "host": cfg["host"],
        "port": cfg["port"],
        "user": cfg["user"],
        "password": masked,
        "hasPassword": has_password,
        "toAddr": cfg["to_addr"],
        "useSsl": cfg["use_ssl"],
    }


async def update_smtp_config(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    to_addr: str,
    use_ssl: bool,
) -> dict:
    """更新 SMTP 配置。password 为空字符串时保持原密码不变。"""
    await set_value("smtp.host", host)
    await set_value("smtp.port", port)
    await set_value("smtp.user", user)
    if password:
        await set_value("smtp.password", password)
    await set_value("smtp.to_addr", to_addr)
    await set_value("smtp.use_ssl", use_ssl)
    logger.info("SMTP 配置已更新: host=%s user=%s", host, user)
    return await get_smtp_config()


async def send_mail(subject: str, html_body: str) -> bool:
    cfg = await smtp_config()
    if not (cfg["host"] and cfg["user"] and cfg["password"] and cfg["to_addr"]):
        logger.info("SMTP 未配置，跳过邮件通知")
        return False
    try:
        if cfg["use_ssl"]:
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=20)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
            server.starttls()
        with server:
            server.login(cfg["user"], cfg["password"])
            message = MIMEText(html_body, "html", "utf-8")
            message["Subject"] = Header(subject, "utf-8")
            message["From"] = formataddr((APP_NAME, cfg["user"]))
            message["To"] = cfg["to_addr"]
            server.sendmail(cfg["user"], [cfg["to_addr"]], message.as_string())
        logger.info("邮件已发送: %s", subject)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("邮件发送失败: %s", e)
        return False


# ─── 通用邮件主题骨架（全部邮件同款 Holdexar 深色视觉）────────────────


def _theme_colors() -> dict[str, str]:
    """主题色板（tokens.css html.dark 同源色值，各邮件模板共用）。"""
    return {
        "accent": "#66c0f4",        # --accent (dark)
        "success": "#a4d007",       # --success (dark)
        "danger": "#f44336",         # 降价红（自选，主题体系内强调色）
        "bg_page": "#0e1a27",       # --bg-base
        "bg_card": "#1b2838",       # --bg-card
        "bg_chip": "#223449",       # --bg-chip
        "text_main": "#d9e3ec",     # --text-primary
        "text_sub": "#8f98a0",      # --text-muted
        "border_strong": "#2a475e",  # --border-strong 等效实色
        "row_border": "#2e3d4f",    # --row-border 等效实色
    }


def _page_html(c: dict[str, str], card_inner: str) -> str:
    """外层页面骨架：深底 + 居中 520px 容器 + 品牌头 + 主卡 + 页脚。

    邮件客户端 CSS 支持有限（Outlook/QQ 邮箱），全部内联样式 + 表格布局，
    不依赖外部图片与 class。card_inner 为各邮件模板的主卡内容行（<tr>）。
    """
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<body style="margin:0;padding:0;background-color:{c['bg_page']};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{c['bg_page']}" style="background-color:{c['bg_page']};">
  <tr>
    <td align="center" style="padding:36px 16px;">
      <table role="presentation" width="520" cellpadding="0" cellspacing="0" border="0" style="max-width:520px;width:100%;">
        <!-- 品牌头 -->
        <tr>
          <td style="padding:0 4px 14px;">
            <span style="font-size:20px;font-weight:700;color:{c['accent']};letter-spacing:.5px;">Holdexar</span>
            <span style="font-size:11px;color:{c['text_sub']};margin-left:10px;letter-spacing:1px;">STEAM 多区价格监控终端</span>
          </td>
        </tr>
        <!-- 主卡片 -->
        <tr>
          <td bgcolor="{c['bg_card']}" style="background-color:{c['bg_card']};border:1px solid {c['border_strong']};border-radius:14px;padding:32px 8px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              {card_inner}
            </table>
          </td>
        </tr>
        <!-- 页脚 -->
        <tr>
          <td align="center" style="padding:16px 4px 0;font-size:11px;color:{c['text_sub']};">
            此邮件由 {APP_NAME} 自动发送，请勿直接回复
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>"""


def _detail_table(c: dict[str, str], rows: list[tuple[str, str]]) -> str:
    """键值详情表（深底内嵌面板，测试邮件连接详情同款）。"""
    from html import escape

    detail_rows = "".join(
        f'<tr>'
        f'<td style="padding:9px 14px;font-size:12px;color:{c["text_sub"]};white-space:nowrap;border-bottom:1px solid {c["row_border"]};">{label}</td>'
        f'<td style="padding:9px 14px;font-size:13px;color:{c["text_main"]};font-family:Consolas,monospace;text-align:right;border-bottom:1px solid {c["row_border"]};">{value}</td>'
        f"</tr>"
        for label, value in rows
    )
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background-color:{c["bg_page"]};border-radius:10px;border:1px solid {c["border_strong"]};">{detail_rows}</table>'
    )


def _headline_card(c: dict[str, str], *, badge: str, badge_color: str, title: str, subtitle: str, panel_html: str) -> str:
    """主卡内容三段式：徽章 + 标题/说明 + 详情面板（panel_html 可为空）。"""
    parts = [
        f'<tr><td align="center" style="padding:0 24px 18px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td align="center" bgcolor="{c["bg_chip"]}" width="56" height="56" style="width:56px;height:56px;border-radius:28px;background-color:{c["bg_chip"]};border:1px solid {c["border_strong"]};font-size:26px;color:{badge_color};font-weight:700;line-height:56px;">{badge}</td>'
        f'</tr></table></td></tr>',
        f'<tr><td align="center" style="padding:0 24px 8px;font-size:19px;font-weight:700;color:{c["text_main"]};">{title}</td></tr>',
        f'<tr><td align="center" style="padding:0 32px 22px;font-size:13px;line-height:1.8;color:{c["text_sub"]};">{subtitle}</td></tr>',
    ]
    if panel_html:
        parts.append(f'<tr><td style="padding:0 24px;">{panel_html}</td></tr>')
    return "".join(parts)


# ─── 连通性测试邮件（前端「测试连通性」键）───────────────────────


def _test_mail_html(*, host: str, port: int, user: str, to_addr: str, use_ssl: bool) -> str:
    """测试邮件正文：通用主题骨架 + 连接详情面板。"""
    from html import escape

    c = _theme_colors()
    rows = [
        ("SMTP 服务器", f"{escape(host)}:{port}"),
        ("加密方式", "SSL/TLS" if use_ssl else "STARTTLS"),
        ("发件账户", escape(user)),
        ("收件邮箱", escape(to_addr)),
        ("测试时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ]
    card = _headline_card(
        c,
        badge="&#10003;",
        badge_color=c["success"],
        title="连接成功",
        subtitle=f"你的邮箱已成功接入 {APP_NAME}。此后当提醒规则命中目标价格时，我们会第一时间把降价消息送到这个邮箱。",
        panel_html=_detail_table(c, rows),
    )
    return _page_html(c, card)


async def send_test_mail(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    to_addr: str,
    use_ssl: bool,
) -> dict:
    """发送连通性测试邮件。password 为空时回退存量密码。

    配置不完整或 SMTP 失败均抛 ValueError（消息可直接展示给用户），
    成功返回 {"ok": True}。
    """
    if not password:  # 表单密码留空 = 沿用已存密码
        password = await get_value("smtp.password", "")
    missing = [
        name
        for name, val in (
            ("SMTP 服务器", host),
            ("发件账户", user),
            ("授权码/密码", password),
            ("收件邮箱", to_addr),
        )
        if not val
    ]
    if missing:
        raise ValueError(f"请先填写：{'、'.join(missing)}")

    subject = f"欢迎使用 {APP_NAME} · 邮箱连通性测试"
    html = _test_mail_html(host=host, port=port, user=user, to_addr=to_addr, use_ssl=use_ssl)
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls()
        with server:
            server.login(user, password)
            message = MIMEText(html, "html", "utf-8")
            message["Subject"] = Header(subject, "utf-8")
            message["From"] = formataddr((APP_NAME, user))
            message["To"] = to_addr
            server.sendmail(user, [to_addr], message.as_string())
        logger.info("连通性测试邮件已发送: %s -> %s", user, to_addr)
        return {"ok": True}
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001 — SMTP 错误原文回传前端
        logger.warning("连通性测试失败: %s", e)
        raise ValueError(f"连接失败：{e}") from e


# ─── 提醒触发邮件正文（与测试邮件同款主题骨架）────────────────────


def _result_row(c: dict[str, str], cells: list[str]) -> str:
    """结果表一行（首列游戏名左对齐，其余列右对齐等宽呈现价格数字）。"""
    tds = "".join(
        f'<td style="padding:10px 14px;font-size:13px;color:{c["text_main"]};'
        f'{"text-align:right;font-family:Consolas,monospace;white-space:nowrap;" if i else ""}'
        f'border-bottom:1px solid {c["row_border"]};">{value}</td>'
        for i, value in enumerate(cells)
    )
    return f"<tr>{tds}</tr>"


def _result_table(c: dict[str, str], headers: list[str], rows: list[list[str]]) -> str:
    """结果表：深底内嵌面板 + 深色表头 + 行分隔线（与 _detail_table 同族视觉）。"""
    head_tds = "".join(
        f'<td style="padding:8px 14px;font-size:12px;color:{c["text_sub"]};'
        f'{"text-align:right;" if i else ""}border-bottom:1px solid {c["border_strong"]};">{h}</td>'
        for i, h in enumerate(headers)
    )
    body = "".join(_result_row(c, cells) for cells in rows)
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background-color:{c["bg_page"]};border-radius:10px;border:1px solid {c["border_strong"]};">'
        f'<tr>{head_tds}</tr>{body}</table>'
    )


def alert_mail_html(triggered: list[dict]) -> str:
    """价格提醒触发邮件正文。

    triggered 项：{name, region, price(分), discount}（check_appids 产出）。
    """
    from html import escape

    from app.domains.games.pricing import format_cny_fen

    c = _theme_colors()
    rows = [
        [
            escape(str(t["name"])),
            escape(t["region"]),
            format_cny_fen(t.get("price")),
            f"-{t.get('discount') or 0}%",
        ]
        for t in triggered
    ]
    card = _headline_card(
        c,
        badge="&#9660;",
        badge_color=c["danger"],
        title="价格提醒已触发",
        subtitle=f"以下 {len(triggered)} 款游戏命中了你设置的提醒条件，价格已到目标位，考虑入手：",
        panel_html=_result_table(
            c, ["游戏", "区服", "当前价", "折扣"], rows
        ),
    )
    return _page_html(c, card)


def new_low_mail_html(picks: list[dict]) -> str:
    """新史低精选邮件正文（每封仅展示 1-2 款，不做全量清单）。

    picks 项：{name, region_name, price_text, cn_price_text, discount}（check_new_lows 产出）。
    """
    from html import escape

    c = _theme_colors()
    rows = [
        [
            escape(str(p["name"])),
            escape(p["region_name"]),
            escape(p["price_text"]),
            escape(p["cn_price_text"]),
            f"-{p.get('discount') or 0}%",
        ]
        for p in picks
    ]
    card = _headline_card(
        c,
        badge="&#9733;",
        badge_color=c["accent"],
        title="新史低速报",
        subtitle="监控池里的游戏刷新出了新的历史最低价，挑了 2 款给你过目，可直接前往 Steam 查看：",
        panel_html=_result_table(
            c, ["游戏", "最低价区", "最低价", "国区价", "折扣"], rows
        ),
    )
    return _page_html(c, card)
