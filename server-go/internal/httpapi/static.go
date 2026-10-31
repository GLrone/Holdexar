package httpapi

import (
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
)

// handleStatic SPA 静态托管：/assets 直出文件，其余非 API 路径回退 index.html；
// 构建产物缺失时按「仅 API」运行，全部路径回 404。
func (s *Server) handleStatic(w http.ResponseWriter, r *http.Request) {
	index := filepath.Join(s.DistDir, "index.html")
	if s.DistDir == "" || !fileExists(index) {
		writeJSON(w, http.StatusNotFound, detailNull())
		return
	}
	// Clean 以 "/" 为根：结果不含 ".." 段；反斜杠伪段与符号链接由 insideDir 兜住
	rel := strings.TrimPrefix(path.Clean("/"+r.URL.Path), "/")
	target := filepath.Join(s.DistDir, filepath.FromSlash(rel))
	if s.assetsReady() && (rel == "assets" || strings.HasPrefix(rel, "assets/")) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			w.Header().Set("Allow", "GET, HEAD")
			writeJSON(w, http.StatusMethodNotAllowed, detailNull())
			return
		}
		if !insideDir(target, s.DistDir) {
			writeJSON(w, http.StatusNotFound, detailNull())
			return
		}
		s.serveFile(w, r, target)
		return
	}
	if rel != "" && fileExists(target) && insideDir(target, s.DistDir) {
		s.serveFile(w, r, target)
		return
	}
	s.serveFile(w, r, index)
}

// fileExists 普通文件判定（跟随符号链接）。
func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular()
}

// assetsReady /assets 静态挂载条件：dist/assets 目录存在。
func (s *Server) assetsReady() bool {
	info, err := os.Stat(filepath.Join(s.DistDir, "assets"))
	return err == nil && info.IsDir()
}

// serveFile 普通文件直出；缺失或目录回 404。
func (s *Server) serveFile(w http.ResponseWriter, r *http.Request, target string) {
	f, err := os.Open(target)
	if err != nil {
		writeJSON(w, http.StatusNotFound, detailNull())
		return
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() {
		writeJSON(w, http.StatusNotFound, detailNull())
		return
	}
	http.ServeContent(w, r, info.Name(), info.ModTime(), f)
}

// insideDir 判定 path 是否位于 dir 内（相等不算）；对已存在部分解析符号
// 链接与 `..` 之后再比。
func insideDir(path, dir string) bool {
	inner, err := canonicalPath(path)
	if err != nil {
		return false
	}
	outer, err := canonicalPath(dir)
	if err != nil {
		return false
	}
	rel, err := filepath.Rel(outer, inner)
	if err != nil {
		return false
	}
	return rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

// canonicalPath 绝对真实路径；不存在时对已存在的最深父级解析后回拼。
func canonicalPath(path string) (string, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	resolved, err := filepath.EvalSymlinks(abs)
	if err == nil {
		return resolved, nil
	}
	if !os.IsNotExist(err) {
		return "", err
	}
	parent := filepath.Dir(abs)
	if parent == abs {
		return "", err
	}
	resolvedParent, err := canonicalPath(parent)
	if err != nil {
		return "", err
	}
	return filepath.Join(resolvedParent, filepath.Base(abs)), nil
}
