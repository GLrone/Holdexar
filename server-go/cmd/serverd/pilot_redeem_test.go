//go:build windows

// pilot_redeem_test.go 领航员 redeem_quota 工具形状（对齐 Python
// tests/test_pilot_tools.py::test_bills_and_redeem_rows 的 redeem 断言与
// agent/tools/builtin/account.py::redeem_quota 行形状）。
package main

import (
	"context"
	"errors"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"holdexar/server-go/internal/account"
	"holdexar/server-go/internal/pilot"
	"holdexar/server-go/internal/redeem"
	"holdexar/server-go/internal/store"
)

const pilotRedeemSteamID = "76561198000000001"

// refusedTransport 钱包/资料请求全部快速失败（测试环境不出网）。
type refusedTransport struct{}

func (refusedTransport) RoundTrip(*http.Request) (*http.Response, error) { return nil, errors.New("refused") }

func pilotRedeemFixture(t *testing.T) (*redeem.Service, *account.Service) {
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
	return svc, accounts
}

func TestPilotRedeemQuotaShapes(t *testing.T) {
	svc, accounts := pilotRedeemFixture(t)
	ctx := &pilot.CallCtx{Ctx: context.Background()}
	out := pilotRedeemQuota(ctx, svc)
	rows := out["rows"].([]map[string]any)
	if out["titleKey"] != "redeem" || rows[0]["vKey"] != "redeemNoCookie" || rows[0]["tone"] != "warn" {
		t.Fatalf("未绑定形状不符: %v", out)
	}
	if _, err := accounts.BindCookies(context.Background(), "sessionid=abc; steamLoginSecure="+pilotRedeemSteamID+"||jwt-token"); err != nil {
		t.Fatal(err)
	}
	out = pilotRedeemQuota(ctx, svc)
	rows = out["rows"].([]map[string]any)
	if rows[0]["vKey"] != "redeemQuota" || rows[0]["tone"] != "ok" {
		t.Fatalf("额度行形状不符: %v", out)
	}
	data, ok := rows[0]["data"].(map[string]any)
	if !ok || data["used"] != 0 || data["limit"] != 10 {
		t.Fatalf("data 字段不符: %v", rows[0]["data"])
	}
}

func TestPilotRedeemQuotaNilService(t *testing.T) {
	out := pilotRedeemQuota(&pilot.CallCtx{Ctx: context.Background()}, nil)
	if !strings.Contains(out["kind"].(string), "empty") {
		t.Fatalf("nil 服务应回空: %v", out)
	}
}
