package account

import (
	"context"
	"encoding/base64"
	"fmt"
	"net/http"
	"testing"
	"time"
)

func TestOwnedTokenUsesEachBoundAccountAndPrimaryOnlyForUnboundTargets(t *testing.T) {
	s, _ := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(429) })
	ctx := context.Background()
	const a, b, c = "76561198000000001", "76561198000000002", "76561198000000003"
	expires := time.Now().Add(24 * time.Hour).Unix()
	tokenFor := func(sid string) string {
		return "e30." + base64.RawURLEncoding.EncodeToString([]byte(fmt.Sprintf(`{"exp":%d,"sub":%q}`, expires, sid))) + "." + sid
	}
	for _, sid := range []string{a, b} {
		if _, e := s.BindCookies(ctx, "steamLoginSecure="+sid+"||"+tokenFor(sid)); e != nil {
			t.Fatal(e)
		}
	}
	for _, sid := range []string{a, b, c} {
		token, e := s.OwnedToken(ctx, sid)
		wantOwner := sid
		if sid == c {
			wantOwner = a
		}
		wanted := tokenFor(wantOwner)
		if e != nil || token != wanted {
			t.Fatalf("target=%s token error=%v", sid, e)
		}
	}
	if _, e := s.BoundToken(ctx, c); e != ErrAccountNotFound {
		t.Fatal("unbound family request borrowed primary", e)
	}
	if _, e := s.BindCookies(ctx, "steamLoginSecure="+a+"||"+tokenFor(b)); e == nil {
		t.Fatal("JWT subject mismatch bound under another identity")
	}
	if _, e := s.db.Exec(`UPDATE steam_accounts SET cookies='steamLoginSecure=76561198000000002||e30.eyJleHAiOjF9.sig' WHERE steam_id=?`, b); e != nil {
		t.Fatal(e)
	}
	if token, e := s.OwnedToken(ctx, b); e == nil || token != "" {
		t.Fatal("expired bound account silently used another account")
	}
}
