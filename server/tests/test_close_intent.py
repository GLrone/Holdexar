"""关窗意图询问（desktop/main.py `_ask_close_intent`）单测：不出网、不弹真窗。

背景：此前点 X **静默隐藏**到托盘——用户以为退出了程序，进程却常驻后台
继续占端口/跑调度。改为点 X 弹二选一后（「最小化」到托盘 /「退出程序」；
按键文案是动作词，系统 MessageBox 出不了，故自绘小 Form），本文件钉死：
1. ShowDialog 结果 → 意图的映射（Yes=最小化、No=退出、未知=安全侧最小化）；
2. 异常降级 = minimize：pythonnet/桌面会话不可用时保持旧的「隐藏常驻」
   语义，绝不误杀进程；
3. 对话框结构：两键文案正确、回车默认=最小化、无 ControlBox、
   owner 传给 ShowDialog；
4. 关窗守卫对两种意图 + 托盘退出置位的行为（返回值即 closing 契约）。

真实弹窗链路（UI 线程同步模态窗）由真窗口冒烟覆盖，此处只测逻辑层。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]


def _load_desktop_main():
    """按路径加载桌面启动器模块（desktop/ 不在 pytest 的 import 路径上）。"""
    spec = importlib.util.spec_from_file_location(
        "holdexar_desktop_main_close", ROOT / "desktop" / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


desktop = _load_desktop_main()

# ── 假件：System.Windows.Forms / System.Drawing（纯 Python 替身） ──────────
# pythonnet 的 CLR 模块对象不可 setattr（.NET 类型对 Python 只读），但
# `from System.Windows.Forms import ...` 走 sys.modules 查找——把条目
# 换成 ModuleType 假模块即可全权控制被测代码拿到的件。


class _FakeButton:
    def __init__(self) -> None:
        self.Text = ""
        self.DialogResult = None
        self.Size = None
        self.Location = None


class _FakeControls:
    def __init__(self) -> None:
        self.items: list = []

    def Add(self, item) -> None:
        self.items.append(item)


class _FakeForm:
    def __init__(self) -> None:
        self.Controls = _FakeControls()
        self.Text = ""
        self.AcceptButton = None
        self.ControlBox = True
        self.ClientSize = None

    def ShowDialog(self, *args):
        _SHOW_ARGS.append(args)  # 类级记账：_ask_close_intent 内部建实例，测试要回看实参
        outcome = _SHOW_OUTCOME.pop("value", None)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def Dispose(self) -> None:
        pass


# ShowDialog 的返回值（或 Exception）——测试按需塞入，消费即弹出
_SHOW_OUTCOME: dict = {}
# 每次 ShowDialog 的实参元组（owner 传递断言用）
_SHOW_ARGS: list = []


def _patch_winforms(monkeypatch):
    """挂假装配件模块，返回 (fake_wf, sentinel_yes, sentinel_no)。"""
    sent_yes, sent_no = object(), object()

    fake_wf = ModuleType("System.Windows.Forms")
    fake_wf.Button = _FakeButton
    fake_wf.Form = _FakeForm

    class _FakeLabel:
        def __init__(self) -> None:
            self.Text = ""
            self.AutoSize = False
            self.Location = None
            self.Font = SimpleNamespace(
                FontFamily=object()
            )  # FontFamily 占位：假 Font 构造器不吃它

    fake_wf.Label = _FakeLabel
    fake_wf.PictureBox = type(
        "PictureBox", (), {"__init__": lambda self: None}
    )
    fake_wf.PictureBoxSizeMode = SimpleNamespace(Zoom=None)
    fake_wf.FormBorderStyle = SimpleNamespace(FixedSingle=None)
    fake_wf.FormStartPosition = SimpleNamespace(CenterParent=None, CenterScreen=None)
    fake_wf.DialogResult = SimpleNamespace(Yes=sent_yes, No=sent_no)

    fake_draw = ModuleType("System.Drawing")
    fake_draw.SystemIcons = SimpleNamespace(
        Question=SimpleNamespace(ToBitmap=lambda: object())
    )
    fake_draw.Point = lambda x, y: (x, y)
    fake_draw.Size = lambda w, h: (w, h)
    fake_draw.Font = lambda *a: object()
    fake_draw.FontStyle = SimpleNamespace(Bold=object())

    import clr  # noqa: F401 —— 真环境同序：装配件命名空间先注册（被测代码也这么走）

    monkeypatch.setitem(sys.modules, "System.Windows.Forms", fake_wf)
    monkeypatch.setitem(sys.modules, "System.Drawing", fake_draw)
    return fake_wf, sent_yes, sent_no


# ── 意图映射与降级 ──────────────────────────────────────────────────────────


def test_yes_maps_to_minimize(monkeypatch):
    """「最小化」键（DialogResult.Yes）→ 最小化到托盘。"""
    fake_wf, sent_yes, _ = _patch_winforms(monkeypatch)
    _SHOW_OUTCOME["value"] = sent_yes
    assert desktop._ask_close_intent(None) == "minimize"


def test_no_maps_to_quit(monkeypatch):
    """「退出程序」键（DialogResult.No）→ 完全退出程序。"""
    fake_wf, _, sent_no = _patch_winforms(monkeypatch)
    _SHOW_OUTCOME["value"] = sent_no
    assert desktop._ask_close_intent(None) == "quit"


def test_unknown_result_falls_back_to_minimize(monkeypatch):
    """未知返回值（如窗口被系统强关返回 None）→ 安全侧最小化。"""
    _patch_winforms(monkeypatch)
    _SHOW_OUTCOME["value"] = None
    assert desktop._ask_close_intent(None) == "minimize"


def test_dialog_failure_falls_back_to_minimize(monkeypatch):
    """弹窗链路炸穿（无桌面/装配件缺失）→ 降级最小化，不误杀进程。"""
    _patch_winforms(monkeypatch)
    _SHOW_OUTCOME["value"] = RuntimeError("no desktop session")
    assert desktop._ask_close_intent(None) == "minimize"


# ── 对话框结构 ──────────────────────────────────────────────────────────────


def test_dialog_structure_and_defaults(monkeypatch):
    """结构：两键文案=「最小化」「退出程序」；回车默认=最小化；无 ControlBox。"""
    fake_wf, sent_yes, sent_no = _patch_winforms(monkeypatch)
    dialog, mapping = desktop._build_close_dialog(None)

    buttons = [
        c for c in dialog.Controls.items
        if isinstance(c, _FakeButton) and c.Text
    ]
    assert [b.Text for b in buttons] == ["最小化", "退出程序"]
    assert buttons[0].DialogResult is sent_yes  # 最小化 → Yes
    assert buttons[1].DialogResult is sent_no   # 退出程序 → No
    assert dialog.AcceptButton is buttons[0]    # 回车默认 = 最小化（安全侧）
    assert dialog.ControlBox is False           # 二选一没有「不选」退路
    assert mapping == {sent_yes: "minimize", sent_no: "quit"}
    assert dialog.Text == f"关闭 {desktop.APP_NAME}"


def test_dialog_gets_owner_in_showdialog(monkeypatch):
    """弹窗必须以主窗口为 owner（CenterParent 居中其上、模态随主窗）。"""
    _patch_winforms(monkeypatch)
    _SHOW_OUTCOME["value"] = None  # 返回值无关紧要，只断言实参
    _SHOW_ARGS.clear()
    owner = object()
    desktop._ask_close_intent(owner)
    assert _SHOW_ARGS and _SHOW_ARGS[-1] == (owner,)  # owner 原样传给 ShowDialog


# ── 关窗守卫（closing 契约） ────────────────────────────────────────────────


def test_guard_quit_intent_releases_close(monkeypatch):
    """守卫：意图 quit → 返回 True 放行真关闭（closed 事件收尾进程）。"""
    monkeypatch.setattr(desktop, "_ask_close_intent", lambda owner=None: "quit")
    hidden = []
    window = SimpleNamespace(hide=lambda: hidden.append(1), native=None)
    guard = desktop._make_closing_guard(window)
    assert guard() is True
    assert not hidden  # 退出路径不再隐藏


def test_guard_minimize_intent_hides_and_cancels(monkeypatch):
    """守卫：意图 minimize → 隐藏窗口 + 返回 False（进程常驻）。"""
    monkeypatch.setattr(desktop, "_ask_close_intent", lambda owner=None: "minimize")
    hidden = []
    window = SimpleNamespace(hide=lambda: hidden.append(1), native=None)
    guard = desktop._make_closing_guard(window)
    assert guard() is False
    assert hidden == [1]


def test_guard_tray_quit_bypasses_dialog(monkeypatch):
    """守卫：托盘「退出」置位 → 不弹询问直接放行（明确意图免二次确认）。"""
    asked: list = []
    monkeypatch.setattr(
        desktop, "_ask_close_intent", lambda owner=None: asked.append(1) or "minimize"
    )
    window = SimpleNamespace(hide=lambda: None, native=None)
    guard = desktop._make_closing_guard(window)
    desktop._tray_state["quit"] = True
    try:
        assert guard() is True
    finally:
        desktop._tray_state["quit"] = False  # 模块级状态，测完必须复位
    assert not asked  # 托盘退出不该触发询问
