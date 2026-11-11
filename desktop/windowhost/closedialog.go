//go:build windows

// 关窗意图弹窗：无边框自绘模态窗，二选一（最小化到托盘 / 退出程序），
// 几何、配色、动效与页面幕布对齐原界面；问不出意图一律降级 minimize。
package main

import (
	"fmt"
	"sync/atomic"
	"syscall"
	"unsafe"
)

const (
	dialogWidth  = 520
	dialogHeight = 240
	cornerRadius = 12
	animTickMS   = 15
	animSteps    = 11 // 170ms/15ms
	animRise     = 16

	closeRectLeft = 466
	closeRectTop  = 12
	closeRectSize = 36
	btnWidth      = 200
	btnHeight     = 44
	btnY          = 170
	btnX0         = 52
	btnGap        = 16

	dialogClassName = "HoldexarCloseDialog"
	gwlpUserdata    = ^uintptr(20) // -21

	vkReturn = 0x0D
	vkEscape = 0x1B
	wmMouseLeave = 0x02A2
	wmNCHitTest  = 0x0084
)

var activeDialog atomic.Pointer[closeDialog]
var dialogClassRegistered bool

type dialogTheme struct {
	bg, border, head, subtle          uint32
	accentFill, accentHover, onFill   uint32
	dangerFill, dangerHover, onDanger uint32
	closeFG, closeFGHover             uint32
	closeHoverBG                      uint32
	logoFile                          string
}

func colorref(r, g, b uint32) uint32 { return r | g<<8 | b<<16 }

// blend 把带透明度的前景色叠到底色上（GDI 笔刷不带 alpha，预先合成）。
func blend(base, fg uint32, alpha uint32) uint32 {
	mix := func(b, f uint32) uint32 { return (b*(255-alpha) + f*alpha) / 255 }
	return colorref(mix(base&0xFF, fg&0xFF), mix((base>>8)&0xFF, (fg>>8)&0xFF), mix((base>>16)&0xFF, (fg>>16)&0xFF))
}

func darkTheme() dialogTheme {
	bg := colorref(27, 40, 56)
	return dialogTheme{
		bg: bg, border: colorref(44, 62, 82), head: colorref(255, 255, 255), subtle: colorref(143, 152, 160),
		accentFill: colorref(102, 192, 244), accentHover: colorref(142, 208, 248), onFill: colorref(16, 32, 46),
		dangerFill: colorref(231, 76, 60), dangerHover: colorref(192, 57, 43), onDanger: colorref(255, 255, 255),
		closeFG: colorref(154, 168, 181), closeFGHover: colorref(255, 255, 255),
		closeHoverBG: blend(bg, colorref(255, 255, 255), 32),
		logoFile:     "logo_dark.ico",
	}
}

func lightTheme() dialogTheme {
	bg := colorref(247, 250, 253)
	return dialogTheme{
		bg: bg, border: colorref(219, 228, 238), head: colorref(23, 32, 42), subtle: colorref(100, 119, 140),
		accentFill: colorref(124, 185, 226), accentHover: colorref(95, 168, 216), onFill: colorref(16, 32, 46),
		dangerFill: colorref(192, 57, 43), dangerHover: colorref(150, 40, 27), onDanger: colorref(255, 255, 255),
		closeFG: colorref(100, 119, 140), closeFGHover: colorref(23, 32, 42),
		closeHoverBG: blend(bg, colorref(23, 32, 42), 26),
		logoFile:     "logo_light.ico",
	}
}

type closeDialog struct {
	owner  uintptr
	hwnd   uintptr
	theme  dialogTheme
	logo   uintptr
	app    *app
	fontTitle  uintptr
	fontHead   uintptr
	fontBody   uintptr
	fontButton uintptr

	hover     int // 0 无 1 最小化 2 退出 3 关闭键
	animIn    bool
	animating bool
	step      int
	baseX     int32
	baseY     int32
	result    string
	done      bool
}

func curtain(on bool) string {
	value := "false"
	if on {
		value = "true"
	}
	return "(function(){if(!window.__hlxCloseCurtain)return false;window.__hlxCloseCurtain(" + value + ");return true;})()"
}

func animationsEnabled() bool {
	var enabled int32
	if call(procSystemParametersInfoW, spiGetClientAreaAnimation, 0, uintptr(unsafe.Pointer(&enabled)), 0) != 0 {
		return enabled != 0
	}
	return true
}

// askCloseIntent 关窗意图询问：同步模态；任何失败路径降级 minimize 不误杀进程。
func askCloseIntent(a *app) string {
	a.log.write("关窗意图询问开始")
	defer func() {
		if r := recover(); r != nil {
			a.log.write(fmt.Sprintf("关窗意图询问异常，按最小化处理：%v", r))
		}
	}()
	if a.chromium != nil {
		a.chromium.Eval(curtain(true))
	}
	intent := a.showCloseDialog()
	a.log.write("关窗意图询问结束：" + intent)
	if a.chromium != nil {
		a.chromium.Eval(curtain(false))
	}
	return intent
}

func (a *app) showCloseDialog() string {
	theme := darkTheme()
	if value, ok := a.theme.Load().(string); ok && value == "light" {
		theme = lightTheme()
	}
	dialog := &closeDialog{owner: a.hwnd, app: a, theme: theme}
	dialog.logo = a.loadDialogLogo(theme.logoFile)
	dialog.fontTitle = makeFont(15, true)
	dialog.fontHead = makeFont(17, true)
	dialog.fontBody = makeFont(14, false)
	dialog.fontButton = makeFont(15, true)
	activeDialog.Store(dialog)
	defer activeDialog.Store(nil)
	defer func() {
		if dialog.logo != 0 {
			call(procDeleteObject, dialog.logo)
		}
		for _, font := range []uintptr{dialog.fontTitle, dialog.fontHead, dialog.fontBody, dialog.fontButton} {
			if font != 0 {
				call(procDeleteObject, font)
			}
		}
	}()
	return dialog.runModal()
}

func (a *app) loadDialogLogo(name string) uintptr {
	candidates := []string{}
	if a.cfg.installRoot != "" {
		candidates = append(candidates,
			filePath(a.cfg.installRoot, "web", "public", "assets", name),
			filePath(a.cfg.installRoot, "web", "dist", "assets", name),
		)
	}
	candidates = append(candidates,
		filePath(exeDir(), "web", "dist", "assets", name),
		filePath(exeDir(), "assets", name),
	)
	for _, path := range candidates {
		icon := call(procLoadImageW, 0, utf16Ptr(path), imageIcon, 20, 20, lrLoadFromFile)
		if icon != 0 {
			return icon
		}
	}
	return a.loadAppIcon()
}

func makeFont(px int, bold bool) uintptr {
	weight := uintptr(fwNormal)
	if bold {
		weight = fwBold
	}
	return call(procCreateFontW,
		uintptr(-px), 0, 0, 0, weight, 0, 0, 0,
		0, 0, clipCharacterPrecision, 0, defaultQuality, utf16Ptr("Microsoft YaHei UI"))
}

// runModal 模态运行：停用属主窗口，自持消息泵直到拿到意图。
func (d *closeDialog) runModal() string {
	d.result = "minimize"
	registerDialogClass()
	exStyle := uintptr(wsExToolWindow | wsExLayered)
	d.hwnd = call(procCreateWindowExW, exStyle, utf16Ptr(dialogClassName), utf16Ptr("关闭 "+appBrandName()),
		uintptr(wsPopup), 0, 0, dialogWidth, dialogHeight, d.owner, 0, 0, 0)
	if d.hwnd == 0 {
		return "minimize"
	}
	defer call(procDestroyWindow, d.hwnd)
	call(procSetWindowLongPtrW, d.hwnd, gwlpUserdata, uintptr(unsafe.Pointer(d)))
	if region := call(procCreateRoundRectRgn, 0, 0, dialogWidth+1, dialogHeight+1, cornerRadius*2, cornerRadius*2); region != 0 {
		call(procSetWindowRgn, d.hwnd, region, 1)
	}
	d.positionOverOwner()
	call(procEnableWindow, d.owner, 0)
	defer call(procEnableWindow, d.owner, 1)
	call(procSetForegroundWindow, d.hwnd)
	call(procSetFocus, d.hwnd)

	animated := animationsEnabled()
	if animated {
		d.animIn = true
		call(procSetLayeredWindowAttributes, d.hwnd, 0, 0, lwaAlpha) // 起始全透明，避免实心首帧
	} else {
		call(procSetLayeredWindowAttributes, d.hwnd, 0, 255, lwaAlpha)
	}
	call(procShowWindow, d.hwnd, swShow)
	if animated {
		call(procSetTimer, d.hwnd, 1, animTickMS, 0)
	}

	var m msg
	for {
		r, _, _ := call3(procGetMessageW, uintptr(unsafe.Pointer(&m)), 0, 0, 0)
		if r == 0 {
			// WM_QUIT（属主销毁/托盘退出）：回投给主泵，按最小化收场
			call(procPostQuitMessage, m.WParam)
			break
		}
		call(procTranslateMessage, uintptr(unsafe.Pointer(&m)))
		call(procDispatchMessageW, uintptr(unsafe.Pointer(&m)))
		if d.done {
			break
		}
	}
	call(procKillTimer, d.hwnd, 1)
	return d.result
}

func (d *closeDialog) positionOverOwner() {
	var ownerRect rect
	if d.owner == 0 || call(procGetWindowRect, d.owner, uintptr(unsafe.Pointer(&ownerRect))) == 0 {
		call(procSetWindowPos, d.hwnd, 0, 0x8000, 0x8000, 0, 0, swpNoSize|swpNoZOrder)
	} else {
		x := ownerRect.Left + ((ownerRect.Right-ownerRect.Left)-dialogWidth)/2
		y := ownerRect.Top + ((ownerRect.Bottom-ownerRect.Top)-dialogHeight)/2
		call(procSetWindowPos, d.hwnd, 0, uintptr(x), uintptr(y), 0, 0, swpNoSize|swpNoZOrder)
	}
	var self rect
	if call(procGetWindowRect, d.hwnd, uintptr(unsafe.Pointer(&self))) != 0 {
		d.baseX, d.baseY = self.Left, self.Top
	}
}

func registerDialogClass() {
	if dialogClassRegistered {
		return
	}
	wc := wndClassEx{
		Size:       uint32(unsafe.Sizeof(wndClassEx{})),
		WndProc:    syscall.NewCallback(dialogProc),
		Instance:   activeApp.Load().instance,
		Background: call(procCreateSolidBrush, uintptr(darkTheme().bg)),
		ClassName:  utf16Ptr(dialogClassName),
	}
	call(procRegisterClassExW, uintptr(unsafe.Pointer(&wc)))
	dialogClassRegistered = true
}

func dialogProc(hwnd uintptr, message uint32, wParam uintptr, lParam uintptr) uintptr {
	d := activeDialog.Load()
	if d == nil || d.hwnd != hwnd {
		return call(procDefWindowProcW, hwnd, uintptr(message), wParam, lParam)
	}
	switch message {
	case wmNCHitTest:
		client := screenPointToClient(hwnd, lParam)
		inClose := client.X >= closeRectLeft && client.X <= closeRectLeft+closeRectSize &&
			client.Y >= closeRectTop && client.Y <= closeRectTop+closeRectSize
		if client.Y < 56 && !inClose {
			return htCaption // 顶部条系统拖动
		}
		return htClient
	case wmTimer:
		d.tickAnimation()
		return 0
	case wmPaint:
		d.paint()
		return 0
	case wmMouseMove:
		d.updateHover(int32(int16(lParam & 0xFFFF)), int32(int16((lParam>>16)&0xFFFF)))
		return 0
	case wmMouseLeave:
		d.setHover(0)
		return 0
	case wmLButtonUp:
		d.clickAt(int32(int16(lParam & 0xFFFF)), int32(int16((lParam>>16)&0xFFFF)))
		return 0
	case wmKeyDown:
		switch wParam {
		case vkReturn:
			d.choose("minimize") // 回车交给安全侧
		case vkEscape:
			d.choose("stay")
		}
		return 0
	case wmClose:
		d.choose("stay")
		return 0
	}
	return call(procDefWindowProcW, hwnd, uintptr(message), wParam, lParam)
}

func screenPointToClient(hwnd uintptr, lParam uintptr) point {
	screen := point{X: int32(int16(lParam & 0xFFFF)), Y: int32(int16((lParam >> 16) & 0xFFFF))}
	var windowRect rect
	call(procGetWindowRect, hwnd, uintptr(unsafe.Pointer(&windowRect)))
	return point{X: screen.X - windowRect.Left, Y: screen.Y - windowRect.Top}
}

func (d *closeDialog) hitTest(x, y int32) int {
	inRect := func(l, t, r, b int32) bool { return x >= l && x <= r && y >= t && y <= b }
	switch {
	case inRect(btnX0, btnY, btnX0+btnWidth, btnY+btnHeight):
		return 1
	case inRect(btnX0+btnWidth+btnGap, btnY, btnX0+2*btnWidth+btnGap, btnY+btnHeight):
		return 2
	case inRect(closeRectLeft, closeRectTop, closeRectLeft+closeRectSize, closeRectTop+closeRectSize):
		return 3
	}
	return 0
}

func (d *closeDialog) updateHover(x, y int32) {
	d.setHover(d.hitTest(x, y))
}

func (d *closeDialog) setHover(hover int) {
	if d.hover != hover {
		d.hover = hover
		call(procInvalidateRect, d.hwnd, 0, 0)
	}
}

func (d *closeDialog) clickAt(x, y int32) {
	switch d.hitTest(x, y) {
	case 1:
		d.choose("minimize")
	case 2:
		d.choose("quit")
	case 3:
		d.choose("stay")
	}
}

func (d *closeDialog) choose(result string) {
	if d.done || d.animating {
		return
	}
	d.result = result
	if d.animIn && animationsEnabled() {
		d.animating = true
		d.step = 0
		call(procSetTimer, d.hwnd, 1, animTickMS, 0)
		return
	}
	d.done = true
}

func (d *closeDialog) tickAnimation() {
	d.step++
	t := float32(d.step) / float32(animSteps)
	if t > 1 {
		t = 1
	}
	k := t * t * (3 - 2*t) // smoothstep，近似网页 --ease-inout
	var offset int32
	var alpha uintptr
	if d.animating { // 出场：下沉淡出
		offset = int32(float32(animRise) * k)
		alpha = uintptr(255 * (1 - k))
	} else { // 入场：自下方浮起淡入
		offset = int32(float32(animRise) * (1 - k))
		alpha = uintptr(255 * k)
	}
	call(procSetLayeredWindowAttributes, d.hwnd, 0, alpha, lwaAlpha)
	call(procSetWindowPos, d.hwnd, 0, uintptr(d.baseX), uintptr(d.baseY+offset), 0, 0,
		swpNoSize|swpNoZOrder|swpNoActivate)
	if d.step >= animSteps {
		call(procKillTimer, d.hwnd, 1)
		if d.animating {
			d.done = true
			return
		}
		d.animIn = false
		call(procSetLayeredWindowAttributes, d.hwnd, 0, 255, lwaAlpha)
		call(procSetWindowPos, d.hwnd, 0, uintptr(d.baseX), uintptr(d.baseY), 0, 0,
			swpNoSize|swpNoZOrder|swpNoActivate)
	}
}

func (d *closeDialog) paint() {
	var ps paintStruct
	hdc := call(procBeginPaint, d.hwnd, uintptr(unsafe.Pointer(&ps)))
	defer call(procEndPaint, d.hwnd, uintptr(unsafe.Pointer(&ps)))

	bgBrush := call(procCreateSolidBrush, uintptr(d.theme.bg))
	borderPen := call(procCreatePen, psSolid, 1, uintptr(d.theme.border))
	oldBrush := call(procSelectObject, hdc, bgBrush)
	oldPen := call(procSelectObject, hdc, borderPen)
	call(procRectangleGdi, hdc, 0, 0, dialogWidth, dialogHeight) // 底色 + 1px 描边
	call(procSelectObject, hdc, oldPen)
	call(procSelectObject, hdc, oldBrush)
	call(procDeleteObject, borderPen)
	call(procDeleteObject, bgBrush)

	call(procSetBkMode, hdc, 1) // TRANSPARENT

	// 标题栏：logo + 标题
	if d.logo != 0 {
		call(procDrawIconEx, hdc, 22, 16, d.logo, 20, 20, 0, 0, 0x3)
	}
	titleFont := call(procSelectObject, hdc, d.fontTitle)
	call(procSetTextColor, hdc, uintptr(d.theme.head))
	drawText(hdc, "关闭 "+appBrandName(), 52, 15, 372, 39, dtSingleLine|dtVCenter|dtNoprefix)
	call(procSelectObject, hdc, titleFont)

	// 正文
	headFont := call(procSelectObject, hdc, d.fontHead)
	call(procSetTextColor, hdc, uintptr(d.theme.head))
	drawText(hdc, "要如何关闭 "+appBrandName()+"？", 28, 74, 492, 102, dtSingleLine|dtVCenter|dtNoprefix)
	call(procSelectObject, hdc, d.fontBody)
	call(procSetTextColor, hdc, uintptr(d.theme.subtle))
	drawText(hdc, "最小化到托盘后，应用仍在后台继续更新数据。", 28, 110, 492, 132, dtSingleLine|dtVCenter|dtNoprefix)
	call(procSelectObject, hdc, headFont)

	// 关闭键：悬停圆底 + 双斜线
	closeHover := d.hover == 3
	if closeHover {
		brush := call(procCreateSolidBrush, uintptr(d.theme.closeHoverBG))
		oldBrush := call(procSelectObject, hdc, brush)
		oldStockPen := call(procSelectObject, hdc, call(procGetStockObject, 8)) // NULL_PEN
		call(procEllipse, hdc, 0, 0, closeRectSize, closeRectSize)
		call(procSelectObject, hdc, oldStockPen)
		call(procSelectObject, hdc, oldBrush)
		call(procDeleteObject, brush)
	}
	fg := d.theme.closeFG
	if closeHover {
		fg = d.theme.closeFGHover
	}
	pen := call(procCreatePen, psSolid, 2, uintptr(fg))
	xOldPen := call(procSelectObject, hdc, pen)
	drawLine(hdc, 13, 13, 23, 23)
	drawLine(hdc, 23, 13, 13, 23)
	call(procSelectObject, hdc, xOldPen)
	call(procDeleteObject, pen)

	// 双按钮：圆角纯色双色（两色分工动作性质）
	d.drawButton(hdc, btnX0, "最小化", d.hover == 1, d.theme.accentFill, d.theme.accentHover, d.theme.onFill)
	d.drawButton(hdc, btnX0+btnWidth+btnGap, "退出程序", d.hover == 2, d.theme.dangerFill, d.theme.dangerHover, d.theme.onDanger)
}

func (d *closeDialog) drawButton(hdc uintptr, x int32, text string, hovered bool, fill, hoverFill, onColor uint32) {
	color := fill
	if hovered {
		color = hoverFill
	}
	brush := call(procCreateSolidBrush, uintptr(color))
	oldBrush := call(procSelectObject, hdc, brush)
	oldPen := call(procSelectObject, hdc, call(procGetStockObject, 8))
	call(procRoundRect, hdc, uintptr(x), uintptr(btnY), uintptr(x+btnWidth), uintptr(btnY+btnHeight), 18, 18) // 圆角半径 9
	call(procSelectObject, hdc, oldPen)
	call(procSelectObject, hdc, oldBrush)
	call(procDeleteObject, brush)
	oldFont := call(procSelectObject, hdc, d.fontButton)
	call(procSetTextColor, hdc, uintptr(onColor))
	drawText(hdc, text, x, btnY, x+btnWidth, btnY+btnHeight, dtSingleLine|dtCenter|dtVCenter|dtNoprefix)
	call(procSelectObject, hdc, oldFont)
}

func drawText(hdc uintptr, text string, l, t, r, b int32, flags uintptr) {
	rc := rect{Left: l, Top: t, Right: r, Bottom: b}
	call(procDrawTextW, hdc, utf16Ptr(text), uintptr(len([]rune(text))), uintptr(unsafe.Pointer(&rc)), flags)
}

func drawLine(hdc uintptr, x1, y1, x2, y2 int32) {
	call(procMoveToEx, hdc, uintptr(x1), uintptr(y1), 0)
	call(procLineTo, hdc, uintptr(x2), uintptr(y2))
}

func copyUTF16(dst []uint16, text string) {
	encoded := syscall.StringToUTF16(text)
	n := len(encoded)
	if n > len(dst) {
		n = len(dst)
	}
	for i := 0; i < n; i++ {
		dst[i] = uint16(encoded[i])
	}
	if n > 0 {
		dst[n-1] = 0
	}
}
