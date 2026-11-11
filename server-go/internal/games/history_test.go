package games

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"holdexar/server-go/internal/crawler/dbwriter"
	"holdexar/server-go/internal/store"
)

func openHistoryFixture(t *testing.T) *sql.DB {
	t.Helper()
	db, e := store.Open(filepath.Join(t.TempDir(), "holdexar.db"))
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { db.Close() })
	if e = store.InitializeFreshDatabase(context.Background(), db); e != nil {
		t.Fatal(e)
	}
	exec := func(query string, args ...any) {
		t.Helper()
		if _, e := db.Exec(query, args...); e != nil {
			t.Fatalf("%s: %v", query, e)
		}
	}
	exec(`INSERT INTO games (appid, name, family_sharing, trading_cards, is_epic, is_hb, hl_flag, pp_flag, diff_fen, is_adult, is_visual_novel, removed_strikes, positive_reviews, review_count, view_count)
		VALUES (872410, 'T角洲行动', 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)`)
	// 官方观测：标准版序列 + 在售 sub 变体行；sale_sub=222 的行有非标准后缀，
	// 只经「标准 ∪ 在售 sub」并集进入缺省序列。
	history := []string{
		// 标准版：NULL product_unknown（旧行）与显式 0 并存
		`(872410, 'CN', 'CNY', 19900, 0, NULL, 0, NULL, 0, 'ok', 1990, '2026-01-01 10:00:00', 'steam', 0)`,
		`(872410, 'CN', 'CNY', 13900, 0, NULL, 0, NULL, 0, 'ok', 1390, '2026-02-01 10:00:00', 'steam', 0)`,
		// 在售 sub 行：非标准后缀 → 仅凭并集右支进入缺省序列
		`(872410, 'CN', 'CNY', 12900, 0, 222, 0, 'Deluxe Edition', 0, 'ok', 1290, '2026-03-01 10:00:00', 'steam', 0)`,
		// 变体行：非在售 sub 的具体版本 → 缺省序列不含
		`(872410, 'CN', 'CNY', 9900, 0, 333, 0, 'Gold Edition', 0, 'ok', 990, '2026-03-05 10:00:00', 'steam', 0)`,
		// 金色 / 捆绑行：永不进标准序列
		`(872410, 'CN', 'CNY', 500, 0, 333, 1, 'Gold Edition', 0, 'ok', 50, '2026-03-06 10:00:00', 'steam', 0)`,
		`(872410, 'CN', 'CNY', 100, 0, NULL, 0, NULL, 1, 'ok', 10, '2026-03-07 10:00:00', 'bundle', 0)`,
		// 未知身份行：独立序列
		`(872410, 'CN', 'CNY', 7700, 0, NULL, 0, NULL, 0, 'ok', 770, '2026-04-01 10:00:00', 'community', 1)`,
		`(872410, 'CN', 'CNY', 6600, 0, NULL, 0, NULL, 0, 'ok', 660, '2026-04-02 10:00:00', 'community', 1)`,
	}
	for _, row := range history {
		exec(`INSERT INTO game_price_history
			(appid, region_code, currency, price, discount_percent, sub_id, is_gold, version_suffix, is_bundle, price_status, cny_fen, snapshot_at, source, product_unknown)
			VALUES `+row)
	}
	exec(`INSERT INTO game_current_prices (appid, region_code, currency, price, discount_percent, price_status, fail_count, sub_id, updated_at)
		VALUES (872410, 'CN', 'CNY', 12900, 0, 'ok', 0, 222, '2026-04-03 10:00:00')`)
	return db
}

func historyGet(t *testing.T, db *sql.DB, backfill *HistoryBackfill, query string) (*httptest.ResponseRecorder, historyResponse) {
	t.Helper()
	mux := http.NewServeMux()
	Register(mux, db, backfill)
	url := "/api/v1/games/872410/history"
	if query != "" {
		url += "?" + query
	}
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest("GET", url, nil))
	if rec.Code != 200 {
		return rec, historyResponse{}
	}
	var resp historyResponse
	if e := json.Unmarshal(rec.Body.Bytes(), &resp); e != nil {
		t.Fatal(e)
	}
	return rec, resp
}

func TestHistorySeriesSelectionParity(t *testing.T) {
	db := openHistoryFixture(t)
	rec, resp := historyGet(t, db, nil, "")
	if rec.Code != 200 {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	if resp.Region != "cn" {
		t.Fatalf("region=%s", resp.Region)
	}
	// 缺省 = 标准版 ∪ 在售 sub：标准两行 + sub 222 一行；变体/金/捆绑/未知都不进。
	if len(resp.Points) != 3 {
		t.Fatalf("default series points=%d: %+v", len(resp.Points), resp.Points)
	}
	got := map[string]bool{}
	for _, p := range resp.Points {
		got[p.Timestamp] = true
	}
	for _, want := range []string{"2026-01-01T10:00:00", "2026-02-01T10:00:00", "2026-03-01T10:00:00"} {
		if !got[want] {
			t.Fatalf("missing %s in %+v", want, got)
		}
	}
	// 史低：最低 1290 分一次到达，无连续回踩。
	if resp.Lowest == nil || resp.Lowest.CnyFen != 1290 {
		t.Fatalf("lowest=%+v", resp.Lowest)
	}
	if resp.Highest == nil || resp.Highest.CnyFen != 1990 {
		t.Fatalf("highest=%+v", resp.Highest)
	}
	if resp.LowestHits != 1 {
		t.Fatalf("lowestHits=%d", resp.LowestHits)
	}
	// 版本枚举：未知行不进版本列表（is_gold 变体存在），unknown 显式条目殿后。
	products := map[string]int{}
	for _, v := range resp.Versions {
		products[v.Product]++
	}
	if products["standard"] != 1 || products["sub"] != 3 || products["unknown"] != 1 {
		t.Fatalf("versions=%+v", resp.Versions)
	}
	// 未知序列：只有两行未知点，永不混入标准版。
	rec, resp = historyGet(t, db, nil, "product=unknown")
	if rec.Code != 200 || len(resp.Points) != 2 {
		t.Fatalf("unknown series=%d status=%d", len(resp.Points), rec.Code)
	}
	if resp.Lowest == nil || resp.Lowest.CnyFen != 660 {
		t.Fatalf("unknown lowest=%+v", resp.Lowest)
	}
	// 指定 sub：只有该 sub 的行（333 含同包金色变体共两行）。
	rec, resp = historyGet(t, db, nil, "subId=333")
	if rec.Code != 200 || len(resp.Points) != 2 {
		t.Fatalf("subId=333 points=%d", len(resp.Points))
	}
	// 无效参数形状对齐 422。
	if rec, _ = historyGet(t, db, nil, "days=9999"); rec.Code != 422 {
		t.Fatalf("days=9999 status=%d", rec.Code)
	}
	if rec, _ = historyGet(t, db, nil, "product=standard"); rec.Code != 422 {
		t.Fatalf("product=standard status=%d", rec.Code)
	}
}

func TestHistoryDaysWindowAndLowestHits(t *testing.T) {
	db := openHistoryFixture(t)
	// 全历史下探回升形状（不依赖当前日期）：既有最低 1290 → 900 首达 →
	// 900 连续 → 950 回升 → 900 再达 = 两次史低事件。
	prices := []struct {
		ts  string
		fen int64
	}{
		{"2026-05-01 10:00:00", 10000},
		{"2026-05-02 10:00:00", 900},
		{"2026-05-03 10:00:00", 900},
		{"2026-05-04 10:00:00", 950},
		{"2026-05-05 10:00:00", 900},
	}
	for _, p := range prices {
		if _, e := db.Exec(`INSERT INTO game_price_history
			(appid, region_code, currency, price, discount_percent, sub_id, is_gold, version_suffix, is_bundle, price_status, cny_fen, snapshot_at, source, product_unknown)
			VALUES (872410, 'CN', 'CNY', ?, 0, NULL, 0, NULL, 0, 'ok', ?, ?, 'steam', 0)`, p.fen, p.fen, p.ts); e != nil {
			t.Fatal(e)
		}
	}
	_, resp := historyGet(t, db, nil, "")
	if resp.Lowest == nil || resp.Lowest.CnyFen != 900 {
		t.Fatalf("lowest=%+v", resp.Lowest)
	}
	if resp.LowestHits != 2 {
		t.Fatalf("lowestHits=%d", resp.LowestHits)
	}

	// days 窗口：相对当前北京时间造行，不依赖固定日期。
	now := time.Now().In(beijingZone)
	relative := []struct {
		offset time.Duration
		fen    int64
	}{
		{-96 * time.Hour, 5000},
		{-24 * time.Hour, 4000},
		{-1 * time.Hour, 3000},
	}
	for _, p := range relative {
		ts := now.Add(p.offset).Format("2006-01-02 15:04:05")
		if _, e := db.Exec(`INSERT INTO game_price_history
			(appid, region_code, currency, price, discount_percent, sub_id, is_gold, version_suffix, is_bundle, price_status, cny_fen, snapshot_at, source, product_unknown)
			VALUES (872410, 'CN', 'CNY', ?, 0, NULL, 0, NULL, 0, 'ok', ?, ?, 'steam', 0)`, p.fen, p.fen, ts); e != nil {
			t.Fatal(e)
		}
	}
	_, resp = historyGet(t, db, nil, "days=5")
	if len(resp.Points) != 3 {
		t.Fatalf("days=5 points=%d", len(resp.Points))
	}
	_, resp = historyGet(t, db, nil, "days=2")
	if len(resp.Points) != 2 {
		t.Fatalf("days=2 points=%d", len(resp.Points))
	}
	if !strings.HasPrefix(resp.Points[0].Timestamp, now.Add(-24*time.Hour).Format("2006-01-02")) {
		t.Fatalf("window start=%s", resp.Points[0].Timestamp)
	}
	if resp.Lowest == nil || resp.Lowest.CnyFen != 3000 {
		t.Fatalf("window lowest=%+v", resp.Lowest)
	}
}

type stubSource struct {
	observations []dbwriter.CommunityObservation
	err          error
	block        chan struct{}
	fetches      chan struct{}
}

func (s *stubSource) Fetch(ctx context.Context, appid int64, region string) ([]dbwriter.CommunityObservation, error) {
	if s.fetches != nil {
		s.fetches <- struct{}{}
	}
	if s.block != nil {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	return s.observations, s.err
}

func historyPost(t *testing.T, db *sql.DB, backfill *HistoryBackfill, url string) *httptest.ResponseRecorder {
	t.Helper()
	mux := http.NewServeMux()
	Register(mux, db, backfill)
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest("POST", url, nil))
	return rec
}

func TestBackfillEndpointWithoutSource(t *testing.T) {
	db := openHistoryFixture(t)
	backfill := NewHistoryBackfill(context.Background(), db, store.NewWriteGate(db, filepath.Join(t.TempDir(), "gate.db")), nil)
	rec := historyPost(t, db, backfill, "/api/v1/games/872410/history/backfill?region=cn")
	if rec.Code != 409 {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "社区历史数据源未安装") {
		t.Fatalf("detail=%s", rec.Body.String())
	}
	// 只读历史不受数据源缺失影响；brief=idle。
	_, resp := historyGet(t, db, backfill, "")
	if resp.Backfill.State != "idle" {
		t.Fatalf("brief=%+v", resp.Backfill)
	}
	// 卸载（置回 nil）后已合流历史保留。
	rec = historyPost(t, db, backfill, "/api/v1/games/872410/history/backfill?region=cn")
	if rec.Code != 409 {
		t.Fatalf("after uninstall status=%d", rec.Code)
	}
}

func TestBackfillDispatchStateAndInflight(t *testing.T) {
	db := openHistoryFixture(t)
	gate := store.NewWriteGate(db, filepath.Join(t.TempDir(), "gate.db"))
	backfill := NewHistoryBackfill(context.Background(), db, gate, nil)
	fetches := make(chan struct{}, 4)
	// 观测：一条与官方同日同价（2026-01-01 10:00 与官方 19900 同日价）→ 佐证
	// 去重跳过；一条新价 → 入库；一条未知身份 → 独立序列。
	bei := time.FixedZone("UTC+8", 8*3600)
	obs := []dbwriter.CommunityObservation{
		{SnapshotAt: time.Date(2026, 1, 1, 8, 0, 0, 0, bei), PriceMinor: int64Ptr(19900), Currency: "CNY", CnyFen: int64Ptr(19900)},
		{SnapshotAt: time.Date(2026, 1, 2, 8, 0, 0, 0, bei), PriceMinor: int64Ptr(18800), Currency: "CNY", CnyFen: int64Ptr(18800)},
		{SnapshotAt: time.Date(2026, 1, 3, 8, 0, 0, 0, bei), PriceMinor: int64Ptr(7700), Currency: "CNY", CnyFen: int64Ptr(7700), ProductUnknown: true},
	}
	source := &stubSource{observations: obs, fetches: fetches}
	backfill.SetSource(source)
	rec := historyPost(t, db, backfill, "/api/v1/games/872410/history/backfill?region=cn")
	if rec.Code != 200 {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var out struct {
		Started bool   `json:"started"`
		State   string `json:"state"`
	}
	if e := json.Unmarshal(rec.Body.Bytes(), &out); e != nil || !out.Started || out.State != "running" {
		t.Fatalf("start=%+v err=%v", out, e)
	}
	<-fetches
	// 轮询直到回填收尾（状态落库）。
	deadline := time.Now().Add(5 * time.Second)
	for {
		_, resp := historyGet(t, db, backfill, "")
		if resp.Backfill.State == "ok" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("brief never ok: %+v", resp.Backfill)
		}
		time.Sleep(20 * time.Millisecond)
	}
	// 只数本轮合流行（2026-01 窗口）：夹具里的 source='community' 行在 4 月，不混入。
	var points int
	if e := db.QueryRow(`SELECT count(*) FROM game_price_history WHERE source='community' AND snapshot_at LIKE '2026-01%'`).Scan(&points); e != nil {
		t.Fatal(e)
	}
	if points != 2 {
		t.Fatalf("community rows=%d（同日同价佐证点必须跳过）", points)
	}
	var unknown int
	if e := db.QueryRow(`SELECT count(*) FROM game_price_history WHERE source='community' AND snapshot_at LIKE '2026-01%' AND product_unknown=1`).Scan(&unknown); e != nil {
		t.Fatal(e)
	}
	if unknown != 1 {
		t.Fatalf("unknown rows=%d", unknown)
	}
	// 空批次 → empty 状态。
	backfill.SetSource(&stubSource{observations: []dbwriter.CommunityObservation{}})
	rec = historyPost(t, db, backfill, "/api/v1/games/872410/history/backfill?region=cn")
	if rec.Code != 200 {
		t.Fatalf("empty status=%d", rec.Code)
	}
	deadline = time.Now().Add(5 * time.Second)
	for {
		_, resp := historyGet(t, db, backfill, "")
		if resp.Backfill.State == "empty" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("brief never empty: %+v", resp.Backfill)
		}
		time.Sleep(20 * time.Millisecond)
	}
	// 失败批次 → error 状态带原因。
	backfill.SetSource(&stubSource{err: fmt.Errorf("上游拒绝")})
	rec = historyPost(t, db, backfill, "/api/v1/games/872410/history/backfill?region=cn")
	if rec.Code != 200 {
		t.Fatalf("error-run status=%d", rec.Code)
	}
	deadline = time.Now().Add(5 * time.Second)
	for {
		_, resp := historyGet(t, db, backfill, "")
		if resp.Backfill.State == "error" && resp.Backfill.LastError != nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("brief never error: %+v", resp.Backfill)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestBackfillInflightDedup(t *testing.T) {
	db := openHistoryFixture(t)
	backfill := NewHistoryBackfill(context.Background(), db, store.NewWriteGate(db, filepath.Join(t.TempDir(), "gate.db")), nil)
	// 阻塞型数据源：请求上下文不结束就不返回，制造稳定在飞窗口。
	cancel := context.AfterFunc(context.Background(), func() {})
	defer cancel()
	backfill.SetSource(&stubSource{block: make(chan struct{})})
	rec := historyPost(t, db, backfill, "/api/v1/games/872410/history/backfill?region=cn")
	if rec.Code != 200 {
		t.Fatalf("first status=%d", rec.Code)
	}
	var out struct {
		Started bool   `json:"started"`
		State   string `json:"state"`
	}
	if e := json.Unmarshal(rec.Body.Bytes(), &out); e != nil || !out.Started {
		t.Fatalf("first=%+v err=%v", out, e)
	}
	rec = historyPost(t, db, backfill, "/api/v1/games/872410/history/backfill?region=cn")
	if e := json.Unmarshal(rec.Body.Bytes(), &out); e != nil {
		t.Fatal(e)
	}
	if out.Started || out.State != "running" {
		t.Fatalf("second dispatch=%+v（同键在飞不得重复派发）", out)
	}
	_, resp := historyGet(t, db, backfill, "")
	if resp.Backfill.State != "running" {
		t.Fatalf("brief=%+v", resp.Backfill)
	}
	cancel()
}

func int64Ptr(v int64) *int64 { return &v }
