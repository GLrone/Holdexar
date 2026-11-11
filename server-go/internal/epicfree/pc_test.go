package epicfree

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"reflect"
	"strings"
	"testing"
	"time"
)

type pcFixtureTransport func(*http.Request) (*http.Response, error)

func (f pcFixtureTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

func pcFixtureResponse(req *http.Request, status int, body string) (*http.Response, error) {
	return &http.Response{
		StatusCode: status,
		Header:     http.Header{"Content-Type": {"application/json"}},
		Body:       io.NopCloser(strings.NewReader(body)),
		Request:    req,
	}, nil
}

func TestFetchPCProjectsVerifiedWindows(t *testing.T) {
	now := time.Now().UTC()
	active := pcFixturePromotion(now.Add(-time.Hour), now.Add(time.Hour), 0)
	upcoming := pcFixturePromotion(now.Add(48*time.Hour), now.Add(96*time.Hour), 0)
	expired := pcFixturePromotion(now.Add(-48*time.Hour), now.Add(-time.Hour), 0)
	discount := pcFixturePromotion(now.Add(-time.Hour), now.Add(time.Hour), 20)
	unknown := pcFixturePromotion(now.Add(-time.Hour), now.Add(time.Hour), nil)
	wrongType := pcFixturePromotion(now.Add(-time.Hour), now.Add(time.Hour), 0)
	wrongType["discountSetting"].(map[string]any)["discountType"] = "FIXED"
	reversed := pcFixturePromotion(now.Add(time.Hour), now.Add(-time.Hour), 0)
	startOnly := pcFixturePromotion(now.Add(-time.Hour), now.Add(time.Hour), 0)
	delete(startOnly, "endDate")

	live := pcFixtureElement("live", "Live Game", active, upcoming)
	live["productSlug"] = "//attacker.example/foo"
	live["catalogNs"] = map[string]any{"mappings": []map[string]string{{"pageSlug": "../free-games"}, {"pageSlug": "live-game"}}}
	live["urlSlug"] = "fallback"
	live["keyImages"] = []map[string]string{
		{"type": "OfferImageWide", "url": "https://cdn1.epicgames.com.attacker.example/wide.jpg"},
		{"type": "Thumbnail", "url": "https://cdn2.unrealengine.com/thumb.jpg"},
		{"type": "DieselStoreFrontWide", "url": "https://cdn1.epicgames.com/wide.jpg"},
	}
	future := pcFixtureElement("future", "Upcoming Game", expired, upcoming)
	future["productSlug"] = "upcoming-game/home"
	future["price"] = map[string]any{"totalPrice": map[string]any{"originalPrice": 1200, "fmtPrice": map[string]string{"originalPrice": "$12.00"}}}
	addon := pcFixtureElement("addon", "Expansion", active)
	addon["offerType"] = "ADD_ON"
	private := pcFixtureElement("private", "Code Only", active)
	private["isCodeRedemptionOnly"] = true
	inactive := pcFixtureElement("inactive", "Private Status", active)
	inactive["status"] = "INACTIVE"
	paid := pcFixtureElement("paid", "Currently Paid", active)
	paid["price"] = map[string]any{"totalPrice": map[string]any{"discountPrice": 300}}

	en := pcFixtureCatalog(t, live, future, addon, private, inactive, paid,
		pcFixtureElement("expired", "Expired", expired),
		pcFixtureElement("discount", "Discount", discount),
		pcFixtureElement("unknown", "Unknown discount", unknown),
		pcFixtureElement("wrong-type", "Unsupported discount", wrongType),
		pcFixtureElement("reversed", "Reversed dates", reversed),
		pcFixtureElement("start-only", "Missing end", startOnly),
	)
	localized := pcFixtureElement("live", "本期游戏", active)
	localized["price"] = map[string]any{"totalPrice": map[string]any{"fmtPrice": map[string]string{"originalPrice": "¥88.00"}}}
	localizedFuture := pcFixtureElement("future", "下期游戏", upcoming)
	localizedFuture["price"] = map[string]any{"totalPrice": map[string]any{"originalPrice": 0}}
	cn := pcFixtureCatalog(t, localizedFuture, pcFixtureElement("unrelated", "Unrelated", active), localized)
	client := &http.Client{Transport: pcFixtureTransport(func(req *http.Request) (*http.Response, error) {
		q := req.URL.Query()
		switch {
		case q.Get("locale") == "en-US" && q.Get("country") == "US":
			return pcFixtureResponse(req, 200, en)
		case q.Get("locale") == "zh-CN" && q.Get("country") == "CN":
			return pcFixtureResponse(req, 200, cn)
		default:
			return pcFixtureResponse(req, 404, "{}")
		}
	})}
	offers, err := FetchPC(context.Background(), client)
	if err != nil || len(offers) != 3 {
		t.Fatalf("FetchPC = %+v, %v", offers, err)
	}
	want := Offer{
		Title: "Live Game", TitleCn: "本期游戏", Namespace: "live-ns", ElementID: "live", OfferType: "BASE_GAME",
		Start: now.Add(-time.Hour).Format("2006-1-2"), End: now.Add(time.Hour).Format("2006-1-2"),
		Image: "https://cdn1.epicgames.com/wide.jpg", URL: "https://store.epicgames.com/en-US/p/live-game", PriceOriginal: "¥88.00",
	}
	if !reflect.DeepEqual(offers[0], want) {
		t.Errorf("active offer = %+v, want %+v", offers[0], want)
	}
	if got := offers[1]; !got.Upcoming || got.Start != now.Add(48*time.Hour).Format("2006-1-2") ||
		got.URL != "https://store.epicgames.com/en-US/p/upcoming-game" || got.PriceOriginal != "$12.00" || got.TitleCn != "下期游戏" {
		t.Errorf("upcoming offer = %+v", got)
	}
	if got := offers[2]; got.OfferType != "ADD_ON" || got.URL != "https://store.epicgames.com/en-US/free-games" {
		t.Errorf("add-on offer = %+v", got)
	}
}

func TestFetchPCDistinguishesEmptyFromUnavailable(t *testing.T) {
	tests := []struct {
		name   string
		en     string
		status int
		err    bool
	}{
		{"empty official window", pcFixtureCatalog(t), 200, false},
		{"missing envelope", `{"data":{"Catalog":{"searchStore":{}}}}`, 200, true},
		{"null elements", `{"data":{"Catalog":{"searchStore":{"elements":null}}}}`, 200, true},
		{"GraphQL error", `{"errors":[{"message":"unavailable"}],"data":{"Catalog":{"searchStore":{"elements":[]}}}}`, 200, true},
		{"service outage", "Unavailable", 503, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			client := &http.Client{Transport: pcFixtureTransport(func(req *http.Request) (*http.Response, error) {
				if req.URL.Query().Get("locale") == "zh-CN" {
					return pcFixtureResponse(req, 503, "Unavailable")
				}
				return pcFixtureResponse(req, tt.status, tt.en)
			})}
			offers, err := FetchPC(context.Background(), client)
			if (err != nil) != tt.err || len(offers) != 0 || !tt.err && offers == nil {
				t.Fatalf("FetchPC = %+v, %v", offers, err)
			}
		})
	}
}

func TestMatchSteamRequiresUniqueExactPaidGame(t *testing.T) {
	searches := map[string]string{
		"Game":      `[{"type":"app","id":10,"name":"Game Demo"},{"type":"app","id":11,"name":"Game - DLC"},{"type":"app","id":"12","name":" GAME "}]`,
		"DLC":       `[{"type":"app","id":21,"name":"DLC"}]`,
		"Demo":      `[{"type":"app","id":22,"name":"Demo"}]`,
		"Music":     `[{"type":"app","id":23,"name":"Music"}]`,
		"Free":      `[{"type":"app","id":24,"name":"Free"}]`,
		"Ambiguous": `[{"type":"app","id":41,"name":"Ambiguous"},{"type":"app","id":42,"name":"Ambiguous"}]`,
		"Missing":   `[{"type":"app","id":51,"name":"Missing II"}]`,
		"Mistyped":  `[{"type":"app","id":61,"name":"Mistyped"}]`,
	}
	details := map[string]string{
		"12": `{"success":true,"data":{"steam_appid":12,"type":"game","name":"Game"}}`,
		"21": `{"success":true,"data":{"steam_appid":21,"type":"dlc","name":"DLC"}}`,
		"22": `{"success":true,"data":{"steam_appid":22,"type":"demo","name":"Demo"}}`,
		"23": `{"success":true,"data":{"steam_appid":23,"type":"music","name":"Music"}}`,
		"24": `{"success":true,"data":{"steam_appid":24,"type":"game","is_free":true,"name":"Free"}}`,
		"61": `{"success":true,"data":{"steam_appid":61,"type":"game","name":"Other Game"}}`,
	}
	client := &http.Client{Transport: pcFixtureTransport(func(req *http.Request) (*http.Response, error) {
		q := req.URL.Query()
		if req.URL.Host == "store.steampowered.com" && q.Get("cc") == "US" && q.Get("l") == "english" {
			switch req.URL.Path {
			case "/api/storesearch/":
				if items, ok := searches[q.Get("term")]; ok {
					return pcFixtureResponse(req, 200, `{"items":`+items+`}`)
				}
			case "/api/appdetails":
				id := q.Get("appids")
				if data, ok := details[id]; ok {
					return pcFixtureResponse(req, 200, `{"`+id+`":`+data+`}`)
				}
			}
		}
		return pcFixtureResponse(req, 503, "Unavailable")
	})}
	existing := int64(77)
	offers := []Offer{
		{Title: "Game", OfferType: "BASE_GAME"}, {Title: "game", OfferType: "BASE_GAME"}, {Title: "Game", OfferType: "ADD_ON"},
		{Title: "DLC"}, {Title: "Demo"}, {Title: "Music"}, {Title: "Free"}, {Title: "Ambiguous"}, {Title: "Missing"}, {Title: "Mistyped"},
		{Title: "Epic Exclusive"}, {Title: "Previously matched", AppID: &existing},
	}
	matched := MatchSteam(context.Background(), client, offers)
	for i, offer := range matched {
		wantID := int64(0)
		if i <= 1 {
			wantID = 12
		} else if i == len(offers)-1 {
			wantID = existing
		}
		if offer.Title != offers[i].Title || wantID == 0 && offer.AppID != nil || wantID != 0 && (offer.AppID == nil || *offer.AppID != wantID) {
			t.Errorf("matched[%d] = %+v, want app ID %d", i, offer, wantID)
		}
	}
	if len(matched) != len(offers) || offers[0].AppID != nil {
		t.Fatalf("MatchSteam modified input or lost offers: %+v", matched)
	}
}

func pcFixtureElement(id, title string, promotions ...map[string]any) map[string]any {
	return map[string]any{
		"id": id, "namespace": id + "-ns", "title": title, "offerType": "BASE_GAME", "status": "ACTIVE",
		"promotions": map[string]any{"promotionalOffers": []map[string]any{{"promotionalOffers": promotions}}},
	}
}

func pcFixturePromotion(start, end time.Time, percentage any) map[string]any {
	return map[string]any{
		"startDate": start.Format(time.RFC3339), "endDate": end.Format(time.RFC3339),
		"discountSetting": map[string]any{"discountType": "PERCENTAGE", "discountPercentage": percentage},
	}
}

func pcFixtureCatalog(t *testing.T, elements ...map[string]any) string {
	t.Helper()
	if elements == nil {
		elements = []map[string]any{}
	}
	body, err := json.Marshal(map[string]any{"data": map[string]any{"Catalog": map[string]any{"searchStore": map[string]any{"elements": elements}}}})
	if err != nil {
		t.Fatal(err)
	}
	return string(body)
}
