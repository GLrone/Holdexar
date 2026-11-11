//go:build windows

// WebView2 COM 事件接线：绑定未暴露 NavigationStarting / NewWindowRequested
// 的高层封装，这里按官方接口槽位自建事件处理器与参数读取，槽位顺序对齐
// WebView2 IDL（接口一经发布不再变动）。
package main

import (
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"

	"github.com/wailsapp/go-webview2/pkg/edge"
)

// comRaw 任意 COM 对象的裸头：首字即 vtable 指针。
type comRaw struct {
	vtbl unsafe.Pointer
}

// wv2Vtbl 镜像 ICoreWebView2 vtable（首槽 IUnknown，随后官方接口顺序）。
type wv2Vtbl struct {
	QueryInterface, AddRef, Release                    uintptr
	GetSettings, GetSource, Navigate, NavigateToString uintptr
	AddNavigationStarting, RemoveNavigationStarting    uintptr
	AddContentLoading, RemoveContentLoading            uintptr
	AddSourceChanged, RemoveSourceChanged              uintptr
	AddHistoryChanged, RemoveHistoryChanged            uintptr
	AddNavigationCompleted, RemoveNavigationCompleted  uintptr
	AddFrameNavigationStarting, RemoveFrameNavigationStarting   uintptr
	AddFrameNavigationCompleted, RemoveFrameNavigationCompleted uintptr
	AddScriptDialogOpening, RemoveScriptDialogOpening  uintptr
	AddPermissionRequested, RemovePermissionRequested  uintptr
	AddProcessFailed, RemoveProcessFailed              uintptr
	AddScriptToExecuteOnDocumentCreated, RemoveScriptToExecuteOnDocumentCreated uintptr
	ExecuteScript, CapturePreview                      uintptr
	Reload, PostWebMessageAsJSON, PostWebMessageAsString uintptr
	AddWebMessageReceived, RemoveWebMessageReceived    uintptr
	CallDevToolsProtocolMethod                         uintptr
	GetBrowserProcessID, GetCanGoBack, GetCanGoForward uintptr
	GoBack, GoForward                                  uintptr
	GetDevToolsProtocolEventReceiver, Stop             uintptr
	AddNewWindowRequested, RemoveNewWindowRequested    uintptr
	AddDocumentTitleChanged, RemoveDocumentTitleChanged uintptr
	GetDocumentTitle                                   uintptr
	AddHostObjectToScript, RemoveHostObjectFromScript  uintptr
	OpenDevToolsWindow                                 uintptr
	AddContainsFullScreenElementChanged, RemoveContainsFullScreenElementChanged uintptr
	GetContainsFullScreenElement                       uintptr
	AddWebResourceRequested, RemoveWebResourceRequested uintptr
	AddWebResourceRequestedFilter, RemoveWebResourceRequestedFilter uintptr
	AddWindowCloseRequested, RemoveWindowCloseRequested uintptr
}

// comEventHandler 事件处理器 COM 对象：WebView2 直接经 vtable 调 Invoke，
// 不做接口查询（与绑定内置事件同一事实）。
type comEventHandler struct {
	vtbl   *handlerVtbl
	onFire func(sender, args *comRaw)
}

type handlerVtbl struct {
	QueryInterface, AddRef, Release, Invoke uintptr
}

func handlerQueryInterface(this *comEventHandler, _, _ uintptr) uintptr { return 0 }
func handlerAddRef(this *comEventHandler) uintptr                       { return 1 }
func handlerRelease(this *comEventHandler) uintptr                      { return 1 }
func handlerInvoke(this *comEventHandler, sender, args *comRaw) uintptr {
	if this.onFire != nil {
		this.onFire(sender, args)
	}
	return 0
}

var handlerFn = handlerVtbl{
	QueryInterface: syscall.NewCallback(handlerQueryInterface),
	AddRef:         syscall.NewCallback(handlerAddRef),
	Release:        syscall.NewCallback(handlerRelease),
	Invoke:         syscall.NewCallback(handlerInvoke),
}

func newEventHandler(onFire func(sender, args *comRaw)) *comEventHandler {
	return &comEventHandler{vtbl: &handlerFn, onFire: onFire}
}

// navEventHandlers 持有注册后的处理器与令牌，防 Go 侧回收。
type navEventHandlers struct {
	navigationStarting *comEventHandler
	newWindow          *comEventHandler
	token1, token2     int64
}

func newNavEventHandlers(a *app, webview *edge.ICoreWebView2) *navEventHandlers {
	vtbl := (*wv2Vtbl)((*comRaw)(unsafe.Pointer(webview)).vtbl)
	handlers := &navEventHandlers{
		navigationStarting: newEventHandler(func(_, args *comRaw) {
			uri := comArgsString(args, 3)
			if !a.onNavigationStarting(uri) {
				comArgsSetFlag(args, 6, 1) // PutCancel：槽 3 GetUri 后第 4 槽
			}
		}),
		newWindow: newEventHandler(func(_, args *comRaw) {
			if uri := comArgsString(args, 3); uri != "" && a.openExternal != nil {
				a.openExternal(uri)
			}
			comArgsSetFlag(args, 4, 1) // PutHandled：GetUri 后第 2 槽
		}),
	}
	webviewPtr := uintptr(unsafe.Pointer(webview))
	if vtbl.AddNavigationStarting != 0 {
		syscall.SyscallN(vtbl.AddNavigationStarting,
			webviewPtr,
			uintptr(unsafe.Pointer(handlers.navigationStarting)),
			uintptr(unsafe.Pointer(&handlers.token1)))
	}
	if vtbl.AddNewWindowRequested != 0 {
		syscall.SyscallN(vtbl.AddNewWindowRequested,
			webviewPtr,
			uintptr(unsafe.Pointer(handlers.newWindow)),
			uintptr(unsafe.Pointer(&handlers.token2)))
	}
	return handlers
}

// comArgsString 读事件参数字符串（槽位以 IUnknown 之后计）；
// 返回字符串由系统堆分配，读后必须释放。
func comArgsString(args *comRaw, slot int) string {
	if args == nil || args.vtbl == nil {
		return ""
	}
	vtbl := (*[8]uintptr)(args.vtbl)
	var value *uint16
	hr, _, _ := syscall.SyscallN(vtbl[slot], uintptr(unsafe.Pointer(args)), uintptr(unsafe.Pointer(&value)))
	if hr != 0 || value == nil {
		return ""
	}
	text := windows.UTF16PtrToString(value)
	windows.CoTaskMemFree(unsafe.Pointer(value))
	return text
}

// comArgsSetFlag 置事件参数布尔位（PutCancel / PutHandled）。
func comArgsSetFlag(args *comRaw, slot int, value int32) {
	if args == nil || args.vtbl == nil {
		return
	}
	vtbl := (*[8]uintptr)(args.vtbl)
	syscall.SyscallN(vtbl[slot], uintptr(unsafe.Pointer(args)), uintptr(unsafe.Pointer(&value)))
}

// processFailedKind 读失败类型（GetProcessFailedKind，槽位 3）。
func processFailedKind(args *edge.ICoreWebView2ProcessFailedEventArgs) uint32 {
	if args == nil {
		return ^uint32(0)
	}
	raw := (*comRaw)(unsafe.Pointer(args))
	if raw.vtbl == nil {
		return ^uint32(0)
	}
	vtbl := (*[4]uintptr)(raw.vtbl)
	var kind uint32
	hr, _, _ := syscall.SyscallN(vtbl[3], uintptr(unsafe.Pointer(args)), uintptr(unsafe.Pointer(&kind)))
	if hr != 0 {
		return ^uint32(0)
	}
	return kind
}
