package clashkernel

import (
	"fmt"
	"net"
	"strconv"
	"strings"
	"time"
)

func PortListening(port int) bool {
	conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 300*time.Millisecond)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}

func procTCPTableOwnsLoopbackPort(content string, port int, inodes map[string]bool) bool {
	for i, line := range strings.Split(content, "\n") {
		if i == 0 {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) < 10 || fields[3] != "0A" || !inodes[fields[9]] {
			continue
		}
		addrHex, portHex, ok := strings.Cut(fields[1], ":")
		if !ok {
			continue
		}
		localPort, err := strconv.ParseUint(portHex, 16, 16)
		if err != nil || int(localPort) != port {
			continue
		}
		if isProcLoopbackHex(addrHex) {
			return true
		}
	}
	return false
}

func isProcLoopbackHex(addrHex string) bool {
	switch strings.ToUpper(addrHex) {
	case "0100007F", "7F000001",
		"00000000000000000000000001000000",
		"00000000000000000000000000000001",
		"0000000000000000FFFF00000100007F",
		"00000000000000000000FFFF7F000001":
		return true
	default:
		return false
	}
}
