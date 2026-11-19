//go:build windows

// 约定：self/raw 均为 COM 对象指针，首指针字为 vtable 地址。
package main

import (
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"

	"github.com/wailsapp/go-webview2/pkg/edge"
)

type comRaw struct{ vtbl unsafe.Pointer }

// comProc 按 COM 对象首字段 vtable + 槽号取方法入口（4096 仅作数组视图上界）。
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

func comArgsBool(self unsafe.Pointer, slot int) bool {
	var value int32
	syscall.SyscallN(comProc(self, slot), uintptr(self), uintptr(unsafe.Pointer(&value)))
	return value != 0
}

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

// comArgsSetBool 写 [in] BOOL 属性：按值传递，不可传指针。
func comArgsSetBool(self unsafe.Pointer, slot int, on bool) {
	var value int32
	if on {
		value = 1
	}
	syscall.SyscallN(comProc(self, slot), uintptr(self), uintptr(value))
}

func comRegister(self unsafe.Pointer, slot int, handler unsafe.Pointer) error {
	var token int64
	hr, _, _ := syscall.SyscallN(comProc(self, slot), uintptr(self), uintptr(handler), uintptr(unsafe.Pointer(&token)))
	if hr != 0 {
		return windows.Errno(hr)
	}
	return nil
}

// comEventHandler QI 返回 E_NOINTERFACE；生命周期由 Go 侧 navEventHandlers 持引用，AddRef/Release 恒返回 1。
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
