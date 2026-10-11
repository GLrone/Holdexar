"""pilot 上下文装配：估算、裁剪、预算与压缩。

派生视图纪律：会话文件是唯一事实源（session.py），本模块只产出「发给
模型的视图」——工具结果按需裁剪、超预算时让位，任何裁剪都不改动存储。

- 估算 estimate_tokens 是唯一计价权威（CJK 感知启发式），预算判断、
  裁剪收敛、压缩触发都消费同一份测量；
- 装配序：要点存档（system）> 未存档轮次原文 > 当前问题；超预算先清较早的
  工具返回结果（免费、保留最近几组），仍超预算才从最老未存档轮次整轮让位，
  始终保留最近 RECENT_KEEP_TURNS 轮逐字原文；
- 压缩 compact 把更早轮次蒸馏成要点存档（LLM，与既有存档合并去陈），
  由服务层落盘；失败返回 None，装配退回「整轮让位」，问答不中断。
- 预算是三个量、同一个口径：历史预算、逐字保留的近期历史、摘要输出上限
  全部由模型窗口派生（窗口来自 model_windows 自动识别或用户手填），窗口
  未知才各自回落固定值。三者的关系是「保留 ⊂ 历史预算」，与 DSH
  compaction-basic 的 thresholdRatio / retainRatio 同构。
- 历史预算与逐字保留另受 HISTORY_COST_TOKENS 约束：窗口管「请求别溢出」，
  成本线管「账单别失控」（每轮每个请求都要重发整段历史）。窗口再大，历史
  也不会无上限地厚下去。
"""
from __future__ import annotations

import json

from app.domains.agent.tools import projection as tool_projection
from app.domains.pilot import llm as pilot_llm

_CLEARABLE_TOOLS = tool_projection._READ_TOOLS
"""可清结果的工具集合 = 只读工具（唯一事实源在 agent/tools/projection.py）。

结果能重新调回来的工具，清了只是让模型多调一次；动作类工具的调用不可复现
（重调会真的再执行一次写操作），所以不清。"""

HISTORY_BUDGET_TOKENS = 6000
"""模型窗口未知时（也没手填、模型名也认不出）的固定历史预算——既有行为，不变。"""

HISTORY_BUDGET_RATIO = 0.85
"""有效窗口中有多大比例给对话历史。

窗口越大历史越长：这是主流 agent 的口径（不把历史钉死在某个绝对值上），
剩下的比例留给输出预留与安全垫，防止请求把窗口挤满导致模型无法作答。
真正的天花板是下面的 HISTORY_COST_TOKENS——窗口管「请求别溢出」，成本线管
「账单别失控」，两件事分开定。"""

HISTORY_COST_TOKENS = 48000
"""请求成本线：固定开销 + 未存档历史，一次请求的真实总量不超过这个体量。

窗口上限（HISTORY_BUDGET_RATIO）保证请求不溢出；成本线约束的是另一件事——
**每一轮、每一个请求都要把整段历史重发一遍**。命中前缀缓存时只付增量，
一旦前缀变了（工具表切换、缓存过期、换模型或协议）就是整段全量重算，历史
越厚这一下的代价越大。两者分工固定：窗口决定「装得下多少」，成本线决定
「值不值得装这么多」。

成本线在校正之前生效（见 derive_history_budget），故它约束的是真实账单
而不是估值。"""

_HISTORY_BUDGET_MIN = 2000
"""小窗口保底：让位机制需要腾挪空间，低于此值则整轮让位无意义。"""

_OUTPUT_RESERVE_TOKENS = 6144
"""回答预留：4096 输出上限 + Claude 协议的 2048 思考预算（最坏情况一并扣）。"""

_WINDOW_BUFFER_TOKENS = 1024
"""安全垫：估算与真实计量的偏差余量。"""

RETAIN_RATIO = 0.16
"""逐字保留的近期历史占「有效窗口」的比例。

压缩不是「只留最后两轮」——留多少要跟窗口走。窗口大就把近期历史整段原样
留着（细节不丢），窗口小才退化成两轮。0.16 是 DSH compaction-basic 的
retainRatio 默认值；比值必须明显小于 HISTORY_BUDGET_RATIO，否则压缩腾不出
空间（保留段自己就把预算吃满了）。"""

RECENT_KEEP_TURNS = 2
"""逐字保留的近期历史**下限轮数**：保留段再小也不能少于此。

也是整轮让位的下限——让位是最后手段，不能把用户刚问的话丢出去。"""

SUMMARY_MAX_TOKENS_RATIO = 0.012
"""摘要输出上限占窗口的比例。

被蒸馏的历史在大窗口下是十万量级，摘要却是它之后唯一的载体——上限必须
跟着窗口走，否则「压缩」等于「丢弃」。同口径参照：ZCode 摘要上限 20k
tokens、DSH 默认 65k；本项目取更小的比例，因为摘要是中文要点而非结构化
长文档。"""

SUMMARY_MAX_TOKENS_MIN = 600
SUMMARY_MAX_TOKENS_MAX = 8000
"""摘要输出上限的夹逼区间：窗口未知时取下限（够写一份完整要点），
窗口再大也不超过上限（摘要本身就是需要被读进上下文的东西，不能失控）。"""

SUMMARY_INPUT_RATIO = 2.5
"""被蒸馏区间至少要有「摘要上限 × 此倍数」的体量才值得蒸馏。

蒸不出来就是白花一次模型调用：区间太小意味着摘要替换后体量未必下降。"""

TOOL_CONTENT_LIMIT = 3000
COMPACT_INPUT_RETRY_LIMIT = 3
COMPACT_INPUT_TAIL_TURNS = 12
REFERENCE_BUDGET_TOKENS = 800

CLEARED_TOOL_RESULT = "[早前工具返回结果已清理；需要该数据请重新调用对应工具]"
"""被清掉的老工具结果留下的占位行（派生视图内的替换，不动存储）。

占位行顺带告诉模型怎么把数据拿回来——ZCode 的占位是光秃秃一句
"[Old tool result content cleared]"，模型只能自己猜。"""

KEEP_RECENT_TOOL_GROUPS = 5
"""保留最近几组工具结果不清。

工具调用是"助手发起 → 工具返回"成组的；最近几组是模型手头正在用的依据，
清掉等于让它失忆。更早的那些只是历史证据，换成占位行不影响继续对话。"""

MIN_PRUNE_SAVINGS = 256
"""清理至少省下这个量才采用：省不下说明没有可清理的，别白改一遍视图。"""

PRUNE_TRIGGER_RATIO = 1.0
"""清理触发线＝历史预算的多少倍。

1.0 = 只在已经超预算时才动手（默认）。闲置回落时调用方传更小的值
（IDLE_PRUNE_TRIGGER_RATIO，ZCode microcompact 的 0.9 同口径），提前清掉
可以省下一次真花钱的压缩调用。"""

IDLE_PRUNE_TRIGGER_RATIO = 0.9
IDLE_PRUNE_MINUTES = 60
"""闲置回落：距上一轮超过这么久（分钟），且体量已过预算的九成，就提前清理。

判据是**前缀缓存已经过期**——缓存有存活时间，隔了一小时再发请求本来就要
重算全量前缀，「重写视图」此刻不额外损失缓存；反过来在缓存还热的时候清理，
等于把上一轮刚建好的前缀缓存作废。ZCode microcompact 的 60 分钟同源。"""

_SUMMARY_FRAME = "本会话因上下文预算进行过压缩：以下摘要把更早的对话浓缩为背景，之后是最近几轮的原文。摘要不是当前请求，请结合它继续对话：\n"

_SUMMARY_TAIL = (
    "\n\n（摘要只保留了要点。若需要更早对话的原文——精确数值、报错原文、"
    "当时的完整工具返回，可用 read_session 读取本会话 sid={sid} 并指定区间回看。"
    "不要凭印象猜摘要里没有的数字。）"
)

_COMPACT_INSTRUCTION = (
    "把上面的对话压缩成一份要点存档，供同一会话的后续提问作为背景。\n"
    "严格按下面小节输出，小节名逐字保留、一个都不能少；无内容写「无」；"
    "每条用短句或短行，不要写成散文段落：\n"
    "对话主线：（用户想要什么，目标如何演变）\n"
    "涉及游戏：（游戏名 + AppID）\n"
    "已执行动作：（关注/提醒/订阅等已生效或待用户确认的操作及其结果）\n"
    "关键价格事实：（保留精确数值、区服、币种与时间点，不要概括成「便宜」「降了」）\n"
    "用户偏好与约束：（明确说过的偏好、禁区、习惯）\n"
    "未决事项：（还没做完或没答复的事）\n"
    "规则：只依据上面的对话，不新增信息；数字、AppID、路径、报错原文、"
    "参数逐字保留；不要提及本次压缩；只输出存档正文，不要调用任何工具。"
)

_COMPACT_MERGE_HEAD = "以下是此前已经生成的要点存档，请把新内容合并进去、去掉过时的部分，输出合并后的完整存档：\n"

_COMPACT_RETRY_MARKER = "[更早的对话因超出压缩输入上限被截断]"

_OVERFLOW_MARKERS = (
    "context length", "context_length", "maximum context", "context window",
    "too many tokens", "input length", "prompt is too long", "reduce the length",
)



def user_text(question: str, appid: int | None,
              view_context: dict | None = None,
              element_refs: list[dict] | None = None) -> str:
    """当前问题 → 用户消息文本（附带对象上下文、页面上下文与元素引用）。

    view_context 由前端采集（路由路径 + 活跃板块/标签），让模型知道用户
    当前在哪个页面、看哪个板块——回答可以针对性地引用当前页面内容，
    而不是把用户当成无上下文的空壳。

    element_refs 是用户在页面上选中的元素引用（仿 ZCode 网页元素选择），
    只传最小信息（标签/文本/selector/role），让模型知道用户指代的具体元素——
    不传完整 HTML/CSS，agent 只需知道「用户说的是什么」。
    """
    parts: list[str] = []
    if appid:
        parts.append(f"用户当前正在看 AppID {appid} 的游戏详情")
    if view_context:
        vc_parts = _format_view_context(view_context)
        if vc_parts:
            parts.append(vc_parts)
    ctx_suffix = f"（{'; '.join(parts)}）" if parts else ""
    # 元素引用作为独立后缀块（可能有多条，每条一行）
    el_block = _format_element_refs(element_refs)
    return f"{question}{ctx_suffix}{el_block}"


# 路由路径 → 中文页面名映射（与前端路由表 + NAV_TARGETS 对齐）
_ROUTE_NAMES: dict[str, str] = {
    "/dashboard": "仪表盘",
    "/library": "游戏商店",
    "/gamelib": "游戏库",
    "/pilot": "领航台",
    "/pool": "监控池",
    "/bundles": "捆绑包",
    "/alerts": "价格提醒",
    "/rates": "汇率",
    "/logs": "日志",
    "/toolbox": "工具箱",
    "/family": "家庭共享",
    "/bills": "账单",
    "/events": "活动日历",
    "/achievements": "成就",
    "/crawl": "抓取任务",
    "/proxies": "代理池",
    "/fetch": "自动抓取",
    "/settings": "设置",
    "/about": "关于",
}


def _format_view_context(vc: dict) -> str | None:
    """把前端传来的 view_context 格式化为自然语言片段。

    view_context 结构：{ route: str, section?: str, tab?: str, sessionTitle?: str, appid?: int }
    route 是前端路由路径（/dashboard、/gamelib 等）；
    section 是页面内板块键（如 gamelib 的 owned/insights/family/play）；
    tab 是页面内标签页名（前端自定义，仅做透传）；
    sessionTitle 是当前会话标题（整页 /pilot 模式下路由无信息量时，
    会话标题是模型理解用户上下文的唯一线索）；
    appid 是游戏详情页的游戏 ID（从路由参数提取，顶栏通用入口不传 game prop）。
    """
    route = str(vc.get("route") or "").strip()
    section = str(vc.get("section") or "").strip()
    tab = str(vc.get("tab") or "").strip()
    session_title = str(vc.get("sessionTitle") or "").strip()
    vc_appid = vc.get("appid")

    if not route:
        return None

    # 路由名查表，查不到用路径兜底
    page_name = _ROUTE_NAMES.get(route, route.lstrip("/"))

    # 游戏详情页特殊处理：从路由或 view_context 提取 appid
    if route.startswith("/game/"):
        page_name = "游戏详情"
        if not vc_appid:
            # 路由路径形如 /game/1817190，提取 appid
            parts = route.split("/")
            if len(parts) >= 3:
                try:
                    vc_appid = int(parts[2])
                except (ValueError, IndexError):
                    pass

    fragments = [f"用户当前在「{page_name}」页面"]
    if vc_appid:
        fragments.append(f"正在看 AppID {vc_appid} 的游戏")
    if section:
        # section 用前端给的原始键，不强行翻译——前端最清楚自己的 tab 语义
        fragments.append(f"看「{section}」板块")
    if tab:
        fragments.append(f"「{tab}」标签页")
    if session_title:
        fragments.append(f"正在讨论「{session_title}」")

    return "，".join(fragments)


def _format_element_refs(refs: list[dict] | None) -> str:
    """把前端传来的元素引用格式化为自然语言后缀。

    每条引用结构：{ tag: str, label: str, text: str, selector: str, role: str }
    只保留 agent 需要的「用户指代什么」信息，不传完整 HTML/CSS。
    """
    if not refs:
        return ""
    lines: list[str] = []
    for i, ref in enumerate(refs, 1):
        tag = str(ref.get("tag") or "").strip()
        label = str(ref.get("label") or "").strip()
        text = str(ref.get("text") or "").strip()
        selector = str(ref.get("selector") or "").strip()
        role = str(ref.get("role") or "").strip()
        # 优先用 label，没有才用 text（label 是 aria-label/alt/title，语义更强）
        desc = label or text
        parts_list = [f"<{tag}>"]
        if role:
            parts_list.append(f"role={role}")
        if desc:
            parts_list.append(f"「{desc[:100]}」")
        if selector:
            parts_list.append(f"selector={selector}")
        lines.append(f"  [{i}] " + " ".join(parts_list))
    return "\n用户引用了以下页面元素：\n" + "\n".join(lines)


def summary_message(summary: str, sid: str | None = None) -> dict:
    """要点存档 → user 消息（ZCode 压缩语义：摘要以 user 角色衔接在历史前，
    system 位留给永不压缩的系统提示；连续 user 消息各协议均接受）。

    sid 在场时附一句回读指引：压缩是「有损但可恢复」，不是永久丢弃——
    同 ZCode 把完整转写文件路径写进摘要消息的做法。"""
    tail = _SUMMARY_TAIL.format(sid=sid) if sid else ""
    return {"role": "user", "content": _SUMMARY_FRAME + summary + tail}


def _corrected(allowance: int, overhead_tokens: int, correction: float) -> int:
    """按「真实用量 / 本地估值」的比率收紧可用额度。

    本地估值只是启发式（两家参考实现都自陈按字符数折算会低估中文与 JSON）；
    上一轮提供方回传的真实输入量说明这一轮真实体量约是估值的 correction 倍，
    故把额度按同一比率折回去，只收紧不放松。"""
    return int(allowance / correction) - max(0, overhead_tokens)


def derive_history_budget(context_window: int | None, overhead_tokens: int = 0,
                          *, correction: float = 1.0) -> int:
    """模型窗口 → 历史预算：有效窗口按比例切给历史，再扣非历史开销。

    窗口未知（未配置且模型名认不出）回落 HISTORY_BUDGET_TOKENS（既有行为）；
    窗口已知时先取两个上限的较小值——窗口派生额度与 HISTORY_COST_TOKENS
    （窗口决定「装得下多少」，成本线决定「值不值得装这么多」，见成本线常量），
    再按校正系数折成估值空间的预算并扣掉非历史开销。

    成本线在**校正之前**生效：校正系数描述的是「真实量 ≈ 估值 × correction」，
    额度先夹到成本线以内再折回估值空间，约束的才是真实账单。"""
    if not context_window or context_window <= 0:
        return HISTORY_BUDGET_TOKENS
    usable = context_window - _OUTPUT_RESERVE_TOKENS - _WINDOW_BUFFER_TOKENS
    allowance = min(int(usable * HISTORY_BUDGET_RATIO), HISTORY_COST_TOKENS)
    budget = _corrected(allowance, overhead_tokens, max(1.0, correction))
    return max(_HISTORY_BUDGET_MIN, max(0, budget))


def _effective_window(context_window: int | None) -> int:
    if not context_window or context_window <= 0:
        return 0
    return max(0, context_window - _OUTPUT_RESERVE_TOKENS)


def derive_retain_tokens(context_window: int | None) -> int:
    """逐字保留的近期历史预算；0 表示窗口未知（调用方退回轮数下限）。

    与历史预算同源同界：窗口派生后同样受 HISTORY_COST_TOKENS 约束。保留段是
    历史预算的一部分，两者必须同界——否则保留段自己就把成本线以下的预算吃满，
    压缩腾不出空间，还会退化成每几轮一次的小压缩。"""
    window = _effective_window(context_window)
    if window <= 0:
        return 0
    return int(min(window, HISTORY_COST_TOKENS) * RETAIN_RATIO)


def derive_summary_max_tokens(context_window: int | None) -> int:
    """摘要输出上限：跟窗口走，窗口未知取下限。"""
    if not context_window or context_window <= 0:
        return SUMMARY_MAX_TOKENS_MIN
    raw = int(_effective_window(context_window) * SUMMARY_MAX_TOKENS_RATIO)
    return max(SUMMARY_MAX_TOKENS_MIN, min(SUMMARY_MAX_TOKENS_MAX, raw))



_CJK_PER_TOKEN_NUM = 4
_CJK_PER_TOKEN_DEN = 5
"""汉字 → token 的换算比（4/5 = 0.8）。

主流中文词表里常见词两三个字合成一个 token，汉字平均约 0.8 token/字；
按 1 字 1 token 计会把中文对话高估约四分之一，让预算看着满了、实际还空着。"""


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef")
    other = len(text) - cjk
    # 汉字部分向上取整：宁可略高估（少放一点）也不要低估（请求真的溢出）
    cjk_tokens = (cjk * _CJK_PER_TOKEN_NUM + _CJK_PER_TOKEN_DEN - 1) // _CJK_PER_TOKEN_DEN
    return cjk_tokens + other // 4 + 1


def estimate_messages(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        total += 4
        total += estimate_tokens(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            total += estimate_tokens(json.dumps(tc.get("arguments") or {}, ensure_ascii=False)) + 8
    return total


def retain_boundary(turns: list[dict], context_window: int | None,
                    tool_limit: int | None = None) -> int:
    """压缩边界：从最新一轮往回累计，直到逐字保留的近期历史吃满保留预算。

    返回第一个「逐字保留」轮次的下标——turns[:边界] 是被蒸馏的对象。窗口
    未知（保留预算算不出来）时退化为「只留最近 RECENT_KEEP_TURNS 轮」，与
    历史行为一致；无论保留预算多大，边界都不会晚于 len(turns)−RECENT_KEEP_TURNS。

    轮次是压缩的最小单位：切在轮与轮之间，助手发起/工具返回的配对天然不会被
    拆开（拆开会各家协议直接报错）。"""
    total = len(turns)
    if total == 0:
        return 0
    latest = max(0, total - RECENT_KEEP_TURNS)
    retain_tokens = derive_retain_tokens(context_window)
    if retain_tokens <= 0:
        return latest
    used = 0
    idx = total
    while idx > 0:
        idx -= 1
        used += estimate_messages(_turn_view(turns[idx], tool_limit))
        if used >= retain_tokens:
            break
    return min(idx, latest)


def prune_tool_content(text: str, limit: int = TOOL_CONTENT_LIMIT) -> str:
    """超长工具结果保留头尾、省略中段（按码元切分，不破坏代理对）。

    limit 由调用方按设置传入（服务层读 settings 的「工具输出预算」），缺省
    回落本模块常量——装配侧不自己读配置，保持「只做视图、不碰配置」的边界。"""
    if len(text) <= limit:
        return text
    head, tail = limit * 2 // 3, limit // 3
    return f"{text[:head]}\n…[中间内容过长已省略]…\n{text[len(text) - tail:]}"


def looks_like_overflow(err_text: str) -> bool:
    lowered = (err_text or "").lower()
    return any(marker in lowered for marker in _OVERFLOW_MARKERS)


def _replay_line(turn: dict) -> dict | None:
    """无消息轮次（快捷动作/导航直通/降级轮）→ 单条回放消息。

    这些轮次没有模型消息可放，但卡片与工具事实用户真实看到过——不回放
    就是账本断裂（模型不知道界面上发生过什么）。按与 digest_lines 同源的
    口径合成一条 user 消息，标注「非当前请求」防模型误答。"""
    resp = turn.get("resp") or {}
    if not (resp.get("tools") or resp.get("cards") or resp.get("answer")):
        return None
    parts = [f"用户：{turn.get('q') or ''}"]
    if resp.get("answer"):
        parts.append(f"助手：{resp['answer']}")
    if resp.get("tools"):
        parts.append("工具：" + "；".join(str(x) for x in resp["tools"]))
    if resp.get("cards"):
        kinds = [str(c.get("kind") or "") for c in resp["cards"] if isinstance(c, dict)]
        parts.append("已生成界面卡片：" + "、".join(k for k in kinds if k))
    return {"role": "user", "content": "[系统回放·非当前请求] " + "\n".join(parts)}


def _turn_view(turn: dict, tool_limit: int | None = None) -> list[dict]:
    msgs = turn.get("messages") or []
    if not msgs:
        replay = _replay_line(turn)
        return [replay] if replay else []
    out = []
    for m in msgs:
        if m.get("role") == "tool" and isinstance(m.get("content"), str):
            content = prune_tool_content(m["content"], tool_limit or TOOL_CONTENT_LIMIT)
            if content != m["content"]:
                out.append({**m, "content": content})
                continue
        out.append(m)
    return out


def _call_names(msgs: list[dict]) -> dict[str, str]:
    """tool_call_id → 工具名（名字只存在于同组 assistant 消息里，工具消息自身不带）。"""
    names: dict[str, str] = {}
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            tid = tc.get("id")
            name = (tc.get("function") or {}).get("name") or tc.get("name")
            if tid and name:
                names[str(tid)] = str(name)
    return names


def _is_failed_result(content: str) -> bool:
    """失败结果不清：它承载的是「这一步没成」这一事实，模型据此决定要不要重试。"""
    return '"kind": "failed"' in content[:200] or '"kind":"failed"' in content[:200]


def _is_clearable(content: str, name: str | None) -> bool:
    """可清判据：结果能重新调回来，或者名字解不出来（老账本形态）。

    只清只读工具的结果——它们的值是「某一刻的事实」，重新调一次即可复现；
    动作类（关注/提醒/订阅）的结果不可复现，重调会真的再执行一次，留着。"""
    if name is None:
        return True
    return name in _CLEARABLE_TOOLS


def prune_old_tool_results(msgs: list[dict], *,
                           keep_groups: int = KEEP_RECENT_TOOL_GROUPS) -> list[dict]:
    """较早的工具返回结果 → 占位行（返回新列表，不改输入、不改存储）。

    这是整轮让位之前那道**免费**的减负：不调模型、不花钱、不改事实，先把
    历史里最占地方又最不需要逐字保留的东西（工具返回的原始文本）收走。
    整轮让位是最后手段——它会把用户的提问和助手的回答一起丢掉。"""
    starts = [
        i for i, m in enumerate(msgs)
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    if len(starts) <= keep_groups:
        return msgs
    boundary = starts[-keep_groups]
    names = _call_names(msgs)
    out = list(msgs)
    for i in range(boundary):
        m = out[i]
        if m.get("role") != "tool" or not isinstance(m.get("content"), str):
            continue
        content = m["content"]
        if content == CLEARED_TOOL_RESULT or _is_failed_result(content):
            continue
        if not _is_clearable(content, names.get(str(m.get("tool_call_id") or ""))):
            continue
        out[i] = {**m, "content": CLEARED_TOOL_RESULT}
    return out


def _fitted(msgs: list[dict], budget: int, *, trigger_ratio: float = PRUNE_TRIGGER_RATIO) -> list[dict]:
    """过清理触发线先做免费清理；省得不够就不动它，交给调用方整轮让位。"""
    before = estimate_messages(msgs)
    if before <= budget * trigger_ratio:
        return msgs
    pruned = prune_old_tool_results(msgs)
    if before - estimate_messages(pruned) >= MIN_PRUNE_SAVINGS:
        return pruned
    return msgs


def assemble(state, question: str, appid: int | None, *,
             budget: int | None = None, tool_limit: int | None = None,
             prune_ratio: float = PRUNE_TRIGGER_RATIO,
             view_context: dict | None = None,
             element_refs: list[dict] | None = None) -> list[dict]:
    """会话状态 + 当前问题 → system 之后的模型消息序列（预算内）。

    减负分两级：先清老工具结果（免费），仍超预算才从最老轮次整轮让位。
    budget 缺省回落 HISTORY_BUDGET_TOKENS（运行时读取，测试可打桩）；
    tool_limit = 历史轮里超长工具结果的保留预算（设置项，缺省回落模块常量）。
    prune_ratio = 清理触发线（相对预算）；闲置回落时传更小的值提前清理。"""
    budget = HISTORY_BUDGET_TOKENS if budget is None else budget
    turns = state.turns if state else []
    summary = state.summary if state else None
    start = state.summary_through if state else 0
    sid = state.sid if state else None
    current = {"role": "user", "content": user_text(question, appid, view_context, element_refs)}

    def _build(from_idx: int) -> list[dict]:
        msgs = [summary_message(summary, sid)] if summary else []
        msgs += [m for t in turns[from_idx:] for m in _turn_view(t, tool_limit)]
        msgs.append(current)
        return msgs

    msgs = _fitted(_build(start), budget, trigger_ratio=prune_ratio)
    while (
        len(turns) - start > RECENT_KEEP_TURNS
        and estimate_messages(msgs) > budget
    ):
        start += 1
        msgs = _fitted(_build(start), budget, trigger_ratio=prune_ratio)
    return msgs


def needs_compaction(messages: list[dict], *,
                     budget: int | None = None) -> bool:
    """给一份消息视图定价，看它是否越过预算。"""
    budget = HISTORY_BUDGET_TOKENS if budget is None else budget
    return estimate_messages(messages) > budget


def full_view(state, question: str, appid: int | None,
              tool_limit: int | None = None,
              view_context: dict | None = None,
              element_refs: list[dict] | None = None) -> list[dict]:
    """不裁剪的全量视图：存档 + 全部未存档轮次 + 当前问题。

    这是「压力」的测量对象——压缩该不该触发看它，而不是看已经让位过的
    装配结果。让位是每次请求现算的临时失明（下一轮那些轮次又回来），
    拿它当触发依据会导致：一直靠让位硬扛、存档永远不更新。"""
    if state is None:
        return [{"role": "user", "content": user_text(question, appid, view_context, element_refs)}]
    msgs = [summary_message(state.summary, state.sid)] if state.summary else []
    msgs += [m for t in state.turns[state.summary_through:] for m in _turn_view(t, tool_limit)]
    msgs.append({"role": "user", "content": user_text(question, appid, view_context, element_refs)})
    return msgs


def full_view_tokens(state, question: str, appid: int | None,
                     tool_limit: int | None = None,
                     view_context: dict | None = None,
                     element_refs: list[dict] | None = None) -> int:
    """全量视图体量（落账用的「压缩前体量」口径）。"""
    if state is None:
        return 0
    return estimate_messages(full_view(state, question, appid, tool_limit, view_context, element_refs))


def compact_plan(state, context_window: int | None,
                 tool_limit: int | None = None) -> tuple[int, int, int] | None:
    """蒸馏计划：(起点, 边界, 摘要输出上限)；None = 不值得蒸馏。

    不值得的两种情形分开算：没有可蒸馏轮次（边界没越过存档点），或区间
    小到蒸不出东西（体量不足摘要上限的 SUMMARY_INPUT_RATIO 倍）——后者
    叫停是省一次模型调用，不是失败。"""
    if state is None:
        return None
    start = state.summary_through
    through = retain_boundary(state.turns, context_window, tool_limit)
    if through <= start:
        return None
    max_tokens = derive_summary_max_tokens(context_window)
    region = _compact_region(state, start, through, tool_limit)
    if estimate_messages(region) < int(max_tokens * SUMMARY_INPUT_RATIO):
        return None
    return start, through, max_tokens


def _compact_region(state, start: int, through: int,
                    tool_limit: int | None = None) -> list[dict]:
    """被蒸馏区间的真实消息（工具结果先按预算裁剪）。

    这里必须是**真实消息**而不是压平后的对话行：指令要求保留精确价格事实与
    AppID，而那些数字只存在于工具返回里；只喂问题与答案，摘要结构上就拿不到
    它们。ZCode 与 DSH 的摘要输入同样是回放原始消息。"""
    return [m for t in state.turns[start:through] for m in _turn_view(t, tool_limit)]


def _compact_messages(sys_prompt: str | None, old_summary: str | None,
                      region: list[dict], instruction: str,
                      *, truncated: bool = False) -> list[dict]:
    """压缩调用输入：系统提示 + 既有存档 + 区间原文 + 指令。

    带上系统提示是为了复用提供方的热前缀缓存（前缀与会话真实请求逐字一致，
    只有尾部指令与摘要输出未命中）；截断重试从区间尾部保留，故需丢掉开头
    落单的 tool 消息——各家协议都要求它紧跟发起它的那次助手调用；并留一条
    截断标记，让摘要知道「上面不是完整区间」，别把缺失当成事实。"""
    msgs: list[dict] = []
    if sys_prompt:
        msgs.append({"role": "system", "content": sys_prompt})
    if old_summary:
        msgs.append({"role": "user", "content": _COMPACT_MERGE_HEAD + old_summary})
    body = list(region)
    while body and body[0].get("role") == "tool":
        body.pop(0)
    if truncated:
        body.insert(0, {"role": "user", "content": _COMPACT_RETRY_MARKER})
    if not body:
        body = [{"role": "user", "content": _COMPACT_RETRY_MARKER}]
    msgs.extend(body)
    msgs.append({"role": "user", "content": instruction})
    return msgs


def _strip_analysis(text: str) -> str:
    """剥掉模型可能自带的推理段，只留存档正文（与 ZCode 同口径的防御）。"""
    out = text.strip()
    if "<analysis>" in out and "</analysis>" in out:
        head, _, tail = out.partition("<analysis>")
        _, _, rest = tail.partition("</analysis>")
        out = (head + rest).strip()
    if "<summary>" in out and "</summary>" in out:
        _, _, rest = out.partition("<summary>")
        body, _, _ = rest.partition("</summary>")
        out = body.strip()
    return out.strip()


async def compact(state, cfg: dict, *, sys_prompt: str | None = None,
                  tool_limit: int | None = None,
                  plan: tuple[int, int, int] | None = None) -> tuple[str, int, tuple[int, int]] | None:
    """把更早轮次蒸馏进要点存档（与既有存档合并），返回 (存档, 存到第几轮, 用量)。

    无可蒸馏轮次、模型失败或空回答返回 None——调用方保持原状即可。压缩输入
    自身超出窗口时按 ZCode 口径丢掉最早一段重试（最多 COMPACT_INPUT_RETRY_LIMIT
    次，附截断标记），重试耗尽才放弃。"""
    if state is None:
        return None
    if plan is None:
        plan = compact_plan(state, cfg.get("context_window"), tool_limit)
    if plan is None:
        return None
    start, through, max_tokens = plan
    region = _compact_region(state, start, through, tool_limit)
    tail = len(region)
    inp = out = 0
    for attempt in range(COMPACT_INPUT_RETRY_LIMIT + 1):
        msgs = _compact_messages(sys_prompt, state.summary, region[-tail:],
                                 _COMPACT_INSTRUCTION, truncated=tail < len(region))
        parts: list[str] = []
        try:
            async for kind, delta in pilot_llm.chat_stream(
                protocol=cfg["protocol"],
                base_url=pilot_llm.effective_base_url(cfg["protocol"], cfg["base_url"]),
                api_key=cfg["api_key"],
                model=cfg["model"],
                messages=msgs,
                max_tokens=max_tokens,
            ):
                if kind == "usage":
                    inp, out = delta
                elif kind == "answer":
                    parts.append(delta)
        except pilot_llm.PilotLlmError as e:
            # 压缩输入自己撑爆窗口：丢掉最早一段再试，不是本会话不可压
            if (attempt >= COMPACT_INPUT_RETRY_LIMIT or tail <= 2
                    or not looks_like_overflow(str(e))):
                return None
            tail = max(2, tail // 2)
            continue
        text = _strip_analysis("".join(parts))
        if not text:
            return None
        return text, through, (inp, out)
    return None



def reference_digest(state) -> str | None:
    """被引会话 → 预算内摘要：有存档用存档，否则从最近轮次往回收集对话行。"""
    if state is None:
        return None
    if state.summary:
        return state.summary
    return digest_lines(state.turns)


_READ_TOOL_CHARS = 400
"""回读历史时单个工具结果的保留字符数。

回读的意义就是把被压缩掉的数据拿回来，精确价格就在工具返回里——整段丢弃
等于「看得见、取不到」，那还不如不给这条路。"""


def _message_line(m: dict, tool_chars: int) -> str | None:
    role = m.get("role")
    if role == "assistant":
        text = str(m.get("content") or "").strip()
        return f"助手：{text}" if text else None
    if role == "tool":
        text = str(m.get("content") or "").strip()
        if not text:
            return None
        if len(text) > tool_chars:
            text = text[:tool_chars] + "…[截断]"
        return f"工具结果：{text}"
    return None


def digest_lines(turns: list[dict], *, budget: int = REFERENCE_BUDGET_TOKENS,
                 tool_chars: int = _READ_TOOL_CHARS) -> str | None:
    """轮次窗口 → 给模型读的文本（含工具返回的关键数据）。

    与 reference_digest 同一件事的另一半：把历史渲染成文本供模型消费。
    从最新往回收集到预算为止，保证越新的越完整。"""
    picked: list[str] = []
    total = 0
    for t in reversed(turns):
        block = [f"用户：{t.get('q') or ''}"]
        for m in t.get("messages") or []:
            line = _message_line(m, tool_chars)
            if line:
                block.append(line)
        if len(block) == 1:
            answer = str((t.get("resp") or {}).get("answer") or "")
            if answer:
                block.append(f"助手：{answer}")
        text = "\n".join(block)
        if picked and total + estimate_tokens(text) > budget:
            break
        picked.append(text)
        total += estimate_tokens(text)
    if not picked:
        return None
    return "\n---\n".join(reversed(picked))


def reference_message(title: str, digest: str) -> dict:
    """跨会话引用 → system 消息（一次性背景，不是当前请求）。"""
    head = f"用户引用了另一段对话《{title or '历史会话'}》。以下是该对话的要点，仅作背景参考，不是当前请求：\n"
    return {"role": "system", "content": head + digest}

