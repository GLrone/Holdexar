//go:build windows

// swapmutex_test.go 换装互斥探测行为：不存在/空闲=不进行中、被持有=进行中、
// 更新事务上下文豁免（对齐桌面壳 _swap_mutex_held 三分支）。
package main

import (
	"fmt"
	"os"
	"runtime"
	"sync"
	"syscall"
	"testing"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	procCreateMutexWT = windows.NewLazySystemDLL("kernel32.dll").NewProc("CreateMutexW")
)

func uniqueMutexName(t *testing.T) string {
	t.Helper()
	return fmt.Sprintf("Holdexar-update-swap-test-%d-%s", os.Getpid(), t.Name())
}

// holdMutex 在锁定线程上创建并（可选）持有互斥体，返回释放函数。
func holdMutex(t *testing.T, name string, acquire bool) (release func()) {
	t.Helper()
	acquired := make(chan error, 1)
	stop := make(chan struct{})
	done := make(chan struct{})
	go func() {
		runtime.LockOSThread()
		defer runtime.UnlockOSThread()
		namePtr, _ := syscall.UTF16PtrFromString(name)
		handle, _, _ := call3(procCreateMutexWT, 0, 0, uintptr(unsafe.Pointer(namePtr)))
		if handle == 0 {
			acquired <- fmt.Errorf("CreateMutexW 失败")
			close(done)
			return
		}
		if acquire {
			status, _, _ := call3(procWaitForSingleObject, handle, 0)
			if status != 0 && status != waitAbandoned {
				call(procCloseHandle, handle)
				acquired <- fmt.Errorf("持锁失败 status=%x", status)
				close(done)
				return
			}
		}
		acquired <- nil
		<-stop
		if acquire {
			call(procReleaseMutex, handle)
		}
		call(procCloseHandle, handle)
		close(done)
	}()
	if err := <-acquired; err != nil {
		t.Fatalf("测试夹具：%v", err)
	}
	var once sync.Once
	return func() {
		once.Do(func() {
			close(stop)
			<-done
		})
	}
}

func TestProbeSwapMutexAbsent(t *testing.T) {
	if probeSwapMutexHeld(uniqueMutexName(t)) {
		t.Fatal("互斥体不存在却判为换装进行中")
	}
}

func TestProbeSwapMutexHeldByOther(t *testing.T) {
	name := uniqueMutexName(t)
	release := holdMutex(t, name, true)
	defer release()
	if !probeSwapMutexHeld(name) {
		t.Fatal("互斥体被他人持有却判为未进行中")
	}
	release()
	if probeSwapMutexHeld(name) {
		t.Fatal("释放后仍判为换装进行中")
	}
}

func TestProbeSwapMutexFreeObject(t *testing.T) {
	name := uniqueMutexName(t)
	// 仅创建不占有：互斥体对象在（句柄未关）但空闲——探测不得误判且必须放回探测占有
	release := holdMutex(t, name, false)
	defer release()
	if probeSwapMutexHeld(name) {
		t.Fatal("空闲互斥体被误判为换装进行中")
	}
	// 探测两次：第一次的探测占有必须已被放回，否则第二次会误判
	if probeSwapMutexHeld(name) {
		t.Fatal("探测后未释放占有，第二次误判为进行中")
	}
}

func TestSwapMutexHeldExemptProbeContext(t *testing.T) {
	name := uniqueMutexName(t)
	release := holdMutex(t, name, true)
	defer release()
	t.Setenv(envPrefix+"UPDATE_ATTEMPT_ID", "probe-attempt")
	if swapMutexHeldNamed(name) {
		t.Fatal("验收探针上下文应豁免让位判定")
	}
	t.Setenv(envPrefix+"UPDATE_ATTEMPT_ID", "")
	if !swapMutexHeldNamed(name) {
		t.Fatal("无豁免上下文时被持有互斥应判进行中")
	}
	t.Setenv(envPrefix+"UPDATE_DATA_TOKEN", "tx-token")
	if swapMutexHeldNamed(name) {
		t.Fatal("数据令牌上下文应豁免让位判定")
	}
}
