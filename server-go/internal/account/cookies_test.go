package account

import (
	"encoding/base64"
	"fmt"
	"strings"
	"testing"
	"time"
)

func accessFixture(sid string, expiry int64) string {
	return sid + "%7C%7Ch." + base64.RawURLEncoding.EncodeToString([]byte(fmt.Sprintf(`{"exp":%d,"sub":%q}`, expiry, sid))) + ".s"
}
func TestCookieIdentityFilteringAndFreshness(t *testing.T) {
	now := time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)
	raw := "Cookie: preference=ignored; steamLoginSecure=" + accessFixture("76561198000000001", now.Add(time.Hour).Unix()) + "\nsessionid=abcd\r\nsteamRefresh_steam=76561198000000001%7C%7Crefresh; steamRememberLogin=true"
	clean := FilterCookies(raw)
	if strings.Contains(clean, "preference") || CookieSteamID(clean) != "76561198000000001" || RefreshToken(clean) != "refresh" {
		t.Fatal("cookie parsing failed")
	}
	f := CookieFreshness(clean, now)
	if f.Expired || !f.Expiring || !f.HasRefresh || f.ExpiresAt == nil || f.SecondsLeft == nil || *f.SecondsLeft != 3600 {
		t.Fatalf("%+v", f)
	}
	f = CookieFreshness("steamLoginSecure=76561198000000001||opaque", now)
	if f.Expired || f.Expiring || f.ExpiresAt != nil {
		t.Fatal(f)
	}
	for _, raw := range []string{"steamLoginSecure=invalid||x", "steamLoginSecure=12x%7C%7Cx", "a=secret"} {
		if CookieSteamID(raw) != "" {
			t.Fatal("invalid owner accepted")
		}
	}
	f = CookieFreshness("steamLoginSecure="+accessFixture("1", now.Add(-time.Second).Unix()), now)
	if !f.Expired || !f.Expiring {
		t.Fatal(f)
	}
}
