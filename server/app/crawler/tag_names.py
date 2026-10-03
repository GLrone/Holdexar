"""标签名懒解析：只有库里连行都没有的 tagid 才拉一次热门标签表（中英双语）。

- 名字的唯一落点是 `tags` 表（见 `app.domains.games.tags`），本模块只负责
  「缺行时补一次」；
- 两张语言表一起拉、一起落（任一语言拉取失败则整轮不写，避免行落库后
  另一语言永远失去补名机会）；
- 拉取成功但表中仍无该 tagid = 冷门标签 → 该语言写 NULL 占位，此后不再请求；
- 拉取失败（主机不可达/超时）**不写占位**：那是通路问题而不是「未收录」，
  留待下轮再试；进程内冷却避免每个批量都重试。
"""
from __future__ import annotations

import asyncio
import logging
import time

from app.domains.games import tags as tags_service

logger = logging.getLogger(__name__)

POPULAR_TAGS_URLS = {
    "name_zh": "https://store.steampowered.com/tagdata/populartags/schinese",
    "name_en": "https://store.steampowered.com/tagdata/populartags/english",
}
# 拉取失败后的重试冷却（秒）：同进程内不因连续批量反复打同一主机
RETRY_COOLDOWN = 600.0

_lock = asyncio.Lock()
_next_attempt_at = 0.0


async def resolve_tag_names(context, tagids) -> int:
    """补齐缺失的标签名，返回本次落库行数（0 = 无需补/冷却中/拉取失败）。"""
    global _next_attempt_at

    unknown = await tags_service.missing_tag_ids(tagids)
    if not unknown:
        return 0

    async with _lock:
        # 等锁期间别的 worker 可能已补齐；冷却期内的失败也不重复打
        unknown = await tags_service.missing_tag_ids(unknown)
        if not unknown:
            return 0
        now = time.monotonic()
        if now < _next_attempt_at:
            return 0
        _next_attempt_at = now + RETRY_COOLDOWN

        payloads: dict[str, object] = {}
        try:
            for col, url in POPULAR_TAGS_URLS.items():
                payloads[col] = await context.http_client.get_json(context.session, url)
        except Exception as e:  # noqa: BLE001 —— 名字是副产物，失败不阻断价格链路
            logger.warning(
                "[标签] 热门标签表拉取失败（%s），%d 个 tagid 待补，下轮再试: %s",
                type(e).__name__, len(unknown), e,
            )
            return 0

    maps: dict[str, dict[int, str | None]] = {}
    for col, data in payloads.items():
        if not isinstance(data, list):
            logger.warning("[标签] 热门标签表响应形态异常（%s）：%s", col, type(data).__name__)
            return 0
        maps[col] = {
            int(t["tagid"]): (t.get("name") or None)
            for t in data
            if isinstance(t, dict) and t.get("tagid")
        }
    zh, en = maps["name_zh"], maps["name_en"]
    wrote = await tags_service.upsert_names(
        {t: zh.get(t) for t in unknown},
        {t: en.get(t) for t in unknown},
    )
    hit = sum(1 for t in unknown if zh.get(t) or en.get(t))
    logger.info(
        "[标签] 热门标签表补齐：命中 %d，未收录占位 %d", hit, len(unknown) - hit
    )
    return wrote
