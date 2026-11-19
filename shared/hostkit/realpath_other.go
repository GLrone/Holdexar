//go:build !windows

package hostkit

import "path/filepath"

// FinalPath resolves symlinks and returns an absolute clean path.
func FinalPath(path string) (string, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	return filepath.EvalSymlinks(abs)
}
