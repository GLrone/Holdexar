package clashkernel

// 端口事实核验：connect 探测（在听）与 iphlpapi GetExtendedTcpTable（属主进程）。

import (
	"encoding/binary"
	"fmt"
	"net"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	iphlpapi                = windows.NewLazySystemDLL("iphlpapi.dll")
	procGetExtendedTcpTable = iphlpapi.NewProc("GetExtendedTcpTable")
)

// TCP_TABLE_OWNER_PID_LISTENER：仅监听套接字的属主表。
const tcpTableOwnerPIDListener = 3

const errorInsufficientBuffer = 122

// PortListening 端口有进程在听（connect 探测，0.3s 超时）。
func PortListening(port int) bool {
	conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 300*time.Millisecond)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}

// mibTcpRowOwnerPID 布局：dwState/dwLocalAddr/dwLocalPort/dwRemoteAddr/
// dwRemotePort/dwOwningPid，共 6 个 DWORD（x64 下无衬垫）。
const mibTcpRowSize = 24

// PortOwnedByProcess 监听端口是否确由该进程持有（只认回环监听）。
func PortOwnedByProcess(port int, pid uint32) bool {
	if !isWindows() || pid == 0 || port <= 0 {
		return false
	}
	var size uint32
	rc, _, _ := procGetExtendedTcpTable.Call(
		0, uintptr(unsafe.Pointer(&size)), 0, 2, tcpTableOwnerPIDListener, 0)
	if rc != 0 && rc != errorInsufficientBuffer {
		return false
	}
	if size == 0 {
		return false
	}
	buf := make([]byte, size)
	rc, _, _ = procGetExtendedTcpTable.Call(
		uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&size)),
		0, 2, tcpTableOwnerPIDListener, 0)
	if rc != 0 || len(buf) < 4 {
		return false
	}
	count := binary.LittleEndian.Uint32(buf)
	for i := uint32(0); i < count; i++ {
		off := 4 + int(i)*mibTcpRowSize
		if off+mibTcpRowSize > len(buf) {
			break
		}
		localAddr := buf[off+4 : off+8]
		// dwLocalPort 按网络字节序存放（高字节在前，低 16 位有效）
		localPort := int(binary.BigEndian.Uint16(buf[off+8 : off+10]))
		owner := binary.LittleEndian.Uint32(buf[off+20 : off+24])
		if localPort != port || owner != pid {
			continue
		}
		if localAddr[0] == 127 && localAddr[1] == 0 && localAddr[2] == 0 && localAddr[3] == 1 {
			return true
		}
	}
	return false
}
