"""pilot 决策飞轮：从会话主账本投影反馈统计与本地训练材料。"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

from app.core.logging import log_event

logger = logging.getLogger(__name__)
_DECISIONS_LOCK = threading.RLock()
_VALID_FEEDBACK = frozenset({"accepted", "rejected"})

FAIL_REASONS = frozenset({
    "llm_failed", "cap_reached", "max_steps", "server_error",
    "session_busy", "bad_request", "busy",
})

INTENT_LABELS: dict[str, str] = {
    "price_analysis": "询问某款游戏的价格、折扣、史低，或判断是否值得买",
    "find_games": "要求推荐或寻找新游戏",
    "add_monitor": "要求把某款游戏加进关注 / 监控",
    "create_alert": "要求设置价格提醒",
    "how_to": "询问某个功能怎么用、在哪里",
    "navigate": "要求打开或跳转到某个页面",
    "chat": "闲聊，或无法归入以上类别的自由问答",
}


def _root(sessions_base: Path) -> Path:
    return sessions_base.parent


def decisions_path(sessions_base: Path) -> Path:
    return _root(sessions_base) / "decisions.jsonl"


def dataset_path(sessions_base: Path) -> Path:
    return _root(sessions_base) / "flywheel_dataset.jsonl"


def preference_path(sessions_base: Path) -> Path:
    return _root(sessions_base) / "flywheel_preferences.jsonl"


def _read_jsonl_unlocked(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _read_jsonl(path: Path) -> list[dict]:
    with _DECISIONS_LOCK:
        return _read_jsonl_unlocked(path)


def append_decision(sessions_base: Path, entry: dict) -> bool:
    """追加决策影子记录，与会话删除的清理使用同一进程锁。"""
    path = decisions_path(sessions_base)
    try:
        with _DECISIONS_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def _write_jsonl_atomic(path: Path, rows: list[dict]) -> None:
    temp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temp.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def purge_sessions(sessions_base: Path, session_ids: set[str] | list[str]) -> bool:
    """删除已删除会话的影子记录与含其问答的派生训练文件。"""
    targets = {str(sid) for sid in session_ids if str(sid)}
    if not targets:
        return True
    root = _root(sessions_base)
    with _DECISIONS_LOCK:
        try:
            for derived in (dataset_path(sessions_base), preference_path(sessions_base)):
                derived.unlink(missing_ok=True)
            path = decisions_path(sessions_base)
            entries = _read_jsonl_unlocked(path)
            kept = [entry for entry in entries if str(entry.get("sid") or "") not in targets]
            if len(kept) != len(entries):
                _write_jsonl_atomic(path, kept)
            return True
        except OSError as e:
            log_event(logger, "领航员会话训练记录清理失败", level=logging.WARNING,
                      detail={"会话数": len(targets), "原因": repr(e), "目录": str(root)})
            return False


def purge_session(sessions_base: Path, sid: str) -> bool:
    return purge_sessions(sessions_base, {sid})


def turn_outcomes(records: list[dict]) -> dict[str, str]:
    """只读取显式评价；失败/空答按运行结果归类，截断本身不构成评价。"""
    turns: list[dict] = []
    active: list[str] = []
    feedback_by_tid: dict[str, str] = {}
    for rec in records:
        kind = rec.get("t")
        if kind == "turn":
            tid = str(rec.get("tid") or "")
            if tid:
                turns.append(rec)
                active.append(tid)
        elif kind == "truncate":
            keep = max(0, int(rec.get("keep") or 0))
            active = active[:keep]
        elif kind == "feedback":
            tid = str(rec.get("tid") or "")
            label = str(rec.get("label") or "")
            if tid and label in _VALID_FEEDBACK:
                feedback_by_tid[tid] = label

    active_set = set(active)
    turn_ids = {str(turn.get("tid") or "") for turn in turns}
    outcomes: dict[str, str] = {}
    for rec in turns:
        tid = str(rec.get("tid") or "")
        if tid not in active_set:
            if tid in turn_ids and tid in feedback_by_tid:
                outcomes[tid] = feedback_by_tid[tid]
            continue
        resp = rec.get("resp") or {}
        reason = str(resp.get("reason") or "")
        if reason in FAIL_REASONS:
            outcomes[tid] = "failed"
        elif str(resp.get("source") or "") in ("", "none"):
            outcomes[tid] = "empty"
        else:
            outcomes[tid] = feedback_by_tid.get(tid, "unlabeled")
    return outcomes


def collect(base: Path, *, raw_records=None) -> list[dict]:
    """关联决策影子行与会话原始记录；不存在的轮次不进入训练投影。"""
    rows: list[dict] = []
    records_by_sid: dict[str, list[dict]] = {}
    outcomes_by_sid: dict[str, dict[str, str]] = {}
    details_by_sid: dict[str, dict[str, dict]] = {}
    for entry in _read_jsonl(decisions_path(base)):
        sid = str(entry.get("sid") or "")
        tid = str(entry.get("tid") or "")
        feedback = "unlabeled"
        detail: dict = {}
        if sid and tid:
            if sid not in records_by_sid:
                recs = raw_records(sid) if callable(raw_records) else []
                records_by_sid[sid] = recs
                outcomes_by_sid[sid] = turn_outcomes(recs)
                details_by_sid[sid] = {
                    str(rec.get("tid")): {
                        "question": str(rec.get("q") or ""),
                        "answer": str(((rec.get("resp") or {}).get("answer") or "")),
                        "ordinal": index,
                    }
                    for index, rec in enumerate(item for item in recs if item.get("t") == "turn")
                    if rec.get("tid")
                }
            if tid not in outcomes_by_sid[sid]:
                continue
            feedback = outcomes_by_sid[sid][tid]
            detail = details_by_sid[sid].get(tid, {})
        rows.append({
            "ts": entry.get("ts"),
            "question": detail.get("question") or str(entry.get("question") or ""),
            "answer": detail.get("answer") or "",
            "intent": str(entry.get("intent") or ""),
            "mode": entry.get("mode"),
            "source": entry.get("source"),
            "reason": entry.get("reason"),
            "tools": entry.get("tools") or [],
            "sid": sid or None,
            "tid": tid or None,
            "feedback": feedback,
            "_ordinal": detail.get("ordinal", -1),
        })
    return rows


def _question_key(question: str) -> str:
    return re.sub(r"\s+", " ", question).strip().casefold()


def preference_pairs(rows: list[dict]) -> list[dict]:
    """只生成同会话同问、先明确否定后明确肯定且答案不同的真实回答对。"""
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        sid = str(row.get("sid") or "")
        question = _question_key(str(row.get("question") or ""))
        answer = str(row.get("answer") or "").strip()
        if (not sid or not question or not answer
                or row.get("feedback") not in _VALID_FEEDBACK or not row.get("tid")):
            continue
        groups.setdefault((sid, question), []).append(row)

    pairs: list[dict] = []
    for (_, _), group in groups.items():
        ordered = sorted(group, key=lambda item: int(item.get("_ordinal", -1)))
        used_accepted: set[str] = set()
        for rejected in ordered:
            if rejected["feedback"] != "rejected":
                continue
            for accepted in ordered:
                accepted_tid = str(accepted.get("tid") or "")
                if (accepted["feedback"] == "accepted"
                        and int(accepted.get("_ordinal", -1)) > int(rejected.get("_ordinal", -1))
                        and accepted_tid not in used_accepted
                        and accepted["answer"].strip() != rejected["answer"].strip()):
                    pairs.append({
                        "session_id": rejected["sid"],
                        "question": rejected["question"],
                        "chosen": accepted["answer"],
                        "rejected": rejected["answer"],
                        "chosen_tid": accepted_tid,
                        "rejected_tid": rejected["tid"],
                    })
                    used_accepted.add(accepted_tid)
                    break
    return pairs


def summarize(rows: list[dict]) -> dict:
    """汇总运行结果与用户明确评价；未知结果不会混入接受/拒绝率。"""
    by_intent: dict[str, dict[str, int]] = {}
    labels: dict[str, int] = {}
    for row in rows:
        intent = row["intent"] or "(unknown)"
        bucket = by_intent.setdefault(intent, {})
        feedback = row["feedback"]
        bucket[feedback] = bucket.get(feedback, 0) + 1
        bucket["total"] = bucket.get("total", 0) + 1
        labels[feedback] = labels.get(feedback, 0) + 1
    labeled = sum(labels.get(label, 0) for label in ("accepted", "rejected", "failed", "empty"))
    rejected = labels.get("rejected", 0)
    rated = labels.get("accepted", 0) + rejected
    return {
        "total": len(rows),
        "labeled": labeled,
        "unlabeled": labels.get("unlabeled", 0),
        "by_feedback": labels,
        "by_intent": by_intent,
        "rejected_rate": (rejected / rated) if rated else None,
        "preference_pairs": len(preference_pairs(rows)),
        "by_mode": _count(rows, "mode"),
        "by_source": _count(rows, "source"),
    }


def _count(rows: list[dict], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        key = str(row.get(field) or "(none)")
        out[key] = out.get(key, 0) + 1
    return out


def dataset_rows(rows: list[dict]) -> list[dict]:
    """导出兼容的意图分类训练格式；答案评价只作为元信息。"""
    out: list[dict] = []
    for row in rows:
        intent = row["intent"]
        if intent not in INTENT_LABELS or not row["question"]:
            continue
        out.append({
            "state": row["question"],
            "questions": {
                "intent": {
                    "type": "choice",
                    "instructions": "判断用户这句话属于哪类意图，只依据问句本身。",
                    "criteria": dict(INTENT_LABELS),
                },
            },
            "gold": {"intent": {"probabilities": {intent: 1.0}}},
            "meta": {"feedback": row["feedback"], "mode": row["mode"],
                     "sid": row["sid"], "tid": row["tid"], "ts": row["ts"]},
        })
    return out


def _build_unlocked(base: Path, *, raw_records=None) -> dict:
    rows = collect(base, raw_records=raw_records)
    pairs = preference_pairs(rows)
    return {"rows": rows, "stats": summarize(rows), "dataset": dataset_rows(rows),
            "preferences": pairs}


def build(base: Path, *, raw_records=None) -> dict:
    with _DECISIONS_LOCK:
        return _build_unlocked(base, raw_records=raw_records)


def export(base: Path, *, raw_records=None) -> dict:
    """覆盖写两种可本地训练材料；所有输入仍由会话与决策记录派生。"""
    dataset = dataset_path(base)
    preferences = preference_path(base)
    try:
        with _DECISIONS_LOCK:
            built = _build_unlocked(base, raw_records=raw_records)
            _write_jsonl_atomic(dataset, built["dataset"])
            _write_jsonl_atomic(preferences, built["preferences"])
    except OSError as e:
        for path in (dataset, preferences):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        log_event(logger, "决策飞轮数据集导出失败", level=logging.WARNING,
                  detail={"原因": repr(e)})
        return {"ok": False, "reason": "write_failed", "count": 0,
                "preference_count": 0, "path": str(dataset), "preference_path": str(preferences)}
    return {"ok": True, "reason": None, "count": len(built["dataset"]),
            "preference_count": len(built["preferences"]),
            "path": str(dataset), "preference_path": str(preferences),
            "stats": built["stats"]}
