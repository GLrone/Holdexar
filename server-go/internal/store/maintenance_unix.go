//go:build unix

package store

import (
	"errors"
	"math"
	"syscall"
)

func processProvenDead(pid int) bool {
	return pid <= math.MaxInt32 && errors.Is(syscall.Kill(pid, 0), syscall.ESRCH)
}
