"""pilot 会话存储：领航台对话的落盘账本（JSONL，追加只写）。

一事实一主账本：一个会话一个文件 `<base>/<sid>.jsonl`，每行一条记录，
重启后按行折叠（fold）还原会话状态——这是对话轮次的唯一记录者。

记录按 t 字段区分（未知类型跳过；坏行跳过，不影响已有内容）：
- {"t":"turn","q","resp","messages","last_game","ts"}  一轮问答；
  messages 只含本轮新增消息（模型上下文由 context 模块装配），降级轮为空表；
- {"t":"summary","text","through","ts"}  压缩存档：turns[:through] 已蒸馏进
  text（同名多份后者胜）。原文轮次永不删除——压缩只改派生视图。
- {"t":"title","text","ts"}  会话标题：首轮按首问自动派生，改名追加新行
  （后者胜）。标题是账本内事实，列表接口只做投影。
- {"t":"proposal","pid","action","items","args","state","ts"}  批量提议：
  待用户确认的批量动作清单（交互中间态，非业务事实）。同 pid 后者胜，
  state=confirmed/rejected/withdrawn 即为终态，不再作为待确认项。
- {"t":"truncate","keep","ts"}  轮次截断（编辑重问）：账本只保留前 keep 轮，
  其后轮次作废。历史行不删不写——折叠到该行时按 keep 截断，审计链完整。
- {"t":"feedback","tid","label","ts"}  用户对单条回答的明确评价。
- {"t":"pin","on","ts"}  置顶标记：会话级布尔（后者胜）。置顶是账本内事实，
  列表接口按它把置顶项排在最前。
- {"t":"archive","on","ts"}  归档标记：会话级布尔（后者胜）。归档只是列表归类，
  账本内容零变化。
- {"t":"fork","from","turn","ts"}  分叉来源：本会话派生自 from 会话的前 turn 轮
  （新文件首行）。它是溯源事实，不参与折叠——仅在明细接口暴露给前端。

写路径：懒创建（首个记录才落文件）、整行单写、失败静默返回 False
（对话不因磁盘故障中断）。读路径：全量读入折叠，末尾撕裂行自然跳过。
生命周期：初始化时清扫——文件数超上限删最旧（闲置会话不再按 TTL 静默删除）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging import log_event
from app.domains.pilot import flywheel as pilot_flywheel

logger = logging.getLogger(__name__)

SESSION_MAX_FILES = 200
_STATE_CACHE_MAX = 50
_TITLE_AUTO_CHARS = 24
_TITLE_MAX_LEN = 60
# 搜索命中片段：以命中词为中心截一段，两端各留定量上下文
_SNIPPET_CHARS = 80


def _clip(text: str, pos: int, span: int, *, width: int = _SNIPPET_CHARS) -> str:
    """以命中位置为中心裁一段片段：命中前后各留一半上下文，两端溢出补省略号。"""
    if len(text) <= width:
        return " ".join(text.split())
    lead = max(0, (width - span) // 2)
    start = max(0, pos - lead)
    end = min(len(text), start + width)
    start = max(0, end - width)
    piece = " ".join(text[start:end].split())
    return ("…" if start > 0 else "") + piece + ("…" if end < len(text) else "")


_SID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def valid_sid(sid: str) -> bool:
    """会话 id 白名单（前端 uuid / 兜底短 id），同时封死路径拼接面。"""
    return bool(_SID_RE.fullmatch(sid or ""))


def derive_title(q: str) -> str:
    """首问 → 标题：压平空白后截断（确定性，不调模型）。"""
    text = " ".join((q or "").split())
    if not text:
        return ""
    return text[:_TITLE_AUTO_CHARS] + ("…" if len(text) > _TITLE_AUTO_CHARS else "")


@dataclass
class SessionState:
    sid: str
    turns: list[dict] = field(default_factory=list)
    summary: str | None = None
    summary_through: int = 0
    # 压缩边界（对话流分隔线的账本依据）：每次压缩一条，after_turn = 压缩时已存在的轮数
    markers: list[dict] = field(default_factory=list)
    title: str | None = None
    last_game: dict | None = None
    updated_at: str | None = None
    # 会话内待确认的批量提议（唯一，新提议覆盖旧的）；终态后清空
    pending_proposal: dict | None = None
    # pid → 提议终态投影（state/done）：历史轮回放时按它校正卡片，避免显示失效按钮
    proposals: dict[str, dict] = field(default_factory=dict)
    # 列表管理标记（会话级布尔事实，账本后者胜）
    pinned: bool = False
    archived: bool = False
    # 分叉溯源：派生自哪个会话的前若干轮（fork 记录，仅明细接口暴露）
    forked_from: str | None = None

    def proposal_state_of(self, pid: str) -> str:
        """提议的有效状态：记录仍为 pending 但已不是当前待定项 = 被新提议取代（withdrawn，
        非用户拒绝）。派生而非落账——重放稳定，避免 fold 里追加记录。"""
        recorded = str(self.proposals.get(pid, {}).get("state") or "")
        if recorded == "pending" and (self.pending_proposal or {}).get("pid") != pid:
            return "withdrawn"
        return recorded


class SessionStore:
    def __init__(self, base: Path):
        self.base = base
        self._cache: dict[str, tuple[float, SessionState]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # 列表投影缓存：sid → (mtime_ns+size 键, 摘要)。命中零折叠——
        # 列表接口高频刷新，逐文件全量折叠是 O(全部历史) 的重复劳动
        self._list_cache: dict[str, tuple[tuple[int, int], dict]] = {}
        self._sweep()

    # ── 读 ────────────────────────────────────────────────────────────

    async def load(self, sid: str) -> SessionState | None:
        """载入会话（缓存优先）；无文件返回 None，不创建。

        冷读的文件 IO 与折叠入线程执行：大账本的首开不在事件循环上阻塞
        并发的流式回合。"""
        if not valid_sid(sid):
            return None
        hit = self._cache.get(sid)
        if hit is not None:
            self._cache[sid] = (time.monotonic(), hit[1])
            return hit[1]
        async with self._lock(sid):
            hit = self._cache.get(sid)
            if hit is not None:
                return hit[1]
            path = self._path(sid)
            if not path.is_file():
                return None
            state = await asyncio.to_thread(self._load_fresh, sid, path)
            self._cache_put(state)
            return state

    def _load_fresh(self, sid: str, path: Path) -> SessionState:
        state = self._fold(sid, self._read_lines(path))
        state.updated_at = self._mtime_iso(path)
        return state

    def raw_records(self, sid: str) -> list[dict]:
        """原始记录行（不折叠）：t 字段分型、坏行已滤——决策飞轮的反馈投影输入。

        返回账本自身形态（含 truncate 这类不参与折叠为状态、但携带用户反馈
        语义的记录），调用方按需自行折叠。读失败按空表处理。"""
        if not valid_sid(sid):
            return []
        return self._read_lines(self._path(sid))

    async def load_or_new(self, sid: str) -> SessionState | None:
        state = await self.load(sid)
        return state if state is not None else self._new(sid)

    def _new(self, sid: str) -> SessionState:
        state = SessionState(sid=sid)
        self._cache_put(state)
        return state

    # ── 写 ────────────────────────────────────────────────────────────

    async def append_turn(self, sid: str, record: dict) -> bool:
        if valid_sid(sid):
            state = await self.load(sid) or self._new(sid)
            if not state.title and record.get("q"):
                await self.set_title(sid, derive_title(str(record["q"])))
        return await self._append(sid, {"t": "turn", **record})

    async def set_turn_feedback(self, sid: str, tid: str, label: str) -> str | None:
        """把用户对某条已落账回答的明确评价追加到同一会话账本。"""
        if not valid_sid(sid) or not tid or label not in ("accepted", "rejected"):
            return "bad_request"
        async with self._lock(sid):
            path = self._path(sid)
            if not path.is_file():
                return "no_session"
            records = await asyncio.to_thread(self._read_lines, path)
            state = self._fold(sid, records)
            if not any(str(turn.get("tid") or "") == tid for turn in state.turns):
                return "turn_gone"
            record = {"t": "feedback", "tid": tid, "label": label, "ts": _now_iso()}
            try:
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError as e:
                log_event(logger, f"会话 {sid} 评价写入失败", level=logging.WARNING,
                          detail={"会话": sid, "原因": repr(e)})
                return "write_failed"
            return None

    async def set_title(self, sid: str, text: str) -> bool:
        """改名/自动起名：追加一条 title 记录（后者胜）。"""
        text = " ".join((text or "").split())[:_TITLE_MAX_LEN]
        if not text:
            return False
        return await self._append(sid, {"t": "title", "text": text})

    async def delete_with_reason(self, sid: str) -> str:
        """删除会话及其训练派生数据，返回可区分的终态。"""
        if not valid_sid(sid):
            return "no_session"
        async with self._lock(sid):
            path = self._path(sid)
            existed = path.is_file()
            if not existed:
                return "no_session"
            if not pilot_flywheel.purge_session(self.base, sid):
                log_event(logger, f"会话 {sid} 飞轮记录清理失败，已保留会话",
                          level=logging.WARNING, detail={"会话": sid})
                return "cleanup_failed"
            try:
                path.unlink()
            except OSError as e:
                log_event(
                    logger,
                    f"会话 {sid} 删除失败",
                    level=logging.WARNING,
                    detail={"会话": sid, "原因": repr(e)},
                )
                return "delete_failed"
            self._cache.pop(sid, None)
            self._list_cache.pop(sid, None)
            return "deleted"

    async def delete(self, sid: str) -> bool:
        """删除会话与其飞轮影子记录、派生训练文件，并逐出缓存。"""
        return await self.delete_with_reason(sid) == "deleted"

    def title_of(self, state: SessionState) -> str:
        """生效标题：账本 title 行优先，无则首问兜底（旧文件兼容）。"""
        if state.title:
            return state.title
        for t in state.turns:
            q = str(t.get("q") or "").strip()
            if q:
                return derive_title(q)
        return ""

    def list_recent(self, limit: int = 50, q: str | None = None) -> list[dict]:
        """最近会话投影：mtime 倒序，每文件折叠出标题/轮数（账本只读投影）。

        无检索时走摘要缓存（stat 键校验，命中零折叠）；q 给定时按关键词
        过滤——命中标题或任一轮的提问/回答正文即保留，并附一段命中片段
        （正文命中时的上下文，标题命中为空）。置顶项排在最前（稳定排序，
        置顶组内仍是 mtime 倒序）。"""
        try:
            files = sorted(self.base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
        needle = (q or "").strip().lower()
        # 检索时扫全部账本（命中可能在很早的会话里，截断窗口会漏），
        # 无检索才按 limit 截断；命中数最后统一按 limit 收口
        window = files if needle else files[: max(0, limit)]
        out: list[dict] = []
        for path in window:
            sid = path.stem
            if not valid_sid(sid):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            key = (st.st_mtime_ns, st.st_size)
            summary: dict | None = None
            snippet = ""
            if not needle:
                hit = self._list_cache.get(sid)
                if hit is not None and hit[0] == key:
                    summary = hit[1]
            if summary is None:
                state = self._fold(sid, self._read_lines(path))
                title = self.title_of(state)
                if needle:
                    hit_match, snippet = self._match(state, title, needle)
                    if not hit_match:
                        continue
                summary = {
                    "sid": sid,
                    "title": title,
                    "turn_total": len(state.turns),
                    "updated_at": _iso_ts(st.st_mtime),
                    "pinned": state.pinned,
                    "archived": state.archived,
                    "forked_from": state.forked_from,
                }
                if not needle:
                    self._list_cache[sid] = (key, summary)
            out.append({**summary, "snippet": snippet})
        out.sort(key=lambda it: not it["pinned"])
        return out[: max(0, limit)] if needle else out

    @staticmethod
    def _match(state: SessionState, title: str, needle: str) -> tuple[bool, str]:
        """关键词命中判定：标题优先，其次逐轮提问与回答正文；返回 (是否命中, 片段)。"""
        if needle in (title or "").lower():
            return True, ""
        for turn in state.turns:
            q = str(turn.get("q") or "")
            if needle in q.lower():
                return True, _clip(q, q.lower().find(needle), len(needle))
            resp = turn.get("resp")
            text = str((resp or {}).get("answer") or "") if isinstance(resp, dict) else ""
            pos = text.lower().find(needle)
            if pos >= 0:
                return True, _clip(text, pos, len(needle))
        return False, ""

    async def append_summary(
        self, sid: str, text: str, through: int, *,
        trigger: str = "auto", pre_tokens: int | None = None,
        post_tokens: int | None = None,
    ) -> bool:
        """压缩落账：摘要 + 边界事实（对话流分隔线由此还原）。

        多次压缩各自成行（后者胜出的是存档内容，边界行都保留）。"""
        record = {"t": "summary", "text": text, "through": int(through),
                  "trigger": trigger, "after_turn": int(through)}
        if pre_tokens is not None:
            record["pre_tokens"] = int(pre_tokens)
        if post_tokens is not None:
            record["post_tokens"] = int(post_tokens)
        return await self._append(sid, record)

    async def append_proposal(self, sid: str, record: dict) -> bool:
        """批量提议落账：同 pid 后者胜（确认/取消即覆盖待确认态）。"""
        return await self._append(sid, {"t": "proposal", **record})

    async def truncate(self, sid: str, keep: int) -> bool:
        """编辑重问：账本只保留前 keep 轮（追加软截断记录，不改写既有行）。

        折叠到该行时把 turns 截到 keep，并同步收紧依赖轮数的派生视图
        （压缩边界、待确认提议）。写失败返回 False，账本保持原样。"""
        if not valid_sid(sid):
            return False
        return await self._append(sid, {"t": "truncate", "keep": max(0, int(keep))})

    async def set_pinned(self, sid: str, on: bool) -> bool:
        """置顶开/关：追加一条 pin 记录（后者胜）。"""
        if not valid_sid(sid):
            return False
        return await self._append(sid, {"t": "pin", "on": bool(on)})

    async def set_archived(self, sid: str, on: bool) -> bool:
        """归档开/关：追加一条 archive 记录（后者胜）。"""
        if not valid_sid(sid):
            return False
        return await self._append(sid, {"t": "archive", "on": bool(on)})

    async def fork(self, src_sid: str, new_sid: str, keep: int) -> bool:
        """分叉：从 src 前 keep 轮派生新会话（写新文件，源账本零改动）。

        新文件首行是 fork 溯源记录，其后是保留前缀的原始轮次行与生效标题、
        仍在保留范围内的压缩存档——原样搬运，不重写字段。目标已存在则拒绝
        （不覆盖任何既有会话）。"""
        if not valid_sid(src_sid) or not valid_sid(new_sid) or src_sid == new_sid:
            return False
        src = await self.load(src_sid)
        if src is None:
            return False
        keep = max(0, min(int(keep), len(src.turns)))
        ts = _now_iso()
        records: list[dict] = [{"t": "fork", "from": src_sid, "turn": keep, "ts": ts}]
        title = self.title_of(src)
        if title:
            records.append({"t": "title", "text": title, "ts": ts})
        # 压缩存档只在边界仍落在保留前缀内时带过来，否则新账本的轮数对不上
        if src.summary and 0 < src.summary_through <= keep:
            records.append({
                "t": "summary", "text": src.summary, "through": src.summary_through,
                "trigger": "auto", "after_turn": src.summary_through, "ts": ts,
            })
        records.extend({**turn} for turn in src.turns[:keep])
        async with self._lock(new_sid):
            if self._path(new_sid).is_file():
                return False  # 目标已存在，绝不覆盖既有会话
            try:
                self.base.mkdir(parents=True, exist_ok=True)
                with self._path(new_sid).open("w", encoding="utf-8") as f:
                    for r in records:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
            except OSError as e:
                log_event(
                    logger,
                    f"分叉会话 {new_sid}（源 {src_sid}）写入失败",
                    level=logging.WARNING,
                    detail={"来源": src_sid, "目标": new_sid, "原因": repr(e)},
                )
                return False
            self._cache_put(self._fold(new_sid, records))
            return True

    async def _append(self, sid: str, record: dict) -> bool:
        if not valid_sid(sid):
            return False
        async with self._lock(sid):
            state = self._cache.get(sid, (0.0, None))[1] or self._new(sid)
            record = {**record, "ts": record.get("ts") or _now_iso()}
            try:
                self.base.mkdir(parents=True, exist_ok=True)
                with self._path(sid).open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError as e:
                log_event(
                    logger,
                    f"会话 {sid} 写入失败，本条记录已丢弃",
                    level=logging.WARNING,
                    detail={"会话": sid, "原因": repr(e)},
                )
                return False
            self._apply(state, record)
            self._cache_put(state)
            return True

    # ── 折叠与清理 ────────────────────────────────────────────────────

    @staticmethod
    def _apply(state: SessionState, record: dict) -> None:
        kind = record.get("t")
        if kind == "turn":
            state.turns.append(record)
            if isinstance(record.get("last_game"), dict) and record["last_game"]:
                state.last_game = record["last_game"]
        elif kind == "summary":
            state.summary = str(record.get("text") or "")
            state.summary_through = max(0, min(int(record.get("through") or 0), len(state.turns)))
            # 每次压缩都是一条边界事实（对话流分隔线）；存档内容后者胜，边界行全保留
            state.markers.append({
                "after_turn": state.summary_through,
                "trigger": str(record.get("trigger") or "auto"),
                "ts": record.get("ts"),
                "pre_tokens": record.get("pre_tokens"),
                "post_tokens": record.get("post_tokens"),
            })
        elif kind == "title":
            text = str(record.get("text") or "")
            if text:
                state.title = text
        elif kind == "proposal":
            pid = str(record.get("pid") or "")
            if pid:
                state.proposals[pid] = {
                    "state": record.get("state"),
                    "done": record.get("done"),
                    "failedCount": len(record.get("failed") or []),
                }
            state.pending_proposal = record if record.get("state") == "pending" else None
        elif kind == "truncate":
            keep = max(0, int(record.get("keep") or 0))
            if keep < len(state.turns):
                state.turns = state.turns[:keep]
            # 派生视图随轮数收紧：压缩边界不能指向已作废的轮
            state.summary_through = min(state.summary_through, len(state.turns))
            state.markers = [m for m in state.markers if m.get("after_turn", 0) <= len(state.turns)]
            state.pending_proposal = None
        elif kind == "pin":
            state.pinned = bool(record.get("on"))
        elif kind == "archive":
            state.archived = bool(record.get("on"))
        elif kind == "fork":
            src = str(record.get("from") or "")
            state.forked_from = src or None

    def _fold(self, sid: str, records: list[dict]) -> SessionState:
        state = SessionState(sid=sid)
        for record in records:
            self._apply(state, record)
        return state

    @staticmethod
    def _read_lines(path: Path) -> list[dict]:
        out: list[dict] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            log_event(
                logger,
                f"会话文件 {path.name} 读取失败，已按空会话处理",
                level=logging.WARNING,
                detail={"文件": path.name, "原因": repr(e)},
            )
            return out
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue  # 末尾撕裂行 / 坏行跳过
            if isinstance(item, dict) and item.get("t"):
                out.append(item)
        return out

    def _sweep(self) -> None:
        """防止会话文件无限增长：仅当文件数超上限时删最旧。

        闲置会话不再按 TTL 静默删除——用户历史对话不应无预警消失；200 文件
        上限足够本地长期使用，超出时删最旧是确定性的、可预期的行为（清扫失败
        不影响服务）。"""
        try:
            self.base.mkdir(parents=True, exist_ok=True)
            files = sorted(self.base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        expired = files[: max(0, len(files) - SESSION_MAX_FILES)]
        for path in expired:
            sid = path.stem
            if valid_sid(sid) and not pilot_flywheel.purge_session(self.base, sid):
                log_event(logger, f"旧会话 {sid} 飞轮记录清理失败，已保留会话",
                          level=logging.WARNING, detail={"会话": sid})
                continue
            try:
                path.unlink()
            except OSError:
                pass

    # ── 路径与缓存 ────────────────────────────────────────────────────

    def _path(self, sid: str) -> Path:
        return self.base / f"{sid}.jsonl"

    def _lock(self, sid: str) -> asyncio.Lock:
        if sid not in self._locks:
            self._locks[sid] = asyncio.Lock()
        return self._locks[sid]

    def _cache_put(self, state: SessionState) -> None:
        self._cache[state.sid] = (time.monotonic(), state)
        if len(self._cache) > _STATE_CACHE_MAX:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            self._locks.pop(oldest, None)
            self._cache.pop(oldest, None)

    @staticmethod
    def _mtime_iso(path: Path) -> str | None:
        try:
            return _iso_ts(path.stat().st_mtime)
        except OSError:
            return None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _iso_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
