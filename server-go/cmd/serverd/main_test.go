package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"io"
	"net"
	"net/http"
	"net/http/cookiejar"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestServerProcessHelper(t *testing.T) {
	if os.Getenv("HOLDEXAR_SERVER_TEST") != "1" {
		return
	}
	flag.CommandLine = flag.NewFlagSet("serverd", flag.ExitOnError)
	os.Args = []string{os.Args[0], "-addr", os.Getenv("HOLDEXAR_TEST_ADDR"), "-data-dir", os.Getenv("HOLDEXAR_TEST_DATA"), "-dev-session"}
	main()
}

func TestParentPipeShutdownDrainsOpenEventStreams(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := listener.Addr().String()
	listener.Close()
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	cmd := exec.Command(exe, "-test.run=^TestServerProcessHelper$")
	for _, env := range os.Environ() {
		key, _, _ := strings.Cut(env, "=")
		if !strings.EqualFold(key, "HOLDEXAR_DEV_WATCH") && !strings.EqualFold(key, "HOLDEXAR_DEV_CHILD") {
			cmd.Env = append(cmd.Env, env)
		}
	}
	cmd.Env = append(cmd.Env, "HOLDEXAR_SERVER_TEST=1", "HOLDEXAR_DEV_CHILD=1", "HOLDEXAR_MCP=0", "HOLDEXAR_INSTALL_ROOT="+root, "HOLDEXAR_TEST_ADDR="+addr, "HOLDEXAR_TEST_DATA="+filepath.Join(root, "data"))
	var output bytes.Buffer
	cmd.Stdout, cmd.Stderr = &output, &output
	input, err := cmd.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err = cmd.Start(); err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	var exit error
	go func() { exit = cmd.Wait(); close(done) }()
	t.Cleanup(func() {
		input.Close()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			cmd.Process.Kill()
			<-done
		}
	})
	jar, _ := cookiejar.New(nil)
	client := &http.Client{Jar: jar, Transport: &http.Transport{Proxy: nil, DisableKeepAlives: true}, Timeout: time.Second}
	base := "http://" + addr + "/api/v1"
	deadline := time.Now().Add(10 * time.Second)
	for {
		res, e := client.Get(base + "/health")
		if e == nil {
			res.Body.Close()
			if res.StatusCode == 200 {
				break
			}
		}
		if time.Now().After(deadline) {
			t.Fatal("service not ready")
		}
		time.Sleep(20 * time.Millisecond)
	}
	res, err := client.Get(base + "/session/dev")
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	res, err = client.Get(base + "/system/readiness")
	if err != nil {
		t.Fatal(err)
	}
	var readiness struct {
		SchedulerReady   bool `json:"schedulerReady"`
		AutomaticEnabled bool `json:"automaticEnabled"`
	}
	err = json.NewDecoder(res.Body).Decode(&readiness)
	res.Body.Close()
	if err != nil || res.StatusCode != http.StatusOK || !readiness.SchedulerReady || !readiness.AutomaticEnabled {
		t.Fatalf("running server reported readiness %+v, status=%d, err=%v", readiness, res.StatusCode, err)
	}
	streamClient := &http.Client{Jar: jar, Transport: &http.Transport{Proxy: nil, DisableKeepAlives: true}, Timeout: 5 * time.Second}
	for _, path := range []string{"/events/stream", "/system/logs/stream"} {
		res, err := streamClient.Get(base + path)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		if res.StatusCode != 200 {
			t.Fatal(res.StatusCode)
		}
		first := make([]byte, 1)
		if _, err = io.ReadFull(res.Body, first); err != nil {
			t.Fatal(err)
		}
	}
	input.Close()
	select {
	case <-done:
		if exit != nil {
			t.Fatalf("exit=%v log=%s", exit, output.String())
		}
	case <-time.After(3 * time.Second):
		t.Fatal("live SSE connections blocked parent-pipe shutdown")
	}
}
