package storebrowse

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strings"

	"holdexar/server-go/internal/steamhttp"
)

const (
	BrowseURL        = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
	MaxURLLen        = 6000
	DefaultBatchSize = 300
)

type ID struct {
	AppID     int64 `json:"appid,omitempty"`
	BundleID  int64 `json:"bundleid,omitempty"`
	PackageID int64 `json:"packageid,omitempty"`
}

type Item map[string]any

type Batch struct {
	Items     map[int64]Item
	Missing   []int64
	BodyBytes int
}

func BuildIDsURL(ids []ID, cc, language string, extras bool) string {
	if ids == nil {
		ids = []ID{}
	}
	data := map[string]any{"include_basic_info": true, "include_all_purchase_options": true}
	if extras {
		for _, k := range []string{"include_release", "include_assets", "include_reviews", "include_platforms", "include_coming_soon"} {
			data[k] = true
		}
		data["include_tag_count"] = 20
	}
	body, _ := json.Marshal(struct {
		IDs     []ID `json:"ids"`
		Context any  `json:"context"`
		Data    any  `json:"data_request"`
	}{ids, map[string]any{"language": language, "country_code": strings.ToUpper(cc), "steam_realm": 1}, data})
	// Keep the request within Steam's request-line limit without double encoding.
	encoded := url.QueryEscape(string(body))
	for _, p := range [][2]string{{"%7B", "{"}, {"%7D", "}"}, {"%22", "\""}, {"%2C", ","}, {"%3A", ":"}} {
		encoded = strings.ReplaceAll(encoded, p[0], p[1])
	}
	return BrowseURL + "?input_json=" + encoded
}

func BuildURL(appids []int64, cc, language string, extras bool) string {
	ids := make([]ID, len(appids))
	for i, id := range appids {
		ids[i] = ID{AppID: id}
	}
	return BuildIDsURL(ids, cc, language, extras)
}

func PlanIDBatches(ids []ID, cc, language string, extras bool, batchSize int) [][]ID {
	if batchSize <= 0 {
		batchSize = DefaultBatchSize
	}
	budget := MaxURLLen - len(BuildIDsURL(nil, cc, language, extras))
	seen := map[ID]bool{}
	var batches [][]ID
	var current []ID
	used := 0
	for _, id := range ids {
		if seen[id] {
			continue
		}
		seen[id] = true
		body, _ := json.Marshal(id)
		size := len(body)
		add := size
		if len(current) > 0 {
			add++
		}
		if len(current) > 0 && (used+add > budget || len(current) >= batchSize) {
			batches = append(batches, current)
			current = nil
			used = 0
			add = size
		}
		current = append(current, id)
		used += add
	}
	if len(current) > 0 {
		batches = append(batches, current)
	}
	return batches
}

func PlanBatches(appids []int64, cc, language string, extras bool, batchSize int) [][]int64 {
	ids := make([]ID, len(appids))
	for i, a := range appids {
		ids[i] = ID{AppID: a}
	}
	var out [][]int64
	for _, batch := range PlanIDBatches(ids, cc, language, extras, batchSize) {
		row := make([]int64, len(batch))
		for i, id := range batch {
			row[i] = id.AppID
		}
		out = append(out, row)
	}
	return out
}

func ParseResponse(body []byte, appids []int64) (Batch, error) {
	var payload struct {
		Response *struct {
			StoreItems []Item `json:"store_items"`
		} `json:"response"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return Batch{}, fmt.Errorf("store browse response: %w", err)
	}
	if payload.Response == nil {
		return Batch{}, fmt.Errorf("store browse response missing response object")
	}
	return mapItems(payload.Response.StoreItems, appids, len(body)), nil
}

func mapItems(items []Item, appids []int64, size int) Batch {
	out := Batch{Items: map[int64]Item{}, BodyBytes: size}
	if len(items) == len(appids) {
		for i, appid := range appids {
			item := items[i]
			if item != nil && integer(item["appid"]) == nil {
				item["appid"] = appid
			}
			if item != nil && value(item["appid"]) == 0 {
				item["appid"] = appid
			}
			out.Items[appid] = item
		}
	} else {
		for _, item := range items {
			if id := value(item["appid"]); id != 0 {
				out.Items[id] = item
			}
		}
		for _, id := range appids {
			if _, ok := out.Items[id]; !ok {
				out.Missing = append(out.Missing, id)
				out.Items[id] = nil
			}
		}
	}
	return out
}

func Fetch(ctx context.Context, client *steamhttp.Client, appids []int64, cc, language string, extras bool, exitKey string) (Batch, error) {
	return FetchAt(ctx, client, BrowseURL, appids, cc, language, extras, exitKey)
}

func FetchAt(ctx context.Context, client *steamhttp.Client, endpoint string, appids []int64, cc, language string, extras bool, exitKey string) (Batch, error) {
	requestURL := strings.TrimSuffix(endpoint, "?") + strings.TrimPrefix(BuildURL(appids, cc, language, extras), BrowseURL)
	_, body, err := client.GetJSON(ctx, requestURL, exitKey)
	if err != nil {
		return Batch{}, err
	}
	return ParseResponse(body, appids)
}
