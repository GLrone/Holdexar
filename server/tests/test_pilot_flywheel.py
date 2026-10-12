"""决策飞轮单测：决策日志 × 会话账本反馈的标签投影与可训数据集导出。

账本以原始记录直接注入（raw_records），不依赖真实库、不触网。关联键用轮次落账时
生成的唯一 tid——序号会被 truncate 重号、秒级时间戳会同秒碰撞。"""
import json

import pytest

from app.domains.pilot import flywheel, session as pilot_session

_N = iter(range(60))


def _turn(q, *, answer="answer", source="llm", reason=None, tid=None):
    return {"t": "turn", "tid": tid or f"tid{next(_N)}", "q": q,
            "resp": {"source": source, "reason": reason, "answer": answer},
            "ts": "2026-10-12T00:00:00"}


def _feedback(tid, label):
    return {"t": "feedback", "tid": tid, "label": label}


def _write_decisions(root, entries):
    """决策日志写入 pilot 根目录（其下有 sessions/ 子目录）。"""
    root.mkdir(parents=True, exist_ok=True)
    with (root / "decisions.jsonl").open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _sessions(tmp_path):
    """建 <tmp>/pilot/sessions 并返回 (账本根, 账本目录)。"""
    root = tmp_path / "pilot"
    base = root / "sessions"
    base.mkdir(parents=True, exist_ok=True)
    return root, base


def _recs(records):
    def _reader(sid):
        return records.get(sid, [])
    return _reader


class TestTurnOutcomes:
    def test_only_explicit_feedback_marks_successful_answers(self):
        yes = _turn("满意")
        no = _turn("不满意")
        unknown = _turn("未知")
        out = flywheel.turn_outcomes([
            yes, _feedback(yes["tid"], "accepted"),
            no, _feedback(no["tid"], "rejected"),
            unknown,
        ])
        assert out == {
            yes["tid"]: "accepted",
            no["tid"]: "rejected",
            unknown["tid"]: "unlabeled",
        }

    def test_failed_and_empty_are_not_positive_feedback(self):
        recs = [
            _turn("问一"),
            _turn("问二", reason="llm_failed"),
            _turn("问三", source="none"),
        ]
        out = flywheel.turn_outcomes(recs)
        assert [out[r["tid"]] for r in recs] == ["unlabeled", "failed", "empty"]

    def test_truncate_does_not_infer_rejection_and_omits_superseded_turns(self):
        keep_me = _turn("问一")
        drop1 = _turn("问二")
        drop2 = _turn("问三")
        recs = [keep_me, drop1, drop2, {"t": "truncate", "keep": 1}, _turn("问一改")]
        out = flywheel.turn_outcomes(recs)
        assert out[keep_me["tid"]] == "unlabeled"
        assert drop1["tid"] not in out
        assert drop2["tid"] not in out

    def test_explicit_feedback_survives_later_truncate(self):
        first = _turn("多少钱")
        second = _turn("多少钱")
        recs = [first, _feedback(first["tid"], "rejected"),
                {"t": "truncate", "keep": 0}, second]
        out = flywheel.turn_outcomes(recs)
        assert out[first["tid"]] == "rejected"
        assert out[second["tid"]] == "unlabeled"

    def test_truncate_all_rejects_everything_before(self):
        a, b, n = _turn("甲"), _turn("乙"), _turn("新问")
        recs = [a, b, {"t": "truncate", "keep": 0}, n]
        out = flywheel.turn_outcomes(recs)
        assert a["tid"] not in out and b["tid"] not in out
        assert out[n["tid"]] == "unlabeled"

    def test_non_turn_records_ignored(self):
        t = _turn("问")
        out = flywheel.turn_outcomes([{"t": "title", "text": "标题"}, t])
        assert out == {t["tid"]: "unlabeled"}


class TestCollectAndStats:
    def test_join_by_sid_and_turn_ts(self, tmp_path):
        root, base = _sessions(tmp_path)
        t0, t1 = _turn("问一"), _turn("问二", reason="llm_failed")
        _write_decisions(root, [
            {"ts": "2026-10-12T00:00:00", "question": "问一", "intent": "price_analysis",
             "mode": "agent", "source": "llm", "reason": None, "sid": "s1", "tid": t0["tid"]},
            {"ts": "2026-10-12T00:01:00", "question": "问二", "intent": "chat",
             "mode": "agent", "source": "llm", "reason": "llm_failed", "sid": "s1", "tid": t1["tid"]},
            {"ts": "2026-10-12T00:02:00", "question": "无会话问", "intent": "chat",
             "mode": "agent", "source": "llm", "reason": None},
        ])
        rows = flywheel.collect(base, raw_records=_recs({"s1": [t0, t1]}))
        assert [r["feedback"] for r in rows] == ["unlabeled", "failed", "unlabeled"]
        assert rows[0]["sid"] == "s1" and rows[0]["tid"] == t0["tid"]
        assert rows[2]["sid"] is None and rows[2]["tid"] is None

    def test_truncate_does_not_create_negative_feedback(self, tmp_path):
        root, base = _sessions(tmp_path)
        t = _turn("原始问")
        _write_decisions(root, [
            {"question": "原始问", "intent": "chat", "mode": "agent", "source": "llm",
             "sid": "s1", "tid": t["tid"]},
        ])
        rows = flywheel.collect(base, raw_records=_recs({
            "s1": [t, {"t": "truncate", "keep": 0}, _turn("改后问")],
        }))
        assert rows == []

    def test_unknown_turn_reference_is_excluded(self, tmp_path):
        root, base = _sessions(tmp_path)
        _write_decisions(root, [
            {"question": "有 sid 无匹配", "intent": "chat", "mode": "agent",
             "source": "llm", "sid": "s1", "tid": "ghost"},
        ])
        rows = flywheel.collect(base, raw_records=_recs({"s1": [_turn("别的轮")]}))
        assert rows == []

    def test_preference_pairs_require_real_answer_feedback_in_same_session(self, tmp_path):
        root, base = _sessions(tmp_path)
        rejected = _turn("同一问", answer="旧回答")
        accepted = _turn("同一问", answer="新回答")
        other_session_rejection = _turn("同一问", answer="另一个会话里的否定回答")
        _write_decisions(root, [
            {"question": "同一问", "intent": "chat", "mode": "agent", "source": "llm",
             "sid": "s1", "tid": rejected["tid"]},
            {"question": "同一问", "intent": "chat", "mode": "agent", "source": "llm",
             "sid": "s1", "tid": accepted["tid"]},
            {"question": "同一问", "intent": "chat", "mode": "agent", "source": "llm",
             "sid": "s2", "tid": other_session_rejection["tid"]},
        ])
        recs = {
            "s1": [rejected, _feedback(rejected["tid"], "rejected"),
                   accepted, _feedback(accepted["tid"], "accepted")],
            "s2": [other_session_rejection,
                   _feedback(other_session_rejection["tid"], "rejected")],
        }
        rows = flywheel.collect(base, raw_records=_recs(recs))
        stats = flywheel.summarize(rows)
        assert stats["total"] == 3 and stats["labeled"] == 3
        assert stats["by_feedback"]["rejected"] == 2
        assert stats["by_feedback"]["accepted"] == 1
        assert stats["preference_pairs"] == 1
        assert stats["rejected_rate"] == pytest.approx(2 / 3)
        assert stats["by_intent"]["chat"]["accepted"] == 1
        assert stats["by_intent"]["chat"]["total"] == 3
        assert stats["by_intent"]["chat"]["rejected"] == 2
        pairs = flywheel.preference_pairs(rows)
        assert pairs == [{
            "session_id": "s1", "question": "同一问",
            "chosen": "新回答", "rejected": "旧回答",
            "chosen_tid": accepted["tid"], "rejected_tid": rejected["tid"],
        }]

    def test_missing_ledger_file_is_tolerated(self, tmp_path):
        _, base = _sessions(tmp_path)
        rows = flywheel.collect(base, raw_records=_recs({}))
        assert rows == []
        assert flywheel.summarize(rows)["rejected_rate"] is None


class TestDataset:
    def test_dataset_rows_shape(self, tmp_path):
        root, base = _sessions(tmp_path)
        t0, t1, t2 = _turn("黑神话多少钱"), _turn("闲聊"), _turn("")
        _write_decisions(root, [
            {"ts": "2026-10-12T00:00:00", "question": "黑神话多少钱", "intent": "price_analysis",
             "mode": "agent", "source": "llm", "reason": None, "sid": "s1", "tid": t0["tid"]},
            {"question": "闲聊", "intent": "not_a_label", "mode": "agent",
             "source": "llm", "reason": None, "sid": "s1", "tid": t1["tid"]},
            {"question": "", "intent": "chat", "mode": "agent", "source": "llm",
             "reason": None, "sid": "s1", "tid": t2["tid"]},
        ])
        rows = flywheel.collect(base, raw_records=_recs({"s1": [t0, t1, t2]}))
        ds = flywheel.dataset_rows(rows)
        assert len(ds) == 1                       # 词表外意图与空问句被滤掉
        item = ds[0]
        assert item["state"] == "黑神话多少钱"
        q = item["questions"]["intent"]
        assert q["type"] == "choice"
        assert "price_analysis" in q["criteria"]
        assert item["gold"]["intent"]["probabilities"] == {"price_analysis": 1.0}
        assert item["meta"]["feedback"] == "unlabeled"

    def test_build_and_export_roundtrip(self, tmp_path):
        root, base = _sessions(tmp_path)
        t = _turn("问一")
        _write_decisions(root, [
            {"question": "问一", "intent": "chat", "mode": "agent", "source": "llm",
             "reason": None, "sid": "s1", "tid": t["tid"]},
        ])
        result = flywheel.export(base, raw_records=_recs({"s1": [t]}))
        assert result["ok"] is True and result["count"] == 1
        written = (root / "flywheel_dataset.jsonl").read_text(encoding="utf-8").strip()
        item = json.loads(written)
        assert item["state"] == "问一"
        assert result["stats"]["total"] == 1
        assert result["preference_count"] == 0

    def test_deleted_session_rows_and_exported_data_are_purged(self, tmp_path):
        root, base = _sessions(tmp_path)
        _write_decisions(root, [
            {"question": "删除我", "intent": "chat", "sid": "gone", "tid": "old"},
            {"question": "保留我", "intent": "chat", "sid": "keep", "tid": "live"},
        ])
        flywheel.dataset_path(base).write_text("stale training row", encoding="utf-8")
        flywheel.preference_path(base).write_text("stale preference row", encoding="utf-8")

        removed = flywheel.purge_session(base, "gone")

        assert removed is True
        entries = flywheel._read_jsonl(flywheel.decisions_path(base))
        assert [entry["question"] for entry in entries] == ["保留我"]
        assert not flywheel.dataset_path(base).exists()
        assert not flywheel.preference_path(base).exists()


class TestServiceShaping:
    """service 层把 by_intent 摊平成行表（接口 schema 要求），字段齐全。"""

    def test_stats_out_shape(self, tmp_path, monkeypatch):
        from app.domains.pilot import service
        store_base = (tmp_path / "pilot" / "sessions")
        store_base.mkdir(parents=True)
        t = _turn("问一")
        _write_decisions(tmp_path / "pilot", [
            {"question": "问一", "intent": "chat", "mode": "agent", "source": "llm",
             "reason": None, "sid": "s1", "tid": t["tid"]},
        ])
        (store_base / "s1.jsonl").write_text(json.dumps(t, ensure_ascii=False) + "\n",
                                             encoding="utf-8")
        monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
        service._store = None
        try:
            built = service.build_flywheel()
            stats = built["stats"]
            assert stats["by_intent"] == [{
                "intent": "chat", "total": 1, "accepted": 0, "rejected": 0,
                "failed": 0, "empty": 0, "unlabeled": 1,
            }]
            assert stats["labeled"] == 0 and stats["by_feedback"] == {"unlabeled": 1}
        finally:
            service._store = None


class TestSessionFeedback:
    @pytest.mark.asyncio
    async def test_feedback_is_in_session_ledger_and_rewind_invalidates_target(self, tmp_path):
        store = pilot_session.SessionStore(tmp_path / "pilot" / "sessions")
        assert await store.append_turn("s-feedback", {
            "tid": "turn-1", "q": "问题", "resp": {"source": "llm", "answer": "回答"},
        })

        assert await store.set_turn_feedback("s-feedback", "turn-1", "accepted") is None
        records = store.raw_records("s-feedback")
        assert flywheel.turn_outcomes(records)["turn-1"] == "accepted"

        assert await store.truncate("s-feedback", 0)
        assert await store.set_turn_feedback("s-feedback", "turn-1", "rejected") == "turn_gone"

    @pytest.mark.asyncio
    async def test_delete_cleans_decisions_and_both_derived_training_files(self, tmp_path):
        base = tmp_path / "pilot" / "sessions"
        store = pilot_session.SessionStore(base)
        assert await store.append_turn("s-delete", {
            "tid": "turn-1", "q": "请删除这条", "resp": {"source": "llm", "answer": "回答"},
        })
        _write_decisions(tmp_path / "pilot", [
            {"question": "请删除这条", "intent": "chat", "sid": "s-delete", "tid": "turn-1"},
            {"question": "留下这条", "intent": "chat", "sid": "s-keep", "tid": "turn-2"},
        ])
        flywheel.dataset_path(base).write_text("old intent data", encoding="utf-8")
        flywheel.preference_path(base).write_text("old preference data", encoding="utf-8")

        assert await store.delete("s-delete")

        entries = flywheel._read_jsonl(flywheel.decisions_path(base))
        assert [entry["sid"] for entry in entries] == ["s-keep"]
        assert not flywheel.dataset_path(base).exists()
        assert not flywheel.preference_path(base).exists()
