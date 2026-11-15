//go:build windows

// WebView2 COM 事件接线。槽位与参数封装全部来自 com_generated.go（tools/comgen
// 从官方头机械产出，槽位不再手数）；本文件只剩与接口无关的固定形状面：
// 事件处理器 COM 对象（IUnknown+Invoke 标准形状）与通用出/入参助手。
// 约定：所有 self/raw 参数都是 COM 对象指针，对象首 8 字节即 vtable 地址。
package main

import (
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"

	"github.com/wailsapp/go-webview2/pkg/edge"
)

// comRaw COM 对象指针视图：首 8 字节即 vtable 地址。
type comRaw struct{ vtbl unsafe.Pointer }

// comProc COM 对象指针 + 槽位 → 方法入口。槽位来自生成常量（com_generated.go），
// 视图定界 4096 只是声明，实际访问的槽号永远落在真实 vtable 内。
func comProc(self unsafe.Pointer, slot int) uintptr {
	return (*[4096]uintptr)((*comRaw)(self).vtbl)[slot]
}

// comArgsString 读 LPWSTR* 出参字符串；返回串由系统堆分配，读后必须释放。
func comArgsString(self unsafe.Pointer, slot int) string {
	var value *uint16
	hr, _, _ := syscall.SyscallN(comProc(self, slot), uintptr(self), uintptr(unsafe.Pointer(&value)))
	if hr != 0 || value == nil {
		return ""
	}
	text := windows.UTF16PtrToString(value)
	windows.CoTaskMemFree(unsafe.Pointer(value))
	return text
}

// comArgsBool 读 BOOL* 出参。
func comArgsBool(self unsafe.Pointer, slot int) bool {
	var value int32
	syscall.SyscallN(comProc(self, slot), uintptr(self), uintptr(unsafe.Pointer(&value)))
	return value != 0
}

// comArgsUint32 读枚举出参；comArgsUint64 读 UINT64 出参。
func comArgsUint32(self unsafe.Pointer, slot int) uint32 {
	var value uint32
	syscall.SyscallN(comProc(self, slot), uintptr(self), uintptr(unsafe.Pointer(&value)))
	return value
}

func comArgsUint64(self unsafe.Pointer, slot int) uint64 {
	var value uint64
	syscall.SyscallN(comProc(self, slot), uintptr(self), uintptr(unsafe.Pointer(&value)))
	return value
}

// comArgsSetBool 调 [in] BOOL 属性写入口：参数按值传，不取地址
// （取地址等价把本地变量地址塞给按值槽位）。
func comArgsSetBool(self unsafe.Pointer, slot int, on bool) {
	var value int32
	if on {
		value = 1
	}
	syscall.SyscallN(comProc(self, slot), uintptr(self), uintptr(value))
}

// comRegister 注册事件处理器：(this, handler, token*)；HRESULT 非 0 报错。
func comRegister(self unsafe.Pointer, slot int, handler unsafe.Pointer) error {
	var token int64
	hr, _, _ := syscall.SyscallN(comProc(self, slot), uintptr(self), uintptr(handler), uintptr(unsafe.Pointer(&token)))
	if hr != 0 {
		return windows.Errno(hr)
	}
	return nil
}

// comEventHandler 事件处理器 COM 对象：WebView2 注册后只走 AddRef/Release/
// Invoke（无身份查询），QI 按契约回 E_NOINTERFACE 不填出参；生命周期由 Go 侧
// navEventHandlers 持引用，引用计数恒 1。
type comEventHandler struct {
	vtbl   *handlerVtbl
	onFire func(sender, args unsafe.Pointer)
}

type handlerVtbl struct {
	QueryInterface, AddRef, Release, Invoke uintptr
}

func handlerQueryInterface(this *comEventHandler, _, _ uintptr) uintptr { return 0x80004002 }
func handlerAddRef(this *comEventHandler) uintptr                       { return 1 }
func handlerRelease(this *comEventHandler) uintptr                      { return 1 }
func handlerInvoke(this *comEventHandler, sender, args unsafe.Pointer) uintptr {
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

func newEventHandler(onFire func(sender, args unsafe.Pointer)) *comEventHandler {
	return &comEventHandler{vtbl: &handlerFn, onFire: onFire}
}

// navEventHandlers 持有注册后的处理器，防 Go 侧回收（宿主生命周期内不移除）。
type navEventHandlers struct {
	navigationStarting *comEventHandler
	newWindow          *comEventHandler
}

func newNavEventHandlers(a *app, webview *edge.ICoreWebView2) *navEventHandlers {
	wv := newCoreWebView2(unsafe.Pointer(webview))
	handlers := &navEventHandlers{
		navigationStarting: newEventHandler(func(_, args unsafe.Pointer) {
			nav := &NavigationStartingArgs{raw: args}
			if !a.onNavigationStarting(nav.Uri()) {
				nav.SetCancel(true)
			}
		}),
		newWindow: newEventHandler(func(_, args unsafe.Pointer) {
			wn := &NewWindowArgs{raw: args}
			if uri := wn.Uri(); uri != "" && a.openExternal != nil {
				a.openExternal(uri)
			}
			wn.SetHandled(true)
		}),
	}
	if err := wv.AddNavigationStarting(unsafe.Pointer(handlers.navigationStarting)); err != nil {
		a.log.write("导航拦截事件注册失败：" + err.Error())
	}
	if err := wv.AddNewWindowRequested(unsafe.Pointer(handlers.newWindow)); err != nil {
		a.log.write("新窗口事件注册失败：" + err.Error())
	}
	return handlers
}
