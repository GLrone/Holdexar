package regions

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"holdexar/server-go/internal/store"
)

func writeFixture(t *testing.T) (*sql.DB, http.Handler) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, e := store.Open(path)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { db.Close() })
	if e = store.InitializeFreshDatabase(context.Background(), db); e != nil {
		t.Fatal(e)
	}
	mux := http.NewServeMux()
	Register(mux, db, Options{Gate: store.NewWriteGate(db, path)})
	return db, mux
}

func TestRegionWritesHaveExactEmptyAndFollowSemantics(t *testing.T) {
	db, handler := writeFixture(t)
	for _, tc := range []struct {
		path, body string
		code       int
	}{{"enabled", `{"enabled":[" CN ","cn"]}`, 200}, {"owned", `{"regions":[" US ","cn","us"]}`, 200}, {"owned", `{"regions":[]}`, 400}, {"enabled", `{"enabled":["unknown"]}`, 400}, {"enabled", `{"enabled":[]}`, 200}, {"owned", `{"regions":null}`, 200}} {
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest("PUT", "/api/v1/regions/"+tc.path, strings.NewReader(tc.body)))
		if response.Code != tc.code {
			t.Fatalf("%s %s status=%d body=%s", tc.path, tc.body, response.Code, response.Body.String())
		}
	}
	var count int
	_ = db.QueryRow("SELECT count(*) FROM crawl_regions WHERE enabled=1").Scan(&count)
	if count != 0 {
		t.Fatalf("disabled regions silently re-enabled=%d", count)
	}
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest("GET", "/api/v1/regions", nil))
	var value map[string]any
	if e := json.Unmarshal(response.Body.Bytes(), &value); e != nil {
		t.Fatal(e)
	}
	if value["ownedRegions"] != nil {
		t.Fatalf("follow not null=%v", value)
	}
}

func TestRegionWriteRollsBackWhenBundleProjectionCannotRefresh(t *testing.T) {
	db, handler := writeFixture(t)
	if _, e := db.Exec(`INSERT INTO bundles(bundle_id,name,must_purchase_as_set,item_kind,is_lowest,view_count) VALUES(100,'bundle',0,0,0,0); CREATE TRIGGER reject_bundle BEFORE UPDATE ON bundles BEGIN SELECT RAISE(ABORT,'controlled failure'); END`); e != nil {
		t.Fatal(e)
	}
	var before int
	_ = db.QueryRow("SELECT count(*) FROM crawl_regions WHERE enabled=1").Scan(&before)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest("PUT", "/api/v1/regions/enabled", strings.NewReader(`{"enabled":["us"]}`)))
	if response.Code != 500 {
		t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
	}
	var after int
	_ = db.QueryRow("SELECT count(*) FROM crawl_regions WHERE enabled=1").Scan(&after)
	if before != after {
		t.Fatalf("region commit escaped failed bundle update before=%d after=%d", before, after)
	}
}
