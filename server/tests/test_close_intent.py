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


class _FakeEvent:
    """可订阅的事件（`+=` 记录处理函数），供测试主动派发。

    MagicMock 的 `+=` 会把处理函数吞掉（增强赋值返回新 Mock），
    弹窗的 Shown / FormClosing 与动效 Timer 的 Tick 都要能被回调，故用真实现。
    """

    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, sender=None, args=None) -> None:
        for handler in list(self.handlers):
            handler(sender, args)


class _FakeTimer:
    """动效定时器替身：记录 Interval/启停，Tick 由测试手动派发。"""

    instances: list = []

    def __init__(self) -> None:
        self.Interval = 0
        self.Tick = _FakeEvent()
        self.started = False
        self.disposed = False
        _FakeTimer.instances.append(self)

    def Start(self) -> None:
        self.started = True

    def Stop(self) -> None:
        self.started = False

    def Dispose(self) -> None:
        self.disposed = True


class _FakeRect:
    """假矩形：带 X/Y/Width/Height/Right/Bottom（_round_path 与断言都要读）。"""

    def __init__(self, x, y, w, h) -> None:
        self.X, self.Y, self.Width, self.Height = x, y, w, h
        self.Right, self.Bottom = x + w, y + h


class _FakePoint:
    """假点：真 Point 是带 X/Y 的结构体，同时保留下标语义（存量断言在用）。"""

    def __init__(self, x, y) -> None:
        self.X, self.Y = x, y
        self._t = (x, y)

    def __getitem__(self, i):
        return self._t[i]

    def __eq__(self, other):
        if isinstance(other, _FakePoint):
            return self._t == other._t
        return self._t == other

    def __hash__(self):
        return hash(self._t)


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
        self.Opacity = 1.0
        self.Location = None
        self.Bounds = None
        self.Shown = _FakeEvent()
        self.FormClosing = _FakeEvent()
        self.closed = False

    def ShowDialog(self, *args):
        _SHOW_ARGS.append(args)
        outcome = _SHOW_OUTCOME.pop("value", None)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def Show(self, *args) -> None:
        pass

    def Close(self) -> None:
        self.closed = True

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
    fake_wf.Timer = _FakeTimer
    fake_wf.PictureBoxSizeMode = SimpleNamespace(Zoom=None)
    fake_wf.ControlStyles = SimpleNamespace(
        UserPaint=1, AllPaintingInWmPaint=2, OptimizedDoubleBuffer=4, Opaque=8
    )
    fake_wf.Cursors = SimpleNamespace(Hand=None, Default=None)
    fake_wf.DialogResult = SimpleNamespace(Yes=sent_yes, No=sent_no, Cancel=sent_cancel)
    fake_wf.FormBorderStyle = _LooseNS(FixedSingle=None)
    fake_wf.FormStartPosition = SimpleNamespace(
        CenterParent=None, CenterScreen=None, Manual=None
    )
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
    fake_draw.Point = lambda x, y: _FakePoint(x, y)
    fake_draw.Size = _Size
    fake_draw.Rectangle = lambda *a: _FakeRect(*a)
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
    fake_draw.Drawing2D.CompositingMode = SimpleNamespace(SourceCopy=object())
    fake_draw.Drawing2D.InterpolationMode = SimpleNamespace(
        NearestNeighbor=object(), HighQualityBicubic=object()
    )
    fake_draw.Drawing2D.PixelOffsetMode = SimpleNamespace(HighSpeed=object())

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
    """守卫：意图 quit → 先撤窗（窗口当场消失）+ 返回 True 放行真关闭（closed 事件收尾进程）。"""
    monkeypatch.setattr(desktop, "_ask_close_intent", lambda owner=None, window=None: "quit")
    hidden = []
    window = SimpleNamespace(hide=lambda: hidden.append(1), native=None)
    guard = desktop._make_closing_guard(window)
    assert guard() is True
    assert hidden == [1], "退出路径必须先撤窗：放行后的进程收尾实测还要一两秒，窗口不能留屏上"


def test_guard_minimize_intent_hides_and_cancels(monkeypatch):
    """守卫：意图 minimize 且托盘在位 → 隐藏窗口 + 返回 False（进程常驻）。"""
    monkeypatch.setattr(desktop, "_ask_close_intent", lambda owner=None, window=None: "minimize")
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
    monkeypatch.setattr(desktop, "_ask_close_intent", lambda owner=None, window=None: "minimize")
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
    monkeypatch.setattr(desktop, "_ask_close_intent", lambda owner=None, window=None: "stay")
    hidden = []
    window = SimpleNamespace(hide=lambda: hidden.append(1), native=None)
    guard = desktop._make_closing_guard(window)
    assert guard() is False
    assert not hidden


def test_guard_tray_quit_bypasses_dialog(monkeypatch):
    """守卫：托盘「退出」置位 → 不弹询问直接放行（明确意图免二次确认）。"""
    asked: list = []
    monkeypatch.setattr(
        desktop, "_ask_close_intent", lambda owner=None, window=None: asked.append(1) or "minimize"
    )
    window = SimpleNamespace(hide=lambda: None, native=None)
    guard = desktop._make_closing_guard(window)
    desktop._tray_state["quit"] = True
    try:
        assert guard() is True
    finally:
        desktop._tray_state["quit"] = False  # 模块级状态，测完必须复位
    assert not asked  # 托盘退出不该触发询问


# ── 关窗幕布与出入场动效 ────────────────────────────────────────────────────
#
# 背景：弹窗原本「凭空出现」——没有遮罩、没有动效，用户注意不到弹窗出现。
# 现由两件组成：① 页面级关窗幕布（.hl-close-curtain，网页 backdrop-filter 模糊，
# 桌面壳经后台线程调 __hlxCloseCurtain 拉起）；② 原生弹窗自身的出入场（透明度
# + 位移）。用例钉死四件事：幕布**必须后台线程派发**（evaluate_js 同步阻塞，
# UI 线程直调会自锁）、关窗流程**无论走哪条路都撤幕布**、动效炸穿不得把弹窗
# 留在「不可见」或「关不掉」、**用户的选择必须原样带回 ShowDialog**（关闭被
# 拦下后 WinForms 会把 DialogResult 复位，延后真关时不放回去就只剩 Cancel——
# 真机症状正是两个按键点完弹窗关了、程序什么都不做）。


def test_curtain_script_returns_explicit_boolean():
    """幕布脚本显式返回布尔：挂载点返回值是 undefined，缩写写法会把「已执行」
    也判成 false（曾经真这么错过，探测结果失真）。"""
    script_on = desktop._CURTAIN_JS.format("true")
    script_off = desktop._CURTAIN_JS.format("false")
    assert "return false" in script_on and "return true" in script_on
    assert "__hlxCloseCurtain(true)" in script_on
    assert "__hlxCloseCurtain(false)" in script_off
    assert "!!" not in script_on, "别再用 !! 缩写"


def test_toggle_curtain_dispatches_from_background_thread(monkeypatch):
    """幕布开关走后台线程（UI 线程直调 evaluate_js 会自锁），且绝不抛给调用方。"""
    calls: list = []
    spawns: list = []

    class _FakeThread:
        def __init__(self, target=None, daemon=None, **kw):
            spawns.append((target, daemon))

        def start(self):
            pass  # 不起真线程：测试里手动执行目标

    monkeypatch.setattr(desktop.threading, "Thread", _FakeThread)
    window = SimpleNamespace(evaluate_js=lambda script: calls.append(script))
    desktop._toggle_close_curtain(window, True)
    assert spawns and spawns[0][1] is True, "必须是 daemon 线程"
    target, _ = spawns[0]
    target()
    assert calls and "true" in calls[-1]
    desktop._toggle_close_curtain(window, False)
    target2, _ = spawns[-1]
    target2()
    assert "false" in calls[-1]


def test_toggle_curtain_survives_missing_page_hook(monkeypatch):
    """页面没有挂载点 / 窗口没了 → 静默（关窗流程不受影响）。"""
    desktop._toggle_close_curtain(None, True)

    def _boom(script):
        raise RuntimeError("页面已销毁")

    spawns: list = []

    class _InlineThread:
        def __init__(self, target=None, daemon=None, **kw):
            spawns.append(target)

        def start(self):
            self._target = None

    monkeypatch.setattr(desktop.threading, "Thread", _InlineThread)
    desktop._toggle_close_curtain(SimpleNamespace(evaluate_js=_boom), True)  # 不该抛


def test_enter_animation_fades_in_and_rises(monkeypatch):
    """入场：显示时先隐形 → 逐帧抬不透明度 → 落回基准位。"""
    _patch_winforms(monkeypatch)
    monkeypatch.setattr(desktop, "_animations_enabled", lambda: True)
    dialog, _ = desktop._build_close_dialog(None, "dark")
    _FakeTimer.instances.clear()
    desktop._attach_close_animation(dialog)
    assert dialog.Opacity == 0.0, "显示前就该隐形（Shown 里再归零会闪一帧）"

    dialog.Location = _FakePoint(100, 200)
    dialog.Shown.fire(dialog)
    timer = _FakeTimer.instances[-1]
    assert timer.started and timer.Interval == desktop._ANIM_TICK_MS

    steps = desktop._ANIM_MS // desktop._ANIM_TICK_MS
    for _ in range(steps // 4):
        timer.Tick.fire(timer)
    assert 0.0 < dialog.Opacity < 1.0, "中途应是半透明（真在淡入，不是瞬切）"
    assert dialog.Location.Y > 200, "位移还没落位（自下浮入）"

    for _ in range(steps):
        timer.Tick.fire(timer)
    assert dialog.Opacity == 1.0
    assert (dialog.Location.X, dialog.Location.Y) == (100, 200), "终点必须精确落回基准位"
    assert not dialog.closed


def test_exit_animation_cancels_close_then_closes(monkeypatch):
    """出场：先取消本次关闭等淡出，走完再真关，且把用户的选择原样带回。"""
    _patch_winforms(monkeypatch)
    monkeypatch.setattr(desktop, "_animations_enabled", lambda: True)
    sent_no = object()
    dialog, _ = desktop._build_close_dialog(None, "dark")
    _FakeTimer.instances.clear()
    desktop._attach_close_animation(dialog)
    dialog.Location = _FakePoint(10, 20)
    dialog.Shown.fire(dialog)
    for _ in range(desktop._ANIM_MS // desktop._ANIM_TICK_MS):
        _FakeTimer.instances[-1].Tick.fire(dialog)

    dialog.DialogResult = sent_no
    event = SimpleNamespace(Cancel=False)
    dialog.FormClosing.fire(dialog, event)
    assert event.Cancel is True, "必须先拦住，等淡出走完"
    assert not dialog.closed
    # WinForms 真行为：本次关闭被拦下后 DialogResult 被复位（实测按键的 Yes/No
    # 都被清成空）。替身照此模拟一次——不模拟，「延后真关把选择丢了」这条
    # 回归就测不出来（真机症状：两个按键点完弹窗关了、程序什么都不做）。
    dialog.DialogResult = None

    timer = _FakeTimer.instances[-1]
    for _ in range(desktop._ANIM_MS // desktop._ANIM_TICK_MS):
        timer.Tick.fire(dialog)
    assert dialog.closed, "淡出走完必须真关（对话框关不掉是致命回归）"
    assert dialog.DialogResult is sent_no, "真关前必须把选择放回去（放不回就只剩 Cancel）"
    assert dialog.Opacity == 0.0


def test_programmatic_close_is_not_intercepted(monkeypatch):
    """DialogResult 未选择 = 非用户按键（程序化/system 关闭）→ 不拦、不动效，直接放行。"""
    _patch_winforms(monkeypatch)
    monkeypatch.setattr(desktop, "_animations_enabled", lambda: True)
    dialog, _ = desktop._build_close_dialog(None, "dark")
    _FakeTimer.instances.clear()
    desktop._attach_close_animation(dialog)
    dialog.Shown.fire(dialog)
    _FakeTimer.instances.clear()  # 入场动效的定时器不算，只看这次关闭有没有起出场

    event = SimpleNamespace(Cancel=False)
    dialog.FormClosing.fire(dialog, event)
    assert event.Cancel is False, "非用户按键的关闭不该被拦住"
    assert not _FakeTimer.instances, "非用户按键的关闭不该起出场动效"


def test_animations_disabled_goes_straight_to_final_state(monkeypatch):
    """系统关动画（SPI_GETCLIENTAREAANIMATION=0）→ 终态直接到位，出场不拦关闭。"""
    _patch_winforms(monkeypatch)
    monkeypatch.setattr(desktop, "_animations_enabled", lambda: False)
    dialog, _ = desktop._build_close_dialog(None, "dark")
    _FakeTimer.instances.clear()
    desktop._attach_close_animation(dialog)
    assert dialog.Opacity == 1.0, "关动画时不该被置成隐形"

    dialog.Shown.fire(dialog)
    assert dialog.Opacity == 1.0
    assert not _FakeTimer.instances, "关动画时不该起定时器"

    dialog.DialogResult = object()
    event = SimpleNamespace(Cancel=False)
    dialog.FormClosing.fire(dialog, event)
    assert event.Cancel is False, "关动画时不得拦截关闭"


def test_broken_timer_never_leaves_invisible_dialog(monkeypatch):
    """动效定时器起不来 → 立刻恢复实底：弹窗绝不能停在不可见状态。"""
    fake_wf, _, _, _ = _patch_winforms(monkeypatch)
    monkeypatch.setattr(desktop, "_animations_enabled", lambda: True)

    class _BoomTimer:
        def __init__(self):
            raise RuntimeError("无定时器可用")

    monkeypatch.setattr(fake_wf, "Timer", _BoomTimer)
    dialog, _ = desktop._build_close_dialog(None, "dark")
    _FakeTimer.instances.clear()
    desktop._attach_close_animation(dialog)
    dialog.Location = _FakePoint(5, 6)
    dialog.Shown.fire(dialog)
    assert dialog.Opacity == 1.0, "定时器炸穿必须立刻兜回实底"


def test_ask_close_intent_raises_and_lowers_curtain(monkeypatch):
    """询问流程：弹窗前拉幕布、返回后必撤（含异常路径）。"""
    _patch_winforms(monkeypatch)
    monkeypatch.setattr(desktop, "_animations_enabled", lambda: False)
    calls: list = []

    class _InlineThread:
        def __init__(self, target=None, daemon=None, **kw):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(desktop.threading, "Thread", _InlineThread)
    window = SimpleNamespace(evaluate_js=lambda script: calls.append(script))
    _SHOW_OUTCOME["value"] = None
    assert desktop._ask_close_intent(None, window) == "minimize"
    assert len(calls) == 2 and "true" in calls[0] and "false" in calls[1], calls

    # 弹窗炸穿也要撤幕布
    calls.clear()
    _SHOW_OUTCOME["value"] = RuntimeError("弹窗链路炸穿")
    assert desktop._ask_close_intent(None, window) == "minimize"
    assert len(calls) == 2 and "true" in calls[0] and "false" in calls[1], calls


def test_ask_close_intent_works_without_window(monkeypatch):
    """没有 pywebview 窗口（浏览器兜底/单测）→ 照常弹窗，不碰幕布。"""
    _patch_winforms(monkeypatch)
    _, _, sent_no, _ = _patch_winforms(monkeypatch)
    _SHOW_OUTCOME["value"] = sent_no
    assert desktop._ask_close_intent(object()) == "quit"


# ─ 主题镜像读取（弹窗配色跟随主界面） ──────────────────────────────────────


def _theme_db(tmp_path, raw):
    """造一个只有 app_settings.ui.theme 的库，返回数据目录。"""
    import sqlite3
    from pathlib import Path

    data_dir = Path(tmp_path)
    db = data_dir / desktop.data_dir_filename(desktop.APP_SLUG)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value_json TEXT)")
    if raw is not None:
        con.execute("INSERT INTO app_settings VALUES ('ui.theme', ?)", (raw,))
    con.commit()
    con.close()
    return data_dir


def test_app_theme_reads_json_encoded_mirror(monkeypatch, tmp_path):
    """镜像值是 JSON 编码的（前端 PUT 的是 "light" 带引号）——必须解码后再判。

    历史缺陷：直接拿带引号的裸串比对 "dark"/"light" 永远不中，弹窗静默回落
    深色，表现为「关闭弹窗的主题不跟主界面走」。
    """
    monkeypatch.setattr(desktop, "resolve_data_dir", lambda *a, **k: _theme_db(tmp_path, '"light"'))
    assert desktop._app_theme() == "light"
    monkeypatch.setattr(desktop, "resolve_data_dir", lambda *a, **k: _theme_db(tmp_path, '"dark"'))
    assert desktop._app_theme() == "dark"


def test_app_theme_fallbacks(monkeypatch, tmp_path):
    """裸值/缺行/库损坏都不得炸：裸值认，缺行与坏库按深色兜底。"""
    import sqlite3
    monkeypatch.setattr(desktop, "resolve_data_dir", lambda *a, **k: _theme_db(tmp_path, "dark"))
    assert desktop._app_theme() == "dark", "兼容无引号的裸值"
    monkeypatch.setattr(desktop, "resolve_data_dir", lambda *a, **k: _theme_db(tmp_path, None))
    assert desktop._app_theme() == "dark", "缺行回落深色"
    def _boom(*a, **k):
        raise sqlite3.OperationalError("no such table: app_settings")

    monkeypatch.setattr(desktop, "resolve_data_dir", _boom)
    assert desktop._app_theme() == "dark", "库读不了回落深色"
