"""pilot 会话存储：领航台对话的落盘账本（JSONL，追加只写）。

一事实一主账本：一个会话一个文件 `<base>/<sid>.jsonl`，每行一条记录，
重启后按行折叠（fold）还原会话状态——这是对话轮次的唯一记录者。

记录三类（t 字段区分，未知类型跳过；坏行跳过，不影响已有内容）：
- {"t":"turn","q","resp","messages","last_game","ts"}  一轮问答；
  messages 只含本轮新增消息（模型上下文由 context 模块装配），降级轮为空表；
- {"t":"summary","text","through","ts"}  压缩存档：turns[:through] 已蒸馏进
  text（同名多份后者胜）。原文轮次永不删除——压缩只改派生视图。
- {"t":"title","text","ts"}  会话标题：首轮按首问自动派生，改名追加新行
  （后者胜）。标题是账本内事实，列表接口只做投影。
- {"t":"proposal","pid","action","items","args","state","ts"}  批量提议：
  待用户确认的批量动作清单（交互中间态，非业务事实）。同 pid 后者胜，
  state=confirmed/rejected/withdrawn 即为终态，不再作为待确认项。

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

logger = logging.getLogger(__name__)

SESSION_MAX_FILES = 200
_STATE_CACHE_MAX = 50
_TITLE_AUTO_CHARS = 24
_TITLE_MAX_LEN = 60

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
        self._sweep()

    # ── 读 ────────────────────────────────────────────────────────────

    async def load(self, sid: str) -> SessionState | None:
        """载入会话（缓存优先）；无文件返回 None，不创建。"""
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
            state = self._fold(sid, self._read_lines(path))
            state.updated_at = self._mtime_iso(path)
            self._cache_put(state)
            return state

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

    async def set_title(self, sid: str, text: str) -> bool:
        """改名/自动起名：追加一条 title 记录（后者胜）。"""
        text = " ".join((text or "").split())[:_TITLE_MAX_LEN]
        if not text:
            return False
        return await self._append(sid, {"t": "title", "text": text})

    async def delete(self, sid: str) -> bool:
        """删除会话文件并逐出缓存；无文件返回 False。"""
        if not valid_sid(sid):
            return False
        async with self._lock(sid):
            path = self._path(sid)
            existed = path.is_file()
            try:
                if existed:
                    path.unlink()
            except OSError as e:
                log_event(
                    logger,
                    f"会话 {sid} 删除失败",
                    level=logging.WARNING,
                    detail={"会话": sid, "原因": repr(e)},
                )
                return False
            self._cache.pop(sid, None)
            return existed

    def title_of(self, state: SessionState) -> str:
        """生效标题：账本 title 行优先，无则首问兜底（旧文件兼容）。"""
        if state.title:
            return state.title
        for t in state.turns:
            q = str(t.get("q") or "").strip()
            if q:
                return derive_title(q)
        return ""

    def list_recent(self, limit: int = 50) -> list[dict]:
        """最近会话投影：mtime 倒序，每文件折叠出标题/轮数（账本只读投影）。"""
        try:
            files = sorted(self.base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
        out: list[dict] = []
        for path in files[: max(0, limit)]:
            sid = path.stem
            if not valid_sid(sid):
                continue
            state = self._fold(sid, self._read_lines(path))
            out.append({
                "sid": sid,
                "title": self.title_of(state),
                "turn_total": len(state.turns),
                "updated_at": self._mtime_iso(path),
            })
        return out

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
        for path in files[: max(0, len(files) - SESSION_MAX_FILES)]:
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
            return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime))
        except OSError:
            return None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
