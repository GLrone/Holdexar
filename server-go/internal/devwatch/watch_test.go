package devwatch

import (
	"context"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestFingerprintTracksContentAddDeleteAndEmbeddedSQL(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "go.mod"), []byte("module example"), 0600); err != nil {
		t.Fatal(err)
	}
	dir := filepath.Join(root, "internal", "example")
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	file := filepath.Join(dir, "value.go")
	write := func(path, content string) {
		t.Helper()
		if e := os.WriteFile(path, []byte(content), 0600); e != nil {
			t.Fatal(e)
		}
	}
	snapshot := func() string {
		t.Helper()
		v, e := fingerprint(root)
		if e != nil {
			t.Fatal(e)
		}
		return v
	}
	write(file, "one")
	first := snapshot()
	info, _ := os.Stat(file)
	write(file, "two")
	if err := os.Chtimes(file, info.ModTime(), info.ModTime()); err != nil {
		t.Fatal(err)
	}
	if snapshot() == first {
		t.Fatal("equal-size same-mtime content change missed")
	}
	stable := snapshot()
	write(filepath.Join(dir, "value_test.go"), "test only")
	write(filepath.Join(root, "serverd.exe"), "build output")
	if snapshot() != stable {
		t.Fatal("test/build output triggered reload")
	}
	write(filepath.Join(dir, "schema.sql"), "create table example(id)")
	if snapshot() == stable {
		t.Fatal("embedded SQL change missed")
	}
	if err := os.Remove(filepath.Join(dir, "schema.sql")); err != nil {
		t.Fatal(err)
	}
	if snapshot() != stable {
		t.Fatal("file removal not reflected")
	}
	write(filepath.Join(root, "go.mod"), "module changed")
	if snapshot() == stable {
		t.Fatal("module change missed")
	}
}

type lockedBuffer struct {
	mu   sync.Mutex
	text strings.Builder
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.text.Write(p)
}
func (b *lockedBuffer) String() string { b.mu.Lock(); defer b.mu.Unlock(); return b.text.String() }

func TestRunRebuildFailurePreservesServiceAndCancelClosesChild(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "cmd", "serverd")
	if e := os.MkdirAll(dir, 0700); e != nil {
		t.Fatal(e)
	}
	if e := os.WriteFile(filepath.Join(root, "go.mod"), []byte("module fixture\n\ngo 1.27.0\n"), 0600); e != nil {
		t.Fatal(e)
	}
	file := filepath.Join(dir, "main.go")
	source := func(version string) string {
		return `package main
import("io";"net";"net/http";"os")
func main(){ listener,e:=net.Listen("tcp",os.Args[1]);if e!=nil{panic(e)};server:=&http.Server{Handler:http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){io.WriteString(w,"` + version + `")})};go server.Serve(listener);io.Copy(io.Discard,os.Stdin);server.Close() }
`
	}
	write := func(content string) {
		t.Helper()
		if e := os.WriteFile(file, []byte(content), 0600); e != nil {
			t.Fatal(e)
		}
	}
	write(source("one"))
	exe := filepath.Join(t.TempDir(), "initial.exe")
	build := exec.Command("go", "build", "-o", exe, "./cmd/serverd")
	build.Dir = root
	if output, e := build.CombinedOutput(); e != nil {
		t.Fatalf("build %v: %s", e, output)
	}
	listener, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	addr := listener.Addr().String()
	listener.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var log lockedBuffer
	done := make(chan error, 1)
	go func() { done <- Run(ctx, root, exe, []string{addr}, &log) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(15 * time.Second):
			t.Error("supervisor did not stop")
		}
	})
	client := &http.Client{Timeout: 200 * time.Millisecond, Transport: &http.Transport{Proxy: nil, DisableKeepAlives: true}}
	read := func() string {
		res, e := client.Get("http://" + addr)
		if e != nil {
			return ""
		}
		defer res.Body.Close()
		b, _ := io.ReadAll(res.Body)
		return string(b)
	}
	wait := func(label string, fn func() bool) {
		t.Helper()
		deadline := time.Now().Add(40 * time.Second)
		for time.Now().Before(deadline) {
			if fn() {
				return
			}
			time.Sleep(30 * time.Millisecond)
		}
		t.Fatalf("%s: %s", label, log.String())
	}
	wait("initial service", func() bool { return read() == "one" })
	write("package main\nsyntax error")
	wait("compile failure", func() bool { return strings.Contains(log.String(), "编译失败") })
	if got := read(); got != "one" {
		t.Fatalf("old service stopped on failed build: %q", got)
	}
	write(source("two"))
	wait("new service", func() bool { return read() == "two" })
	write("package main\nfunc main(){panic(\"startup failure\")}\n")
	wait("runtime startup failure", func() bool { return strings.Contains(log.String(), "后端已退出") })
	write(source("three"))
	wait("repair after runtime failure", func() bool { return read() == "three" })
	cancel()
	wait("child closes on supervisor cancellation", func() bool { return read() == "" })
}
