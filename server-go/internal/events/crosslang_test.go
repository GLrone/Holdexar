package events

// 跨语言帧等价对照：真实 venv 里 Python Event.as_sse() 与 Go AsSSE() 逐字节
// 比对（ts 冻结）。无 venv 时自动跳过。

import (
	"bytes"
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func venvPython(t *testing.T) string {
	t.Helper()
	rel := filepath.Join("..", "..", "..", "server", ".venv", "Scripts", "python.exe")
	py, err := filepath.Abs(rel)
	if err != nil {
		t.Skip("无法定位 worktree venv，跳过跨语言对照")
	}
	if _, err := os.Stat(py); err != nil {
		t.Skip("无 worktree venv，跳过跨语言对照")
	}
	return py
}

func TestAsSSECrossLanguage(t *testing.T) {
	py := venvPython(t)
	serverRoot, _ := filepath.Abs(filepath.Join("..", "..", "..", "server"))

	cases := []struct {
		name   string
		evType string
		ts     float64
		kwargs string // Python 字典字面量内容（可为空）
		data   []KV
	}{
		{"状态事件", "job.started", 1730367300.123456,
			`"job_id": 5, "scope": "appids", "count": 3, "regions": ["CN", "US"]`,
			[]KV{{"job_id", 5}, {"scope", "appids"}, {"count", 3}, {"regions", []any{"CN", "US"}}}},
		{"进度事件", "crawl.progress", 1730367300.0,
			`"done": 12, "ok": 11, "fail": 1, "total": 100, "qsize": 0, "speed": 0.55`,
			[]KV{{"done", 12}, {"ok", 11}, {"fail", 1}, {"total", 100}, {"qsize", 0}, {"speed", 0.55}}},
		{"嵌套对象", "maintenance.started", 1730367300.5,
			`"kind": "health", "totals": {"l1": 3, "ok": True}`,
			[]KV{{"kind", "health"}, {"totals", []KV{{"l1", 3}, {"ok", true}}}}},
		{"空载荷", "crawl.idle", 42.0, ``, nil},
		{"中文与浮点形状", "job.status", 123456.789,
			`"status": "已停止", "ratio": 0.1`,
			[]KV{{"status", "已停止"}, {"ratio", 0.1}}},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			src := strings.Join([]string{pyStrLit(c.evType), "{" + c.kwargs + "}", pyFloatLit(c.ts)}, ", ")
			code := "import sys\nsys.stdout.reconfigure(encoding=\"utf-8\", newline=\"\")\n" +
				"from app.core.events import Event\n" +
				"print(Event(" + src + ").as_sse(), end=\"\")"
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			cmd := exec.CommandContext(ctx, py, "-c", code)
			cmd.Dir = serverRoot
			var stdout, stderr bytes.Buffer
			cmd.Stdout = &stdout
			cmd.Stderr = &stderr
			if err := cmd.Run(); err != nil {
				t.Fatalf("venv 执行失败: %v\nstderr: %s\ncode: %s", err, stderr.String(), code)
			}
			ev := Event{Type: c.evType, Ts: c.ts, Data: c.data}
			if got := ev.AsSSE(); got != stdout.String() {
				t.Fatalf("帧跨语言不一致\n go:     %q\n python: %q", got, stdout.String())
			}
		})
	}
}

func pyStrLit(s string) string {
	return "\"" + strings.ReplaceAll(strings.ReplaceAll(s, "\\", "\\\\"), "\"", "\\\"") + "\""
}

func pyFloatLit(f float64) string {
	var sb strings.Builder
	pyFloat(&sb, f)
	return sb.String()
}
