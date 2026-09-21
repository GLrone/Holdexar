"""注释留痕门禁（scripts/check_dev_traces.py）的行为测试。

**本文件不能出现留痕词形本身**：命中样例一律由拼接构造（`"实" + "测"`），
否则本文件会命中自己的特征——这反过来正证明门禁是「按注释文本」判定，
而不是把整棵树当文本搜关键词。

边界由两组用例钉死：
- 命中：注释 / docstring 里的过程叙述、AI 痕迹、改动交代、问答式论证；
- 放过：行为与约束描述、字符串字面量（哪怕值里带留痕词）、注释里的 URL。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))  # 与 conftest 一致，供 app.* 导入

_spec = importlib.util.spec_from_file_location(
    "check_dev_traces", ROOT / "scripts" / "check_dev_traces.py"
)
assert _spec and _spec.loader
check_dev_traces = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_dev_traces)

# 留痕词形按字符拼接，避免本文件自我命中（见模块头注）
MEASURED = "实" + "测"
DECIDED = "拍" + "板"
OURS = "我" + "们"
WHY = "为" + "什么"


def labels(findings) -> list[str]:
    return [f.label for f in findings]


def scan_py(text: str):
    return check_dev_traces.scan_comments(
        "probe.py", check_dev_traces.extract_comments("probe.py", text)
    )


def scan_js(text: str):
    return check_dev_traces.scan_comments(
        "probe.js", check_dev_traces.extract_comments("probe.js", text)
    )


# ── 必须命中 ──


def test_detects_measurement_narrative():
    assert labels(scan_py(f"# {MEASURED} 2000040：字段在 best_purchase_option 上")) == [
        "过程叙述（验证 / 决策经过）"
    ]


def test_detects_measurement_stats():
    """把开发期统计写进注释（数量分布）同属过程叙述。"""
    line = f"# …会把可共享的也标成已排除：{MEASURED} 859 款分布为 0:751 / 3:73"
    assert labels(scan_py(line)) == ["过程叙述（验证 / 决策经过）"]


def test_detects_decision_narrative():
    assert labels(scan_py(f"# {DECIDED}：阈值整体抬高")) == [
        "过程叙述（验证 / 决策经过）"
    ]


def test_detects_ai_generated_marker():
    assert labels(scan_py("# 图标资产（AI " + "生成，assets/ 四档）")) == ["AI 生成痕迹"]


def test_detects_history_and_removal():
    for line in (
        "# 旧写法 `is not None` 会误伤",
        "# 该分支随 v-if 链一并去掉了",
        "# 重构后经 account_service 读表",
    ):
        assert labels(scan_py(line)) == ["历史对比 / 改动交代"], line


def test_detects_question_style_heading():
    for line in (
        f"// {WHY}需要它：切页慢\n",
        f"/* {WHY}不用轮询：轮询会平摊 */\n",
        f"/**\n * {WHY}必须定时重拉：节点列表会变\n */\n",
    ):
        assert "问答式开发论证" in labels(scan_js(line)), line


def test_detects_background_section():
    assert labels(scan_py("# 背景：旧形态点了就静默隐藏")) == ["开发背景叙述"]


def test_detects_first_person():
    assert labels(scan_js(f"// 留给我们写的那一份\n")) == ["第一人称叙述"]


def test_detects_dev_date_in_comment():
    assert labels(scan_py("# 动效分级（2026-09-20 定）：") ) == ["注释内开发日期"]


def test_detects_in_docstring_not_just_hash_comment():
    text = f'def f():\n    """{WHY}必须绕开代理：否则探测一起失败。"""\n'
    assert "问答式开发论证" in labels(scan_py(text))


# ── 必须放过 ──


def test_allows_behavior_and_constraint_comments():
    for line in (
        "# 先记账再发信（发信失败不重试、不刷屏）",
        "# hook(true) 返回 undefined，不能用 !!x 缩写",
        "# 赠送判定必须在 is_free 之前：赠送中条目 is_free 也为 true",
        "# 判「被排除」必须比 0，`is not None` 会把可共享的也标成已排除",
    ):
        assert scan_py(line) == [], line


def test_ignores_string_literals():
    """词形出现在字符串值里不算留痕——门禁判定的是注释文本。"""
    text = f'MAIL = "{OURS}会第一时间把消息送到这个邮箱。"\n'
    assert scan_py(text) == []


def test_ignores_url_double_slash():
    text = 'const u = "https://example.com/a"  // 直链\n'
    assert scan_js(text) == []


def test_ignores_markup_and_selectors():
    text = "const h = `<div class='x'>${MEASURED}高度</div>`\n"
    assert scan_js(text) == []


def test_html_comment_is_scanned():
    text = f"<!-- {WHY}需要它：占位 -->\n"
    assert "问答式开发论证" in labels(scan_js(text))


def test_block_comment_multiline_reports_real_line():
    text = "/*\n 行为描述\n " + MEASURED + " 859 款分布\n*/\n"
    findings = scan_js(text)
    assert [f.label for f in findings] == ["过程叙述（验证 / 决策经过）"]
    assert "第 3 行" in findings[0].detail


def test_non_scannable_extension_returns_no_comments():
    assert check_dev_traces.extract_comments("data.json", f"// {MEASURED}\n") == []


# ── 门禁自身的纪律 ──


def test_patterns_are_single_source_for_agents_md():
    """禁入词形的判定类目必须齐备，且与脚本清单逐条对应。"""
    labels_all = {label for _p, label in check_dev_traces.TRACE_PATTERNS}
    assert labels_all == {
        "过程叙述（验证 / 决策经过）",
        "AI 生成痕迹",
        "历史对比 / 改动交代",
        "问答式开发论证",
        "开发背景叙述",
        "第一人称叙述",
        "注释内开发日期",
    }


def test_allow_entries_carry_reason():
    for path, rules in check_dev_traces.ALLOW.items():
        for pattern, reason in rules:
            assert pattern and reason, path