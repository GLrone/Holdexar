package epicfree

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"sync"
)

const pcSteamStoreURL = "https://store.steampowered.com"

// MatchSteam copies the offers and adds an ID only for an unambiguous Steam game match.
func MatchSteam(ctx context.Context, client *http.Client, offers []Offer) []Offer {
	matched := slices.Clone(offers)
	byTitle := make(map[string][]int)
	for i, offer := range offers {
		if offer.AppID != nil || offer.PriceOriginal == "0" || offer.OfferType != "" && offer.OfferType != "BASE_GAME" {
			continue
		}
		if title := strings.TrimSpace(offer.Title); title != "" {
			key := strings.ToLower(title)
			byTitle[key] = append(byTitle[key], i)
		}
	}
	if len(byTitle) == 0 {
		return matched
	}
	if client == nil {
		client = newPublicClient()
	}
	var group sync.WaitGroup
	for _, indices := range byTitle {
		group.Go(func() {
			appID := matchPCSteamGame(ctx, client, strings.TrimSpace(offers[indices[0]].Title))
			if appID == 0 {
				return
			}
			for _, i := range indices {
				id := appID
				matched[i].AppID = &id
			}
		})
	}
	group.Wait()
	return matched
}

func matchPCSteamGame(ctx context.Context, client *http.Client, title string) int64 {
	var search struct {
		Items []struct {
			Type string
			Name string
			ID   json.Number
		}
	}
	query := url.Values{"term": {title}, "cc": {"US"}, "l": {"english"}}
	if err := readPublicJSON(ctx, client, pcSteamStoreURL+"/api/storesearch/?"+query.Encode(), nil, &search); err != nil {
		return 0
	}
	var candidate int64
	for _, item := range search.Items {
		if item.Type != "app" || !strings.EqualFold(strings.TrimSpace(item.Name), title) {
			continue
		}
		id, err := item.ID.Int64()
		if err != nil || id <= 0 {
			continue
		}
		if candidate != 0 && candidate != id {
			return 0
		}
		candidate = id
	}
	if candidate == 0 {
		return 0
	}
	key := strconv.FormatInt(candidate, 10)
	var details map[string]struct {
		Success bool
		Data    struct {
			Type   string
			Name   string
			AppID  int64 `json:"steam_appid"`
			IsFree bool  `json:"is_free"`
		}
	}
	query.Del("term")
	query.Set("appids", key)
	if err := readPublicJSON(ctx, client, pcSteamStoreURL+"/api/appdetails?"+query.Encode(), nil, &details); err != nil {
		return 0
	}
	game := details[key]
	if game.Success && game.Data.AppID == candidate && game.Data.Type == "game" && !game.Data.IsFree &&
		strings.EqualFold(strings.TrimSpace(game.Data.Name), title) {
		return candidate
	}
	return 0
}
