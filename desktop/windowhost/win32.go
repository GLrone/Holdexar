//go:build windows

package main

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
	"os/exec"
	"strconv"
	"strings"
	"crypto/rand"
	"encoding/hex"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	modUser32   = windows.NewLazySystemDLL("user32.dll")
	modShell32  = windows.NewLazySystemDLL("shell32.dll")
	modKernel32 = windows.NewLazySystemDLL("kernel32.dll")
	modGdi32    = windows.NewLazySystemDLL("gdi32.dll")

	procSetProcessDpiAwarenessContext = modUser32.NewProc("SetProcessDpiAwarenessContext")
	procRegisterClassExW              = modUser32.NewProc("RegisterClassExW")
	procCreateWindowExW               = modUser32.NewProc("CreateWindowExW")
	procDefWindowProcW                = modUser32.NewProc("DefWindowProcW")
	procGetMessageW                   = modUser32.NewProc("GetMessageW")
	procTranslateMessage              = modUser32.NewProc("TranslateMessage")
	procDispatchMessageW              = modUser32.NewProc("DispatchMessageW")
	procPostQuitMessage               = modUser32.NewProc("PostQuitMessage")
	procPostMessageW                  = modUser32.NewProc("PostMessageW")
	procShowWindow                    = modUser32.NewProc("ShowWindow")
	procSetForegroundWindow           = modUser32.NewProc("SetForegroundWindow")
	procGetForegroundWindow           = modUser32.NewProc("GetForegroundWindow")
	procIsIconic                      = modUser32.NewProc("IsIconic")
	procIsWindowVisible               = modUser32.NewProc("IsWindowVisible")
	procFindWindowW                   = modUser32.NewProc("FindWindowW")
	procGetClientRect                 = modUser32.NewProc("GetClientRect")
	procDestroyWindow                 = modUser32.NewProc("DestroyWindow")
	procEnableWindow                  = modUser32.NewProc("EnableWindow")
	procMessageBoxW                   = modUser32.NewProc("MessageBoxW")
	procLoadImageW                    = modUser32.NewProc("LoadImageW")
	procSetWindowPos                  = modUser32.NewProc("SetWindowPos")
	procGetWindowRect                 = modUser32.NewProc("GetWindowRect")
	procMonitorFromWindow             = modUser32.NewProc("MonitorFromWindow")
	procGetMonitorInfoW               = modUser32.NewProc("GetMonitorInfoW")
	procSystemParametersInfoW         = modUser32.NewProc("SystemParametersInfoW")
	procTrackPopupMenuEx              = modUser32.NewProc("TrackPopupMenuEx")
	procCreatePopupMenu               = modUser32.NewProc("CreatePopupMenu")
	procAppendMenuW                   = modUser32.NewProc("AppendMenuW")
	procDestroyMenu                   = modUser32.NewProc("DestroyMenu")
	procGetCursorPos                  = modUser32.NewProc("GetCursorPos")
	procSetLayeredWindowAttributes    = modUser32.NewProc("SetLayeredWindowAttributes")
	procSetWindowLongPtrW             = modUser32.NewProc("SetWindowLongPtrW")
	procGetWindowLongPtrW             = modUser32.NewProc("GetWindowLongPtrW")
	procSetWindowRgn                  = modUser32.NewProc("SetWindowRgn")
	procGetDC                         = modUser32.NewProc("GetDC")
	procReleaseDC                     = modUser32.NewProc("ReleaseDC")
	procSetTimer                      = modUser32.NewProc("SetTimer")
	procKillTimer                     = modUser32.NewProc("KillTimer")
	procRegisterWindowMessageW        = modUser32.NewProc("RegisterWindowMessageW")
	procSetWindowTextW                = modUser32.NewProc("SetWindowTextW")
	procGetWindowThreadProcessId      = modUser32.NewProc("GetWindowThreadProcessId")

	procShellExecuteW        = modShell32.NewProc("ShellExecuteW")
	procShellNotifyIconW     = modShell32.NewProc("Shell_NotifyIconW")
	procExtractAssociatedIconW = modShell32.NewProc("ExtractAssociatedIconW")

	procOpenProcess            = modKernel32.NewProc("OpenProcess")
	procWaitForSingleObject    = modKernel32.NewProc("WaitForSingleObject")
	procGetExitCodeProcess     = modKernel32.NewProc("GetExitCodeProcess")
	procTerminateProcess       = modKernel32.NewProc("TerminateProcess")
	procCloseHandle            = modKernel32.NewProc("CloseHandle")
	procCreateFileW            = modKernel32.NewProc("CreateFileW")
	procGetFinalPathNameByHandleW = modKernel32.NewProc("GetFinalPathNameByHandleW")

	procCreateRoundRectRgn = modGdi32.NewProc("CreateRoundRectRgn")
	procCreateSolidBrush   = modGdi32.NewProc("CreateSolidBrush")
	procCreatePen          = modGdi32.NewProc("CreatePen")
	procCreateFontW        = modGdi32.NewProc("CreateFontW")
	procSelectObject       = modGdi32.NewProc("SelectObject")
	procDeleteObject       = modGdi32.NewProc("DeleteObject")
	procSetBkMode          = modGdi32.NewProc("SetBkMode")
	procSetTextColor       = modGdi32.NewProc("SetTextColor")
	procFillRect           = modUser32.NewProc("FillRect")
	procFrameRect          = modGdi32.NewProc("FrameRect")
	procBeginPaint         = modUser32.NewProc("BeginPaint")
	procEndPaint           = modUser32.NewProc("EndPaint")
	procDrawIconEx         = modUser32.NewProc("DrawIconEx")
	procInvalidateRect     = modUser32.NewProc("InvalidateRect")
	procRectangleGdi       = modGdi32.NewProc("Rectangle")
	procEllipse            = modGdi32.NewProc("Ellipse")
	procRoundRect          = modGdi32.NewProc("RoundRect")
	procMoveToEx           = modGdi32.NewProc("MoveToEx")
	procLineTo             = modGdi32.NewProc("LineTo")
	procGetStockObject     = modGdi32.NewProc("GetStockObject")

	procSetFocus = modUser32.NewProc("SetFocus")

	procDrawTextW = modUser32.NewProc("DrawTextW")
)

// Win32 常量（仅本工程用到的子集）。
const (
	// 窗口样式
	wsOverlappedWindow = 0x00CF0000 | 0x00080000 // WS_OVERLAPPEDWINDOW|WS_CLIPCHILDREN
	wsPopup            = 0x80000000
	wsVisible          = 0x10000000
	wsMinimizeBox      = 0x00020000
	wsExLayered        = 0x00080000
	wsExToolWindow     = 0x00000080
	wsExAppWindow      = 0x00040000

	// 消息
	wmDestroy       = 0x0002
	wmSize          = 0x0005
	wmClose         = 0x0010
	wmQuit          = 0x0012
	wmPaint         = 0x000F
	wmEraseBkgnd    = 0x0014
	wmGetMinMaxInfo = 0x0024
	wmCommand       = 0x0111
	wmTimer         = 0x0113
	wmKeyDown       = 0x0100
	wmMouseMove     = 0x0200
	wmLButtonDown   = 0x0201
	wmLButtonUp     = 0x0202
	wmNCMove        = 0x00A1
	htCaption       = 0x0002
	htClient        = 0x0001

	// ShowWindow
	swHide     = 0
	swShow     = 5
	swMinimize = 6
	swRestore  = 9

	// LoadImage
	imageIcon       = 1
	lrLoadFromFile  = 0x00000010
	lrDefaultSize   = 0x00000040

	// Shell_NotifyIcon
	nimAdd    = 0x0
	nimModify = 0x1
	nimDelete = 0x2
	nifMessage = 0x1
	nifIcon    = 0x2
	nifTip     = 0x4

	// TrackPopupMenu
	tpmRightButton = 0x0002
	tpmReturnCmd   = 0x0100

	// MessageBox
	mbIconError       = 0x10
	mbIconInformation = 0x40
	mbYesNo           = 0x4
	mbRetryCancel     = 0x5
	idYes   = 6
	idRetry = 4

	monitorDefaultToNearest = 0x2

	spiGetClientAreaAnimation = 0x1042

	colorWindow = 5
	csHredraw   = 0x0002
	csVredraw   = 0x0001

	// 分层窗口 / SetWindowPos
	lwaAlpha      = 0x2
	swpNoSize     = 0x1
	swpNoZOrder   = 0x4
	swpNoActivate = 0x10

	// GDI
	psSolid  = 0
	fwBold   = 700
	fwNormal = 400
	clipCharacterPrecision = 1 // GDI 象形裁切：仅保留完整字形
	defaultQuality         = 0
	dtSingleLine = 0x1
	dtCenter     = 0x1
	dtVCenter    = 0x4
	dtEndEllipsis = 0x8000
	dtNoprefix    = 0x800
)

type msg struct {
	HWnd     uintptr
	Message  uint32
	WParam   uintptr
	LParam   uintptr
	Time     uint32
	Pt       point
	LPrivate uint32
}

type point struct {
	X, Y int32
}

type rect struct {
	Left, Top, Right, Bottom int32
}

type minMaxInfo struct {
	Reserved1, Reserved2, MaxX, MaxY, MaxTrackX, MaxTrackY, MinTrackX, MinTrackY int32
}

type wndClassEx struct {
	Size       uint32
	Style      uint32
	WndProc    uintptr
	ClsExtra   int32
	WndExtra   int32
	Instance   syscall.Handle
	Icon       uintptr
	Cursor     uintptr
	Background uintptr
	MenuName   uintptr
	ClassName  uintptr
	IconSmall  uintptr
}

type notifyIconData struct {
	Size        uint32
	HWnd        uintptr
	UID         uint32
	UFlags      uint32
	UCallback   uint32
	HIcon       uintptr
	Tip         [128]uint16
	State       uint32
	StateMask   uint32
	Info        [256]uint16
	Union       uintptr
	InfoTip     [64]uint16
	InfoFlags   uint32
	ReservedGUID [16]byte
	BalloonIcon uintptr
}

type monitorInfo struct {
	Size         uint32
	Monitor      rect
	Work         rect
	Flags        uint32
}

type paintStruct struct {
	HDC         uintptr
	FErase      int32
	RCPaint     rect
	Restore     uintptr
	FIncUpdate  int32
	Reserved    [32]byte
}

func utf16Ptr(s string) uintptr {
	ptr, err := syscall.UTF16PtrFromString(s)
	if err != nil {
		return 0
	}
	return uintptr(unsafe.Pointer(ptr))
}

func call(proc *windows.LazyProc, args ...uintptr) uintptr {
	value, _, _ := proc.Call(args...)
	return value
}

func setDPIAware() error {
	const perMonitorV2 = ^uintptr(3) // DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
	if err := procSetProcessDpiAwarenessContext.Find(); err != nil {
		return err
	}
	call(procSetProcessDpiAwarenessContext, perMonitorV2)
	return nil
}

func portFree(port int) bool {
	listener, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
	if err != nil {
		return false
	}
	_ = listener.Close()
	return true
}

// sameDirPath 目录一致性：真实路径解析后大小写不敏感比较，解析失败退回字面比较。
func sameDirPath(first, second string) bool {
	resolvedFirst, err1 := finalPathName(first)
	resolvedSecond, err2 := finalPathName(second)
	if err1 == nil && err2 == nil {
		return strings.EqualFold(resolvedFirst, resolvedSecond)
	}
	return strings.EqualFold(first, second)
}

// finalPathName 解析符号链接与 NTFS junction 的真实路径（对齐 Path.resolve）。
func finalPathName(path string) (string, error) {
	ptr, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return "", err
	}
	handle, _, callErr := call3(procCreateFileW,
		uintptr(unsafe.Pointer(ptr)),
		0,
		syscall.FILE_SHARE_READ|syscall.FILE_SHARE_WRITE|syscall.FILE_SHARE_DELETE,
		0, syscall.OPEN_EXISTING, syscall.FILE_FLAG_BACKUP_SEMANTICS, 0)
	if handle == uintptr(syscall.InvalidHandle) || handle == 0 {
		return "", callErr
	}
	defer call(procCloseHandle, handle)
	buf := make([]uint16, 1024)
	n, _, callErr := call3(procGetFinalPathNameByHandleW, handle, uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)), 0)
	if n == 0 || n > uintptr(len(buf)) {
		if n > uintptr(len(buf)) {
			buf = make([]uint16, n)
			n, _, callErr = call3(procGetFinalPathNameByHandleW, handle, uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)), 0)
			if n == 0 {
				return "", callErr
			}
		} else {
			return "", callErr
		}
	}
	resolved := syscall.UTF16ToString(buf[:n])
	resolved = strings.TrimPrefix(resolved, `\\?\`)
	if strings.HasPrefix(resolved, `UNC\`) {
		resolved = `\\` + strings.TrimPrefix(resolved, `UNC\`)
	}
	return resolved, nil
}

// call3 允许返回错误值（CreateFileW 失败返回 INVALID_HANDLE_VALUE 而非 0）。
func call3(proc *windows.LazyProc, args ...uintptr) (uintptr, uintptr, error) {
	r1, r2, err := proc.Call(args...)
	return r1, r2, err
}

func shellOpen(target string) {
	call(procShellExecuteW, 0, utf16Ptr("open"), utf16Ptr(target), 0, 0, wsShowNormalShell)
}

const wsShowNormalShell = 1 // SW_SHOWNORMAL

func exePath() string {
	exe, err := os.Executable()
	if err != nil {
		return "."
	}
	return exe
}

// rgb COLORREF（0x00BBGGRR）。
func rgb(r, g, b uint32) uint32 { return r | g<<8 | b<<16 }

func fatalMessage(message string) {
	fmt.Fprintln(os.Stderr, message)
	if os.Getenv("HOLDEXAR_NO_DIALOG") == "1" || os.Getenv("CI") == "1" {
		return
	}
	call(procMessageBoxW, 0, utf16Ptr(message), utf16Ptr(appBrandName()+" 启动失败"), mbIconError)
}

func infoMessage(owner uintptr, message string) {
	call(procMessageBoxW, owner, utf16Ptr(message), utf16Ptr(appBrandName()), mbIconInformation)
}

func appBrandName() string {
	if name := strings.TrimSpace(os.Getenv(envPrefix + "APP_NAME")); name != "" {
		return name
	}
	return defaultAppName
}

// waitPIDExit 等待进程退出；仅确认退出才返回 true，超时或权限不足均失败。
func waitPIDExit(pid int, timeout time.Duration) bool {
	handle := openProcessHandle(pid)
	if handle == 0 {
		return false
	}
	defer call(procCloseHandle, handle)
	status, _, _ := call3(procWaitForSingleObject, handle, uintptr(timeout.Milliseconds()))
	return status == 0
}

func pidExitCode(pid int, timeout time.Duration) (int, bool) {
	handle := openProcessHandle(pid)
	if handle == 0 {
		return 0, false
	}
	defer call(procCloseHandle, handle)
	call(procWaitForSingleObject, handle, uintptr(timeout.Milliseconds()))
	var code uint32
	if call(procGetExitCodeProcess, handle, uintptr(unsafe.Pointer(&code))) == 0 {
		return 0, false
	}
	return int(code), true
}

func openProcessHandle(pid int) uintptr {
	const synchronize = 0x00100000
	handle, _, _ := call3(procOpenProcess, synchronize, 0, uintptr(pid))
	return handle
}

func terminateProcess(handle uintptr) {
	call(procTerminateProcess, handle, 1)
}

// focusWindowByTitle 置前已运行实例窗口：还原最小化/隐藏态并抢前台。
func focusWindowByTitle(title string) bool {
	hwnd := call(procFindWindowW, 0, utf16Ptr(title))
	if hwnd == 0 {
		return false
	}
	focusHwnd(hwnd)
	return true
}

func focusHwnd(hwnd uintptr) {
	if call(procIsIconic, hwnd) != 0 || call(procIsWindowVisible, hwnd) == 0 {
		call(procShowWindow, hwnd, swRestore)
	}
	if call(procGetForegroundWindow) != hwnd {
		// 前台锁限制：模拟 Alt 键释放输入焦点后再抢前台
		keybdAlt()
		call(procSetForegroundWindow, hwnd)
	}
}

var procKeybdEvent = modUser32.NewProc("keybd_event")

func keybdAlt() {
	const vkMenu, keyUp = 0x12, 0x2
	call(procKeybdEvent, vkMenu, 0, 0, 0)
	call(procKeybdEvent, vkMenu, 0, keyUp, 0)
}

// supportsDataProtocol 能力探针：--update-protocol 应答 updateDataProtocol=1。
func supportsDataProtocol(exe string) bool {
	result := filepath.Join(os.TempDir(), "holdexar-update-protocol-"+randomHex(6)+".json")
	defer os.Remove(result)
	cmd := exec.Command(exe, "--update-protocol", "--help")
	cmd.Env = append(os.Environ(), envPrefix+"UPDATE_PROTOCOL_RESULT="+result)
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x08000000}
	if err := cmd.Start(); err != nil {
		return false
	}
	done := make(chan struct{})
	go func() { _ = cmd.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(25 * time.Second):
		_ = cmd.Process.Kill()
		return false
	}
	raw, err := os.ReadFile(result)
	if err != nil {
		return false
	}
	return strings.Contains(string(raw), `"updateDataProtocol":1`)
}

func randomHex(bytes int) string {
	buf := make([]byte, bytes)
	_, _ = rand.Read(buf)
	return hex.EncodeToString(buf)
}
