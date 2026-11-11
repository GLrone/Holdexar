package epicfree

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

const pcPromotionsURL = "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions"
const pcStoreURL = "https://store.epicgames.com/en-US"
const pcDayLayout = "2006-1-2"

var pcStoreSlug = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_-]*$`)

type pcCatalog struct {
	Errors []json.RawMessage `json:"errors"`
	Data   struct {
		Catalog struct {
			SearchStore struct {
				Elements []pcElement `json:"elements"`
			} `json:"searchStore"`
		} `json:"Catalog"`
	} `json:"data"`
}

type pcElement struct {
	ID, Namespace, Title, OfferType, Status, ProductSlug, URLSlug string
	IsCodeRedemptionOnly                                          bool
	KeyImages                                                     []struct {
		Type, URL string
	}
	CatalogNs struct {
		Mappings []struct {
			PageSlug string
		}
	}
	Price struct {
		TotalPrice struct {
			OriginalPrice, DiscountPrice *int64
			FmtPrice                     struct {
				OriginalPrice string
			}
		}
	}
	Promotions struct {
		PromotionalOffers, UpcomingPromotionalOffers []pcPromotionGroup
	}
}

type pcPromotionGroup struct {
	PromotionalOffers []struct {
		StartDate, EndDate string
		DiscountSetting    struct {
			DiscountType       string
			DiscountPercentage *float64
		}
	}
}

// FetchPC reads the authoritative PC window and optionally enriches it with the Chinese catalog.
func FetchPC(ctx context.Context, client *http.Client) ([]Offer, error) {
	if client == nil {
		client = newPublicClient()
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	localized := make(chan []pcElement, 1)
	go func() {
		elements, _ := readPCCatalog(ctx, client, "zh-CN", "CN")
		localized <- elements
	}()
	elements, err := readPCCatalog(ctx, client, "en-US", "US")
	if err != nil {
		return nil, err
	}
	now := time.Now()
	offers := make([]Offer, 0, len(elements))
	for _, e := range elements {
		if offer, ok := e.offer(now); ok {
			offers = append(offers, offer)
		}
	}
	if len(offers) == 0 {
		return offers, nil
	}
	cnByID := make(map[string]pcElement)
	for _, e := range <-localized {
		cnByID[e.ID] = e
	}
	for i := range offers {
		if e, ok := cnByID[offers[i].ElementID]; ok {
			offers[i].TitleCn = strings.TrimSpace(e.Title)
			if price := e.originalPrice(); price != "" && price != "0" && offers[i].PriceOriginal != "0" {
				offers[i].PriceOriginal = price
			}
		}
	}
	return offers, nil
}

func readPCCatalog(ctx context.Context, client *http.Client, locale, country string) ([]pcElement, error) {
	address := pcPromotionsURL + "?" + url.Values{"locale": {locale}, "country": {country}}.Encode()
	var payload pcCatalog
	if err := readPublicJSON(ctx, client, address, nil, &payload); err != nil {
		return nil, fmt.Errorf("Epic PC catalog (%s): %w", locale, err)
	}
	elements := payload.Data.Catalog.SearchStore.Elements
	if len(payload.Errors) > 0 || elements == nil {
		return nil, fmt.Errorf("Epic PC catalog (%s): invalid catalog response", locale)
	}
	return elements, nil
}

func (e pcElement) offer(now time.Time) (Offer, bool) {
	title := strings.TrimSpace(e.Title)
	if title == "" || e.ID == "" || e.IsCodeRedemptionOnly || e.Status != "" && e.Status != "ACTIVE" {
		return Offer{}, false
	}
	var start, end time.Time
	for _, groups := range [][]pcPromotionGroup{e.Promotions.PromotionalOffers, e.Promotions.UpcomingPromotionalOffers} {
		for _, group := range groups {
			for _, p := range group.PromotionalOffers {
				d := p.DiscountSetting
				if d.DiscountPercentage == nil || *d.DiscountPercentage != 0 || d.DiscountType != "" && d.DiscountType != "PERCENTAGE" {
					continue
				}
				from, fromErr := time.Parse(time.RFC3339, p.StartDate)
				to, toErr := time.Parse(time.RFC3339, p.EndDate)
				if fromErr != nil || toErr != nil || !to.After(from) || !to.After(now) {
					continue
				}
				if price := e.Price.TotalPrice.DiscountPrice; !from.After(now) && price != nil && *price != 0 {
					continue
				}
				// A card represents one real window, with the current or next promotion first.
				if start.IsZero() || from.Before(start) || from.Equal(start) && to.After(end) {
					start, end = from, to
				}
			}
		}
	}
	if start.IsZero() {
		return Offer{}, false
	}
	return Offer{
		Title: title, Namespace: e.Namespace, ElementID: e.ID, OfferType: e.OfferType,
		Start: start.UTC().Format(pcDayLayout), End: end.UTC().Format(pcDayLayout), Upcoming: start.After(now),
		Image: e.cover(), URL: e.storeURL(), PriceOriginal: e.originalPrice(),
	}, true
}

func (e pcElement) originalPrice() string {
	price := e.Price.TotalPrice
	if price.OriginalPrice != nil && *price.OriginalPrice == 0 {
		return "0"
	}
	return strings.TrimSpace(price.FmtPrice.OriginalPrice)
}

func (e pcElement) storeURL() string {
	slug, _, _ := strings.Cut(e.ProductSlug, "/")
	if pcStoreSlug.MatchString(slug) {
		return pcStoreURL + "/p/" + slug
	}
	for _, mapping := range e.CatalogNs.Mappings {
		if pcStoreSlug.MatchString(mapping.PageSlug) {
			return pcStoreURL + "/p/" + mapping.PageSlug
		}
	}
	if pcStoreSlug.MatchString(e.URLSlug) {
		return pcStoreURL + "/p/" + e.URLSlug
	}
	return pcStoreURL + "/free-games"
}

func (e pcElement) cover() string {
	priority := map[string]int{"OfferImageWide": 3, "DieselStoreFrontWide": 2, "Thumbnail": 1}
	best, bestRank := "", -1
	for _, img := range e.KeyImages {
		rank := priority[img.Type]
		if rank <= bestRank {
			continue
		}
		u, err := url.Parse(img.URL)
		if err != nil || u.Scheme != "https" || u.User != nil || u.Port() != "" || u.Path == "" {
			continue
		}
		host := strings.ToLower(u.Hostname())
		if strings.HasSuffix(host, ".epicgames.com") || strings.HasSuffix(host, ".unrealengine.com") {
			best, bestRank = u.String(), rank
		}
	}
	return best
}
