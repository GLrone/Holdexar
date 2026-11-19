//go:build windows

package settings

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"holdexar/server-go/internal/store"
)

const sampleLoginusers = `// Steam account records
"users"
{
	"76561198000000011"
	{
		"AccountName"		"probe_one"
		"PersonaName"		"探针一号"
		"RememberPassword"		"1"
	}
	"not_a_steamid"
	{
		"PersonaName"		"忽略"
	}
	"76561198000000012"
	{
		"PersonaName"		"probe\"two\\x"
	}
}
"something_else"
{
	"nested"
	{
		"deeper" "value"
	}
}
`

func TestParseLoginusers(t *testing.T) {
	accounts := parseLoginusers(sampleLoginusers)
	if len(accounts) != 2 {
		t.Fatalf("应出 2 个账号：%v", accounts)
	}
	if accounts[0].SteamID != "76561198000000011" || accounts[0].PersonaName != "探针一号" {
		t.Fatalf("首账号不符：%v", accounts[0])
	}
	if accounts[1].PersonaName != `probe"two\x` {
		t.Fatalf("转义解码不符：%q", accounts[1].PersonaName)
	}
	if got := parseLoginusers(`"users" { "broken" { "PersonaName" "no-close`); got != nil {
		t.Fatalf("块结构残缺应回空：%v", got)
	}
	if got := parseLoginusers("no users block here"); got != nil {
		t.Fatalf("无 users 块应回空：%v", got)
	}
}

func steamRouteFixture(t *testing.T, loginusers string) (*sql.DB, *store.WriteGate, func(method, path, body string) *httptest.ResponseRecorder) {
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
	gate := store.NewWriteGate(db, dbPath)
	prevResolve, prevRoots := resolveDataDir, steamRoots
	resolveDataDir = func() string { return dir }
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "steam.exe"), []byte("stub"), 0o644); err != nil {
		t.Fatal(err)
	}
	if loginusers != "" {
		if err := os.MkdirAll(filepath.Join(root, "config"), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(root, "config", "loginusers.vdf"), []byte(loginusers), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	steamRoots = func() []string { return []string{root} }
	t.Cleanup(func() { resolveDataDir, steamRoots = prevResolve, prevRoots })
	mux := http.NewServeMux()
	Register(mux, db, gate)
	do := func(method, path, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)
		return rec
	}
	return db, gate, do
}

func TestSteamLocalListAndImport(t *testing.T) {
	db, _, do := steamRouteFixture(t, sampleLoginusers)
	rec := do("GET", "/api/v1/settings/steam-local-accounts", "")
	if rec.Code != 200 {
		t.Fatalf("列表应 200：%d %s", rec.Code, rec.Body.String())
	}
	var payload struct {
		Accounts []struct {
			SteamID     string `json:"steam_id"`
			PersonaName string `json:"persona_name"`
		} `json:"accounts"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if len(payload.Accounts) != 2 || payload.Accounts[0].PersonaName != "探针一号" {
		t.Fatalf("列表形状不符：%s", rec.Body.String())
	}
	rec = do("POST", "/api/v1/settings/steam-local-accounts/import", `{"steam_id": "76561198000000011"}`)
	if rec.Code != 200 {
		t.Fatalf("导入应 200：%d %s", rec.Code, rec.Body.String())
	}
	imported := decodeSteam(t, rec)
	if imported["steam_id"] != "76561198000000011" || imported["persona_name"] != "探针一号" {
		t.Fatal(imported)
	}
	var stored string
	if err := db.QueryRow(`SELECT value_json FROM app_settings WHERE key='account.steam_id'`).Scan(&stored); err != nil {
		t.Fatal(err)
	}
	if stored != `"76561198000000011"` {
		t.Fatalf("KV 应存 JSON 字符串：%s", stored)
	}
}

func TestSteamLocalImportGuards(t *testing.T) {
	_, _, do := steamRouteFixture(t, sampleLoginusers)
	for _, body := range []string{`{"steam_id": "123"}`, `{}`, `{"steam_id": 76561198000000011}`} {
		rec := do("POST", "/api/v1/settings/steam-local-accounts/import", body)
		if rec.Code != 422 {
			t.Fatalf("%s 应 422：%d %s", body, rec.Code, rec.Body.String())
		}
	}
	rec := do("POST", "/api/v1/settings/steam-local-accounts/import", `{"steam_id": "76561111111111111"}`)
	if rec.Code != 404 || !strings.Contains(rec.Body.String(), "本机 Steam 账号记录已不可用") {
		t.Fatalf("缺席账号应 404：%d %s", rec.Code, rec.Body.String())
	}
	_, _, doEmpty := steamRouteFixture(t, "")
	rec = doEmpty("GET", "/api/v1/settings/steam-local-accounts", "")
	empty := decodeSteam(t, rec)
	if list, ok := empty["accounts"].([]any); !ok || len(list) != 0 {
		t.Fatalf("无登录文件应空列表：%s", rec.Body.String())
	}
}

func decodeSteam(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("响应不是 JSON：%v（%s）", err, rec.Body.String())
	}
	return out
}
