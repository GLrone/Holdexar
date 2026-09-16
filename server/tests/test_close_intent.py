"""关窗意图询问（desktop/main.py）单测：不出网、不弹真窗。

背景：此前点 X **静默隐藏**到托盘——用户以为退出了程序，进程却常驻后台
继续占端口/跑调度。现改为点 X 弹二选一（「最小化」/「退出程序」，右上角
X = 留在窗口）。按键文案是动作词，系统 MessageBox 出不了，故整窗自绘
（无边框 + 自绘标题栏 + 圆角按键）。

本文件钉死：
1. ShowDialog 结果 → 意图映射（Yes=最小化、No=退出、Cancel=留在窗口、
   未知/弹窗链路炸穿=安全侧最小化）；
2. 对话框结构：三个可点控件（最小化/退出程序/关闭）齐全、两键成组居中、
   关闭键在右上、文本 Label 固定宽（AutoSize 的 Label 不折行，超窗宽即截断）；
3. 关窗守卫对三种意图 + 托盘退出置位的行为（返回值即 closing 契约）。

假件用 MagicMock 派生：控件 API 面很大（Paint/Click/MouseEnter/SetStyle/
FindForm…），逐个手写既脆又长；MagicMock 对任意属性与方法都安全，只在
需要断言处读回真实赋值（Text/Size/Location/AccessibleName）。

真实弹窗链路（UI 线程同步模态窗 + 自绘渲染）由真窗口冒烟覆盖，此处只测
逻辑与结构。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

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


class _Widget(MagicMock):
    """假控件：任意属性读写、方法调用、事件绑定（+=）都安全。"""


class _FakeLabel(_Widget):
    """Label 载体（标题/正文/关闭键）。"""


class _FakeButton(_Widget):
    """Button 载体（两个主按键，DialogResult / AcceptButton 语义）。"""


class _LooseNS:
    """假枚举容器：任意成员名都取得到（含 Python 关键字成员名如 "None"）。

    AutoScaleMode 的 None 成员只能用 getattr 取，而 getattr 对普通
    SimpleNamespace 缺该属性会抛 AttributeError——这里用 __getattr__ 兜底
    返回 None，让关键字成员名也能被取到。
    """

    def __init__(self, **members) -> None:
        self.__dict__.update(members)

    def __getattr__(self, _name):
        return None


class _Controls:
    def __init__(self) -> None:
        self.items: list = []

    def Add(self, item) -> None:
        self.items.append(item)


_SHOW_OUTCOME: dict = {}   # ShowDialog 的返回值（或 Exception）
_SHOW_ARGS: list = []      # ShowDialog 的实参（owner 传递断言）


class _FakeForm(_Widget):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.Controls = _Controls()
        self.ClientSize = SimpleNamespace(Width=440, Height=204)
        self.Width = 440
        self.Height = 204
        self.DialogResult = None

    def ShowDialog(self, *args):
        _SHOW_ARGS.append(args)
        outcome = _SHOW_OUTCOME.pop("value", None)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def Dispose(self) -> None:
        pass


def _patch_winforms(monkeypatch):
    """挂假装配件模块，返回 (fake_wf, sentinel_yes, sentinel_no, sentinel_cancel)。"""
    sent_yes, sent_no, sent_cancel = object(), object(), object()
    # 主题读取钉死深色：结构测试不该碰真实库（_app_theme 同步读 app_settings）
    monkeypatch.setattr(desktop, "_app_theme", lambda: "dark")

    fake_wf = ModuleType("System.Windows.Forms")
    fake_wf.Form = _FakeForm
    fake_wf.Label = _FakeLabel
    fake_wf.Button = _FakeButton
    fake_wf.PictureBox = _Widget
    fake_wf.PictureBoxSizeMode = SimpleNamespace(Zoom=None)
    fake_wf.ControlStyles = SimpleNamespace(
        UserPaint=1, AllPaintingInWmPaint=2, OptimizedDoubleBuffer=4
    )
    fake_wf.Cursors = SimpleNamespace(Hand=None)
    fake_wf.DialogResult = SimpleNamespace(Yes=sent_yes, No=sent_no, Cancel=sent_cancel)
    fake_wf.FormBorderStyle = _LooseNS(FixedSingle=None)
    fake_wf.FormStartPosition = SimpleNamespace(CenterParent=None, CenterScreen=None)
    fake_wf.AutoScaleMode = _LooseNS(Dpi=None, Font=None)
    fake_wf.MouseButtons = SimpleNamespace(Left=None)
    fake_wf.FlatStyle = SimpleNamespace(Flat=None)
    fake_wf.Keys = SimpleNamespace(Enter=None, Escape=None)

    fake_draw = ModuleType("System.Drawing")

    class _Gfx:
        def __init__(self, *a) -> None:
            pass

        def __getattr__(self, _name):
            return lambda *a, **k: None

    class _Size:
        """既可下标取值（Location[0] 的 tuple 语义）也可读 Width/Height。"""

        def __init__(self, w, h) -> None:
            self.Width = w
            self.Height = h
            self._t = (w, h)

        def __getitem__(self, i):
            return self._t[i]

        def __eq__(self, other):
            return isinstance(other, _Size) and self._t == other._t

        def __hash__(self):
            return hash(self._t)

    fake_draw.Bitmap = _Widget
    fake_draw.Graphics = _Gfx
    fake_draw.Color = SimpleNamespace(FromArgb=lambda *a: object(), Transparent=object())
    fake_draw.SolidBrush = lambda *a: object()
    fake_draw.Pen = lambda *a: object()
    fake_draw.Point = lambda x, y: (x, y)
    fake_draw.Size = _Size
    fake_draw.Rectangle = lambda *a: object()
    fake_draw.RectangleF = lambda *a: object()
    fake_draw.Region = lambda *a: object()
    fake_draw.Icon = lambda *a: SimpleNamespace(ToBitmap=lambda: object())
    fake_draw.Font = lambda *a: object()
    fake_draw.FontStyle = SimpleNamespace(Bold=object(), Regular=object())
    fake_draw.FontFamily = lambda *a: object()
    fake_draw.GraphicsUnit = SimpleNamespace(Pixel=object())
    fake_draw.StringFormat = _Widget
    fake_draw.StringAlignment = SimpleNamespace(Center=object())
    fake_draw.Drawing2D = ModuleType("System.Drawing.Drawing2D")
    fake_draw.Drawing2D.GraphicsPath = _Widget
    fake_draw.Drawing2D.SmoothingMode = SimpleNamespace(AntiAlias=object())

    import clr  # noqa: F401 —— 真环境同序：装配件命名空间先注册（被测代码也这么走）

    monkeypatch.setitem(sys.modules, "System.Windows.Forms", fake_wf)
    monkeypatch.setitem(sys.modules, "System.Drawing", fake_draw)
    monkeypatch.setitem(sys.modules, "System.Drawing.Drawing2D", fake_draw.Drawing2D)
    return fake_wf, sent_yes, sent_no, sent_cancel


def _by_name(controls):
    """按 AccessibleName 取控件（自绘按键用语义名标识，读屏同源）。

    MagicMock 未显式赋值的属性也返回 Mock（truthy），故只认**真实字符串**
    的语义名——被测代码必须显式 `AccessibleName = text` 才会被找到。
    """
    out = {}
    for control in controls:
        name = getattr(control, "AccessibleName", None)
        if isinstance(name, str) and name:
            out[name] = control
    return out


# ── 意图映射与降级 ──────────────────────────────────────────────────────────


def test_yes_maps_to_minimize(monkeypatch):
    """「最小化」键（DialogResult.Yes）→ 最小化到托盘。"""
    _, sent_yes, _, _ = _patch_winforms(monkeypatch)
    _SHOW_OUTCOME["value"] = sent_yes
    assert desktop._ask_close_intent(None) == "minimize"


def test_no_maps_to_quit(monkeypatch):
    """「退出程序」键（DialogResult.No）→ 完全退出程序。"""
    _, _, sent_no, _ = _patch_winforms(monkeypatch)
    _SHOW_OUTCOME["value"] = sent_no
    assert desktop._ask_close_intent(None) == "quit"


def test_cancel_maps_to_stay(monkeypatch):
    """右上角 X（DialogResult.Cancel）→ 留在窗口，什么都不做。"""
    _, _, _, sent_cancel = _patch_winforms(monkeypatch)
    _SHOW_OUTCOME["value"] = sent_cancel
    assert desktop._ask_close_intent(None) == "stay"


def test_unknown_result_falls_back_to_minimize(monkeypatch):
    """未知返回值 → 安全侧最小化。"""
    _patch_winforms(monkeypatch)
    _SHOW_OUTCOME["value"] = None
    assert desktop._ask_close_intent(None) == "minimize"


def test_dialog_failure_falls_back_to_minimize(monkeypatch):
    """弹窗链路炸穿（无桌面/装配件缺失）→ 降级最小化，不误杀进程。"""
    _patch_winforms(monkeypatch)
    _SHOW_OUTCOME["value"] = RuntimeError("no desktop session")
    assert desktop._ask_close_intent(None) == "minimize"


# ── 对话框结构 ──────────────────────────────────────────────────────────────


def test_dialog_structure(monkeypatch):
    """结构：三键齐全；两键成组居中且同尺寸；关闭键在右上。"""
    _patch_winforms(monkeypatch)
    dialog, mapping = desktop._build_close_dialog(None)

    controls = _by_name(dialog.Controls.items)
    assert set(controls) == {"最小化", "退出程序", "关闭"}, list(controls)

    min_btn, quit_btn = controls["最小化"], controls["退出程序"]
    close_btn = controls["关闭"]
    assert min_btn.Size == quit_btn.Size
    assert min_btn.Location[1] == quit_btn.Location[1]
    width = dialog.ClientSize.Width
    left = min(min_btn.Location[0], quit_btn.Location[0])
    right = max(
        min_btn.Location[0] + min_btn.Size[0], quit_btn.Location[0] + quit_btn.Size[0]
    )
    assert abs((left + right) / 2 - width / 2) <= 1.0, "两键组应水平居中"
    assert close_btn.Location[1] < min_btn.Location[1], "关闭键应在标题栏行"
    assert close_btn.Location[0] > left, "关闭键应在右侧"
    assert set(mapping.values()) == {"minimize", "quit", "stay"}


def test_dialog_texts_not_autosize(monkeypatch):
    """文本 Label 必须 AutoSize=False：AutoSize 的 Label 不折行，超宽即截断。"""
    _patch_winforms(monkeypatch)
    dialog, _ = desktop._build_close_dialog(None)
    # 文本 Label = Label 载体且无语义名（标题/正文；关闭键有语义名"关闭"）
    labels = [
        c
        for c in dialog.Controls.items
        if isinstance(c, _FakeLabel)
        and not isinstance(getattr(c, "AccessibleName", None), str)
    ]
    assert labels, "对话框应有文本 Label"
    for label in labels:
        assert label.AutoSize is False
        assert label.Size is not None


def test_dialog_gets_owner_in_showdialog(monkeypatch):
    """弹窗必须以主窗口为 owner（居中其上、模态随主窗）。"""
    _patch_winforms(monkeypatch)
    _SHOW_OUTCOME["value"] = None  # 返回值无关紧要，只断言实参
    _SHOW_ARGS.clear()
    owner = object()
    desktop._ask_close_intent(owner)
    assert _SHOW_ARGS and _SHOW_ARGS[-1] == (owner,)


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
    """守卫：意图 minimize 且托盘在位 → 隐藏窗口 + 返回 False（进程常驻）。"""
    monkeypatch.setattr(desktop, "_ask_close_intent", lambda owner=None: "minimize")
    hidden = []
    window = SimpleNamespace(hide=lambda: hidden.append(1), minimize=lambda: None, native=None)
    guard = desktop._make_closing_guard(window)
    desktop._tray_state["tray"] = object()  # 托盘在位：隐藏后还有图标可召回
    try:
        assert guard() is False
    finally:
        desktop._tray_state["tray"] = None
    assert hidden == [1]


def test_guard_minimize_without_tray_minimizes_to_taskbar(monkeypatch):
    """托盘缺失时**不得** hide()：窗口从任务栏一起消失又无处召回，用户只能去
    任务管理器杀进程（「托盘里没有它、任务管理器里却还活着」的成因）。

    退化路径：最小化到任务栏——窗口还在，随时点得回来。"""
    monkeypatch.setattr(desktop, "_ask_close_intent", lambda owner=None: "minimize")
    hidden: list = []
    minimized: list = []
    window = SimpleNamespace(
        hide=lambda: hidden.append(1), minimize=lambda: minimized.append(1), native=None
    )
    guard = desktop._make_closing_guard(window)
    desktop._tray_state["tray"] = None
    assert guard() is False
    assert minimized == [1], "无托盘时必须最小化而不是隐藏"
    assert not hidden


def test_guard_stay_intent_keeps_window(monkeypatch):
    """守卫：意图 stay（点了弹窗 X）→ 只返回 False，不隐藏窗口。"""
    monkeypatch.setattr(desktop, "_ask_close_intent", lambda owner=None: "stay")
    hidden = []
    window = SimpleNamespace(hide=lambda: hidden.append(1), native=None)
    guard = desktop._make_closing_guard(window)
    assert guard() is False
    assert not hidden


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
