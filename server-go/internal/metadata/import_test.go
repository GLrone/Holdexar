package metadata

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"holdexar/server-go/internal/store"
)

func metaFixture(t *testing.T) (*sql.DB, *store.WriteGate, string) {
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "holdexar.db")
	db, err := store.Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := store.InitializeFreshDatabase(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	seed := func(appids ...int64) {
		t.Helper()
		for _, appid := range appids {
			if _, err := db.Exec(`INSERT INTO games(appid,name,type,family_sharing,trading_cards,is_epic,is_hb,
				hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count,updated_at)
				VALUES(?,'夹具','game',0,0,0,0,0,0,0,0,0,0,0,0,0,'2026-11-14 10:00:00')`, appid); err != nil {
				t.Fatal(err)
			}
		}
	}
	seed(220, 300, 400)
	return db, store.NewWriteGate(db, filepath.Join(dir, "gate.lock")), dir
}

func routeFixture(t *testing.T, db *sql.DB, gate *store.WriteGate, dataDir string) func(string, string) *httptest.ResponseRecorder {
	t.Helper()
	mux := http.NewServeMux()
	Register(mux, db, gate, WithDataDir(dataDir))
	return func(method, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, "/api/v1/metadata/import", strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)
		return rec
	}
}

func seedEpicFile(t *testing.T, dir string) {
	t.Helper()
	body := `{"applist":{"apps":[{"appid":"220","free_period":"2026-11-01~2026-11-08"},{"appid":"999999","free_period":null}]}}`
	if err := os.WriteFile(filepath.Join(dir, epicFile), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func seedXGPFiles(t *testing.T, dir string) {
	t.Helper()
	tiered := `{"PC Collection":{"Core":[{"appid":220},{"name":"Fixture Game (PC)"}]},"Console Collection":{"Gold":[{"appid":220,"note":"dup"}]}}`
	if err := os.WriteFile(filepath.Join(dir, xgpTiered), []byte(tiered), 0o644); err != nil {
		t.Fatal(err)
	}
	links := `[{"game_name":"Fixture Game","steam_appid":"300"}]`
	if err := os.WriteFile(filepath.Join(dir, xgpLinks), []byte(links), 0o644); err != nil {
		t.Fatal(err)
	}
}

func seedHBFile(t *testing.T, dir string) {
	t.Helper()
	path := filepath.Join(dir, historicalDB)
	if err := os.Remove(path); err == nil {
		_ = path
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec(`CREATE TABLE game_lows (appid INTEGER, humble_choice TEXT);
		INSERT INTO game_lows VALUES (220, 'Humble Choice (Nov 2026) extras');
		INSERT INTO game_lows VALUES (400, 'Humble Choice (Sep 2025)');
		INSERT INTO game_lows VALUES (500, 'no marker here');`); err != nil {
		t.Fatal(err)
	}
}

func TestImportEpic(t *testing.T) {
	db, gate, dir := metaFixture(t)
	seedEpicFile(t, dir)
	result := ImportEpic(context.Background(), db, gate, dir)
	if result["ok"] != true || result["inSource"] != 2 || result["updated"] != 1 {
		t.Fatal(result)
	}
	var isEpic int
	var epicDate sql.NullString
	if err := db.QueryRow(`SELECT is_epic, epic_date FROM games WHERE appid=220`).Scan(&isEpic, &epicDate); err != nil {
		t.Fatal(err)
	}
	if isEpic != 1 || epicDate.String != "2026-11-01~2026-11-08" {
		t.Fatal(isEpic, epicDate.String)
	}
	var count int
	if err := db.QueryRow(`SELECT COUNT(*) FROM games WHERE appid=999999`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatal("库外 appid 不得入库")
	}
}

func TestImportXGP(t *testing.T) {
	db, gate, dir := metaFixture(t)
	seedXGPFiles(t, dir)
	result := ImportXGP(context.Background(), db, gate, dir)
	if result["ok"] != true || result["updated"] != 2 || result["nameMatched"] != 1 {
		t.Fatal(result)
	}
	var tier string
	if err := db.QueryRow(`SELECT xgp_tier FROM games WHERE appid=220`).Scan(&tier); err != nil {
		t.Fatal(err)
	}
	if tier != "Core" {
		t.Fatalf("PC Collection 应优先：%q", tier)
	}
	if err := db.QueryRow(`SELECT xgp_tier FROM games WHERE appid=300`).Scan(&tier); err != nil {
		t.Fatal(err)
	}
	if tier != "Core" {
		t.Fatalf("名称匹配应落 300：%q", tier)
	}
}

func TestImportHB(t *testing.T) {
	db, gate, dir := metaFixture(t)
	seedHBFile(t, dir)
	result := ImportHB(context.Background(), db, gate, dir)
	if result["ok"] != true || result["inSource"] != 2 || result["updated"] != 2 {
		t.Fatal(result)
	}
	var label string
	if err := db.QueryRow(`SELECT hb_data FROM games WHERE appid=220`).Scan(&label); err != nil {
		t.Fatal(err)
	}
	if label != "HB慈善包26年11月包" {
		t.Fatalf("月份标注不符：%q", label)
	}
	if err := db.QueryRow(`SELECT hb_data FROM games WHERE appid=400`).Scan(&label); err != nil {
		t.Fatal(err)
	}
	if label != "HB慈善包25年9月包" {
		t.Fatalf("九月标注不符：%q", label)
	}
}

func TestImportHBNoColumn(t *testing.T) {
	db, gate, dir := metaFixture(t)
	path := filepath.Join(dir, historicalDB)
	hb, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := hb.Exec(`CREATE TABLE game_lows (appid INTEGER)`); err != nil {
		t.Fatal(err)
	}
	hb.Close()
	result := ImportHB(context.Background(), db, gate, dir)
	if result["ok"] != false || result["error"] != "game_lows 无 humble_choice 列" {
		t.Fatal(result)
	}
}

func TestImportRouteSemantics(t *testing.T) {
	db, gate, dir := metaFixture(t)
	do := routeFixture(t, db, gate, dir)
	rec := do("POST", "")
	if rec.Code != 422 {
		t.Fatalf("缺 source 应 422：%d %s", rec.Code, rec.Body.String())
	}
	rec = do("POST", `{"source": 123}`)
	if rec.Code != 422 {
		t.Fatalf("source 非串应 422：%d", rec.Code)
	}
	rec = do("POST", `{"source": "wat"}`)
	if rec.Code != 200 {
		t.Fatalf("未知来源应 200 error：%d", rec.Code)
	}
	body := decodeMeta(t, rec)
	if body["error"] != "未知来源: wat（可选 epic/xgp/hb/all）" {
		t.Fatal(body)
	}
	for _, rejected := range []string{`Z:\nope`, filepath.Join(dir, "..", "escape")} {
		rec = do("POST", `{"source": "epic", "source_dir": `+jsonString(rejected)+`}`)
		body = decodeMeta(t, rec)
		if rec.Code != 422 || body["detail"] != ErrSourceDirRejected.Error() {
			t.Fatalf("越界 source_dir %q 应 422：%d %s", rejected, rec.Code, rec.Body.String())
		}
	}
	rec = do("POST", `{"source": "epic", "source_dir": `+jsonString(filepath.Join(dir, "missing_sub"))+`}`)
	body = decodeMeta(t, rec)
	if rec.Code != 200 || body["ok"] != false || !hasPrefix(body["error"], "找不到数据文件: ") {
		t.Fatalf("目录内缺文件形状不符：%d %s", rec.Code, rec.Body.String())
	}
	seedEpicFile(t, dir)
	rec = do("POST", `{"source": "epic", "source_dir": `+jsonString(dir)+`}`)
	body = decodeMeta(t, rec)
	if body["ok"] != true || body["updated"] != 1.0 {
		t.Fatal(body)
	}
	seedXGPFiles(t, dir)
	seedHBFile(t, dir)
	rec = do("POST", `{"source": "all", "source_dir": `+jsonString(dir)+`}`)
	body = decodeMeta(t, rec)
	results, _ := body["results"].([]any)
	if len(results) != 3 {
		t.Fatalf("all 应聚合三源：%s", rec.Body.String())
	}
	sources := []string{}
	for _, item := range results {
		entry, _ := item.(map[string]any)
		source, _ := entry["source"].(string)
		sources = append(sources, source)
	}
	if fmt.Sprint(sources) != "[epic xgp hb]" {
		t.Fatalf("all 顺序应 epic→xgp→hb：%v", sources)
	}
}

func jsonString(value string) string {
	encoded, _ := json.Marshal(value)
	return string(encoded)
}

func hasPrefix(value any, prefix string) bool {
	text, _ := value.(string)
	return len(text) >= len(prefix) && text[:len(prefix)] == prefix
}

func decodeMeta(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("响应不是 JSON：%v（%s）", err, rec.Body.String())
	}
	return out
}
