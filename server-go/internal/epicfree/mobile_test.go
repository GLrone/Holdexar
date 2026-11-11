package epicfree

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
)

type mobileTestTransport func(*http.Request) (*http.Response, error)

func (f mobileTestTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

func mobileFixture(status int, body string) (*http.Response, error) {
	return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(body))}, nil
}

const mobileGPFixture = `[
	{"title":"Example (Mobile) Giveaway","type":"Game","status":"Active","description":"Free on the Epic Games Store App.","published_date":"2026-10-08 11:12:47","end_date":"2026-10-15 23:59:00","worth":"$5.99","image":"https://www.gamerpower.com/offers/example.jpg","open_giveaway_url":"https://other.example/open"},
	{"title":"PC Giveaway","type":"Game","status":"Active","description":"Free via Epic Games Store","published_date":"2026-10-09 00:00:00","end_date":"2026-10-16 23:59:00"}
]`

func mobileClaimFixture(namespace, id, price, end string) string {
	return fmt.Sprintf(`{"data":[{"content":{"purchase":[
		{"purchaseType":"Claim","price":{"decimalPrice":%s},"purchasePayload":{"offerId":%q,"sandboxId":%q},"discount":{"discountEndDate":%q}},
		{"purchaseType":"Purchase","price":{"decimalPrice":0},"purchasePayload":{"offerId":"purchase","sandboxId":%q}},
		{"purchaseType":"Claim","price":{"decimalPrice":null},"purchasePayload":{"offerId":"unknown-price","sandboxId":%q}},
		{"purchaseType":"Claim","price":{"decimalPrice":0},"purchasePayload":{"offerId":"malformed&offers=extra","sandboxId":%q}}
	]}}]}`, price, id, namespace, end, namespace, namespace, namespace)
}

func TestMobileDiscoveryEntryPriority(t *testing.T) {
	for _, tc := range []struct {
		name, price, slug, url string
		direct                 bool
	}{
		{"checkout", `"0"`, "example-android", "https://store.epicgames.com/purchase?offers=1-sandbox-mobile-offer&offers=1-sandbox-ios-offer", true},
		{"product", `499`, "example-android", "https://store.epicgames.com/p/example-android", true},
		{"app", `499`, "../purchase", "https://store.epicgames.com/mobile", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var searches atomic.Int32
			client := &http.Client{Transport: mobileTestTransport(func(req *http.Request) (*http.Response, error) {
				if req.Method != http.MethodGet || req.URL.Scheme != "https" {
					return nil, fmt.Errorf("unexpected request %s %s", req.Method, req.URL)
				}
				switch req.URL.Hostname() {
				case "www.gamerpower.com":
					return mobileFixture(200, mobileGPFixture)
				case "store.epicgames.com":
					if req.URL.Path != "/graphql" {
						return nil, fmt.Errorf("unexpected Epic path %s", req.URL.Path)
					}
					switch req.URL.Query().Get("operationName") {
					case "primarySearchAutocomplete":
						searches.Add(1)
						if !strings.Contains(req.URL.Query().Get("variables"), `"keywords":"Example"`) {
							return nil, fmt.Errorf("search title missing")
						}
						return mobileFixture(200, `{"data":{"Catalog":{"searchStore":{"elements":[{"offerId":"pc","sandboxId":"sandbox","title":"Example"}]}}}}`)
					case "getCatalogOffer":
						return mobileFixture(200, fmt.Sprintf(`{"data":{"Catalog":{"catalogOffer":{
							"catalogNs":{"mappings":[{"pageType":"productHome","pageSlug":"pc-example"},{"pageType":"offer","pageSlug":%q}]},
							"keyImages":[{"type":"Thumbnail","url":"https://cdn1.epicgames.com/small.jpg"},{"type":"OfferImageWide","url":"https://cdn1.epicgames.com/cover.jpg"}]
						}}}}`, tc.slug))
					}
				case "egs-platform-service.store.epicgames.com":
					if req.URL.Query().Get("platform") == "ios" {
						if tc.name == "checkout" {
							return mobileFixture(200, mobileClaimFixture("sandbox", "ios-offer", "0.0", "2026-10-15"))
						}
						return mobileFixture(404, "")
					}
					return mobileFixture(200, mobileClaimFixture("sandbox", "mobile-offer", tc.price, "2026-10-15T15:00:00Z"))
				}
				return nil, fmt.Errorf("unexpected URL %s", req.URL)
			})}
			offer, err := DiscoverMobile(context.Background(), client)
			if err != nil || offer == nil {
				t.Fatalf("DiscoverMobile = %v, %v", offer, err)
			}
			if offer.Title == nil || *offer.Title != "Example" || offer.Source != "epic" ||
				offer.URL != tc.url || offer.Direct != tc.direct || offer.Image != "https://cdn1.epicgames.com/cover.jpg" ||
				offer.End == nil || *offer.End != "2026-10-15" || offer.Worth == nil || *offer.Worth != "$5.99" || searches.Load() != 1 {
				t.Fatalf("offer = %+v, searches = %d", offer, searches.Load())
			}
		})
	}
}

func TestMobilePCProbeUsesCurrentNamespacesAndLatestClaim(t *testing.T) {
	var probes atomic.Int32
	client := &http.Client{Transport: mobileTestTransport(func(req *http.Request) (*http.Response, error) {
		probes.Add(1)
		switch req.URL.Path {
		case "/api/v2/public/sandboxes/old/offers":
			return mobileFixture(200, mobileClaimFixture("old", "old-offer", "0", "2026-10-08"))
		case "/api/v2/public/sandboxes/new/offers":
			return mobileFixture(200, mobileClaimFixture("new", "new-offer", "0.0", "2026-10-15"))
		default:
			return nil, fmt.Errorf("unexpected sandbox %s", req.URL.Path)
		}
	})}
	offer, err := ResolveMobileFromPC(context.Background(), client, []Offer{
		{Title: "Future", Namespace: "future", Upcoming: true},
		{Title: "Missing"},
		{Title: "Old", Namespace: "old"},
		{Title: "New", TitleCn: "本期", Namespace: "new", PriceOriginal: "$9.99"},
		{Title: "Duplicate", Namespace: "new"},
	})
	if err != nil || offer == nil {
		t.Fatalf("ResolveMobileFromPC = %v, %v", offer, err)
	}
	if offer.Title == nil || *offer.Title != "本期" || !offer.Direct || offer.URL != "https://store.epicgames.com/purchase?offers=1-new-new-offer" ||
		offer.End == nil || *offer.End != "2026-10-15" || probes.Load() != 4 {
		t.Fatalf("offer = %+v, probes = %d", offer, probes.Load())
	}
}

func TestMobileBreakerUsesTrustedImageAndAppEntry(t *testing.T) {
	client := &http.Client{Transport: mobileTestTransport(func(*http.Request) (*http.Response, error) {
		return mobileFixture(200, `{"layout":{"section":[{"breakers":{"breakerList":[
			{"title":"Free Giveaway","backgroundImage":"https://cdn1.epicgames.com.bad.example/invalid.jpg"},
			{"title":"Free Giveaway","backgroundImage":"http://cdn1.epicgames.com/invalid.jpg"},
			{"title":"Free Giveaway","backgroundImage":"https://cdn1.epicgames.com/free.jpg"}
		]}}]}}`)
	})}
	offer, err := FetchMobileBreaker(context.Background(), client)
	if err != nil || offer == nil || offer.Source != "breaker" || offer.Title != nil || offer.Direct ||
		offer.URL != "https://store.epicgames.com/mobile" || offer.Image != "https://cdn1.epicgames.com/free.jpg" {
		t.Fatalf("FetchMobileBreaker = %+v, %v", offer, err)
	}
}
