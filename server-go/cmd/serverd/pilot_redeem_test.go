package main

import (
	"context"
	"errors"
	"net/http"
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/account"
	"holdexar/server-go/internal/pilot"
	"holdexar/server-go/internal/redeem"
	"holdexar/server-go/internal/store"
)

const pilotRedeemSteamID = "76561198000000001"

type refusedTransport struct{}

func (refusedTransport) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, errors.New("refused")
}

func pilotRedeemFixture(t *testing.T) (pilot.ToolBridge, *account.Service) {
	t.Helper()
	data := t.TempDir()
	path := filepath.Join(data, "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err = store.InitializeFreshDatabase(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	gate := store.NewWriteGate(db, path)
	client := account.NewSteamClient(account.SteamOptions{HTTPClient: &http.Client{Transport: refusedTransport{}}})
	accounts := account.NewService(context.Background(), db, gate, account.ServiceOptions{DataDir: data, Steam: client})
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if err := accounts.Close(ctx); err != nil {
			t.Error(err)
		}
	})
	svc := redeem.NewService(db, gate, redeem.ServiceOptions{Accounts: accounts})
	mux := http.NewServeMux()
	svc.Register(mux)
	return pilotToolBridge(db, gate, mux, nil), accounts
}

func TestPilotRedeemQuotaShapes(t *testing.T) {
	bridge, accounts := pilotRedeemFixture(t)
	ctx := &pilot.CallCtx{Ctx: context.Background()}
	out := bridge(ctx, "redeem_quota", nil, false)
	rows, ok := out["rows"].([]any)
	if !ok || len(rows) == 0 {
		t.Fatalf("未绑定行缺失: %v", out)
	}
	row0, _ := rows[0].(map[string]any)
	if out["titleKey"] != "redeem" || row0["vKey"] != "redeemNoCookie" || row0["tone"] != "warn" {
		t.Fatalf("未绑定形状不符: %v", out)
	}
	if _, err := accounts.BindCookies(context.Background(), "sessionid=abc; steamLoginSecure="+pilotRedeemSteamID+"||jwt-token"); err != nil {
		t.Fatal(err)
	}
	out = bridge(ctx, "redeem_quota", nil, false)
	rows, ok = out["rows"].([]any)
	if !ok || len(rows) == 0 {
		t.Fatalf("额度行缺失: %v", out)
	}
	row0, _ = rows[0].(map[string]any)
	if row0["vKey"] != "redeemQuota" || row0["tone"] != "ok" {
		t.Fatalf("额度行形状不符: %v", out)
	}
	data, ok := row0["data"].(map[string]any)
	if !ok || jsonInt(data["used"]) != 0 || jsonInt(data["limit"]) != 10 {
		t.Fatalf("data 字段不符: %v", row0["data"])
	}
}

func TestPilotRedeemQuotaUnwiredMux(t *testing.T) {
	bridge := pilotToolBridge(nil, nil, http.NewServeMux(), nil)
	out := bridge(&pilot.CallCtx{Ctx: context.Background()}, "redeem_quota", nil, false)
	if out["kind"] != "failed" {
		t.Fatalf("未挂载路由应回 failed: %v", out)
	}

	var gotPeer string
	mux := http.NewServeMux()
	mux.HandleFunc("POST /probe", func(w http.ResponseWriter, r *http.Request) {
		gotPeer = r.RemoteAddr
		_, _ = w.Write([]byte(`{"ok":true}`))
	})
	status, _, err := muxJSON(context.Background(), mux, http.MethodPost, "/probe", map[string]any{"x": 1})
	if err != nil || status != http.StatusOK || gotPeer != "127.0.0.1:0" {
		t.Fatalf("muxJSON status=%d peer=%q err=%v", status, gotPeer, err)
	}
}
