package wishlist_test

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/cookiejar"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/crawl"
	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/regions"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/steamhttp"
	"holdexar/server-go/internal/store"
	"holdexar/server-go/internal/storebrowse"
	"holdexar/server-go/internal/systemx"
	"holdexar/server-go/internal/wishlist"
)

func TestUserPoolAPIAutomaticallyFetchesAndPreservesDataOnRemove(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, e := store.Open(path)
	if e != nil {
		t.Fatal(e)
	}
	defer db.Close()
	if e = store.InitializeFreshDatabase(ctx, db); e != nil {
		t.Fatal(e)
	}
	gate := store.NewWriteGate(db, path)
	bus := events.NewBus()
	mon := monitoring.New(db, gate)
	pool := wishlist.NewPool(db, gate, mon)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		item := map[string]any{"appid": 620, "success": 1, "visible": true, "type": 0, "name": "传送门验证", "purchase_options": []any{map[string]any{"packageid": "400", "purchase_option_name": "传送门验证", "final_price_in_cents": "1499", "original_price_in_cents": "2999", "discount_pct": "50"}}}
		_ = json.NewEncoder(w).Encode(map[string]any{"response": map[string]any{"store_items": []any{item}}})
	}))
	defer upstream.Close()
	steam := steamhttp.New(steamhttp.Config{MaxRetries: 1})
	runtime := crawl.New(db, gate, bus, crawl.Options{Fetch: func(ctx context.Context, ids []int64, cc, lang string, extras bool) (map[int64]storebrowse.Item, error) {
		batch, e := storebrowse.FetchAt(ctx, steam, upstream.URL, ids, cc, lang, extras, "")
		return batch.Items, e
	}, AfterJob: func(ctx context.Context, ids []int64) error { _, e := pool.ReleaseFree(ctx, ids); return e }})
	defer func() {
		cancel()
		closeCtx, cancelClose := context.WithTimeout(context.Background(), time.Second)
		defer cancelClose()
		if e := runtime.Close(closeCtx); e != nil {
			t.Error(e)
		}
	}()
	pool.OnAdded = func(_ context.Context, ids []int64) (bool, error) {
		_, e := runtime.StartJob(ctx, "appids", ids, nil, "pool_add", nil)
		return e == nil, e
	}
	mux := http.NewServeMux()
	pool.Register(mux)
	mon.Register(mux)
	runtime.Register(mux, ctx)
	regions.Register(mux, db, regions.Options{Gate: gate, Settings: settings.Options{Bus: bus}})
	settings.Register(mux, db, gate, settings.Options{Bus: bus})
	security := access.New()
	systemx.RegisterSession(mux, security, true)
	server := httptest.NewServer(security.Middleware(mux))
	defer server.Close()
	jar, _ := cookiejar.New(nil)
	client := &http.Client{Jar: jar, Timeout: 5 * time.Second}
	request := func(method, path, body string) (int, any) {
		t.Helper()
		req, e := http.NewRequest(method, server.URL+"/api/v1"+path, strings.NewReader(body))
		if e != nil {
			t.Fatal(e)
		}
		if body != "" {
			req.Header.Set("Content-Type", "application/json")
		}
		res, e := client.Do(req)
		if e != nil {
			t.Fatal(e)
		}
		defer res.Body.Close()
		var decoded any
		if e = json.NewDecoder(res.Body).Decode(&decoded); e != nil {
			t.Fatal(e)
		}
		return res.StatusCode, decoded
	}
	code, _ := request("POST", "/pool/items", `{"appids":[620]}`)
	if code == 200 {
		t.Fatal("unauthenticated pool mutation accepted")
	}
	if code, value := request("GET", "/session/dev", ""); code != 200 {
		t.Fatalf("session %d %v", code, value)
	}
	for _, r := range []struct{ path, body string }{{"/regions/enabled", `{"enabled":["CN"]}`}, {"/settings/fetch", `{"queue_discounts":false,"catalog_refresh":false,"price_interval_hours":1}`}} {
		if code, value := request("PUT", r.path, r.body); code != 200 {
			t.Fatalf("config %d %v", code, value)
		}
	}
	code, value := request("POST", "/pool/items", `{"appids":[620],"source":"受控导入.txt"}`)
	if code != 200 || value.(map[string]any)["added"] != float64(1) || value.(map[string]any)["crawlTriggered"] != true {
		t.Fatalf("add %d %v", code, value)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		_, active := request("GET", "/crawl/active", "")
		if active.(map[string]any)["busy"] == false {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("added game did not complete first crawl")
		}
		time.Sleep(time.Millisecond)
	}
	_, value = request("GET", "/wishlist", "")
	items := value.([]any)
	if len(items) != 1 {
		t.Fatalf("items=%v", value)
	}
	item := items[0].(map[string]any)
	if item["name"] != "传送门验证" || item["manualPool"] != true || len(item["steamids"].([]any)) != 0 {
		t.Fatalf("item=%v", item)
	}
	_, value = request("GET", "/crawl/jobs", "")
	jobs := value.([]any)
	if len(jobs) != 1 || jobs[0].(map[string]any)["kind"] != "pool_add" || jobs[0].(map[string]any)["status"] != "done" {
		t.Fatalf("jobs=%v", value)
	}
	for _, method := range []string{"PUT", "DELETE"} {
		if code, value := request(method, "/follows/620", ""); code != 200 {
			t.Fatalf("follow %d %v", code, value)
		}
	}
	_, value = request("GET", "/monitoring/state?target_id=620", "")
	if value.(map[string]any)["state"] != "active" {
		t.Fatalf("unfollow removed manual membership=%v", value)
	}
	if code, value := request("POST", "/pool/items/remove", `{"appids":[620]}`); code != 200 || value.(map[string]any)["removed"] != float64(1) {
		t.Fatalf("remove %d %v", code, value)
	}
	_, value = request("GET", "/wishlist", "")
	if len(value.([]any)) != 0 {
		t.Fatalf("removed still visible=%v", value)
	}
	var price, catalog, history, accounts int
	for query, target := range map[string]*int{"SELECT price FROM game_current_prices WHERE appid=620 AND region_code='CN'": &price, "SELECT count(*) FROM games WHERE appid=620": &catalog, "SELECT count(*) FROM game_price_history WHERE appid=620": &history, "SELECT count(*) FROM wishlist_items": &accounts} {
		if e = db.QueryRow(query).Scan(target); e != nil && e != sql.ErrNoRows {
			t.Fatal(e)
		}
	}
	if price != 1499 || catalog != 1 || history != 1 || accounts != 0 {
		t.Fatalf("price=%d catalog=%d history=%d account rows=%d", price, catalog, history, accounts)
	}
}
