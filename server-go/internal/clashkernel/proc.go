package clashkernel

// Windows 进程设施：Toolhelp32 快照枚举、命令行读取（PEB）、子进程拉起与三段停止。

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	ntdll                         = windows.NewLazySystemDLL("ntdll.dll")
	procNtQueryInformationProcess = ntdll.NewProc("NtQueryInformationProcess")
)

// ImageProcessPIDs Toolhelp32 快照枚举：映像名为 image_name 的全部进程号
// （忽略大小写）。快照失败返回空表（查询失败不阻断的保守语义）。
func ImageProcessPIDs(imageName string) []uint32 {
	snapshot, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return nil
	}
	defer windows.CloseHandle(snapshot)
	target := strings.ToLower(imageName)
	var pids []uint32
	entry := windows.ProcessEntry32{Size: uint32(unsafe.Sizeof(windows.ProcessEntry32{}))}
	err = windows.Process32First(snapshot, &entry)
	for err == nil {
		if strings.ToLower(windows.UTF16ToString(entry.ExeFile[:])) == target {
			pids = append(pids, entry.ProcessID)
		}
		err = windows.Process32Next(snapshot, &entry)
	}
	return pids
}

// KernelProcessAlive 内核进程存活探测：是否存在该映像名的进程（真孤儿兜底）。
func KernelProcessAlive(imageName string) bool {
	return len(ImageProcessPIDs(imageName)) > 0
}

// processCommandLine 读目标进程命令行（ntdll → PEB → RTL_USER_PROCESS_PARAMETERS）。
// 读不到（权限/退出/形态不符）返回空串——调用方按「看不清的不动它」保守处理。
func processCommandLine(pid uint32) string {
	handle, err := windows.OpenProcess(
		windows.PROCESS_QUERY_INFORMATION|windows.PROCESS_VM_READ, false, pid)
	if err != nil {
		return ""
	}
	defer windows.CloseHandle(handle)
	var pbi struct {
		Reserved1      uintptr
		PebBaseAddress uintptr
		Reserved2      [2]uintptr
		UniquePID      uintptr
		Reserved3      uintptr
	}
	returnLen := uint32(0)
	status, _, _ := procNtQueryInformationProcess.Call(
		uintptr(handle), 0, uintptr(unsafe.Pointer(&pbi)), uintptr(unsafe.Sizeof(pbi)),
		uintptr(unsafe.Pointer(&returnLen)),
	)
	if status != 0 || pbi.PebBaseAddress == 0 {
		return ""
	}
	readRemote := func(addr uintptr, size int) ([]byte, bool) {
		buf := make([]byte, size)
		var read uintptr
		err := windows.ReadProcessMemory(handle, addr, &buf[0], uintptr(size), &read)
		if err != nil || read != uintptr(size) {
			return nil, false
		}
		return buf, true
	}
	paramsPtrBytes, ok := readRemote(pbi.PebBaseAddress+0x20, 8)
	if !ok {
		return ""
	}
	params := *(*uintptr)(unsafe.Pointer(&paramsPtrBytes[0]))
	// x64 RTL_USER_PROCESS_PARAMETERS.CommandLine（UNICODE_STRING，偏移 0x70）
	cmdBytes, ok := readRemote(params+0x70, 16)
	if !ok {
		return ""
	}
	length := int(*(*uint16)(unsafe.Pointer(&cmdBytes[0])))
	buffer := *(*uintptr)(unsafe.Pointer(&cmdBytes[8]))
	if length <= 0 || buffer == 0 {
		return ""
	}
	raw, ok := readRemote(buffer, length)
	if !ok {
		return ""
	}
	return strings.TrimSpace(windows.UTF16ToString(rawToUint16(raw)))
}

func rawToUint16(raw []byte) []uint16 {
	out := make([]uint16, len(raw)/2)
	for i := range out {
		out[i] = uint16(raw[i*2]) | uint16(raw[i*2+1])<<8
	}
	return out
}

// KillOrphanKernels 清理同映像名 + 命令行含本配置路径的残留内核进程（服务被
// 强杀后 mihomo 不随父进程退出）。imageName 必须可参数化——测试用改名副本，
// 绝不匹配真内核映像名去试杀。只杀看得清命令行且确属本配置的实例，不动用户
// 自启的 Clash；返回清理数。
func KillOrphanKernels(imageName, configPath string) (int, error) {
	return KillOrphanKernelsExclude(imageName, configPath, 0)
}

// KillOrphanKernelsExclude 同上，另排除 excludePid（本 runtime 现役进程）。
func KillOrphanKernelsExclude(imageName, configPath string, excludePid uint32) (int, error) {
	if !isWindows() {
		return 0, nil
	}
	killed := 0
	wantConfig := strings.ToLower(configPath)
	for _, pid := range ImageProcessPIDs(imageName) {
		if pid == excludePid || pid == uint32(os.Getpid()) {
			continue
		}
		if configPath != "" {
			cmdline := strings.ToLower(processCommandLine(pid))
			if cmdline == "" || !strings.Contains(cmdline, wantConfig) {
				continue
			}
		}
		if err := terminateProcess(pid); err != nil {
			continue
		}
		killed++
	}
	return killed, nil
}

func terminateProcess(pid uint32) error {
	handle, err := windows.OpenProcess(windows.PROCESS_TERMINATE|windows.SYNCHRONIZE, false, pid)
	if err != nil {
		return fmt.Errorf("孤儿进程打开失败：%w", err)
	}
	defer windows.CloseHandle(handle)
	if err := windows.TerminateProcess(handle, 1); err != nil {
		return fmt.Errorf("孤儿进程终止失败：%w", err)
	}
	return nil
}

// KernelProcess 长驻内核子进程句柄。
type KernelProcess struct {
	cmd     *exec.Cmd
	process *os.Process
	pid     int
	done    chan struct{}
	waitErr error
	once    sync.Once
}

// Spawn 拉起长驻内核进程；日志追加到 logPath（内核对绑定失败只打日志不退出，
// 没有这份日志那些失败不可见）。句柄随子进程存活，父进程不长期持有。
func Spawn(exePath string, args []string, logPath string) (*KernelProcess, error) {
	cmd := exec.Command(exePath, args...)
	cmd.Env = KernelEnv(os.Environ())
	if logPath != "" {
		logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err != nil {
			return nil, fmt.Errorf("内核日志文件打开失败：%w", err)
		}
		cmd.Stdout = logFile
		cmd.Stderr = logFile
		defer logFile.Close()
	}
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("内核进程拉起失败：%w", err)
	}
	proc := &KernelProcess{
		cmd:     cmd,
		process: cmd.Process,
		pid:     cmd.Process.Pid,
		done:    make(chan struct{}),
	}
	go func() {
		proc.waitErr = cmd.Wait()
		close(proc.done)
	}()
	return proc, nil
}

// Pid 子进程号。
func (p *KernelProcess) Pid() int {
	if p == nil {
		return 0
	}
	return p.pid
}

// Running 进程是否仍在运行。
func (p *KernelProcess) Running() bool {
	if p == nil {
		return false
	}
	select {
	case <-p.done:
		return false
	default:
		return true
	}
}

// WaitExit 阻塞等子进程退出。
func (p *KernelProcess) WaitExit() error {
	if p == nil {
		return nil
	}
	<-p.done
	return p.waitErr
}

// Stop 三段停止：terminate → wait 5s → kill → wait 5s（Windows 两侧同为
// TerminateProcess，第二段兜底回收已无实质差异，保持结构与超时上限）。
func (p *KernelProcess) Stop() {
	if p == nil || !p.Running() {
		return
	}
	_ = p.process.Kill()
	if p.waitTimeout(5 * time.Second) {
		return
	}
	_ = p.process.Kill()
	p.waitTimeout(5 * time.Second)
}

func (p *KernelProcess) waitTimeout(d time.Duration) bool {
	select {
	case <-p.done:
		return true
	case <-time.After(d):
		return false
	}
}
