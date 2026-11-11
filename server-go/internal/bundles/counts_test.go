package bundles

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/store"
)

func TestBundleCountsRefreshAndFreshness(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err = store.InitializeFreshDatabase(ctx, db); err != nil {
		t.Fatal(err)
	}
	if _, err = db.Exec(`INSERT INTO games(appid,name,bundle_count,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,
		diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count)
		VALUES (220,'Test',9,0,0,0,0,0,0,0,0,0,0,0,0,0),(221,'Absent',3,0,0,0,0,0,0,0,0,0,0,0,0,0)`); err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 11, 9, 14, 0, 0, 0, sched.DefaultLoc)
	service := NewCounts(db, store.NewWriteGate(db, path))
	service.now = func() time.Time { return now }
	requests := 0
	service.fetch = func(context.Context) (map[int64]int64, error) {
		requests++
		return map[int64]int64{220: 5, 999999: 7}, nil
	}
	refresh := func(force bool, wantUpdated int64) {
		t.Helper()
		result, err := service.Refresh(ctx, force)
		if err != nil || result["updated"] != wantUpdated {
			t.Fatalf("refresh=%v err=%v", result, err)
		}
	}
	refresh(false, 1)
	result, err := service.Refresh(ctx, false)
	if err != nil || result["skipped"] != true || requests != 1 {
		t.Fatalf("freshness result=%v requests=%d err=%v", result, requests, err)
	}
	refresh(true, 0)
	var got, absent, placeholders int
	if err = db.QueryRow(`SELECT (SELECT bundle_count FROM games WHERE appid=220),
		(SELECT bundle_count FROM games WHERE appid=221),(SELECT count(*) FROM games WHERE appid=999999)`).Scan(&got, &absent, &placeholders); err != nil || got != 5 || absent != 3 || placeholders != 0 {
		t.Fatalf("count=%d absent=%d placeholders=%d err=%v", got, absent, placeholders, err)
	}
	old, err := service.last(ctx)
	if err != nil {
		t.Fatal(err)
	}
	now = now.Add(48 * time.Hour)
	service.fetch = func(context.Context) (map[int64]int64, error) { return nil, errors.New("HTTP 503") }
	mux := http.NewServeMux()
	service.Register(mux)
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/api/v1/metadata/bundles/refresh", nil))
	var failure map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &failure); err != nil || response.Code != 200 || failure["ok"] != false || failure["error"] != "HTTP 503" {
		t.Fatalf("status=%d body=%v err=%v", response.Code, failure, err)
	}
	after, err := service.last(ctx)
	if err != nil || after.FetchedAt != old.FetchedAt {
		t.Fatalf("failed refresh moved success time from %v to %v, err=%v", old, after, err)
	}
	service.fetch = func(context.Context) (map[int64]int64, error) { return map[int64]int64{220: 6}, nil }
	if _, err = db.Exec(`CREATE TRIGGER block_bundle_watermark BEFORE UPDATE ON app_settings
		WHEN NEW.key='bartervg_bundles_last_fetch' BEGIN SELECT RAISE(ABORT,'test rejection'); END`); err != nil {
		t.Fatal(err)
	}
	if _, err = service.Refresh(ctx, false); err == nil {
		t.Fatal("expected transactional write to fail")
	}
	if err = db.QueryRow(`SELECT bundle_count FROM games WHERE appid=220`).Scan(&got); err != nil || got != 5 {
		t.Fatalf("count after rollback=%d err=%v", got, err)
	}
}

func TestBundleArchiveValidation(t *testing.T) {
	for _, body := range []string{`null`, `[]`, `{"220":{"bundles":5}}`, `{"220":`} {
		if _, err := parseCounts([]byte(body)); err == nil {
			t.Fatalf("accepted invalid archive %q", body)
		}
	}
	archive := map[string]any{"220": map[string]int{"bundles": 5}, "221": map[string]string{"bundles": "2"},
		"222": map[string]int{"bundles": -2}, "223": map[string]float64{"bundles": 1.5}, "224": false}
	for i := range minArchiveRecords {
		archive["other"+strconv.Itoa(i)] = map[string]int{"bundles": 2}
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(archive)
	}))
	defer upstream.Close()
	counts, err := fetchCounts(t.Context(), upstream.Client(), upstream.URL)
	if err != nil || len(counts) != 1 || counts[220] != 5 {
		t.Fatalf("counts=%v err=%v", counts, err)
	}
}
