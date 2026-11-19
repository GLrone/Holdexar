//go:build windows

package clashkernel

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	ntdll                         = windows.NewLazySystemDLL("ntdll.dll")
	procNtQueryInformationProcess = ntdll.NewProc("NtQueryInformationProcess")
)

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

func cmdlineMatchesConfig(cmdline, configPath string) bool {
	return cmdline != "" && strings.Contains(strings.ToLower(cmdline), strings.ToLower(configPath))
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
	_, _ = windows.WaitForSingleObject(handle, 2000)
	return nil
}

func configureSpawnCmd(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: windows.CREATE_NO_WINDOW,
	}
}

func attachProcessGuard(proc *os.Process) func() {
	if proc == nil || proc.Pid <= 0 {
		return nil
	}
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil || job == 0 {
		return nil
	}
	var info windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
	info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	if _, err := windows.SetInformationJobObject(
		job,
		windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)),
		uint32(unsafe.Sizeof(info)),
	); err != nil {
		_ = windows.CloseHandle(job)
		return nil
	}
	ph, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(proc.Pid))
	if err != nil {
		_ = windows.CloseHandle(job)
		return nil
	}
	assignErr := windows.AssignProcessToJobObject(job, ph)
	_ = windows.CloseHandle(ph)
	if assignErr != nil {
		_ = windows.CloseHandle(job)
		return nil
	}
	var once sync.Once
	return func() {
		once.Do(func() {
			_ = windows.TerminateJobObject(job, 1)
			_ = windows.CloseHandle(job)
		})
	}
}

func stopKernelProcess(p *KernelProcess) {
	if p.releaseGuard != nil {
		p.releaseGuard()
	}
	if p.process != nil {
		_ = p.process.Kill()
	}
	if p.waitTimeout(5 * time.Second) {
		return
	}
	if p.process != nil {
		_ = p.process.Kill()
	}
	p.waitTimeout(5 * time.Second)
}
