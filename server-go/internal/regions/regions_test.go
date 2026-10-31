package regions

import (
	"database/sql"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	_ "modernc.org/sqlite"
)

func newTestDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatalf("打开测试库失败：%v", err)
	}
	db.SetMaxOpenConns(1)
	t.Cleanup(func() { db.Close() })
	schema := `
CREATE TABLE crawl_regions (
    code VARCHAR(10) NOT NULL PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    currency VARCHAR(10) NOT NULL,
    enabled BOOLEAN NOT NULL,
    sort INTEGER NOT NULL,
    updated_at DATETIME
);
CREATE TABLE app_settings (
    key VARCHAR(64) NOT NULL PRIMARY KEY,
    value_json TEXT,
    updated_at DATETIME
);`
	if _, err := db.Exec(schema); err != nil {
		t.Fatalf("建表失败：%v", err)
	}
	return db
}

func seedRegions(t *testing.T, db *sql.DB, rows [][]any) {
	t.Helper()
	for _, row := range rows {
		if _, err := db.Exec(
			"INSERT INTO crawl_regions (code, name, currency, enabled, sort, updated_at) VALUES (?, ?, ?, ?, ?, NULL)",
			row...); err != nil {
			t.Fatalf("种子写入失败：%v", err)
		}
	}
}

func do(t *testing.T, h http.Handler, method, target string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, target, nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestListRegionsOrderAndShape(t *testing.T) {
	mux := http.NewServeMux()
	db := newTestDB(t)
	Register(mux, db)
	seedRegions(t, db, [][]any{
		{"us", "美国", "USD", 1, 1},
		{"ru", "俄罗斯", "RUB", 0, 2},
		{"cn", "中国", "CNY", 1, 0},
	})
	rec := do(t, mux, http.MethodGet, "/api/v1/regions")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	want := `{"regions":[{"code":"cn","name":"中国","currency":"CNY","enabled":true,"sort":0},` +
		`{"code":"us","name":"美国","currency":"USD","enabled":true,"sort":1},` +
		`{"code":"ru","name":"俄罗斯","currency":"RUB","enabled":false,"sort":2}],"ownedRegions":null}`
	if got := rec.Body.String(); got != want {
		t.Fatalf("响应体不一致\n got: %s\nwant: %s", got, want)
	}
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		t.Fatalf("Content-Type = %q", ct)
	}
}

func TestListRegionsEmpty(t *testing.T) {
	mux := http.NewServeMux()
	Register(mux, newTestDB(t))
	rec := do(t, mux, http.MethodGet, "/api/v1/regions")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	want := `{"regions":[],"ownedRegions":null}`
	if got := rec.Body.String(); got != want {
		t.Fatalf("响应体不一致\n got: %s\nwant: %s", got, want)
	}
}

func TestListRegionsOwnedVariants(t *testing.T) {
	cases := []struct {
		name string
		json string // app_settings value_json 原文；"" 表示不写行
		want string
	}{
		{"无行", "", `null`},
		{"SQL null", `NULL`, `null`},
		{"空列表", `[]`, `null`},
		{"非列表", `"cn"`, `null`},
		{"归一化", `["CN", " Ru ", "jp"]`, `["cn","ru","jp"]`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			mux := http.NewServeMux()
			db := newTestDB(t)
			Register(mux, db)
			if tc.json == "" {
				// 不写行
			} else if tc.json == "NULL" {
				if _, err := db.Exec(
					"INSERT INTO app_settings (key, value_json, updated_at) VALUES (?, NULL, NULL)",
					ownedRegionsKey); err != nil {
					t.Fatalf("种子写入失败：%v", err)
				}
			} else if _, err := db.Exec(
				"INSERT INTO app_settings (key, value_json, updated_at) VALUES (?, ?, NULL)",
				ownedRegionsKey, tc.json); err != nil {
				t.Fatalf("种子写入失败：%v", err)
			}
			rec := do(t, mux, http.MethodGet, "/api/v1/regions")
			if rec.Code != http.StatusOK {
				t.Fatalf("状态码 = %d, want 200", rec.Code)
			}
			if !strings.HasSuffix(rec.Body.String(), `,"ownedRegions":`+tc.want+`}`) {
				t.Fatalf("ownedRegions 段不一致：%s", rec.Body.String())
			}
		})
	}
}

func TestListRegionsMethodFallback(t *testing.T) {
	mux := http.NewServeMux()
	Register(mux, newTestDB(t))
	rec := do(t, mux, http.MethodPost, "/api/v1/regions")
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("状态码 = %d, want 405", rec.Code)
	}
	if allow := rec.Header().Get("Allow"); allow != "GET" {
		t.Fatalf("Allow = %q, want GET", allow)
	}
	if got := rec.Body.String(); got != `{"detail":"Method Not Allowed"}` {
		t.Fatalf("响应体 = %s", got)
	}
}
