package proxypool

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/clashcfg"
	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/store"
)

func deadProbeKernelCopy(t *testing.T, dataDir string) {
	t.Helper()
	exe := liveKernelExe(t)
	dst := filepath.Join(dataDir, "clash", clashkernel.KernelFilename())
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		t.Fatal(err)
	}
	src, err := os.Open(exe)
	if err != nil {
		t.Fatal(err)
	}
	defer src.Close()
	out, err := os.Create(dst)
	if err != nil {
		t.Fatal(err)
	}
	defer out.Close()
	if _, err := io.Copy(out, src); err != nil {
		t.Fatal(err)
	}
}

func deadSandboxNode(t *testing.T, db *sql.DB, name string, state string, failures int64, subID any, socksPort int) {
	t.Helper()
	config := map[string]any{"name": name, "type": "socks5",
		"server": "127.0.0.1", "port": socksPort}
	blob, err := json.Marshal(config)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO proxy_nodes
		(node_id, runtime_name, original_name, proxy_type, normalized_config, state, subscription_id, consecutive_failures)
		VALUES (?, ?, ?, 'socks5', ?, ?, ?, ?)`,
		"fp-"+name, "1|"+name, name, string(blob), state, subID, failures); err != nil {
		t.Fatal(err)
	}
}

func TestLiveDeadProbeRecovery(t *testing.T) {
	db, gate, dataDir := deadTestDB(t)
	deadProbeKernelCopy(t, dataDir)
	ctx := context.Background()
	if _, err := db.Exec(`INSERT INTO proxy_subscriptions (id, url) VALUES (1, 'sandbox://sub')`); err != nil {
		t.Fatal(err)
	}
	socksPort := minimalSocks5Server(t)
	target := newLiveProbeTarget(t)
	probeURL := target.server.URL + "/generate_204"

	deadSandboxNode(t, db, "Dead-01", NodeDead, 2, int64(1), socksPort)
	deadSandboxNode(t, db, "Dead-02", NodeDead, 1, int64(1), socksPort)
	deadSandboxNode(t, db, "无主", NodeDead, 0, nil, socksPort)

	runner := &DeadProbeRunner{DB: db, Gate: gate, DataDir: dataDir}
	var outcomes []DeadProbeOutcome
	for round := 0; ; round++ {
		got, err := runner.RecoverDeadNodes(ctx, probeURL, 24)
		if err != nil {
			t.Fatalf("恢复探测失败：%v", err)
		}
		outcomes = got
		failed := 0
		for _, o := range outcomes {
			if o.RuntimeName != "1|无主" && !o.OK {
				failed++
			}
		}
		if failed == 0 {
			break
		}
		if round == 5 {
			for _, o := range outcomes {
				if !o.OK && o.RuntimeName != "1|无主" {
					t.Logf("持续不可达：%s detail=%q cause=%s", o.RuntimeName, o.Detail, o.Cause)
				}
			}
			t.Fatalf("重探 6 轮仍有 %d 个节点不可达", failed)
		}
		time.Sleep(500 * time.Millisecond)
	}

	byName := map[string]DeadProbeOutcome{}
	for _, o := range outcomes {
		byName[o.RuntimeName] = o
	}
	sourceless := byName["1|无主"]
	if sourceless.NodeID != "fp-无主" || sourceless.OK || sourceless.NodeFailure ||
		sourceless.Cause != CauseDependency || sourceless.Detail != "节点来源不存在" {
		t.Fatalf("无主节点应依赖归因：%+v", sourceless)
	}
	for _, name := range []string{"1|Dead-01", "1|Dead-02"} {
		o := byName[name]
		if !o.OK || o.DelayMS <= 0 || !o.NodeFailure || o.Cause != CauseNode {
			t.Fatalf("%s 应恢复成功：%+v", name, o)
		}
	}

	for _, name := range []string{"1|Dead-01", "1|Dead-02"} {
		var state string
		var failures int64
		var lastL2 sql.NullString
		if err := db.QueryRow(`SELECT state, consecutive_failures, CAST(last_l2_at AS TEXT)
			FROM proxy_nodes WHERE runtime_name = ?`, name).Scan(&state, &failures, &lastL2); err != nil {
			t.Fatal(err)
		}
		if state != NodeActive || failures != 0 || !lastL2.Valid {
			t.Fatalf("%s 应复活清零：%s %d %v", name, state, failures, lastL2)
		}
		var ok int64
		var detail, channel sql.NullString
		var runID sql.NullInt64
		if err := db.QueryRow(`SELECT ok, detail, run_id, channel FROM health_observations
			WHERE node_id = ? AND level = 'L2' AND ok = 1 ORDER BY id DESC LIMIT 1`, "fp-"+name[2:]).
			Scan(&ok, &detail, &runID, &channel); err != nil {
			t.Fatal(err)
		}
		if detail.String != DetailRecoveryOK || channel.String != DefaultScheduleChannel || runID.Valid {
			t.Fatalf("%s 恢复观测行形状不对：detail=%q channel=%q run=%v",
				name, detail.String, channel.String, runID)
		}
	}
	var state string
	if err := db.QueryRow(`SELECT state FROM proxy_nodes WHERE runtime_name = '1|无主'`).Scan(&state); err != nil {
		t.Fatal(err)
	}
	if state != NodeDead {
		t.Fatalf("无主节点不得被推进：%s", state)
	}
	if got := queryInt(t, db, `SELECT COUNT(*) FROM health_observations WHERE node_id = 'fp-无主'`); got < 1 {
		t.Fatalf("无主节点应留观测行，实际 %d", got)
	}
	entries, _ := os.ReadDir(filepath.Join(dataDir, "proxypool", "_dead_probe"))
	if len(entries) != 0 {
		t.Fatalf("探测工作目录应清理，残留 %d 项", len(entries))
	}
}

func TestLiveBuildPoolKernelLoads(t *testing.T) {
	db, gate, dir := poolTestDB(t)
	exe := liveKernelExe(t)
	ctx := context.Background()
	if _, err := db.Exec(`INSERT INTO proxy_subscriptions (id, url, admission_status) VALUES (1, 'sandbox://pool-sub', ?)`,
		AdmissionActive); err != nil {
		t.Fatal(err)
	}
	socksPort := minimalSocks5Server(t)
	target := newLiveProbeTarget(t)
	for _, name := range []string{"Pool-01", "Pool-02", "Pool-03"} {
		deadSandboxNode(t, db, name, NodeActive, 0, int64(1), socksPort)
	}
	deadSandboxNode(t, db, "Dead-Pool", NodeDead, 1, int64(1), socksPort)
	deadSandboxNode(t, db, "剩余流量：10GB", NodeActive, 0, int64(1), socksPort)

	before, err := PoolSignature(ctx, mustConn(t, db), dir, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if len(before) != 3 {
		t.Fatalf("合格集应 3 条签名（DEAD 与信息占位不合格），实际 %v", before)
	}

	var result PoolBuildResult
	if err := gate.WithWrite(ctx, store.PriorityBackground, func(c *sql.Conn) error {
		got, berr := BuildPool(ctx, c, dir, time.Now())
		if berr != nil {
			return berr
		}
		result = got
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if result.ExpectedCount != 3 || result.WrittenCount != 3 {
		t.Fatalf("池账目不对：%+v", result)
	}
	if names := PoolFileNames(dir); len(names) != 3 {
		t.Fatalf("池文件读数应 3 个，实际 %v", names)
	}

	raw, err := os.ReadFile(result.Path)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := clashcfg.LoadYAML(string(raw))
	if err != nil {
		t.Fatalf("池文件应能被包内解析器回读：%v", err)
	}
	mixedPort, err := clashcfg.FreeLocalPort()
	if err != nil {
		t.Fatal(err)
	}
	controllerPort, err := clashcfg.FreeLocalPort()
	if err != nil {
		t.Fatal(err)
	}
	doc := clashcfg.NewDoc()
	doc.Set("mode", "global")
	doc.Set("log-level", "warning")
	doc.Set("allow-lan", false)
	doc.Set("bind-address", "127.0.0.1")
	doc.Set("mixed-port", int64(mixedPort))
	doc.Set("external-controller", fmt.Sprintf("127.0.0.1:%d", controllerPort))
	doc.Set("secret", "pool-live-secret")
	doc.Set("proxies", parsed["proxies"])
	doc.Set("rules", []any{"MATCH,DIRECT"})
	configText := clashcfg.DumpYAML(doc)
	runDir := t.TempDir()
	configPath := filepath.Join(runDir, "pool-runtime.yaml")
	if err := os.WriteFile(configPath, []byte(configText), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := clashkernel.ValidateConfig(exe, configPath, runDir); err != nil {
		t.Fatalf("真内核预检拒载池产物：%v", err)
	}
	var runtime clashkernel.Runtime
	status, err := runtime.Start(exe, configPath, clashkernel.StartOptions{MixedPort: mixedPort})
	if err != nil {
		t.Fatalf("真内核启动失败：%v", err)
	}
	t.Cleanup(func() { runtime.Stop() })
	if !status.EntryReady {
		t.Fatalf("混合入口未就绪：%+v", status)
	}
	names := controllerProxyNames(t, runtime.ControllerURL, runtime.Secret)
	for _, want := range result.RuntimeNames {
		if !names[want] {
			t.Fatalf("内核里没有池节点 %s", want)
		}
	}
	for _, absent := range []string{"1|Dead-Pool", "1|剩余流量：10GB"} {
		if names[absent] {
			t.Fatalf("不合格节点 %s 不得进池", absent)
		}
	}
	code, perr := getViaProxy(mixedProbePort(mixedPort), target.server.URL+"/generate_204", 8*time.Second)
	if perr != nil || code != http.StatusNoContent {
		t.Fatalf("池节点经真内核不可达：code=%d err=%v", code, perr)
	}

	if _, err := db.Exec(`UPDATE proxy_nodes SET normalized_config = json_set(normalized_config, '$.port', 1)
		WHERE runtime_name = '1|Pool-02'`); err != nil {
		t.Fatal(err)
	}
	after, err := PoolSignature(ctx, mustConn(t, db), dir, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if !PoolSignatureChanged(before, after) {
		t.Fatalf("节点参数变化应改变运行集签名")
	}
	fired := 0
	orig := RequestPoolRebuild
	RequestPoolRebuild = func() { fired++ }
	defer func() { RequestPoolRebuild = orig }()
	MaybeRequestPoolRebuild(before, after)
	if fired != 1 {
		t.Fatalf("签名变化应请求一次重建，实际 %d", fired)
	}
}

func controllerProxyNames(t *testing.T, controllerURL, secret string) map[string]bool {
	t.Helper()
	client := probeHTTPClient(8 * time.Second)
	req, err := http.NewRequest(http.MethodGet, controllerURL+"/proxies", nil)
	if err != nil {
		t.Fatal(err)
	}
	if secret != "" {
		req.Header.Set("Authorization", "Bearer "+secret)
	}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("读控制器失败：%v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("读控制器 HTTP %d", resp.StatusCode)
	}
	var payload struct {
		Proxies map[string]json.RawMessage `json:"proxies"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		t.Fatalf("控制器响应不是 JSON：%v", err)
	}
	out := map[string]bool{}
	for name := range payload.Proxies {
		out[name] = true
	}
	return out
}
