//go:build windows

package main

import (
	"archive/zip"
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"holdexar/server-go/internal/games"
	"holdexar/server-go/internal/plugins"
	"holdexar/server-go/internal/store"
)

func historyPluginFixture(t *testing.T, install bool) (*pluginHistorySource, *games.HistoryBackfill) {
	t.Helper()
	ctx := context.Background()
	dir := t.TempDir()
	path := filepath.Join(dir, "holdexar.db")
	db, e := store.Open(path)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { db.Close() })
	if e = store.InitializeFreshDatabase(ctx, db); e != nil {
		t.Fatal(e)
	}
	gate := store.NewWriteGate(db, path)
	manager := plugins.New(db, gate, dir, plugins.Options{})
	if install {
		var b bytes.Buffer
		w := zip.NewWriter(&b)
		for name, value := range map[string]string{
			"manifest.json":     `{"id":"example.history","name":"History","version":"1.0.0","apiVersion":1,"kind":"datasource.community_history","configSchema":{"type":"object","properties":{}}}`,
			"observations.json": `{"appid":10,"region":"cn","observations":[{"date":"2026-06-01","priceMinor":8800,"cnyFen":880,"productUnknown":false}]}`,
		} {
			f, e := w.Create(name)
			if e != nil {
				t.Fatal(e)
			}
			if _, e = f.Write([]byte(value)); e != nil {
				t.Fatal(e)
			}
		}
		if e = w.Close(); e != nil {
			t.Fatal(e)
		}
		if _, e = manager.InstallLocal(ctx, b.Bytes()); e != nil {
			t.Fatal(e)
		}
		on := true
		if _, e = manager.Update(ctx, "example.history", &on, nil); e != nil {
			t.Fatal(e)
		}
	}
	source := &pluginHistorySource{manager: manager}
	backfill := games.NewHistoryBackfill(ctx, db, gate, nil)
	backfill.SetSource(source)
	return source, backfill
}

func TestPluginHistoryUsesBeijingDateAndCoreIdentity(t *testing.T) {
	source, _ := historyPluginFixture(t, true)
	rows, e := source.Fetch(context.Background(), 10, "CN")
	if e != nil || len(rows) != 1 {
		t.Fatalf("rows=%v err=%v", rows, e)
	}
	point := rows[0]
	if got := point.SnapshotAt.Format("2006-01-02 15:04:05 -0700"); got != "2026-06-01 00:00:00 +0800" {
		t.Fatalf("wrong date: %s", got)
	}
	if !point.ProductUnknown || point.SubID != nil {
		t.Fatal("unverified source declared a standard product")
	}
	if point.Currency != "CNY" || point.CnyFen != nil {
		t.Fatalf("source overrode core conversion: %+v", point)
	}
}

func TestPluginHistoryAbsentSourceReturnsConflict(t *testing.T) {
	source, backfill := historyPluginFixture(t, false)
	if _, e := source.Fetch(context.Background(), 10, "CN"); e == nil {
		t.Fatal("missing plugin appeared as empty successful data")
	}
	if _, _, e := backfill.Start(10, "CN"); e == nil {
		t.Fatal("missing plugin was dispatched")
	}
	mux := http.NewServeMux()
	games.Register(mux, nil, backfill, games.Options{})
	r := httptest.NewRecorder()
	mux.ServeHTTP(r, httptest.NewRequest("POST", "/api/v1/games/10/history/backfill?region=cn", nil))
	if r.Code != 409 {
		t.Fatalf("missing plugin status=%d body=%s", r.Code, r.Body)
	}
}
