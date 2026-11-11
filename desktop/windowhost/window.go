//go:build windows

package main

import (
	"fmt"
	"net"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"

	"github.com/wailsapp/go-webview2/pkg/edge"
)

// 窗口内部消息：跨 goroutine 的动作全部编组到 UI 线程执行。
const (
	wmAppTray          = 0x8000 + 1
	wmAppNavigate      = 0x8000 + 2
	wmAppShow          = 0x8000 + 3
	wmAppServerDied    = 0x8000 + 4
	wmAppQuit          = 0x8000 + 5
	wmAppWebFailed     = 0x8000 + 6
	wmAppServerRestart = 0x8000 + 7

	trayCmdShow = 1
	trayCmdQuit = 2

	defaultWindowWidth  = 1280
	defaultWindowHeight = 860
	minWindowWidth      = 960
	minWindowHeight     = 600
)

var activeApp atomic.Pointer[app]

type app struct {
	cfg      *options
	log      *hostLog
	srv      *serverProcess
	hwnd     uintptr
	instance syscall.Handle
	exitCode int
	chromium *edge.Chromium
	webview  *edge.ICoreWebView2

	navHandlers *navEventHandlers

	trayMu    sync.Mutex
	trayAdded bool
	trayIcon  uintptr

	quitting       bool
	restartPending bool
	background     atomic.Bool
	webDead        atomic.Bool

	openExternal func(string) // 外链出口；测试注入收集器

	theme atomic.Value // "dark" / "light"

	navMu       sync.Mutex
	navOrigin   string // 唯一允许的实例本机源
	appURL      string
	currentPath string // 最近一次同源导航路径+查询+片段（开发热更回跳用）
	pendingNav  string
}

func newApp(cfg *options, log *hostLog, srv *serverProcess) *app {
	a := &app{cfg: cfg, log: log, srv: srv, openExternal: shellOpen}
	a.theme.Store("dark")
	return a
}

func setBackgroundMode(on bool) {
	if a := activeApp.Load(); a != nil {
		a.background.Store(on)
	}
}

func requestForeground() {
	if a := activeApp.Load(); a != nil && a.hwnd != 0 {
		call(procPostMessageW, a.hwnd, wmAppShow, 0, 0)
	}
}

func (a *app) run() int {
	activeApp.Store(a)
	defer activeApp.Store(nil)

	if !a.createWindow() {
		return 1
	}
	a.setupWebview()
	if a.cfg.background {
		// 后台常驻：先不露窗，等单实例唤醒或托盘唤起
		call(procShowWindow, a.hwnd, swHide)
	} else {
		call(procShowWindow, a.hwnd, swShow)
		_ = a.chromium.Show() // 父窗显示后控制器可见性须显式置位
		focusHwnd(a.hwnd)
	}
	a.addTray()

	// 健康等待与窗口初始化并行：窗口先呈现本地等待页
	go a.bootstrapSession()
	go a.watchServerProcess(a.srv)

	var m msg
	for {
		r, _, _ := call3(procGetMessageW, uintptr(unsafe.Pointer(&m)), 0, 0, 0)
		if r == 0 {
			break
		}
		call(procTranslateMessage, uintptr(unsafe.Pointer(&m)))
		call(procDispatchMessageW, uintptr(unsafe.Pointer(&m)))
	}
	return a.exitCode
}

func (a *app) createWindow() bool {
	kernelGetModuleHandle := modKernel32.NewProc("GetModuleHandleW")
	a.instance = syscall.Handle(call(kernelGetModuleHandle, 0))
	wc := wndClassEx{
		Size:       uint32(unsafe.Sizeof(wndClassEx{})),
		WndProc:    syscall.NewCallback(windowProc),
		Instance:   a.instance,
		Background: call(procCreateSolidBrush, uintptr(rgb(0x1b, 0x28, 0x38))),
		ClassName:  utf16Ptr(windowClassName),
	}
	if icon := a.loadAppIcon(); icon != 0 {
		wc.Icon = icon
		wc.IconSmall = icon
	}
	if call(procRegisterClassExW, uintptr(unsafe.Pointer(&wc))) == 0 {
		a.log.write("窗口类注册失败")
		fatalMessage("窗口初始化失败：窗口类注册被拒绝。")
		return false
	}
	const cwUseDefault = 0x80000000
	a.hwnd = call(procCreateWindowExW, 0, utf16Ptr(windowClassName), utf16Ptr(a.cfg.windowTitle),
		wsOverlappedWindow|wsMinimizeBox, cwUseDefault, cwUseDefault, defaultWindowWidth, defaultWindowHeight,
		0, 0, uintptr(a.instance), 0)
	if a.hwnd == 0 {
		a.log.write("主窗创建失败")
		fatalMessage("窗口初始化失败：主窗创建被拒绝。")
		return false
	}
	return true
}

func (a *app) loadAppIcon() uintptr {
	paths := []string{
		filePath(exeDir(), "app.ico"),
		filePath(exeDir(), "_internal", "app.ico"),
	}
	if a.cfg.installRoot != "" {
		paths = append(paths,
			filePath(a.cfg.installRoot, "desktop", "app.ico"),
			filePath(a.cfg.installRoot, "app.ico"),
		)
	}
	for _, path := range paths {
		icon := call(procLoadImageW, 0, utf16Ptr(path), imageIcon, 0, 0, lrLoadFromFile|lrDefaultSize)
		if icon != 0 {
			return icon
		}
	}
	var iconIndex uint16
	icon, _, _ := call3(procExtractAssociatedIconW, 0, utf16Ptr(exePath()), uintptr(unsafe.Pointer(&iconIndex)))
	return icon
}

func filePath(parts ...string) string {
	return strings.Join(parts, `\`)
}

// setupWebview 嵌入 WebView2 并导航到本地等待页；失败给可行动的桌面提示。
func (a *app) setupWebview() {
	chromium := edge.NewChromium()
	chromium.SetErrorCallback(func(err error) {
		a.log.write("WebView2 组件错误：" + err.Error())
	})
	chromium.DataPath = webviewUserDataDir(a.cfg)
	chromium.AdditionalBrowserArgs = []string{"--disable-component-update", "--disable-background-networking"}
	chromium.MessageCallback = a.onWebMessage
	chromium.ProcessFailedCallback = a.onProcessFailed
	if !chromium.Embed(a.hwnd) {
		a.log.write("WebView2 嵌入失败")
		fatalMessage("渲染组件（WebView2 Runtime）缺失或初始化失败。\n请安装微软 WebView2 Runtime 后重新启动：\nhttps://go.microsoft.com/fwlink/p/?LinkId=2124703")
		a.exitCode = 1
		call(procPostQuitMessage, 1)
		return
	}
	a.chromium = chromium
	chromium.SetBackgroundColour(0x1b, 0x28, 0x38, 0xff)
	chromium.Resize() // 窗口建好后无 WM_SIZE：控制器边界须显式铺满客户区
	if controller := chromium.GetController(); controller != nil {
		if webview, err := controller.GetCoreWebView2(); err == nil {
			a.webview = webview
		}
	}
	chromium.Init(bridgeBootstrapScript)
	if a.webview == nil {
		a.log.write("WebView2 控制器创建失败")
		fatalMessage("渲染组件初始化失败，请重新启动应用。")
		a.exitCode = 1
		call(procPostQuitMessage, 1)
		return
	}
	a.navHandlers = newNavEventHandlers(a, a.webview)
	chromium.NavigateToString(splashHTML(a.cfg.appName))
}

func webviewUserDataDir(cfg *options) string {
	if custom := strings.TrimSpace(os.Getenv("WEBVIEW2_USER_DATA_FOLDER")); custom != "" {
		return custom
	}
	return filePath(cfg.dataDir, "cache", "webview")
}

func (a *app) onWebMessage(message string, _ *edge.ICoreWebView2, _ *edge.ICoreWebView2WebMessageReceivedEventArgs) {
	decoded, ok := parseJSONMap(message)
	if !ok {
		return
	}
	if method, ok := decoded["method"].(string); ok && method != "" {
		a.dispatchBridgeCall(method, decoded)
		return
	}
	if theme, ok := decoded["theme"].(string); ok {
		if theme == "dark" || theme == "light" {
			a.theme.Store(theme)
		}
	}
}

func (a *app) dispatchBridgeCall(method string, payload map[string]any) {
	id, _ := payload["id"].(string)
	response := func(body string) {
		if a.webview != nil {
			_ = a.webview.PostWebMessageAsString(body)
		}
	}
	switch method {
	case "restart_app":
		response(bridgeResult(id, map[string]any{"ok": true}))
		go a.performRestart()
	default:
		response(bridgeError(id, -32601, "未知桌面能力："+method))
	}
}

// performRestart 更新换装重启：先回收本实例服务，再拉起新进程交棒。
func (a *app) performRestart() {
	a.restartPending = true
	self := exePath()
	a.srv.stop(a.log)
	pid, err := execDetached(self, append(os.Environ(),
		envPrefix+"UPDATE_RESTART=1",
		envPrefix+"PREV_PID="+strconv.Itoa(os.Getpid()),
	))
	if err != nil {
		a.log.write("重启进程拉起失败：" + err.Error())
	} else {
		a.log.write(fmt.Sprintf("重启已交接：新进程 PID %d", pid))
	}
	a.performQuit()
}

func execDetached(exe string, env []string) (int, error) {
	cmd := exec.Command(exe)
	cmd.Env = env
	cmd.Dir = exeDir()
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: 0x00000008 | 0x00000200, // DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
	}
	if err := cmd.Start(); err != nil {
		return 0, err
	}
	pid := cmd.Process.Pid
	go func() { _ = cmd.Process.Release() }()
	return pid, nil
}

func (a *app) onProcessFailed(_ *edge.ICoreWebView2, args *edge.ICoreWebView2ProcessFailedEventArgs) {
	kind := processFailedKind(args)
	if kind != 0 /* 浏览器进程退出 */ && kind != 2 /* 主帧渲染无响应 */ {
		return // 其余失败类型由 WebView2 自动恢复，不提示
	}
	call(procPostMessageW, a.hwnd, wmAppWebFailed, uintptr(kind), 0)
}

func (a *app) handleWebFailed(kind uintptr) {
	if a.webDead.Swap(true) {
		return
	}
	a.chromium.Eval("window.__hlxCloseCurtain && window.__hlxCloseCurtain(true)")
	choice := call(procMessageBoxW, a.hwnd,
		utf16Ptr("页面组件异常退出。重试会重新加载当前页面；取消将退出程序。"),
		utf16Ptr(a.cfg.appName), mbRetryCancel|mbIconError)
	a.chromium.Eval("window.__hlxCloseCurtain && window.__hlxCloseCurtain(false)")
	a.webDead.Store(false)
	if choice == idRetry {
		a.chromium.Navigate(a.currentAppURL())
		return
	}
	a.performQuit()
}

// bootstrapSession 等健康身份→读一次性票据→导航进应用；超时仅提示不杀进程。
func (a *app) bootstrapSession() {
	payload := a.waitHealthyIdentified()
	if payload == nil {
		a.log.write("健康检查超时（90s）")
		hint := ""
		if probeHealth(a.cfg.serverPort, time.Second) == nil && !portFree(a.cfg.serverPort) {
			hint = "\n\n[提示] 服务进程在监听，但健康探测被拦截——疑似代理/加速器类软件劫持了本机回环流量。关闭其系统代理/TUN 后重试，或重启应用。"
		}
		infoMessage(0, fmt.Sprintf("服务启动失败（90s 内健康检查未通过）。%s\n\n详细错误见数据目录 logs 日志。", hint))
		return
	}
	a.log.write(fmt.Sprintf("本地服务就绪 │ PID=%d，数据目录=%s，端口=%d", payload.PID, payload.DataDir, a.cfg.serverPort))
	nonce := readTicket(ticketPath(a.cfg), 3*time.Second)
	if nonce == "" {
		a.log.write("引导票据未就绪，无法进入应用")
		return
	}
	a.setAppURL(buildAppURL(a.cfg))
	a.postNav(buildEntryURL(a.appURL, nonce, ""))
	if a.cfg.devWatch {
		go a.watchServerReload(nonce)
	}
}

func (a *app) waitHealthyIdentified() *healthSnapshot {
	deadline := time.Now().Add(healthTimeout)
	for time.Now().Before(deadline) {
		if payload := healthIsOurs(a.cfg.serverPort, a.cfg, 1500*time.Millisecond); payload != nil {
			return payload
		}
		time.Sleep(250 * time.Millisecond)
	}
	return nil
}

func (a *app) setAppURL(url string) {
	a.navMu.Lock()
	a.appURL = url
	a.navOrigin = urlOrigin(url)
	a.navMu.Unlock()
}

func (a *app) currentAppURL() string {
	a.navMu.Lock()
	defer a.navMu.Unlock()
	return a.appURL
}

func (a *app) watchServerProcess(srv *serverProcess) {
	code, ok := srv.waitExit()
	if !ok || a.quitting || a.restartPending {
		return
	}
	if code == restartExitCode {
		// 数据库恢复换库：重启服务并按当前路由重载
		call(procPostMessageW, a.hwnd, wmAppServerRestart, 0, 0)
		return
	}
	call(procPostMessageW, a.hwnd, wmAppServerDied, uintptr(code), 0)
}

// handleServerRestart 恢复后的服务重启：同参拉起新进程，健康就绪后凭
// 新引导票据按当前路由重载（会话随重启失效，必须重取）。
func (a *app) handleServerRestart() {
	a.log.write("本地服务请求重启（数据库恢复），正在重启")
	next, err := a.srv.restart(a.log)
	if err != nil {
		a.log.write("本地服务重启失败：" + err.Error())
		a.handleServerDied(0)
		return
	}
	a.srv = next
	go a.watchServerProcess(next)
	go a.rebindSession()
}

// rebindSession 健康就绪后取新票据，按当前路由重进应用。
func (a *app) rebindSession() {
	payload := a.waitHealthyIdentified()
	if payload == nil {
		a.log.write("重启后健康检查超时")
		infoMessage(0, "本地服务重启后未就绪（90s 内健康检查未通过）。详细错误见数据目录 logs 日志。")
		return
	}
	a.log.write(fmt.Sprintf("本地服务重启就绪 │ PID=%d，数据目录=%s，端口=%d", payload.PID, payload.DataDir, a.cfg.serverPort))
	nonce := readTicket(ticketPath(a.cfg), 3*time.Second)
	if nonce == "" {
		a.log.write("重启后引导票据未就绪")
		return
	}
	a.postNav(buildEntryURL(a.appURL, nonce, a.reloadNextPath()))
}

func (a *app) handleServerDied(code uintptr) {
	a.log.write(fmt.Sprintf("本地服务进程已退出（exit=%d），应用即将关闭", code))
	fatalMessage(fmt.Sprintf("本地服务进程已退出（exit=%d），应用即将关闭。\n详细错误见数据目录 logs 日志。", code))
	a.performQuit()
}

// watchServerReload 开发热更：服务重建后新票据出现即按当前路由重进应用。
func (a *app) watchServerReload(seenNonce string) {
	ticket := ticketPath(a.cfg)
	for a.srv.alive() && !a.quitting {
		time.Sleep(300 * time.Millisecond)
		nonce, err := readTicketOnce(ticket)
		if err != nil || nonce == "" || nonce == seenNonce {
			continue
		}
		if healthIsOurs(a.cfg.serverPort, a.cfg, 2*time.Second) == nil {
			continue
		}
		entry := buildEntryURL(a.appURL, nonce, a.reloadNextPath())
		a.log.write("检测到服务热更新，重新载入应用")
		a.postNav(entry)
		seenNonce = nonce
	}
}

func (a *app) reloadNextPath() string {
	if a.webview == nil {
		return ""
	}
	source, err := a.webview.GetSource()
	if err != nil {
		return ""
	}
	path := urlPathWithQuery(source)
	if path == "" || path == "/" {
		return ""
	}
	return path
}

func (a *app) postNav(url string) {
	a.navMu.Lock()
	a.pendingNav = url
	a.navMu.Unlock()
	call(procPostMessageW, a.hwnd, wmAppNavigate, 0, 0)
}

func (a *app) takePendingNav() string {
	a.navMu.Lock()
	defer a.navMu.Unlock()
	url := a.pendingNav
	a.pendingNav = ""
	return url
}

func (a *app) navigateTo(url string) {
	if url == "" || a.chromium == nil {
		return
	}
	a.log.write("窗口导航：" + urlRedacted(url))
	a.chromium.Navigate(url)
}

func (a *app) addTray() {
	a.trayMu.Lock()
	defer a.trayMu.Unlock()
	if a.trayAdded {
		return
	}
	icon := a.loadAppIcon()
	if icon == 0 {
		a.log.write("托盘图标不可用，托盘未创建")
		return
	}
	data := notifyIconData{
		Size:      uint32(unsafe.Sizeof(notifyIconData{})),
		HWnd:      a.hwnd,
		UID:       1,
		UFlags:    nifMessage | nifIcon | nifTip,
		UCallback: wmAppTray,
		HIcon:     icon,
	}
	copyUTF16(data.Tip[:], a.cfg.appName)
	if call(procShellNotifyIconW, nimAdd, uintptr(unsafe.Pointer(&data))) == 0 {
		a.log.write("托盘创建失败")
		return
	}
	a.trayAdded = true
	a.trayIcon = icon
}

func (a *app) removeTray() {
	a.trayMu.Lock()
	defer a.trayMu.Unlock()
	if !a.trayAdded {
		return
	}
	data := notifyIconData{Size: uint32(unsafe.Sizeof(notifyIconData{})), HWnd: a.hwnd, UID: 1}
	call(procShellNotifyIconW, nimDelete, uintptr(unsafe.Pointer(&data)))
	if a.trayIcon != 0 {
		call(procDeleteObject, a.trayIcon)
		a.trayIcon = 0
	}
	a.trayAdded = false
}

// trayMenu 托盘菜单：显示主窗口 / 退出；返回 0 表示未选择。
func (a *app) trayMenu() int {
	call(procSetForegroundWindow, a.hwnd)
	menu := call(procCreatePopupMenu)
	if menu == 0 {
		return 0
	}
	defer call(procDestroyMenu, menu)
	call(procAppendMenuW, menu, 0, trayCmdShow, utf16Ptr("显示主窗口"))
	call(procAppendMenuW, menu, 0x800, 0, 0) // MF_SEPARATOR
	call(procAppendMenuW, menu, 0, trayCmdQuit, utf16Ptr("退出"))
	var cursor point
	call(procGetCursorPos, uintptr(unsafe.Pointer(&cursor)))
	cmd := call(procTrackPopupMenuEx, menu, tpmRightButton|tpmReturnCmd, uintptr(cursor.X), uintptr(cursor.Y), a.hwnd, 0)
	return int(cmd)
}

func (a *app) hasTray() bool {
	a.trayMu.Lock()
	defer a.trayMu.Unlock()
	return a.trayAdded
}

// performQuit 退出链：先收窗撤托盘，等本地服务与其代理子进程回收、端口释放。
func (a *app) performQuit() {
	if a.quitting {
		return
	}
	a.quitting = true
	a.log.write("开始退出：回收本地服务")
	call(procShowWindow, a.hwnd, swHide)
	a.removeTray()
	if !a.restartPending {
		a.srv.stop(a.log)
		if waitPortFree(a.cfg.serverPort, 5*time.Second) {
			a.log.write("本地服务已退出，端口已释放")
		} else {
			a.log.write("本地服务退出但端口尚未释放")
		}
	}
	a.log.write("退出完成")
	if a.hwnd != 0 {
		call(procDestroyWindow, a.hwnd)
	}
}

func waitPortFree(port int, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for {
		conn, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)), 300*time.Millisecond)
		if err != nil {
			return true
		}
		_ = conn.Close()
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(200 * time.Millisecond)
	}
}

func (a *app) handleClose() {
	if a.quitting {
		call(procDestroyWindow, a.hwnd)
		return
	}
	if a.background.Load() {
		call(procShowWindow, a.hwnd, swHide)
		return
	}
	if activeDialog.Load() != nil {
		return // 意图弹窗进行中：忽略重复关窗请求
	}
	intent := askCloseIntent(a)
	switch intent {
	case "quit":
		call(procShowWindow, a.hwnd, swHide)
		a.performQuit()
	case "stay":
		// 原地不动
	default: // minimize
		if !a.hasTray() {
			// 托盘不可用时不能隐藏：任务栏仍要点得回来
			call(procShowWindow, a.hwnd, swMinimize)
			return
		}
		call(procShowWindow, a.hwnd, swHide)
	}
}

// windowProc 主窗过程；lParam 以裸指针形参承载（系统消息既有指针也有整型，
// Windows 用户态地址与 Go 堆区间互斥，按指针槽扫描无碰撞）。
func windowProc(hwnd uintptr, message uint32, wParam uintptr, lParam unsafe.Pointer) uintptr {
	switch message {
	case wmGetMinMaxInfo:
		info := (*minMaxInfo)(lParam)
		info.MinTrackX = minWindowWidth
		info.MinTrackY = minWindowHeight
		return 0
	}
	a := activeApp.Load()
	if a == nil {
		return call(procDefWindowProcW, hwnd, uintptr(message), wParam, uintptr(lParam))
	}
	switch message {
	case wmAppTray:
		switch uintptr(lParam) & 0xFFFF {
		case 0x0203, 0x0205: // 左双击 / 右键抬起
			if cmd := a.trayMenu(); cmd == trayCmdShow {
				focusHwnd(a.hwnd)
			} else if cmd == trayCmdQuit {
				a.performQuit()
			}
		}
		return 0
	case wmAppNavigate:
		a.navigateTo(a.takePendingNav())
		return 0
	case wmAppShow:
		focusHwnd(a.hwnd)
		if a.chromium != nil {
			_ = a.chromium.Show() // 唤醒路径同样要显式点亮控制器
		}
		return 0
	case wmAppServerDied:
		a.handleServerDied(wParam)
		return 0
	case wmAppServerRestart:
		a.handleServerRestart()
		return 0
	case wmAppQuit:
		a.performQuit()
		return 0
	case wmAppWebFailed:
		a.handleWebFailed(wParam)
		return 0
	case wmClose:
		a.handleClose()
		return 0
	case wmDestroy:
		a.removeTray()
		call(procPostQuitMessage, 0)
		return 0
	case wmSize:
		if a.chromium != nil && wParam != 1 { // SIZE_MINIMIZED 之外都同步布局
			a.chromium.Resize()
		}
		return 0
	case wmEraseBkgnd:
		return 1
	}
	return call(procDefWindowProcW, hwnd, uintptr(message), wParam, uintptr(lParam))
}
