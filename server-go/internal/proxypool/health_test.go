package proxypool

// 三层探测落账单测：假内核三件套（桩控制器 / 桩 HTTP 代理 / 桩业务与回显目标）
// 承载真实网络语义；落账边界、状态推进、冷却复位、回滚全清用临时库真写闸验证。

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

// ── 假内核三件套 ─────────────────────────────────────────────────────────────

// stubTarget 业务与回显目标：按路径给 StoreBrowse 形状 / 纯文本出口 / 204。
type stubTarget struct {
	server    *httptest.Server
	browseOK  atomic.Bool // false = 返回目标服务 500
	closeHard func()      // 模拟目标整体失联（传输失败归因用）
}

func newStubTarget(t *testing.T) *stubTarget {
	t.Helper()
	st := &stubTarget{}
	mux := http.NewServeMux()
	mux.HandleFunc("/generate_204", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("/IStoreBrowseService/GetItems/v1/", func(w http.ResponseWriter, r *http.Request) {
		if !st.browseOK.Load() {
			w.WriteHeader(http.StatusInternalServerError)
			_, _ = w.Write([]byte("steam down"))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"response":{"store_items":[{"appid":220,"success":1,
			"best_purchase_option":{"final_price_in_cents":"999"}}]}}`))
	})
	mux.HandleFunc("/ip", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("127.0.0.1"))
	})
	st.server = httptest.NewServer(mux)
	st.closeHard = st.server.Close
	t.Cleanup(st.server.Close)
	return st
}

// liveProxyHandler 桩出口代理的转发逻辑：兼容绝对 URI 与 origin-form 两种
// 入站形态（mihomo 的 http 出站对明文目标发 origin-form）。
func liveProxyHandler(client *http.Client) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		target := r.URL.String()
		if r.URL.Host == "" {
			target = "http://" + r.Host + r.URL.RequestURI()
		}
		upstream, err := client.Get(target)
		if err != nil {
			// 上游失联：本地代理入口建隧失败的空体 502（与真内核同形状）
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		defer upstream.Body.Close()
		w.WriteHeader(upstream.StatusCode)
		buf := make([]byte, 4096)
		for {
			n, rerr := upstream.Body.Read(buf)
			if n > 0 {
				_, _ = w.Write(buf[:n])
			}
			if rerr != nil {
				break
			}
		}
	}
}

// newStubProxy 桩内核混合口 / lane listener：对绝对 URI 请求做真实转发
// （经它出去的请求真的会打到目标服务器，语义与真内核代理链一致）。
func newStubProxy(t *testing.T) *httptest.Server {
	t.Helper()
	client := &http.Client{Transport: &http.Transport{Proxy: nil}}
	server := httptest.NewServer(liveProxyHandler(client))
	t.Cleanup(server.Close)
	return server
}

// stubController 桩内核控制器：记录各 selector 的选中节点并回读核验，
// delay 端点按名字给延迟（failDelay 非 0 时对全部名字返回该状态码）。
type stubController struct {
	server    *httptest.Server
	mu        sync.Mutex // now 由各请求 goroutine 读写，httptest 每请求独立 goroutine
	now       map[string]string
	delayFail atomic.Int32 // 0=正常回 delay；非 0 = 返回该 HTTP 状态码
}

func newStubController(t *testing.T) *stubController {
	t.Helper()
	sc := &stubController{now: map[string]string{}}
	proxies := func(name string) map[string]any {
		sc.mu.Lock()
		got, ok := sc.now[name]
		sc.mu.Unlock()
		if ok {
			return map[string]any{"now": got}
		}
		return map[string]any{"now": ""}
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/proxies/", func(w http.ResponseWriter, r *http.Request) {
		rest := strings.TrimPrefix(r.URL.Path, "/proxies/")
		switch {
		case strings.HasSuffix(rest, "/delay"):
			if code := sc.delayFail.Load(); code != 0 {
				w.WriteHeader(int(code))
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"delay": 42}`))
		case r.Method == http.MethodPut:
			var body struct {
				Name string `json:"name"`
			}
			_ = jsonUnmarshalUseNumber([]byte(readAllBody(r)), &body)
			sc.mu.Lock()
			sc.now[rest] = body.Name
			sc.mu.Unlock()
			w.WriteHeader(http.StatusNoContent)
		default:
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(proxies(rest))
		}
	})
	sc.server = httptest.NewServer(mux)
	t.Cleanup(sc.server.Close)
	return sc
}

func readAllBody(r *http.Request) string {
	buf := make([]byte, 0, 256)
	tmp := make([]byte, 128)
	for {
		n, err := r.Body.Read(tmp)
		buf = append(buf, tmp[:n]...)
		if err != nil {
			break
		}
	}
	return string(buf)
}

// newHealthChecker 组一个 HealthChecker：库 + 写闸 + 桩内核端点 + 运行配置文件。
// lanes>0 时把对应数量的假 lane listener（桩代理端口）写进配置。
func newHealthChecker(t *testing.T, lanes int) (*HealthChecker, *stubController, *stubTarget, []*httptest.Server) {
	t.Helper()
	db, gate, dir := newTestDB(t)
	if err := EnsureSchema(context.Background(), mustConn(t, db)); err != nil {
		t.Fatal(err)
	}
	controller := newStubController(t)
	target := newStubTarget(t)
	proxies := make([]*httptest.Server, 0, lanes)
	var laneLines strings.Builder
	for i := 0; i < lanes; i++ {
		proxy := newStubProxy(t)
		proxies = append(proxies, proxy)
		port := portOf(t, proxy)
		laneLines.WriteString(fmt.Sprintf("  - name: HlProbeLane-%d-in\n    type: mixed\n    port: %d\n    listen: 127.0.0.1\n    proxy: HlProbeLane-%d\n", i+1, port, i+1))
	}
	var groupLines strings.Builder
	if lanes > 0 {
		groupLines.WriteString("proxy-groups:\n")
	}
	for i := 0; i < lanes; i++ {
		groupLines.WriteString(fmt.Sprintf("  - name: HlProbeLane-%d\n    type: select\n    include-all: true\n", i+1))
	}
	// 无 lane 的桩里混合口也指向一个桩代理，GLOBAL 串行路径才有真实可达性。
	mixedPort := 0
	if lanes == 0 {
		mixedProxy := newStubProxy(t)
		proxies = append(proxies, mixedProxy)
		mixedPort = portOf(t, mixedProxy)
	}
	configPath := filepath.Join(dir, "runtime.yaml")
	configText := fmt.Sprintf("mixed-port: %d\n", mixedPort) +
		groupLines.String() + "listeners:\n" + laneLines.String()
	if err := os.WriteFile(configPath, []byte(configText), 0o644); err != nil {
		t.Fatal(err)
	}
	h := &HealthChecker{
		DB: db, Gate: gate, DataDir: dir,
		ControllerURL: controller.server.URL, Secret: "test-secret",
		ConfigPath: configPath, Now: func() time.Time { return time.Date(2026, 10, 31, 12, 0, 0, 0, time.Local) },
	}
	return h, controller, target, proxies
}

func portOf(t *testing.T, server *httptest.Server) int {
	t.Helper()
	u, err := url.Parse(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	var port int
	if _, err := fmt.Sscanf(u.Port(), "%d", &port); err != nil {
		t.Fatalf("桩端口解析失败：%v", err)
	}
	return port
}

func mustConn(t *testing.T, db *sql.DB) *sql.Conn {
	t.Helper()
	conn, err := db.Conn(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return conn
}

// seedNode 插一个节点行（默认 NEW、归属订阅 1、失败计数 0），返回库内 id。
func seedNode(t *testing.T, db *sql.DB, runtimeName string, state string, failures int64, subID any) int64 {
	t.Helper()
	res, err := db.Exec(`INSERT INTO proxy_nodes
		(node_id, runtime_name, proxy_type, state, subscription_id, consecutive_failures)
		VALUES (?, ?, 'ss', ?, ?, ?)`,
		"fp-"+runtimeName, runtimeName, state, subID, failures)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := res.LastInsertId()
	return id
}

func queryInt(t *testing.T, db *sql.DB, query string, args ...any) int64 {
	t.Helper()
	var out sql.NullInt64
	if err := db.QueryRow(query, args...).Scan(&out); err != nil {
		t.Fatalf("查询 %s 失败：%v", query, err)
	}
	if !out.Valid {
		return -1 // 显式区分 NULL
	}
	return out.Int64
}

func queryString(t *testing.T, db *sql.DB, query string, args ...any) sql.NullString {
	t.Helper()
	var out sql.NullString
	if err := db.QueryRow(query, args...).Scan(&out); err != nil {
		t.Fatalf("查询 %s 失败：%v", query, err)
	}
	return out
}

func countRows(t *testing.T, db *sql.DB, query string, args ...any) int {
	t.Helper()
	var n int
	if err := db.QueryRow(query, args...).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

// ── L0 整池落账 ──────────────────────────────────────────────────────────────

func TestL0CheckPoolWriteBack(t *testing.T) {
	h, _, _, _ := newHealthChecker(t, 0)
	for _, name := range []string{"n1", "n2", "n3"} {
		seedNode(t, h.DB, name, NodeNew, 0, int64(1))
	}
	outcomes, err := h.L0CheckPool(context.Background(), []string{"n1", "n2", "n3"}, ProbeOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if len(outcomes) != 3 {
		t.Fatalf("应有 3 条 L0 结论，实际 %d", len(outcomes))
	}
	for _, o := range outcomes {
		if !o.OK || o.DelayMS != 42 {
			t.Fatalf("L0 应全部成功： %+v", o)
		}
	}
	if got := queryInt(t, h.DB, `SELECT l0_delay_ms FROM proxy_nodes WHERE runtime_name = 'n1'`); got != 42 {
		t.Fatalf("延迟应挂到节点行，实际 %v", got)
	}
	if n := countRows(t, h.DB, `SELECT COUNT(*) FROM health_observations`); n != 0 {
		t.Fatalf("L0 成功不落观测行，实际 %d 行", n)
	}
}

func TestL0CheckPoolFailureObservationOnly(t *testing.T) {
	h, controller, _, _ := newHealthChecker(t, 0)
	controller.delayFail.Store(500)
	for _, name := range []string{"n1", "n2"} {
		seedNode(t, h.DB, name, NodeActive, 0, int64(1))
	}
	outcomes, err := h.L0CheckPool(context.Background(), []string{"n1", "n2"}, ProbeOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if len(outcomes) != 2 || outcomes[0].OK {
		t.Fatalf("L0 应失败：%+v", outcomes)
	}
	if n := countRows(t, h.DB, `SELECT COUNT(*) FROM health_observations WHERE level = 'L0'`); n != 2 {
		t.Fatalf("失败应落 L0 观测，实际 %d 行", n)
	}
	// L0 只诊断不推状态：状态与失败计数都不许动
	if state := queryString(t, h.DB, `SELECT state FROM proxy_nodes WHERE runtime_name = 'n1'`); state.String != NodeActive {
		t.Fatalf("L0 失败不得推进状态，实际 %s", state.String)
	}
	if failures := queryInt(t, h.DB, `SELECT consecutive_failures FROM proxy_nodes WHERE runtime_name = 'n1'`); failures != 0 {
		t.Fatalf("L0 失败不得累加计数，实际 %d", failures)
	}
}

// ── L1 出口身份 ──────────────────────────────────────────────────────────────

func TestExitIPCheckPoolLanePath(t *testing.T) {
	h, _, _, proxies := newHealthChecker(t, 2)
	if got := len(h.Lanes()); got != 2 {
		t.Fatalf("应读到 2 条在听 lane，实际 %d", got)
	}
	for _, name := range []string{"n1", "n2", "n3", "n4"} {
		seedNode(t, h.DB, name, NodeNew, 0, int64(1))
	}
	echoURL := newStubTarget(t).server.URL + "/ip"
	outcomes, err := h.ExitIPCheckPool(context.Background(), []string{"n1", "n2", "n3", "n4"},
		ExitProbeOptions{URL: echoURL, Timeout: 5 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	for _, o := range outcomes {
		if !o.OK || o.ExitIP != "127.0.0.1" {
			t.Fatalf("回环拓扑下出口身份应为 127.0.0.1：%+v", o)
		}
	}
	if got := queryString(t, h.DB, `SELECT exit_ip FROM proxy_nodes WHERE runtime_name = 'n4'`); got.String != "127.0.0.1" {
		t.Fatalf("exit_ip 未落节点行：%v", got)
	}
	if n := countRows(t, h.DB, `SELECT COUNT(*) FROM health_observations WHERE level = 'L1' AND ok = 1`); n != 4 {
		t.Fatalf("L1 观测应 4 行，实际 %d", n)
	}
	_ = proxies
}

func TestExitIPCheckPoolFailureKeepsExitIP(t *testing.T) {
	h, _, _, _ := newHealthChecker(t, 0) // 无 lane → GLOBAL 串行路径
	seedNode(t, h.DB, "n1", NodeActive, 0, int64(1))
	if _, err := h.DB.Exec(`UPDATE proxy_nodes SET exit_ip = '9.9.9.9' WHERE runtime_name = 'n1'`); err != nil {
		t.Fatal(err)
	}
	// 混合口 0 不可能连通 → 目标请求失败归因，不得擦已有出口事实
	outcomes, err := h.ExitIPCheckPool(context.Background(), []string{"n1"}, ExitProbeOptions{Timeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	if outcomes[0].OK || outcomes[0].ExitIP != "" {
		t.Fatalf("应失败：%+v", outcomes[0])
	}
	if got := queryString(t, h.DB, `SELECT exit_ip FROM proxy_nodes WHERE runtime_name = 'n1'`); got.String != "9.9.9.9" {
		t.Fatalf("目标服务故障不得擦出口身份，实际 %v", got)
	}
	if state := queryString(t, h.DB, `SELECT state FROM proxy_nodes WHERE runtime_name = 'n1'`); state.String != NodeActive {
		t.Fatalf("L1 失败不得推进状态，实际 %s", state.String)
	}
}

// ── L2 业务准入 ──────────────────────────────────────────────────────────────

func TestBusinessCheckPoolLanePathPromotes(t *testing.T) {
	h, _, target, _ := newHealthChecker(t, 3)
	target.browseOK.Store(true)
	names := []string{"n1", "n2", "n3", "n4"}
	for _, name := range names {
		seedNode(t, h.DB, name, NodeNew, 0, int64(1))
	}
	outcomes, err := h.BusinessCheckPool(context.Background(), names,
		BusinessProbeOptions{URL: target.server.URL + "/IStoreBrowseService/GetItems/v1/", Timeout: 5 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	if len(outcomes) != 4 {
		t.Fatalf("应有 4 条 L2 结论，实际 %d", len(outcomes))
	}
	for _, o := range outcomes {
		if !o.OK || o.FinalPriceCents != 999 || o.State != NodeActive {
			t.Fatalf("业务成功应晋升 ACTIVE：%+v", o)
		}
	}
	if state := queryString(t, h.DB, `SELECT state FROM proxy_nodes WHERE runtime_name = 'n2'`); state.String != NodeActive {
		t.Fatalf("节点未晋升：%v", state)
	}
	if n := countRows(t, h.DB, `SELECT COUNT(*) FROM health_observations WHERE level = 'L2' AND ok = 1`); n != 4 {
		t.Fatalf("L2 观测应 4 行，实际 %d", n)
	}
}

func TestBusinessCommitTargetFailureNoPenalty(t *testing.T) {
	h, _, target, _ := newHealthChecker(t, 0)
	seedNode(t, h.DB, "n1", NodeActive, 2, int64(1))
	// Steam 侧 500（归因 target）：只记观测，不罚节点
	outcomes, err := h.BusinessCheckPool(context.Background(), []string{"n1"},
		BusinessProbeOptions{URL: target.server.URL + "/IStoreBrowseService/GetItems/v1/", Timeout: 3 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	o := outcomes[0]
	if o.OK || o.Attribution != "target" {
		t.Fatalf("应为 target 归因失败：%+v", o)
	}
	if state := queryString(t, h.DB, `SELECT state FROM proxy_nodes WHERE runtime_name = 'n1'`); state.String != NodeActive {
		t.Fatalf("目标服务故障不得罚节点，实际 %s", state.String)
	}
	if failures := queryInt(t, h.DB, `SELECT consecutive_failures FROM proxy_nodes WHERE runtime_name = 'n1'`); failures != 2 {
		t.Fatalf("失败计数不得累加，实际 %d", failures)
	}
}

func TestBusinessCommitTransportFailureFailsFast(t *testing.T) {
	h, _, _, _ := newHealthChecker(t, 0)
	seedNode(t, h.DB, "n1", NodeActive, 0, int64(1))
	// 经节点连不上（无归因、无状态码）= 节点失败证据：fail-fast 判死
	result := BusinessResult{RuntimeName: "n1",
		Detail: fmt.Sprintf("%s: ConnectTimeout", TargetServiceFail)}
	if _, err := h.commitBusinessWave(context.Background(), []struct {
		row    nodeRow
		result BusinessResult
	}{{row: nodeRow{ID: 1, NodeID: "fp-n1", State: NodeActive, SubscriptionID: sql.NullInt64{Int64: 1, Valid: true}}, result: result}}, time.Now(), "target", 0); err != nil {
		t.Fatal(err)
	}
	if state := queryString(t, h.DB, `SELECT state FROM proxy_nodes WHERE runtime_name = 'n1'`); state.String != NodeDead {
		t.Fatalf("传输失败应 fail-fast 判死，实际 %s", state.String)
	}
	if failures := queryInt(t, h.DB, `SELECT consecutive_failures FROM proxy_nodes WHERE runtime_name = 'n1'`); failures != 1 {
		t.Fatalf("失败计数应 +1，实际 %d", failures)
	}
}

// L2 写入失败全部回滚：观测插入中途炸掉（触发器 RAISE），同事务里已写的
// 状态推进必须一并消失——落账失败不能只写一半健康证据。
func TestBusinessCommitRollbackAll(t *testing.T) {
	db, gate, dir := newTestDB(t)
	if err := EnsureSchema(context.Background(), mustConn(t, db)); err != nil {
		t.Fatal(err)
	}
	conn := mustConn(t, db)
	if err := EnsureHealthSchema(context.Background(), conn); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`CREATE TRIGGER boom_on_obs BEFORE INSERT ON health_observations
		WHEN NEW.detail = 'BOOM' BEGIN SELECT RAISE(ABORT, '写入失败注入'); END`); err != nil {
		t.Fatal(err)
	}
	seedNode(t, db, "n1", NodeNew, 0, int64(1))
	seedNode(t, db, "n2", NodeNew, 0, int64(1))
	h := &HealthChecker{
		DB: db, Gate: gate, DataDir: dir,
		ControllerURL: "http://127.0.0.1:1", ConfigPath: filepath.Join(dir, "none.yaml"),
		Now: func() time.Time { return time.Now() },
	}
	probed := []struct {
		row    nodeRow
		result BusinessResult
	}{
		{row: nodeRow{ID: 1, NodeID: "fp-n1", State: NodeNew, SubscriptionID: sql.NullInt64{Int64: 1, Valid: true}},
			result: BusinessResult{RuntimeName: "n1", OK: true, Detail: "BUSINESS_OK"}},
		{row: nodeRow{ID: 2, NodeID: "fp-n2", State: NodeNew, SubscriptionID: sql.NullInt64{Int64: 1, Valid: true}},
			result: BusinessResult{RuntimeName: "n2", Detail: "BOOM"}},
	}
	if _, err := h.commitBusinessWave(context.Background(), probed, time.Now(), "target", 0); err == nil {
		t.Fatalf("注入的写入失败必须浮出")
	}
	// 整波回滚：n1 的晋升与 n1/n2 的观测行都不存在
	if state := queryString(t, db, `SELECT state FROM proxy_nodes WHERE runtime_name = 'n1'`); state.String != NodeNew {
		t.Fatalf("回滚后 n1 应保持 NEW，实际 %s", state.String)
	}
	if n := countRows(t, db, `SELECT COUNT(*) FROM health_observations`); n != 0 {
		t.Fatalf("回滚后不应有观测行，实际 %d", n)
	}
	// 写闸必须仍然可用（lease 正常释放，不悬挂）
	if err := gate.WithWrite(context.Background(), store.PriorityBackground, func(c *sql.Conn) error {
		_, err := c.ExecContext(context.Background(), `SELECT 1`)
		return err
	}); err != nil {
		t.Fatalf("回滚后写闸应可用：%v", err)
	}
}

// ── 手动体检落账（冷却语义）─────────────────────────────────────────────────

func TestIngestNodeCheckSuccessClearsCooldown(t *testing.T) {
	db, _, _ := newTestDB(t)
	conn := mustConn(t, db)
	if err := EnsureSchema(context.Background(), conn); err != nil {
		t.Fatal(err)
	}
	if err := EnsureHealthSchema(context.Background(), conn); err != nil {
		t.Fatal(err)
	}
	seedNode(t, db, "n1", NodeDead, 3, int64(1))
	if _, err := db.Exec(`UPDATE proxy_nodes SET cooldown_until = '2026-10-31 23:00:00.000000' WHERE runtime_name = 'n1'`); err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 10, 31, 12, 0, 0, 0, time.Local)
	result, err := IngestNodeCheck(context.Background(), conn, 1, []CheckRow{{
		Name: "n1", Alive: true, ExitIP: "1.2.3.4", MS: 88, Probed: true,
	}}, map[string]string{"n1": "fp-n1"}, now, 7, "")
	if err != nil {
		t.Fatal(err)
	}
	if result.Matched != 1 || result.Activated != 1 || result.ExitIPsAdded != 1 || !result.Changed() {
		t.Fatalf("账目不符：%+v", result)
	}
	var state string
	var failures int64
	var cooldown sql.NullString
	if err := db.QueryRow(`SELECT state, consecutive_failures, CAST(cooldown_until AS TEXT)
		FROM proxy_nodes WHERE runtime_name = 'n1'`).Scan(&state, &failures, &cooldown); err != nil {
		t.Fatal(err)
	}
	if state != NodeActive || failures != 0 || cooldown.Valid {
		t.Fatalf("成功证据应复活并清冷却：state=%s failures=%d cooldown=%v", state, failures, cooldown)
	}
	if n := countRows(t, db, `SELECT COUNT(*) FROM health_observations WHERE level='L2' AND ok=1 AND run_id=7`); n != 1 {
		t.Fatalf("应落 L2 成功观测，实际 %d", n)
	}
	if n := countRows(t, db, `SELECT COUNT(*) FROM health_observations WHERE level='L1' AND ok=1`); n != 1 {
		t.Fatalf("有出口 IP 应落 L1 观测，实际 %d", n)
	}
}

func TestIngestNodeCheckTransportFailureCooldownTiers(t *testing.T) {
	db, _, _ := newTestDB(t)
	conn := mustConn(t, db)
	if err := EnsureSchema(context.Background(), conn); err != nil {
		t.Fatal(err)
	}
	if err := EnsureHealthSchema(context.Background(), conn); err != nil {
		t.Fatal(err)
	}
	seedNode(t, db, "n1", NodeActive, 0, int64(1))
	now := time.Date(2026, 10, 31, 12, 0, 0, 0, time.Local)
	// 第 1 次传输失败：30 分钟冷却 + fail-fast 判死
	if _, err := IngestNodeCheck(context.Background(), conn, 1, []CheckRow{{
		Name: "n1", Alive: false, Probed: true,
	}}, map[string]string{"n1": "fp-n1"}, now, 0, ""); err != nil {
		t.Fatal(err)
	}
	var state string
	var failures int64
	var cooldown sql.NullString
	if err := db.QueryRow(`SELECT state, consecutive_failures, CAST(cooldown_until AS TEXT)
		FROM proxy_nodes WHERE runtime_name = 'n1'`).Scan(&state, &failures, &cooldown); err != nil {
		t.Fatal(err)
	}
	if state != NodeDead || failures != 1 {
		t.Fatalf("传输失败应判死并计 1 次：state=%s failures=%d", state, failures)
	}
	if !cooldown.Valid || !strings.HasPrefix(cooldown.String, "2026-10-31 12:30") {
		t.Fatalf("第 1 次失败应冷却 30 分钟，实际 %v", cooldown)
	}
	if n := countRows(t, db, `SELECT COUNT(*) FROM health_observations WHERE detail='CLASH_STEAM_TRANSPORT_FAILURE'`); n != 1 {
		t.Fatalf("应落传输失败观测，实际 %d", n)
	}
	// 第 2 次失败（失败计数已是 1）：升档 60 分钟
	if _, err := IngestNodeCheck(context.Background(), conn, 1, []CheckRow{{
		Name: "n1", Alive: false, Probed: true,
	}}, map[string]string{"n1": "fp-n1"}, now, 0, ""); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`SELECT CAST(cooldown_until AS TEXT) FROM proxy_nodes
		WHERE runtime_name = 'n1'`).Scan(&cooldown); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(cooldown.String, "2026-10-31 13:00") {
		t.Fatalf("第 2 次失败应冷却 60 分钟，实际 %v", cooldown)
	}
}

func TestIngestNodeCheckSteamResponseNoPenalty(t *testing.T) {
	db, _, _ := newTestDB(t)
	conn := mustConn(t, db)
	if err := EnsureSchema(context.Background(), conn); err != nil {
		t.Fatal(err)
	}
	if err := EnsureHealthSchema(context.Background(), conn); err != nil {
		t.Fatal(err)
	}
	seedNode(t, db, "n1", NodeActive, 2, int64(1))
	if _, err := db.Exec(`UPDATE proxy_nodes SET cooldown_until = '2026-10-31 23:00:00.000000' WHERE runtime_name = 'n1'`); err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 10, 31, 12, 0, 0, 0, time.Local)
	// 收到 Steam HTTP 错误响应（503）：不罚节点、清冷却、记 HTTP_ERROR 观测
	result, err := IngestNodeCheck(context.Background(), conn, 1, []CheckRow{{
		Name: "n1", Alive: false, SteamStatus: 503, HasSteam: true, Probed: true,
	}}, map[string]string{"n1": "fp-n1"}, now, 0, "")
	if err != nil {
		t.Fatal(err)
	}
	if result.Deactivated != 0 {
		t.Fatalf("Steam 响应在场不得罚节点：%+v", result)
	}
	var state string
	var failures int64
	var cooldown sql.NullString
	if err := db.QueryRow(`SELECT state, consecutive_failures, CAST(cooldown_until AS TEXT)
		FROM proxy_nodes WHERE runtime_name = 'n1'`).Scan(&state, &failures, &cooldown); err != nil {
		t.Fatal(err)
	}
	if state != NodeActive || failures != 2 || cooldown.Valid {
		t.Fatalf("HTTP 错误响应应只清冷却不罚：state=%s failures=%d cooldown=%v", state, failures, cooldown)
	}
	if n := countRows(t, db, `SELECT COUNT(*) FROM health_observations WHERE detail='CLASH_STEAM_HTTP_ERROR'`); n != 1 {
		t.Fatalf("应落 HTTP 错误观测，实际 %d", n)
	}
}

func TestIngestNodeCheckValidationAndSkip(t *testing.T) {
	db, _, _ := newTestDB(t)
	conn := mustConn(t, db)
	if err := EnsureSchema(context.Background(), conn); err != nil {
		t.Fatal(err)
	}
	if err := EnsureHealthSchema(context.Background(), conn); err != nil {
		t.Fatal(err)
	}
	seedNode(t, db, "n1", NodeActive, 0, int64(1))
	seedNode(t, db, "foreign", NodeActive, 0, int64(2))
	now := time.Now()
	// 未登记身份拒绝落账
	if _, err := IngestNodeCheck(context.Background(), conn, 1, []CheckRow{{
		Name: "ghost", Alive: true, Probed: true,
	}}, map[string]string{"ghost": "fp-missing"}, now, 0, ""); err == nil {
		t.Fatalf("未登记身份必须报错")
	}
	// 归属其他订阅拒绝落账
	if _, err := IngestNodeCheck(context.Background(), conn, 1, []CheckRow{{
		Name: "foreign", Alive: true, Probed: true,
	}}, map[string]string{"foreign": "fp-foreign"}, now, 0, ""); err == nil {
		t.Fatalf("跨订阅归属必须报错")
	}
	// probed=false 的行整条跳过，不产生证据
	if _, err := IngestNodeCheck(context.Background(), conn, 1, []CheckRow{{
		Name: "n1", Alive: false, Probed: false,
	}}, map[string]string{"n1": "fp-n1"}, now, 0, ""); err != nil {
		t.Fatal(err)
	}
	if n := countRows(t, db, `SELECT COUNT(*) FROM health_observations`); n != 0 {
		t.Fatalf("未探到的行不得落账，实际 %d 行", n)
	}
	if failures := queryInt(t, db, `SELECT consecutive_failures FROM proxy_nodes WHERE runtime_name='n1'`); failures != 0 {
		t.Fatalf("未探到的行不得累加失败，实际 %d", failures)
	}
}

// 502 归因：有响应体 = 目标服务；无响应体回探控制器定节点隧道成败。
func TestAttributeGateway502(t *testing.T) {
	ctx := context.Background()
	detail, attr := AttributeGateway502(ctx, "http://127.0.0.1:1", "", "n1", "http://t", "upstream says no", time.Second)
	if attr != "target" || !strings.HasPrefix(detail, TargetServiceFail) {
		t.Fatalf("有响应体应归因目标服务：%s %s", attr, detail)
	}
	// 控制器 delay 正常 → 通道通 → 目标服务
	controller := newStubController(t)
	detail, attr = AttributeGateway502(ctx, controller.server.URL, "", "n1", "http://t", "", time.Second)
	if attr != "target" {
		t.Fatalf("通道通时应归因目标服务：%s %s", attr, detail)
	}
	// 控制器 delay 也挂（有效失败）→ 节点隧道失败
	controller.delayFail.Store(500)
	detail, attr = AttributeGateway502(ctx, controller.server.URL, "", "n1", "http://t", "", time.Second)
	if attr != "node" || !strings.HasPrefix(detail, NodeTunnelFailed) {
		t.Fatalf("通道断应归因节点隧道：%s %s", attr, detail)
	}
	// 控制器不可达 → 归因不确定，不罚节点
	detail, attr = AttributeGateway502(ctx, "http://127.0.0.1:1", "", "n1", "http://t", "", time.Second)
	if attr != "uncertain" || !strings.HasPrefix(detail, UncertainFailure) {
		t.Fatalf("控制器不可达应归因不确定：%s %s", attr, detail)
	}
}
