"""Holdexar 领航员 SVG 框架与视觉提示词单元测试。"""
from __future__ import annotations

import pytest

from app.domains.pilot import intent as pilot_intent
from app.domains.pilot import service
from app.domains.pilot import svg_framework


def test_is_visual_request():
    assert svg_framework.is_visual_request("帮我画一个价格走势图") is True
    assert svg_framework.is_visual_request("用 SVG 绘制系统架构图") is True
    assert svg_framework.is_visual_request("展示购买决策流程图") is True
    assert svg_framework.is_visual_request("做一个版本对比图表") is True
    assert svg_framework.is_visual_request("分析网络代理调用链") is True
    assert svg_framework.is_visual_request("展示购买决策分支") is True
    assert svg_framework.is_visual_request("这游戏多少钱") is False
    assert svg_framework.is_visual_request("同步我的愿望单") is False
    assert svg_framework.is_visual_request("") is False


def test_deterministic_note_injects_svg_framework():
    # 非视觉提问：不携带 SVG 框架提示
    note_normal = service._deterministic_note("查一下星露谷物语价格", pilot_intent.PRICE_ANALYSIS)
    assert note_normal is not None
    assert "Holdexar 专属 SVG 矢量图形设计规范" not in note_normal["content"]

    # 视觉提问：动态注入 SVG 框架规范与调色板
    note_visual = service._deterministic_note("帮我画一个价格走势图", pilot_intent.PRICE_ANALYSIS)
    assert note_visual is not None
    assert "Holdexar 专属 SVG 矢量图形设计规范" in note_visual["content"]
    assert "#66c0f4" in note_visual["content"]
    assert "三次贝塞尔" in note_visual["content"]
    assert "组件分工" in note_visual["content"]


def test_system_prompt_contains_svg_rule():
    assert "矢量图形与图表渲染" in service._SYSTEM_PROMPT
    assert "#66c0f4" in service._SYSTEM_PROMPT
    assert "贝塞尔曲线" in service._SYSTEM_PROMPT
    assert "专有组件优先" in service._SYSTEM_PROMPT
    assert "无法直接套用" in service._SYSTEM_PROMPT

