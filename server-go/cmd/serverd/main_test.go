package main

import (
	"bytes"
	"context"
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
	if gatePath := os.Getenv("HOLDEXAR_TEST_TAIL_GATE"); gatePath != "" {
		beforeStartupTail = func(ctx context.Context) {
			for ctx.Err() == nil {
				if _, err := os.Stat(gatePath); os.IsNotExist(err) {
					return
				}
				select {
				case <-ctx.Done():
					return
				case <-time.After(10 * time.Millisecond):
				}
			}
		}
	}
	flag.CommandLine = flag.NewFlagSet("serverd", flag.ExitOnError)
	os.Args = []string{os.Args[0], "-addr", os.Getenv("HOLDEXAR_TEST_ADDR"), "-data-dir", os.Getenv("HOLDEXAR_TEST_DATA"), "-dev-session"}
	main()
}

func startServerProcess(t *testing.T, root, dataDir string, extraEnv ...string) (string, io.WriteCloser, <-chan struct{}, *error, *bytes.Buffer) {
	t.Helper()
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
	cmd := exec.Command(exe, "-test.run=^TestServerProcessHelper$")
	for _, env := range os.Environ() {
		key, _, _ := strings.Cut(env, "=")
		if !strings.EqualFold(key, "HOLDEXAR_DEV_WATCH") && !strings.EqualFold(key, "HOLDEXAR_DEV_CHILD") &&
			!strings.EqualFold(key, "HOLDEXAR_UPDATE_ATTEMPT_ID") && !strings.EqualFold(key, "HOLDEXAR_UPDATE_DATA_TOKEN") &&
			!strings.EqualFold(key, "HOLDEXAR_TEST_TAIL_GATE") {
			cmd.Env = append(cmd.Env, env)
		}
	}
	cmd.Env = append(cmd.Env,
		"HOLDEXAR_SERVER_TEST=1",
		"HOLDEXAR_DEV_CHILD=1",
		"HOLDEXAR_MCP=0",
		"HOLDEXAR_INSTALL_ROOT="+root,
		"HOLDEXAR_TEST_ADDR="+addr,
		"HOLDEXAR_TEST_DATA="+dataDir,
	)
	cmd.Env = append(cmd.Env, extraEnv...)
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
		case <-time.After(5 * time.Second):
			cmd.Process.Kill()
			<-done
		}
	})
	return addr, input, done, &exit, &output
}

func TestParentPipeShutdownDrainsOpenEventStreams(t *testing.T) {
	root := t.TempDir()
	if resolved, err := filepath.EvalSymlinks(root); err == nil {
		root = resolved
	}
	addr, input, done, exit, output := startServerProcess(t, root, filepath.Join(root, "data"))

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
			t.Fatalf("service not ready: %s", output.String())
		}
		time.Sleep(20 * time.Millisecond)
	}
	res, err := client.Get(base + "/session/dev")
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()

	var readiness struct {
		SchedulerReady   bool `json:"schedulerReady"`
		AutomaticEnabled bool `json:"automaticEnabled"`
	}
	var lastStatus int
	readyDeadline := time.Now().Add(10 * time.Second)
	for {
		res, err = client.Get(base + "/system/readiness")
		if err == nil {
			lastStatus = res.StatusCode
			err = json.NewDecoder(res.Body).Decode(&readiness)
			res.Body.Close()
			if err == nil && lastStatus == http.StatusOK && readiness.SchedulerReady && readiness.AutomaticEnabled {
				break
			}
		}
		if time.Now().After(readyDeadline) {
			t.Fatalf("running server reported readiness %+v, status=%d, err=%v, log=%s", readiness, lastStatus, err, output.String())
		}
		time.Sleep(20 * time.Millisecond)
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
		if *exit != nil {
			t.Fatalf("exit=%v log=%s", *exit, output.String())
		}
	case <-time.After(3 * time.Second):
		t.Fatal("live SSE connections blocked parent-pipe shutdown")
	}
}

func TestStartupOpensHTTPBeforeTailCompletes(t *testing.T) {
	t.Run("post_listen_tail", func(t *testing.T) {
		root := t.TempDir()
		if resolved, err := filepath.EvalSymlinks(root); err == nil {
			root = resolved
		}
		gateFile := filepath.Join(root, "tail.gate")
		if err := os.WriteFile(gateFile, []byte("hold"), 0o600); err != nil {
			t.Fatal(err)
		}
		addr, _, _, _, output := startServerProcess(t, root, filepath.Join(root, "data"), "HOLDEXAR_TEST_TAIL_GATE="+gateFile)
		client := &http.Client{Transport: &http.Transport{Proxy: nil, DisableKeepAlives: true}, Timeout: time.Second}
		base := "http://" + addr + "/api/v1"

		type healthPayload struct {
			Status         string `json:"status"`
			SchedulerReady *bool  `json:"schedulerReady"`
		}
		var initial healthPayload
		deadline := time.Now().Add(10 * time.Second)
		for {
			res, err := client.Get(base + "/health")
			if err == nil {
				decodeErr := json.NewDecoder(res.Body).Decode(&initial)
				res.Body.Close()
				if res.StatusCode == http.StatusOK && decodeErr == nil {
					break
				}
			}
			if time.Now().After(deadline) {
				t.Fatalf("health did not respond 200 before tail completion: %s", output.String())
			}
			time.Sleep(20 * time.Millisecond)
		}
		if initial.Status != "ok" || initial.SchedulerReady == nil || *initial.SchedulerReady {
			t.Fatalf("expected initial health ok with schedulerReady=false while tail gated, got %+v", initial)
		}

		if err := os.Remove(gateFile); err != nil {
			t.Fatal(err)
		}
		readyDeadline := time.Now().Add(10 * time.Second)
		for {
			res, err := client.Get(base + "/health")
			if err == nil {
				var current healthPayload
				decodeErr := json.NewDecoder(res.Body).Decode(&current)
				res.Body.Close()
				if res.StatusCode == http.StatusOK && decodeErr == nil && current.SchedulerReady != nil && *current.SchedulerReady {
					break
				}
			}
			if time.Now().After(readyDeadline) {
				t.Fatalf("schedulerReady did not become true after tail release: %s", output.String())
			}
			time.Sleep(20 * time.Millisecond)
		}
	})

	t.Run("updateguard_tx_clear_starts_tail", func(t *testing.T) {
		root := t.TempDir()
		if resolved, err := filepath.EvalSymlinks(root); err == nil {
			root = resolved
		}
		root = filepath.Clean(root)
		dataDir := filepath.Join(root, "data")
		dbPath := filepath.Join(dataDir, "holdexar.db")
		if err := os.MkdirAll(filepath.Join(dataDir, "backups"), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.MkdirAll(filepath.Join(root, "state"), 0o755); err != nil {
			t.Fatal(err)
		}
		markerRaw, _ := json.Marshal(map[string]any{
			"purpose":     "update",
			"token":       "tok-verify",
			"attemptId":   "att-verify",
			"installRoot": root,
			"pid":         os.Getpid(),
		})
		if err := os.WriteFile(filepath.Join(dataDir, "backups", ".restore.lock"), markerRaw, 0o600); err != nil {
			t.Fatal(err)
		}
		txPath := filepath.Join(root, "state", "transaction.json")
		txRaw, _ := json.Marshal(map[string]any{
			"attemptId":                     "att-verify",
			"dataToken":                     "tok-verify",
			"dataDir":                       dataDir,
			"dataDbPath":                    dbPath,
			"dataPrepared":                  true,
			"dataAccepted":                  false,
			"dataRecoveryPhase":             "prepared",
			"dataProbeRegistrationRequired": false,
		})
		if err := os.WriteFile(txPath, txRaw, 0o600); err != nil {
			t.Fatal(err)
		}

		addr, _, _, _, output := startServerProcess(t, root, dataDir,
			"HOLDEXAR_UPDATE_ATTEMPT_ID=att-verify",
			"HOLDEXAR_UPDATE_DATA_TOKEN=tok-verify",
		)
		client := &http.Client{Transport: &http.Transport{Proxy: nil, DisableKeepAlives: true}, Timeout: time.Second}
		base := "http://" + addr + "/api/v1"

		type updateHealth struct {
			Status         string `json:"status"`
			SchedulerReady *bool  `json:"schedulerReady"`
			WriteProtected *bool  `json:"writeProtected"`
		}
		var initial updateHealth
		deadline := time.Now().Add(10 * time.Second)
		for {
			res, err := client.Get(base + "/health")
			if err == nil {
				decodeErr := json.NewDecoder(res.Body).Decode(&initial)
				res.Body.Close()
				if res.StatusCode == http.StatusOK && decodeErr == nil {
					break
				}
			}
			if time.Now().After(deadline) {
				t.Fatalf("updateguard health did not respond 200: %s", output.String())
			}
			time.Sleep(20 * time.Millisecond)
		}
		if initial.WriteProtected == nil || !*initial.WriteProtected || initial.SchedulerReady == nil || *initial.SchedulerReady {
			t.Fatalf("expected writeProtected=true and schedulerReady=false during verification, got %+v", initial)
		}

		if err := os.Remove(txPath); err != nil {
			t.Fatal(err)
		}
		clearDeadline := time.Now().Add(10 * time.Second)
		for {
			res, err := client.Get(base + "/health")
			if err == nil {
				var current updateHealth
				decodeErr := json.NewDecoder(res.Body).Decode(&current)
				res.Body.Close()
				if res.StatusCode == http.StatusOK && decodeErr == nil &&
					current.WriteProtected != nil && !*current.WriteProtected &&
					current.SchedulerReady != nil && *current.SchedulerReady {
					break
				}
			}
			if time.Now().After(clearDeadline) {
				t.Fatalf("removing transaction.json did not clear write protection and start tail: %s", output.String())
			}
			time.Sleep(50 * time.Millisecond)
		}
	})
}
