package boards

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func noBoardDelay(context.Context, time.Duration) error { return nil }

func boardItems(w http.ResponseWriter, ids ...int64) {
	items := []map[string]string{}
	for _, id := range ids {
		items = append(items, map[string]string{"logo": fmt.Sprintf("https://cdn.example/apps/%d/capsule.jpg", id)})
	}
	json.NewEncoder(w).Encode(map[string]any{"items": items})
}

func TestSpecialsPaginationDeduplicatesRankOrder(t *testing.T) {
	starts := []int{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		start, _ := strconv.Atoi(q.Get("start"))
		starts = append(starts, start)
		for k, want := range map[string]string{"count": "100", "json": "1", "specials": "1", "sort_by": "Global_Topsellers", "hidef2p": "1", "category1": "998", "l": "english"} {
			if q.Get(k) != want {
				t.Errorf("query %s=%q want %q", k, q.Get(k), want)
			}
		}
		if r.Header.Get("Cookie") != "birthtime=315532800; mature_content=1" || r.Header.Get("Accept-Language") == "" {
			t.Error("missing public content request headers")
		}
		switch start {
		case 0:
			json.NewEncoder(w).Encode(map[string]any{"items": []any{map[string]string{"logo": "/apps/12/a"}, map[string]string{"logo": "/apps/10/a"}, map[string]string{"logo": "/bundles/99/a"}}})
		case 100:
			boardItems(w, 12, 15)
		default:
			boardItems(w)
		}
	}))
	defer srv.Close()
	c := New(Options{Endpoint: srv.URL, HTTPClient: srv.Client(), Sleep: noBoardDelay})
	got, err := c.Get(context.Background(), "specials")
	if err != nil || !reflect.DeepEqual(got, []int64{12, 10, 15}) {
		t.Fatalf("got=%v err=%v", got, err)
	}
	if !reflect.DeepEqual(starts, []int{0, 100, 200, 300, 400}) {
		t.Fatalf("pagination=%v", starts)
	}
}

func TestBoardParametersAndFivePageLimit(t *testing.T) {
	for key, def := range definitions {
		if key == "specials" {
			continue
		}
		t.Run(key, func(t *testing.T) {
			calls := 0
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				q := r.URL.Query()
				for k, v := range def.params {
					if q.Get(k) != v[0] {
						t.Errorf("%s=%q want %q", k, q.Get(k), v[0])
					}
				}
				start, _ := strconv.Atoi(q.Get("start"))
				ids := make([]int64, 100)
				for i := range ids {
					ids[i] = int64(start + i + 1)
				}
				boardItems(w, ids...)
			}))
			defer srv.Close()
			c := New(Options{Endpoint: srv.URL, Sleep: noBoardDelay})
			got, err := c.Get(context.Background(), key)
			if err != nil || len(got) != 500 || calls != 5 || got[0] != 1 || got[499] != 500 {
				t.Fatalf("len=%d calls=%d err=%v", len(got), calls, err)
			}
		})
	}
}

func TestBoardHotStaleFailureAndForcedRefresh(t *testing.T) {
	var failed atomic.Bool
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if failed.Load() {
			http.Error(w, "unavailable", 503)
			return
		}
		if r.URL.Query().Get("start") == "0" {
			boardItems(w, 20, 30)
		} else {
			boardItems(w)
		}
	}))
	defer srv.Close()
	now := time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)
	c := New(Options{Endpoint: srv.URL, Now: func() time.Time { return now }, Sleep: noBoardDelay})
	got, err := c.Get(context.Background(), "topsellers")
	if err != nil || len(got) != 2 {
		t.Fatalf("initial=%v err=%v", got, err)
	}
	got[0] = 99
	failed.Store(true)
	now = now.Add(HotTTL - time.Nanosecond)
	got, err = c.Get(context.Background(), "topsellers")
	if err != nil || got[0] != 20 || calls.Load() != 2 {
		t.Fatalf("hot=%v calls=%d err=%v", got, calls.Load(), err)
	}
	now = now.Add(time.Nanosecond)
	got, err = c.Get(context.Background(), "topsellers")
	if err != nil || got[0] != 20 || calls.Load() != 3 {
		t.Fatalf("stale=%v calls=%d err=%v", got, calls.Load(), err)
	}
	now = now.Add(23 * time.Hour)
	got, err = c.Get(context.Background(), "topsellers")
	if err != nil || len(got) != 0 {
		t.Fatalf("expired=%v err=%v", got, err)
	}
	got, err = c.Refresh(context.Background(), "topsellers")
	if err != nil || !reflect.DeepEqual(got, []int64{20, 30}) {
		t.Fatalf("forced refresh fallback=%v err=%v", got, err)
	}
	got, err = c.Get(context.Background(), "popularnew")
	if err != nil || len(got) != 0 {
		t.Fatalf("board caches mixed: %v %v", got, err)
	}
}

func TestBoardPartialFailurePreservesCollectedSnapshot(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Query().Get("start") == "0" {
			boardItems(w, 42)
			return
		}
		http.Error(w, "limited", 429)
	}))
	defer srv.Close()
	c := New(Options{Endpoint: srv.URL, Sleep: noBoardDelay})
	got, err := c.Get(context.Background(), "specials")
	if err != nil || !reflect.DeepEqual(got, []int64{42}) || calls.Load() != 2 {
		t.Fatalf("partial=%v calls=%d err=%v", got, calls.Load(), err)
	}
	got, err = c.Get(context.Background(), "specials")
	if err != nil || len(got) != 1 || calls.Load() != 2 {
		t.Fatalf("partial not cached: %v %v", got, err)
	}
}

func TestBoardConcurrentMissAndCancellation(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Query().Get("start") == "0" {
			boardItems(w, 7)
		} else {
			boardItems(w)
		}
	}))
	defer srv.Close()
	c := New(Options{Endpoint: srv.URL, Sleep: noBoardDelay})
	var wg sync.WaitGroup
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			got, err := c.Get(context.Background(), "topsellers")
			if err != nil || !reflect.DeepEqual(got, []int64{7}) {
				t.Errorf("concurrent=%v %v", got, err)
			}
		}()
	}
	wg.Wait()
	if calls.Load() != 2 {
		t.Fatalf("duplicate fetches=%d", calls.Load())
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := c.Get(ctx, "topsellers"); err != context.Canceled {
		t.Fatalf("cancel=%v", err)
	}
	if _, err := c.Get(context.Background(), "unknown"); err == nil {
		t.Fatal("unknown source accepted")
	}
}

func TestBoardProxyResolverUsesExistingPublicChannel(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Query().Get("start") == "0" {
			boardItems(w, 88)
		} else {
			boardItems(w)
		}
	}))
	defer srv.Close()
	c := New(Options{Endpoint: "http://steam.invalid/search/results", Proxy: func(context.Context) (string, error) { return srv.URL, nil }, Sleep: noBoardDelay})
	got, err := c.Get(context.Background(), "topsellers")
	if err != nil || !reflect.DeepEqual(got, []int64{88}) || calls.Load() != 2 {
		t.Fatalf("proxy=%v calls=%d err=%v", got, calls.Load(), err)
	}
}
