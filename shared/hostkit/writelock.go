package hostkit

import (
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// ProcessWriteLockPath computes the cross-process write lock path in os.TempDir() matching store.WriteGate byte-for-byte.
func ProcessWriteLockPath(dbPath string) string {
	abs, err := filepath.Abs(dbPath)
	if err != nil {
		abs = dbPath
	}
	if resolved, err := filepath.EvalSymlinks(abs); err == nil {
		abs = resolved
	}
	identity := CanonicalPath(abs)
	if runtime.GOOS == "windows" {
		identity = strings.ToLower(filepath.FromSlash(filepath.ToSlash(identity)))
	}
	digest := sha256.Sum256([]byte(identity))
	return filepath.Join(os.TempDir(), fmt.Sprintf("holdexar-write-%x.lock", digest[:12]))
}
