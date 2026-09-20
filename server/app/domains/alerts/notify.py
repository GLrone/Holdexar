"""邮件通知（SMTP）与邮件模板。

配置存 app_settings：smtp.host / smtp.port / smtp.user / smtp.password /
smtp.to_addr / smtp.use_ssl（默认 True，QQ/163 邮箱 465 端口）。
未配置时静默跳过。

模板层（本文件下半部）是一套零依赖的「表格 + 内联样式」组件库：邮件客户端
（QQ 邮箱 / Outlook / Gmail）不支持 flex / grid / 外部 CSS，版式只能靠
`<table role="presentation">` 与行内样式表达。全部邮件共用同一套深色主题
令牌（与前端 tokens.css 的 dark 主题同源），并按类型复用原子组件：

- `_cover_url()` / `_flag_url()` / `_region_store_url()` —— 三类显著提升游戏
  辨识度的外部资源：封面图（Steam header / Epic keyImage）、初始区旗、
  该区商店直链（`?cc=` 直达对应区价格页）；
- `_game_row()` —— 紧凑单行卡（封面缩略图 + 区旗 + 该区价 + 国区价 + 折扣）；
- `_feature_card()` —— 大封面主推卡（新史低 / Epic 喜加一）；
- `_stat_table()` —— 系统与通道告警的键值面板。

各邮件正文一律由 `_page_html()` 套壳：品牌头 + 主卡 + 页脚，主题切换只改
`_theme_colors()` 一处。
"""
from __future__ import annotations

import logging
import re
import smtplib
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape

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


# ══════════════════════════════════════════════════════════════════
# 一、设计令牌与外部资源出口
# ══════════════════════════════════════════════════════════════════

# 卡片内容区宽度：容器 600 − 左右各 24px 内距 = 552
CARD_W = 552
# 封面比例基准：Steam header 为 460×215（≈2.14:1），各尺寸按同一比例换算
COVER_RATIO = 215 / 460


def _theme_colors() -> dict[str, str]:
    """主题色板（web/src/styles/tokens.css 的 dark 主题同源色值）。"""
    return {
        "accent": "#66c0f4",         # --accent
        "accent_deep": "#1a9fff",    # 按钮底色（accent 加深一档）
        "success": "#a4d007",        # --success
        "danger": "#f44336",         # 降价红
        "warning": "#e6a23c",        # 告警橙
        "on_accent": "#0c1724",      # accent 底上的文字（深色保证对比度）
        "bg_page": "#0e1a27",        # --bg-base
        "bg_card": "#1b2838",        # --bg-card
        "bg_chip": "#223449",        # --bg-chip
        "bg_panel": "#0e1a27",       # 卡片内嵌面板底（与页面底同色）
        "text_main": "#d9e3ec",      # --text-primary
        "text_sub": "#8f98a0",       # --text-muted
        "text_dim": "#6b7683",       # 更弱一档（眉标签 / 页脚注）
        "border_strong": "#2a475e",  # --border-strong 等效实色
        "row_border": "#2e3d4f",     # 行分隔线
        "cover_border": "#2a475e",   # 封面描边（深底上给图一个边界）
    }


_FLAG_TMPL = "https://flagcdn.com/w40/{code}.png"
# 封面 CDN 归一：历史域名 → 现行 akamai（与 games/bundles 域的图片归一同一口径）
_COVER_HOST_RE = re.compile(
    r"^https?://shared\.(?:fastly\.steamstatic|cdn\.queniuqe)\.com", re.IGNORECASE
)
_COVER_HOST = "https://shared.akamai.steamstatic.com"


def _esc(value) -> str:
    """HTML 转义（None → 空串）。邮件正文里所有动态字段都过这一层。"""
    return escape(str(value if value is not None else ""))


def _flag_url(code: str) -> str:
    """区旗 URL（远端 flagcdn，与前端 public/flags 同源素材）。"""
    return _FLAG_TMPL.format(code=(code or "").strip().lower())


def _cover_url(header_image, appid=None) -> str:
    """封面 URL：库内 header_image 优先，缺失按 appid 拼 Steam header 兜底。"""
    url = str(header_image or "").strip()
    if url:
        return _COVER_HOST_RE.sub(_COVER_HOST, url)
    if appid:
        from app.domains.games.pricing import build_steam_header_url

        return build_steam_header_url(int(appid))
    return ""


def _region_store_url(appid, region: str = "") -> str:
    """该区商店直链：`?cc=` 直达对应区价格页（区域辨识的落点）。"""
    if not appid:
        return ""
    base = f"https://store.steampowered.com/app/{int(appid)}/"
    cc = (region or "").strip().lower()
    return f"{base}?cc={cc}&l=schinese" if cc else base


def _bundle_store_url(url, bundle_id, item_kind: int = -1) -> str:
    """捆绑包商店直链：库内 url 优先，缺失按形态（bundle / sub）兜底。"""
    if url:
        return str(url)
    if not bundle_id:
        return ""
    kind = "sub" if int(item_kind or -1) == 1 else "bundle"
    return f"https://store.steampowered.com/{kind}/{int(bundle_id)}/"


# ══════════════════════════════════════════════════════════════════
# 二、原子组件
# ══════════════════════════════════════════════════════════════════


def _row(inner: str, *, pad: str = "0 24px") -> str:
    """主卡一行（单列）：inner 为该行的 HTML 内容。"""
    return f'<tr><td style="padding:{pad};">{inner}</td></tr>'


def _divider(c: dict[str, str], *, pad: str = "20px 24px 0") -> str:
    """细横线分隔（1px div：不是所有客户端都渲染 td 的 border-bottom）。"""
    return _row(
        f'<div style="height:1px;line-height:1px;font-size:0;'
        f'background-color:{c["row_border"]};">&nbsp;</div>',
        pad=pad,
    )


def _badge_circle(c: dict[str, str], glyph: str, color: str) -> str:
    """圆形徽章（主卡顶部图标位）。"""
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td align="center" bgcolor="{c["bg_chip"]}" width="56" height="56" '
        f'style="width:56px;height:56px;border-radius:28px;background-color:{c["bg_chip"]};'
        f'border:1px solid {c["border_strong"]};font-size:26px;color:{color};'
        f'font-weight:700;line-height:56px;">{glyph}</td>'
        f"</tr></table>"
    )


def _chip(c: dict[str, str], text: str, *, color: str, bg: str | None = None,
          size: int = 11) -> str:
    """圆角标签（折扣 / 条件 / 状态）。"""
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:9px;'
        f'background-color:{bg or c["bg_chip"]};color:{color};font-size:{size}px;'
        f'font-weight:700;letter-spacing:.3px;">{text}</span>'
    )


def _button(c: dict[str, str], href: str, text: str, *, fill: bool = True) -> str:
    """CTA 按键（表格 + bgcolor：Outlook 不认 padding 的裸 <a>）。"""
    if not href:
        return ""
    if fill:
        bg, fg = c["accent_deep"], c["on_accent"]
    else:
        bg, fg = c["bg_chip"], c["text_main"]
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td bgcolor="{bg}" style="border-radius:7px;background-color:{bg};">'
        f'<a href="{href}" target="_blank" style="display:inline-block;padding:9px 18px;'
        f'font-size:13px;font-weight:700;color:{fg};text-decoration:none;">{text}</a>'
        f'</td></tr></table>'
    )


def _flag_img(c: dict[str, str], code: str, *, height: int = 12, href: str = "") -> str:
    """区旗小图（16×12）：有该区商店链接时旗帜本身就是入口。"""
    if not code:
        return ""
    width = int(round(height * 4 / 3))
    img = (
        f'<img src="{_flag_url(code)}" width="{width}" height="{height}" '
        f'alt="{_esc(code).upper()}" '
        f'style="display:inline-block;vertical-align:-1px;border:0;outline:none;'
        f'width:{width}px;height:{height}px;" />'
    )
    if href:
        img = f'<a href="{href}" target="_blank" style="text-decoration:none;">{img}</a>'
    return img


def _cover_img(c: dict[str, str], url: str, alt: str, *, width: int, href: str = "",
               fluid: bool = False) -> str:
    """封面图：按 2.14:1 定高，深底上描一道边，alt 兜底图裂时仍可读。

    fluid=True 走「width/height 属性 + CSS width:100%」的流式图套路：老
    客户端（Outlook）吃属性里的定尺寸，现代客户端按容器宽度等比缩放，
    窄屏邮件（手机）里整宽封面不会横向溢出。
    """
    if not url:
        return ""
    height = max(24, int(round(width * COVER_RATIO)))
    sizing = (
        f"width:100%;max-width:{width}px;height:auto;"
        if fluid
        else f"width:{width}px;height:{height}px;"
    )
    img = (
        f'<img src="{url}" width="{width}" height="{height}" alt="{alt}" '
        f'style="display:block;{sizing}border:0;outline:none;'
        f'border-radius:8px;background-color:{c["bg_chip"]};" />'
    )
    if href:
        img = f'<a href="{href}" target="_blank" style="text-decoration:none;">{img}</a>'
    return img


def _panel_open(c: dict[str, str]) -> str:
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="background-color:{c["bg_panel"]};border-radius:10px;'
        f'border:1px solid {c["border_strong"]};">'
    )


def _stat_table(c: dict[str, str], rows: list[tuple[str, str]]) -> str:
    """键值详情面板（测试邮件连接详情 / 系统告警上下文同款）。"""
    body = "".join(
        f"<tr>"
        f'<td style="padding:9px 14px;font-size:12px;color:{c["text_sub"]};'
        f'white-space:nowrap;border-bottom:1px solid {c["row_border"]};">{label}</td>'
        f'<td style="padding:9px 14px;font-size:13px;color:{c["text_main"]};'
        f'font-family:Consolas,monospace;text-align:right;'
        f'border-bottom:1px solid {c["row_border"]};">{value}</td>'
        f"</tr>"
        for label, value in rows
    )
    return f"{_panel_open(c)}{body}</table>"


def _kv_grid(c: dict[str, str], items: list[tuple[str, str, str]]) -> str:
    """指标格：[(标题, 数值, 数值色)] —— 2×N 网格（外层表格拆列，非 flex）。"""
    cells = []
    for i in range(0, len(items), 2):
        tds = []
        for label, value, color in items[i:i + 2]:
            tds.append(
                f'<td width="50%" valign="top" style="padding:6px;">'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'border="0" style="background-color:{c["bg_panel"]};border-radius:10px;'
                f'border:1px solid {c["border_strong"]};">'
                f'<tr><td style="padding:12px 14px;">'
                f'<div style="font-size:11px;color:{c["text_sub"]};">{label}</div>'
                f'<div style="margin-top:4px;font-size:19px;font-weight:700;color:{color};'
                f'font-family:Consolas,monospace;">{value}</div>'
                f"</td></tr></table></td>"
            )
        if len(tds) == 1:  # 奇数项补空列，保证左右等宽
            tds.append('<td width="50%">&nbsp;</td>')
        cells.append(f"<tr>{''.join(tds)}</tr>")
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="margin:0 -6px;">{"".join(cells)}</table>'
    )


def _bullet_list(c: dict[str, str], items: list[str]) -> str:
    """要点清单（行首 accent 方块）。"""
    rows = "".join(
        f'<tr><td width="14" valign="top" style="padding:5px 0 0;font-size:12px;'
        f'line-height:1.7;color:{c["accent"]};">&#9642;</td>'
        f'<td valign="top" style="padding:5px 0 0 6px;font-size:12.5px;line-height:1.7;'
        f'color:{c["text_sub"]};">{text}</td></tr>'
        for text in items
    )
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0">{rows}</table>'
    )


# ─── 邮件骨架 ──────────────────────────────────────────────────────


def _brand_header(c: dict[str, str]) -> str:
    return (
        f'<span style="font-size:20px;font-weight:700;color:{c["accent"]};'
        f'letter-spacing:.5px;">Holdexar</span>'
        f'<span style="font-size:11px;color:{c["text_sub"]};margin-left:10px;'
        f'letter-spacing:1px;">STEAM 多区价格监控终端</span>'
    )


def _footer(c: dict[str, str]) -> str:
    return (
        f'此邮件由 {APP_NAME} 自动发送，请勿直接回复<br />'
        f'<span style="color:{c["text_dim"]};">多区价格仅供参考，'
        f'最终以下单时的 Steam 商店页为准</span>'
    )


def _page_html(c: dict[str, str], card_inner: str, *, preheader: str = "") -> str:
    """外层页面骨架：深底 + 居中 600px 容器 + 品牌头 + 主卡 + 页脚。"""
    hidden = (
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;'
        f'color:{c["bg_page"]};font-size:1px;line-height:1px;">{preheader}</div>'
        if preheader
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta name="color-scheme" content="dark" />
<title>{APP_NAME}</title>
</head>
<body style="margin:0;padding:0;background-color:{c['bg_page']};">
{hidden}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{c['bg_page']}" style="background-color:{c['bg_page']};">
  <tr>
    <td align="center" style="padding:34px 12px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;">
        <!-- 品牌头 -->
        <tr>
          <td style="padding:0 4px 14px;">{_brand_header(c)}</td>
        </tr>
        <!-- 主卡片 -->
        <tr>
          <td bgcolor="{c['bg_card']}" style="background-color:{c['bg_card']};border:1px solid {c['border_strong']};border-radius:14px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              {card_inner}
            </table>
          </td>
        </tr>
        <!-- 页脚 -->
        <tr>
          <td align="center" style="padding:16px 4px 0;font-size:11px;line-height:1.9;color:{c['text_sub']};">{_footer(c)}</td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>"""


def _hero(
    c: dict[str, str],
    *,
    badge: str,
    badge_color: str,
    title: str,
    subtitle: str,
    eyebrow: str = "",
) -> str:
    """主卡头部：徽章 + 眉标签 + 标题 + 说明。"""
    inner = []
    if badge:
        inner.append(
            f'<tr><td align="center" style="padding:0 0 14px;">'
            f"{_badge_circle(c, badge, badge_color)}</td></tr>"
        )
    if eyebrow:
        inner.append(
            f'<tr><td align="center" style="padding:0 0 7px;font-size:11px;'
            f'letter-spacing:2px;color:{c["text_dim"]};">{eyebrow}</td></tr>'
        )
    inner.append(
        f'<tr><td align="center" style="padding:0 26px 9px;font-size:20px;'
        f'font-weight:700;line-height:1.45;color:{c["text_main"]};">{title}</td></tr>'
    )
    inner.append(
        f'<tr><td align="center" style="padding:0 34px;font-size:13px;line-height:1.85;'
        f'color:{c["text_sub"]};">{subtitle}</td></tr>'
    )
    return _row(
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0">{"".join(inner)}</table>',
        pad="34px 24px 0",
    )


def _section_label(c: dict[str, str], text: str, *, note: str = "") -> str:
    """小节标题（左对齐，可在右侧带一句注解）。"""
    right = (
        f'<span style="font-size:11px;color:{c["text_dim"]};font-weight:400;">{note}</span>'
        if note
        else ""
    )
    return _row(
        f'<div style="font-size:12px;font-weight:700;letter-spacing:1.5px;'
        f'color:{c["text_sub"]};">{text}{right}</div>',
        pad="26px 24px 0",
    )


def _notice(c: dict[str, str], text: str, *, color: str = "") -> str:
    """页脚前的一句提示（主题色方块 + 弱化文字）。"""
    return _row(
        f'<div style="font-size:11.5px;line-height:1.7;color:{c["text_dim"]};">'
        f'<span style="color:{color or c["accent"]};">&#9642;</span> {text}</div>',
        pad="22px 24px 0",
    )


# ─── 游戏卡（封面 + 区旗 + 该区直链）────────────────


def _price_block(c: dict[str, str], item: dict) -> str:
    """价格列：该区现价（accent 加粗）+ 国区参照 + 折扣标签。"""
    parts = []
    main = _esc(item.get("price_text") or "")
    if main:
        parts.append(
            f'<div style="font-size:16px;font-weight:700;line-height:1.3;'
            f'color:{c["accent"]};font-family:Consolas,monospace;">{main}</div>'
        )
    cn = _esc(item.get("cn_price_text") or "")
    if cn and cn != "—":  # 无国区价时不渲染空参照行
        parts.append(
            f'<div style="margin-top:3px;font-size:11px;color:{c["text_sub"]};">'
            f'国区 {cn}</div>'
        )
    discount = int(item.get("discount") or 0)
    if discount > 0:
        parts.append(
            f'<div style="margin-top:7px;">'
            f'{_chip(c, f"-{discount}%", color=c["danger"])}</div>'
        )
    return "".join(parts)


def _game_row(c: dict[str, str], item: dict, *, last: bool = False) -> str:
    """紧凑单行卡：封面缩略图 + 名称/区服 + 价格/折扣（可点击整行入口）。"""
    name = _esc(item.get("name") or item.get("title") or "")
    region = (item.get("region_code") or item.get("region") or "").upper()
    region_name = _esc(item.get("region_name") or region or "")
    appid = item.get("appid")
    url = item.get("url") or _region_store_url(appid, region)
    cover = _cover_url(item.get("header_image") or item.get("cover"), appid)

    cells = []
    if cover:
        cells.append(
            f'<td width="132" valign="top" style="padding:14px 0 14px 24px;">'
            f'{_cover_img(c, cover, name or str(appid or ""), width=120, href=url)}</td>'
        )
    detail_pad = "14px 10px 14px 14px" if cover else "14px 10px 14px 24px"
    meta = []
    if region_name:
        flag = _flag_img(c, region, href=url)
        meta.append(
            f'<div style="margin-top:6px;font-size:12px;color:{c["text_sub"]};">'
            f'{flag}<span style="margin-left:6px;">{region_name}</span></div>'
        )
    condition = _esc(item.get("condition") or "")
    if condition:
        meta.append(
            f'<div style="margin-top:7px;font-size:11.5px;line-height:1.6;'
            f'color:{c["text_dim"]};">{condition}</div>'
        )
    if url:
        meta.append(
            f'<div style="margin-top:7px;font-size:11.5px;">'
            f'<a href="{url}" target="_blank" style="color:{c["accent"]};'
            f'text-decoration:none;font-weight:600;">前往该区商店 &rarr;</a></div>'
        )
    cells.append(
        f'<td valign="top" style="padding:{detail_pad};">'
        f'<div style="font-size:15px;font-weight:700;line-height:1.4;'
        f'color:{c["text_main"]};">{name}</div>'
        f'{"".join(meta)}</td>'
    )
    cells.append(
        f'<td width="118" valign="top" align="right" '
        f'style="padding:14px 24px 14px 8px;white-space:nowrap;">'
        f'{_price_block(c, item)}</td>'
    )
    row = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0"><tr>{"".join(cells)}</tr></table>'
    )
    tail = "" if last else _divider(c, pad="0 24px")
    return _row(row, pad="0") + tail


def _feature_card(c: dict[str, str], item: dict, *, last: bool = False) -> str:
    """大封面主推卡：整宽封面 + 名称 + 区服/价格 + CTA。"""
    name = _esc(item.get("name") or item.get("title") or "")
    region = (item.get("region_code") or item.get("region") or "").upper()
    region_name = _esc(item.get("region_name") or region or "")
    appid = item.get("appid")
    url = item.get("url") or _region_store_url(appid, region)
    cover = _cover_url(item.get("header_image") or item.get("cover"), appid)
    discount = int(item.get("discount") or 0)

    head = []
    if cover:
        head.append(
            _cover_img(c, cover, name or str(appid or ""), width=CARD_W,
                       href=url, fluid=True)
        )
    title_row = (
        f'<div style="font-size:16px;font-weight:700;line-height:1.45;'
        f'color:{c["text_main"]};">{name}</div>'
    )
    if discount > 0:
        title_row += f'<div style="margin-top:8px;">{_chip(c, f"-{discount}%", color=c["danger"])}</div>'
    head.append(f'<div style="margin-top:12px;">{title_row}</div>')

    meta = []
    if region_name:
        meta.append(
            f'<div style="margin-top:8px;font-size:12.5px;color:{c["text_sub"]};">'
            f'{_flag_img(c, region, height=13, href=url)}'
            f'<span style="margin-left:7px;">最低价区 · {region_name}</span></div>'
        )
    price = _esc(item.get("price_text") or "")
    if price:
        cn = _esc(item.get("cn_price_text") or "")
        reference = (
            f'<span style="margin-left:10px;font-size:12px;font-weight:400;'
            f'color:{c["text_sub"]};">国区 {cn}</span>'
            if cn and cn != "—"
            else ""
        )
        meta.append(
            f'<div style="margin-top:10px;font-size:22px;font-weight:700;'
            f'color:{c["accent"]};font-family:Consolas,monospace;">{price}'
            f"{reference}</div>"
        )
    if url:
        meta.append(f'<div style="margin-top:14px;">{_button(c, url, "前往该区商店")}</div>')

    inner = "".join(head) + "".join(meta)
    tail = "" if last else _divider(c, pad="24px 24px 0")
    return _row(inner, pad="20px 24px 0") + tail


def _game_list(c: dict[str, str], items: list[dict], *, feature: bool = False) -> str:
    builder = _feature_card if feature else _game_row
    return "".join(
        builder(c, item, last=(i == len(items) - 1)) for i, item in enumerate(items)
    )


# ══════════════════════════════════════════════════════════════════
# 三、邮件模板
# ══════════════════════════════════════════════════════════════════


def _test_mail_html(*, host: str, port: int, user: str, to_addr: str, use_ssl: bool) -> str:
    """连通性测试邮件：连接详情 + 推送范围说明。"""
    c = _theme_colors()
    rows = [
        ("SMTP 服务器", f"{_esc(host)}:{port}"),
        ("加密方式", "SSL/TLS" if use_ssl else "STARTTLS"),
        ("发件账户", _esc(user)),
        ("收件邮箱", _esc(to_addr)),
        ("测试时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ]
    inner = _hero(
        c,
        badge="&#10003;",
        badge_color=c["success"],
        eyebrow="CONNECTION OK",
        title="连接成功",
        subtitle=(
            f"你的邮箱已成功接入 {APP_NAME}。此后监控命中时，"
            f"我们会第一时间把消息送到这个邮箱。"
        ),
    )
    inner += _row(_stat_table(c, rows), pad="24px 24px 0")
    inner += _section_label(c, "推送范围")
    inner += _row(
        _bullet_list(c, [
            "价格提醒触发：自设目标价 / 折扣率 / 跌破史低命中",
            "新史低速报：监控池刷新出新的历史最低价",
            "监控池折扣速报：关注的游戏出现新的折扣",
            "捆绑包精选：国区最低或高折扣的 Steam 捆绑包",
            "Epic 喜加一 / Humble Choice 当月包上新",
            "代理通道与系统异常告警（异常时才发，不刷屏）",
        ]),
        pad="12px 24px 0",
    )
    inner += _notice(c, "同一类提醒会合并成一封邮件发送，不会逐条打扰。")
    return _page_html(c, inner, preheader="邮箱连通性测试通过，Holdexar 推送已就绪")


def alert_mail_html(triggered: list[dict]) -> str:
    """价格提醒触发邮件正文。

    triggered 项：{name, region, region_name, price(本币分), price_cny(人民币分),
    price_text(本币价文本), discount, appid, header_image, alertType,
    targetValue(人民币分/百分数), condition}（check_appids 产出）。
    """
    from app.domains.games.pricing import format_cny_fen

    c = _theme_colors()
    items = []
    for t in triggered:
        condition = t.get("condition") or ""
        if not condition:
            value = t.get("targetValue")
            kind = t.get("alertType")
            if kind == "pct" and value is not None:
                condition = f"命中条件：折扣率 ≥ {int(value)}%"
            elif kind == "historic_low":
                condition = "命中条件：跌破该区历史最低价"
            elif value is not None:
                condition = f"命中条件：价格 ≤ {format_cny_fen(int(value))}"
        items.append({
            **t,
            "price_text": t.get("price_text") or format_cny_fen(t.get("price")),
            "condition": condition,
        })
    inner = _hero(
        c,
        badge="&#9660;",
        badge_color=c["danger"],
        eyebrow="PRICE ALERT",
        title="价格提醒已触发",
        subtitle=(
            f"以下 {len(triggered)} 款游戏命中了你设置的提醒条件，"
            f"价格已到目标位，考虑入手："
        ),
    )
    inner += _game_list(c, items)
    inner += _notice(c, "点击封面或区旗可直达该区商店页，价格与折扣以页面为准。")
    return _page_html(c, inner, preheader=f"{len(triggered)} 条价格提醒命中，最低 {_lowest_hint(items)}")


def new_low_mail_html(picks: list[dict]) -> str:
    """新史低精选邮件正文（每封仅展示 1-2 款，不做全量清单）。

    picks 项：{name, region_name, region_code, price_text, cn_price_text,
    discount, appid, header_image}（check_new_lows 产出）。
    """
    c = _theme_colors()
    inner = _hero(
        c,
        badge="&#9733;",
        badge_color=c["accent"],
        eyebrow="ALL-TIME LOW",
        title="新史低速报",
        subtitle=(
            "监控池里的游戏刷新出了新的历史最低价，"
            "挑了 2 款给你过目，可直接前往 Steam 查看："
        ),
    )
    inner += _game_list(c, picks, feature=True)
    inner += _notice(c, "史低判定基于站内历史快照，仅供参考。")
    return _page_html(c, inner, preheader="监控池刷新出新史低")


def wishlist_deal_mail_html(deals: list[dict]) -> str:
    """监控池折扣速报正文。

    deals 项：{appid, name, header_image, region_code, region_name,
    price_text, cn_price_text, discount, old_discount, url}。
    """
    c = _theme_colors()
    inner = _hero(
        c,
        badge="&#8595;",
        badge_color=c["accent"],
        eyebrow="WATCHLIST DEALS",
        title="监控池折扣速报",
        subtitle=(
            f"监控池里有 {len(deals)} 款游戏出现了新的折扣，"
            f"按折扣深度排序，点封面直达该区商店："
        ),
    )
    inner += _game_list(c, deals)
    inner += _notice(c, "同一款游戏只有折扣进一步加深时才会再次出现在本速报里。")
    return _page_html(c, inner, preheader=f"监控池 {len(deals)} 款新折扣")


def bundle_deal_mail_html(deals: list[dict]) -> str:
    """捆绑包精选正文。

    deals 项：{bundle_id, name, header_image, url, region_code, region_name,
    price_text, cn_price_text, discount, item_count}。
    """
    c = _theme_colors()
    inner = _hero(
        c,
        badge="&#9635;",
        badge_color=c["success"],
        eyebrow="BUNDLE PICKS",
        title="捆绑包精选",
        subtitle=(
            f"本轮刷出 {len(deals)} 个值得看的 Steam 捆绑包，"
            f"按折扣深度排序（含封面与包内数量）："
        ),
    )
    inner += _game_list(c, deals)
    inner += _notice(c, "捆绑包价格随包内补齐情况变化，以 Steam 商店页结算为准。")
    return _page_html(c, inner, preheader=f"捆绑包 {len(deals)} 个新折扣")


def epic_free_mail_html(offers: list[dict], upcoming: list[dict] | None = None) -> str:
    """Epic 喜加一正文。

    offers 项：{title, title_cn, appid, cover, url, free_end, worth}；
    upcoming 结构相同（预告段，可为空）。
    """
    c = _theme_colors()
    inner = _hero(
        c,
        badge="&#43;",
        badge_color=c["success"],
        eyebrow="EPIC FREE",
        title="Epic 喜加一",
        subtitle=(
            f"本周有 {len(offers)} 款游戏在 Epic 商城限时免费，"
            f"领取窗口内入库即永久保留："
        ),
    )
    inner += _game_list(c, offers)
    if upcoming:
        inner += _section_label(c, "下周预告", note="届时会再单独提醒")
        inner += _game_list(c, upcoming)
    inner += _notice(c, "免费领取需要 Epic 账号，链接直达官方商城页。")
    return _page_html(c, inner, preheader=f"Epic 本周 {len(offers)} 款限免")


def hb_choice_mail_html(*, month_label: str, product_name: str, games: list[dict]) -> str:
    """Humble Choice 当月包正文。

    games 项：{appid, name, appid, header_image}。
    """
    c = _theme_colors()
    title = _esc(product_name or month_label or "本月包")
    inner = _hero(
        c,
        badge="&#9670;",
        badge_color=c["warning"],
        eyebrow="HUMBLE CHOICE",
        title="Humble Choice 当月包",
        subtitle=(
            f"<b style=\"color:{c['text_main']};\">{title}</b> 已上架，"
            f"其中 {len(games)} 款可在 Steam 兑换，封面与商店页如下："
        ),
    )
    inner += _game_list(c, games)
    inner += _notice(c, "兑换码需在 Humble 网站上领取，本邮件只做清单速览。")
    return _page_html(c, inner, preheader=f"Humble Choice {title} 上新")


def proxy_health_mail_html(snapshot: dict) -> str:
    """代理通道 / Clash 订阅健康告警正文。

    snapshot：{reason, available, total, pool_total, pool_ok, clash_running,
    clash_nodes, clash_ok_nodes, clash_exit_ips, clash_ok_exit_ips,
    subscriptions: [{label, kind, traffic}]}。
    """
    c = _theme_colors()
    available = int(snapshot.get("available") or 0)
    total = int(snapshot.get("total") or 0)
    level_color = c["danger"] if available == 0 else c["warning"]
    inner = _hero(
        c,
        badge="&#33;",
        badge_color=level_color,
        eyebrow="PROXY HEALTH",
        title="代理通道告警",
        subtitle=(
            f"{_esc(snapshot.get('reason') or '可用通道不足')}。"
            f"爬取与更新下载可能失败，建议进「代理池」页检查订阅与节点。"
        ),
    )
    inner += _row(
        _kv_grid(c, [
            ("可用通道", f"{available} / {total}", level_color),
            ("手动代理", f"{int(snapshot.get('pool_ok') or 0)} / {int(snapshot.get('pool_total') or 0)}", c["text_main"]),
            ("Clash 内核", "运行中" if snapshot.get("clash_running") else "未运行",
             c["success"] if snapshot.get("clash_running") else c["danger"]),
            ("节点存活", f"{int(snapshot.get('clash_ok_nodes') or 0)} / {int(snapshot.get('clash_nodes') or 0)}", c["text_main"]),
        ]),
        pad="24px 18px 0",
    )
    rows = [("检测时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))]
    rows.append(("出口 IP 存活", f"{int(snapshot.get('clash_ok_exit_ips') or 0)} / {int(snapshot.get('clash_exit_ips') or 0)}"))
    for sub in snapshot.get("subscriptions") or []:
        traffic = sub.get("traffic") or {}
        if isinstance(traffic, dict) and traffic:
            used = (int(traffic.get("upload") or 0) + int(traffic.get("download") or 0)) / 1024 ** 3
            quota = int(traffic.get("total") or 0) / 1024 ** 3
            text = f"{used:.1f} / {quota:.1f} GB" if quota else f"{used:.1f} GB"
            expire = traffic.get("expire")
            if expire:
                text += "，到期 " + datetime.fromtimestamp(int(expire)).strftime("%Y-%m-%d")
        else:
            text = "面板未返回流量头"
        rows.append((f"订阅 · {_esc(sub.get('label') or sub.get('id') or '-')}", text))
    inner += _row(_stat_table(c, rows), pad="20px 24px 0")
    inner += _notice(c, "该告警 12 小时内同一原因只发一次。", color=level_color)
    return _page_html(c, inner, preheader=f"代理通道告警：{_esc(snapshot.get('reason') or '')}")


def system_alert_mail_html(
    *, title: str, summary: str, rows: list[tuple[str, str]],
    level: str = "warning", hint: str = "",
) -> str:
    """系统异常告警正文（备份失败 / 调度异常等）：键值面板 + 处置提示。"""
    c = _theme_colors()
    color = {"danger": c["danger"], "warning": c["warning"], "info": c["accent"]}.get(
        level, c["warning"]
    )
    inner = _hero(
        c,
        badge="&#33;",
        badge_color=color,
        eyebrow="SYSTEM ALERT",
        title=_esc(title),
        subtitle=_esc(summary),
    )
    if rows:
        inner += _row(_stat_table(c, rows), pad="24px 24px 0")
    if hint:
        inner += _notice(c, _esc(hint), color=color)
    inner += _notice(c, "该告警 24 小时内同一类只发一次。", color=color)
    return _page_html(c, inner, preheader=f"系统告警：{_esc(title)}")


# ─── 连通性测试（前端「测试连通性」键）─────────────────────────────


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


def _lowest_hint(items: list[dict]) -> str:
    """收件箱预览用：给出本封里最低的 CNY 价（拿不到就回「见正文」）。"""
    # 优先取触发时刻落库的人民币分快照（与 cny_fen 同口径的人民币值）；
    # 旧事件无快照回退 price（本币分，仅量级参考）
    values = [i.get("price_cny") for i in items if isinstance(i.get("price_cny"), int)]
    if not values:
        values = [i.get("price") for i in items if isinstance(i.get("price"), int)]
    if not values:
        return "见正文"
    from app.domains.games.pricing import format_cny_fen

    return format_cny_fen(min(values))