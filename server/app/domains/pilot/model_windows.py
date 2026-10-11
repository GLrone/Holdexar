"""模型上下文窗口自动识别。

领航台的历史预算从模型窗口派生（见 context.derive_history_budget），窗口
就得先有个数。让用户在设置页手填既不现实也容易填错——手填大了请求直接
溢出失败，填小了白浪费模型能力。

本模块是一张内置规则表：按模型名正则匹配出该模型的上下文窗口，让「选了
模型」就自动带上窗口值，这里只取窗口这一个属性。

纪律：
- 表的每一项都要有厂商公开口径为依据；拿不准的模型**不进表**（识别不出
  就返回 None，回落到固定历史预算，行为与今天完全一致）；
- 同系列的新型号沿用该系列上一代的窗口（如 glm-5 沿用 glm-4 的 128k）。
  这是保守取值：估小只会少用一点窗口，估大则请求直接溢出失败——两害相权，
  宁可保守。识别值与真实值不符时，设置页手填可以覆盖；
- 手动填写永远优先于自动识别——网关自定义模型名命中不了任何正则时，
  这条路是用户的兜底；
- 匹配顺序由具体到宽泛（gpt-4o 必须在 gpt-4 之前），同名字取首个命中。
"""
from __future__ import annotations

import re

# (模型名正则, 上下文窗口 token)。顺序敏感：具体在前，宽泛在后。
_WINDOW_RULES: tuple[tuple[str, int], ...] = (
    # ── DeepSeek ──
    (r"deepseek-(v[0-9]|r[0-9]|chat|coder|reasoner)", 131072),
    # ── GLM 系列 ──
    (r"glm-4\.6", 200000),
    (r"glm-[45]", 131072),
    # ── Kimi / Moonshot 系列 ──
    (r"moonshot-v1-8k", 8192),
    (r"moonshot-v1-32k", 32768),
    (r"moonshot-v1-128k", 131072),
    (r"kimi-k[0-9]", 131072),
    # ── Qwen 系列 ──
    (r"qwen-long", 1000000),
    (r"qwen[0-9.]*-(max|plus|turbo|flash|coder)", 131072),
    (r"qwen2\.5", 32768),
    (r"qwen3[:\-]\d", 32768),
    (r"qwen[0-9]", 131072),
    # ── MiniMax ──
    (r"minimax-text-01", 1000000),
    (r"minimax", 131072),
    # ── claude 系列 ──
    (r"claude-(opus|sonnet|haiku)-[34]|claude-3", 200000),
    (r"claude", 200000),
    # ── OpenAI ──
    (r"gpt-5", 400000),
    (r"gpt-4\.1", 1047576),
    (r"gpt-4\.5", 131072),
    (r"gpt-4o|gpt-4-turbo|\bo[13]\b", 128000),
    (r"gpt-4", 8192),
    # ── Google Gemini ──
    (r"gemini-1\.5-pro", 2097152),
    (r"gemini-1\.5-flash", 1048576),
    (r"gemini-2", 1048576),
    # ── 字节豆包 ──
    (r"doubao-pro-32k|doubao-lite-32k", 32768),
    (r"doubao-(pro|lite)-128k", 131072),
    (r"doubao", 32768),
    # ── 本地小模型（ollama 常见）──
    (r"llama-?3\.[12]", 131072),
    (r"llama-?3\b", 8192),
    # ── Cohere / 零一万物 ──
    (r"command-r", 128000),
    (r"yi-large", 32768),
)

_COMPILED: tuple[tuple[re.Pattern[str], int], ...] = tuple(
    (re.compile(p, re.IGNORECASE), w) for p, w in _WINDOW_RULES
)

# 表外模型的兜底窗口。参考同类工具的通配做法：未知模型不再拒绝猜测，一律按
# 现代主流模型的宽裕口径估一个，让预算随窗口派生的链路始终有数。代价是真实
# 窗口小于该值时会以请求报错的形式暴露（手填可纠正），换取的是用户零配置。
FALLBACK_CONTEXT_WINDOW = 200000


def guess_context_window(model: str | None) -> int | None:
    """模型名 → 上下文窗口（token）；表里没有就返回 None（不猜、不估计）。"""
    name = str(model or "").strip()
    if not name:
        return None
    for pattern, window in _COMPILED:
        if pattern.search(name):
            return window
    return None
