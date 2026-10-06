"""游戏名检索影子文本：CJK 二元组滑窗 + 英文原词保留。

bigram 与 FTS5 的约束匹配：SQLite FTS5 内置 unicode61 分词器不切 CJK
（连续汉字整段成词），trigram 需 ≥3 字符成词——2 字查询（只狼/黑魂/群星，
游戏名的主体形态）会整批无命中。把名称预切为空格分隔的 2-gram 后，每个
片段在 unicode61 下恰好自成一个 token，2 字查询即可命中。纯确定性函数，
零网络零依赖：写入侧经 SQLite UDF 由触发器同步（覆盖全部 SQL 写入方），
查询侧由 search_catalog 用同一函数变换查询词（两侧口径同源，永不漂移）。

英文 token 原样保留（unicode61 对 ASCII 自带大小写折叠）；含 CJK 的
token 内逐字符滑窗（标点随之入片，跨空格不连片）。
"""

_CJK_RANGES = (
    (0x2E80, 0x9FFF),  # CJK 部首扩展 / 音标 / 主平面统一表意
    (0xAC00, 0xD7AF),  # 谚文
    (0xF900, 0xFAFF),  # CJK 兼容表意
)


def _has_cjk(token: str) -> bool:
    return any(lo <= ord(ch) <= hi for ch in token for lo, hi in _CJK_RANGES)


def name_bigram(text: str | None) -> str:
    """名称 → 影子检索文本。例：只狼：影逝二度 Sekiro → 只狼 狼： ：影 影逝 逝二 二度 Sekiro"""
    if not text:
        return ""
    out: list[str] = []
    for token in text.split():
        if _has_cjk(token):
            out.extend(token[i : i + 2] for i in range(len(token) - 1))
        else:
            out.append(token)
    # 去重保序：OR 语义下重复片段只增索引体积；保序让影子文本可读可核对
    return " ".join(dict.fromkeys(out))


def fts_match_expr(query: str, max_tokens: int = 32) -> str:
    """查询词 → FTS5 MATCH 表达式（token 间 OR，评测数排序兜精度）。

    无可用 token（纯标点 / 单字 CJK）返回空串，调用方退回 LIKE 通道。
    """
    if not query:
        return ""
    pieces = name_bigram(query).split()
    if not pieces:
        return ""
    quoted = ' OR '.join('"%s"' % p.replace('"', '""') for p in pieces[:max_tokens])
    return quoted
