package games

import (
	"context"
	"encoding/json"
	"path/filepath"
	"testing"

	"holdexar/server-go/internal/store"
)

func TestImportAppidsClassifies(t *testing.T) {
	db, err := store.Open(filepath.Join(t.TempDir(), "import.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	for _, stmt := range []string{
		`CREATE TABLE games (appid INTEGER PRIMARY KEY, updated_at TEXT)`,
		`CREATE TABLE game_current_prices (appid INTEGER, region_code TEXT)`,
	} {
		if _, err := db.Exec(stmt); err != nil {
			t.Fatal(err)
		}
	}
	seed := func(q string, args ...any) {
		t.Helper()
		if _, err := db.Exec(q, args...); err != nil {
			t.Fatal(err)
		}
	}
	seed(`INSERT INTO games (appid, updated_at) VALUES (1, '2026-10-30 10:00:00'), (2, NULL), (3, '2026-10-30 10:00:00'), (4, NULL)`)
	seed(`INSERT INTO game_current_prices (appid, region_code) VALUES (2, 'CN'), (3, 'US')`)

	result, err := ImportAppids(context.Background(), db, []any{
		int64(1),
		int64(2),
		int64(4),
		int64(99),
		"77",
		json.Number("88"), int64(0), "abc", int64(-5),
	})
	if err != nil {
		t.Fatal(err)
	}
	if result["ok"] != 4 || result["own"] != 2 || result["fail"] != 3 {
		t.Fatalf("计数=%v", result)
	}
	results := result["results"].([]map[string]any)
	want := []struct {
		appid  any
		status string
		detail string
	}{
		{int64(0), "fail", "AppID 无效"},
		{"abc", "fail", "AppID 无效"},
		{int64(-5), "fail", "AppID 无效"},
		{int64(1), "own", "已在库"},
		{int64(2), "own", "已在库"},
		{int64(4), "ok", "待首爬入库"},
		{int64(99), "ok", "待首爬入库"},
		{int64(77), "ok", "待首爬入库"},
		{int64(88), "ok", "待首爬入库"},
	}
	if len(results) != len(want) {
		t.Fatalf("结果条数=%d want %d", len(results), len(want))
	}
	for i, w := range want {
		got := results[i]
		if got["appid"] != w.appid || got["status"] != w.status || got["detail"] != w.detail {
			t.Fatalf("results[%d]=%v want %v", i, got, w)
		}
	}
}

func TestImportAppidsEmptyInput(t *testing.T) {
	db, err := store.Open(filepath.Join(t.TempDir(), "import.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	result, err := ImportAppids(context.Background(), db, nil)
	if err != nil {
		t.Fatal(err)
	}
	if result["ok"] != 0 || result["own"] != 0 || result["fail"] != 0 {
		t.Fatalf("空入参计数=%v", result)
	}
	if rows := result["results"].([]map[string]any); len(rows) != 0 {
		t.Fatalf("空入参不应有结果行：%v", rows)
	}
}
