package epicfree

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"
)

const (
	MobilePageURL = "https://store.epicgames.com/mobile"

	mobileFeedURL     = "https://www.gamerpower.com/api/giveaways?platform=android"
	mobileCMSURL      = "https://store-content-ipv4.ak.epicgames.com/api/en-US/content/static/mobile"
	mobileGraphQLURL  = "https://store.epicgames.com/graphql"
	mobileSandboxURL  = "https://egs-platform-service.store.epicgames.com/api/v2/public/sandboxes/"
	mobileSearchHash  = "be4fe909f9a35f9704db7fed06fc4a47fc798ec0a6cbfa24d737aec2465904fa"
	mobileOfferHash   = "0bd79d7aaf89de3693abb813eec8b664321fab84037cbb968730631c8afe9a9d"
	mobileSearchTypes = "games/edition/base|bundles/games|games/edition|editors|addons|games/demo|software/edition/base|games/experience|subscription"
)

var (
	mobileTitleSuffix = regexp.MustCompile(`\s*\(Mobile\)\s*Giveaway\s*$`)
	mobileSegment     = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
	mobileZeroPrice   = regexp.MustCompile(`^-?0(\.0+)?([eE][+-]?[0-9]+)?$`)
	mobileWebHeaders  = map[string]string{
		"User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
		"Accept":          "application/json",
		"Accept-Language": "en-US,en;q=0.9",
		"Referer":         "https://store.epicgames.com/en-US/",
	}
	mobileAppHeaders = map[string]string{
		"User-Agent": "EpicGamesStore/50102 Android/14",
		"Accept":     "application/json",
	}
)

type mobileFeedEntry struct {
	Title, Description, Type, Status, Image, Worth string
	Published                                      string `json:"published_date"`
	End                                            string `json:"end_date"`
}

type mobileCatalogID struct {
	OfferID, SandboxID string
}

type mobileCatalog struct {
	KeyImages []struct{ Type, URL string }
	CatalogNs struct {
		Mappings []struct{ PageType, PageSlug string }
	}
}

type mobileClaim struct {
	part, end string
}

func mobileText(value string) *string {
	if value == "" {
		return nil
	}
	return &value
}

func mobileDay(value string) string {
	if len(value) < len(time.DateOnly) {
		return ""
	}
	day := value[:len(time.DateOnly)]
	if _, err := time.Parse(time.DateOnly, day); err != nil {
		return ""
	}
	return day
}

func mobileImage(value string) string {
	u, err := url.Parse(value)
	if err != nil || u.Scheme != "https" || u.User != nil || u.Port() != "" {
		return ""
	}
	host := strings.ToLower(u.Hostname())
	for _, domain := range []string{"epicgames.com", "epicgamescdn.com", "unrealengine.com", "gamerpower.com"} {
		if host == domain || strings.HasSuffix(host, "."+domain) {
			return value
		}
	}
	return ""
}

func parseMobileFeed(raw json.RawMessage) (*MobileOffer, error) {
	if len(raw) == 0 || raw[0] != '[' {
		return nil, nil
	}
	var rows []json.RawMessage
	if err := json.Unmarshal(raw, &rows); err != nil {
		return nil, err
	}
	var best *MobileOffer
	var published string
	for _, row := range rows {
		var entry mobileFeedEntry
		if json.Unmarshal(row, &entry) != nil || entry.Status != "Active" || entry.Type != "Game" ||
			!strings.Contains(strings.ToLower(entry.Description), "epic games store app") || entry.Published <= published {
			continue
		}
		end := mobileDay(entry.End)
		title := strings.TrimSpace(mobileTitleSuffix.ReplaceAllString(entry.Title, ""))
		if title == "" || end == "" {
			continue
		}
		published = entry.Published
		best = &MobileOffer{
			Title: &title, Image: mobileImage(entry.Image), URL: MobilePageURL,
			End: &end, Worth: mobileText(entry.Worth), Source: "epic",
		}
	}
	return best, nil
}

func readMobileCatalog(ctx context.Context, client *http.Client, operation, hash string, variables map[string]any, dest any) error {
	encoded, err := json.Marshal(variables)
	if err != nil {
		return err
	}
	query := url.Values{
		"operationName": {operation},
		"variables":     {string(encoded)},
		"extensions":    {`{"persistedQuery":{"version":1,"sha256Hash":"` + hash + `"}}`},
	}
	return readPublicJSON(ctx, client, mobileGraphQLURL+"?"+query.Encode(), mobileWebHeaders, dest)
}

func searchMobileCatalog(ctx context.Context, client *http.Client, title string) (mobileCatalogID, error) {
	var response struct {
		Data struct {
			Catalog struct {
				SearchStore struct{ Elements []mobileCatalogID }
			}
		}
	}
	err := readMobileCatalog(ctx, client, "primarySearchAutocomplete", mobileSearchHash, map[string]any{
		"allowCountries": "CN", "category": mobileSearchTypes, "count": 4, "country": "CN",
		"keywords": title, "locale": "en-US", "sortBy": nil, "sortDir": "DESC",
	}, &response)
	if elements := response.Data.Catalog.SearchStore.Elements; err == nil && len(elements) > 0 {
		return elements[0], nil
	}
	return mobileCatalogID{}, err
}

func fetchMobileProduct(ctx context.Context, client *http.Client, id mobileCatalogID) (link, image string) {
	if !mobileSegment.MatchString(id.OfferID) {
		return "", ""
	}
	var response struct {
		Data struct {
			Catalog struct{ CatalogOffer mobileCatalog }
		}
	}
	err := readMobileCatalog(ctx, client, "getCatalogOffer", mobileOfferHash, map[string]any{
		"locale": "zh-CN", "country": "CN", "offerId": id.OfferID, "sandboxId": id.SandboxID,
	}, &response)
	if err != nil {
		return "", ""
	}
	catalog := response.Data.Catalog.CatalogOffer
	for _, mapping := range catalog.CatalogNs.Mappings {
		if mapping.PageType == "offer" && mobileSegment.MatchString(mapping.PageSlug) {
			link = "https://store.epicgames.com/p/" + mapping.PageSlug
			break
		}
	}
	images := make(map[string]string, len(catalog.KeyImages))
	for _, candidate := range catalog.KeyImages {
		if safe := mobileImage(candidate.URL); safe != "" {
			images[candidate.Type] = safe
			if image == "" {
				image = safe
			}
		}
	}
	for _, key := range []string{"OfferImageWide", "DieselStoreFrontWide", "Thumbnail"} {
		if safe := images[key]; safe != "" {
			return link, safe
		}
	}
	return link, image
}

func fetchMobileClaims(ctx context.Context, client *http.Client, namespace, platform string) ([]mobileClaim, error) {
	query := url.Values{
		"count": {"10"}, "country": {"US"}, "locale": {"en-US"},
		"platform": {platform}, "start": {"0"}, "categories": {"games"},
	}
	var response struct {
		Data []struct {
			Content struct {
				Purchase []struct {
					PurchaseType    string
					PurchasePayload mobileCatalogID
					Price           struct{ DecimalPrice json.RawMessage }
					Discount        struct{ DiscountEndDate string }
				}
			}
		}
	}
	err := readPublicJSON(ctx, client, mobileSandboxURL+namespace+"/offers?"+query.Encode(), mobileAppHeaders, &response)
	if errors.Is(err, publicStatusError(http.StatusNotFound)) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var claims []mobileClaim
	for _, row := range response.Data {
		for _, purchase := range row.Content.Purchase {
			price := string(purchase.Price.DecimalPrice)
			id := purchase.PurchasePayload
			if purchase.PurchaseType == "Claim" && (price == `"0"` || mobileZeroPrice.MatchString(price)) &&
				mobileSegment.MatchString(id.SandboxID) && mobileSegment.MatchString(id.OfferID) {
				claims = append(claims, mobileClaim{
					part: "1-" + id.SandboxID + "-" + id.OfferID,
					end:  mobileDay(purchase.Discount.DiscountEndDate),
				})
			}
		}
	}
	return claims, nil
}

func fetchMobilePlatforms(ctx context.Context, client *http.Client, namespace string) ([]mobileClaim, error) {
	var claims [2][]mobileClaim
	var errs [2]error
	var group sync.WaitGroup
	for i, platform := range [2]string{"android", "ios"} {
		group.Go(func() {
			claims[i], errs[i] = fetchMobileClaims(ctx, client, namespace, platform)
		})
	}
	group.Wait()
	return append(claims[0], claims[1]...), errors.Join(errs[:]...)
}

func mobileCheckout(claims []mobileClaim) string {
	query := url.Values{}
	seen := make(map[string]bool, len(claims))
	for _, claim := range claims {
		if !seen[claim.part] {
			query.Add("offers", claim.part)
			seen[claim.part] = true
		}
	}
	return "https://store.epicgames.com/purchase?" + query.Encode()
}

func DiscoverMobile(ctx context.Context, client *http.Client) (*MobileOffer, error) {
	var raw json.RawMessage
	if err := readPublicJSON(ctx, client, mobileFeedURL, mobileWebHeaders, &raw); err != nil {
		return nil, err
	}
	offer, err := parseMobileFeed(raw)
	if err != nil || offer == nil {
		return offer, err
	}
	id, err := searchMobileCatalog(ctx, client, *offer.Title)
	if err != nil || !mobileSegment.MatchString(id.SandboxID) {
		return offer, nil
	}
	var page, image string
	var group sync.WaitGroup
	group.Go(func() {
		page, image = fetchMobileProduct(ctx, client, id)
	})
	claims, _ := fetchMobilePlatforms(ctx, client, id.SandboxID)
	group.Wait()
	if image != "" {
		offer.Image = image
	}
	if len(claims) > 0 {
		offer.URL, offer.Direct = mobileCheckout(claims), true
	} else if page != "" {
		offer.URL, offer.Direct = page, true
	}
	return offer, nil
}

func ResolveMobileFromPC(ctx context.Context, client *http.Client, games []Offer) (*MobileOffer, error) {
	type probe struct {
		game   Offer
		claims []mobileClaim
		err    error
	}
	var probes []probe
	seen := make(map[string]bool, len(games))
	for _, game := range games {
		if !game.Upcoming && mobileSegment.MatchString(game.Namespace) && !seen[game.Namespace] {
			probes = append(probes, probe{game: game})
			seen[game.Namespace] = true
		}
	}
	var group sync.WaitGroup
	for i := range probes {
		group.Go(func() {
			probes[i].claims, probes[i].err = fetchMobilePlatforms(ctx, client, probes[i].game.Namespace)
		})
	}
	group.Wait()
	var best *MobileOffer
	var bestEnd string
	var errs []error
	for _, result := range probes {
		if len(result.claims) == 0 {
			errs = append(errs, result.err)
			continue
		}
		var end string
		for _, claim := range result.claims {
			end = max(end, claim.end)
		}
		if best != nil && end <= bestEnd {
			continue
		}
		game := result.game
		title := game.TitleCn
		if title == "" {
			title = game.Title
		}
		bestEnd = end
		best = &MobileOffer{
			Title: mobileText(title), Image: mobileImage(game.Image), URL: mobileCheckout(result.claims),
			End: mobileText(end), Worth: mobileText(game.PriceOriginal), Source: "epic", Direct: true,
		}
	}
	if best != nil {
		return best, nil
	}
	return nil, errors.Join(errs...)
}

func FetchMobileBreaker(ctx context.Context, client *http.Client) (*MobileOffer, error) {
	var response struct {
		Layout struct {
			Section []struct {
				Breakers struct {
					BreakerList []struct{ Title, BackgroundImage string }
				}
			}
		}
	}
	if err := readPublicJSON(ctx, client, mobileCMSURL, mobileWebHeaders, &response); err != nil {
		return nil, err
	}
	for _, section := range response.Layout.Section {
		for _, item := range section.Breakers.BreakerList {
			if item.Title == "Free Giveaway" {
				if image := mobileImage(item.BackgroundImage); image != "" {
					return &MobileOffer{Image: image, URL: MobilePageURL, Source: "breaker"}, nil
				}
			}
		}
	}
	return nil, nil
}
