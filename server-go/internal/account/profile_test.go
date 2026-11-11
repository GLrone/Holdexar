package account

import (
	"context"
	"fmt"
	"net/http"
	"net/http/cookiejar"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

func TestProfilePreviewFallsBackFromEmptyAvatarAndNeverCarriesCookies(t *testing.T) {
	const sid = "76561198000000001"
	calls := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Cookie") != "" {
			t.Error("anonymous preview carried credentials")
		}
		if strings.HasPrefix(r.URL.Path, "/mini/") {
			fmt.Fprint(w, `{"persona_name":"","avatar_url":"https://avatars.steamstatic.com/_medium.jpg"}`)
			return
		}
		calls++
		fmt.Fprintf(w, `<profile><steamID64>%s</steamID64><steamID><![CDATA[Preview Name]]></steamID><avatarFull><![CDATA[https://avatars.akamai.steamstatic.com/hash_full.jpg]]></avatarFull></profile>`, sid)
	}))
	defer upstream.Close()
	client := upstream.Client()
	client.Jar, _ = cookiejar.New(nil)
	u, _ := url.Parse(upstream.URL)
	client.Jar.SetCookies(u, []*http.Cookie{{Name: "steamLoginSecure", Value: "wrong-account"}})
	steam := NewSteamClient(SteamOptions{HTTPClient: client, ProfileURL: upstream.URL + "/mini/%s", ProfileXMLURL: upstream.URL + "/xml/%s"})
	defer steam.Close()
	p, e := steam.ProfilePreview(context.Background(), sid)
	if e != nil || p["persona_name"] != "Preview Name" || p["avatar_url"] != "https://avatars.fastly.steamstatic.com/hash_full.jpg" || calls != 1 {
		t.Fatal(p, e, calls)
	}
}
