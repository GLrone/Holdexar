package storebrowse

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strings"
	"testing"
	"time"

	"holdexar/server-go/internal/crawler/dbwriter"
	"holdexar/server-go/internal/steamhttp"
)

func itemJSON(t *testing.T, s string) Item {
	t.Helper()
	var item Item
	if err := json.Unmarshal([]byte(s), &item); err != nil {
		t.Fatal(err)
	}
	return item
}

func TestBatchPlanAndURL(t *testing.T) {
	ids := []int64{5, 3, 5, 1, 3, 2}
	if got := PlanBatches(ids, "cn", "schinese", true, 2); !reflect.DeepEqual(got, [][]int64{{5, 3}, {1, 2}}) {
		t.Fatal(got)
	}
	mixed := []ID{{BundleID: 13608}, {PackageID: 1066582}}
	u := BuildIDsURL(mixed, "cn", "schinese", true)
	if strings.Contains(u, "%7B") || !strings.Contains(u, `"ids"`) {
		t.Fatal(u)
	}
	parsed, err := url.Parse(u)
	if err != nil {
		t.Fatal(err)
	}
	var request struct {
		IDs     []ID           `json:"ids"`
		Context map[string]any `json:"context"`
		Data    map[string]any `json:"data_request"`
	}
	if err := json.Unmarshal([]byte(parsed.Query().Get("input_json")), &request); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(request.IDs, mixed) || request.Context["country_code"] != "CN" || request.Context["language"] != "schinese" || request.Data["include_tag_count"] != float64(20) {
		t.Fatalf("%+v", request)
	}
	var many []int64
	for i := int64(900000000); i < 900001200; i++ {
		many = append(many, i)
	}
	batches := PlanBatches(many, "us", "english", true, 400)
	var flattened []int64
	for i, batch := range batches {
		if len(BuildURL(batch, "us", "english", true)) > MaxURLLen {
			t.Fatalf("oversized batch %d", i)
		}
		if i < len(batches)-1 && len(batch) < 400 {
			if len(BuildURL(append(append([]int64{}, batch...), batches[i+1][0]), "us", "english", true)) <= MaxURLLen {
				t.Fatal("batch not full")
			}
		}
		flattened = append(flattened, batch...)
	}
	if !reflect.DeepEqual(flattened, many) {
		t.Fatal("lost or repeated ids")
	}
}

func TestResponseMappingDistinguishesUnavailableAndTruncated(t *testing.T) {
	b, err := ParseResponse([]byte(`{"response":{"store_items":[{"appid":0,"success":15,"visible":false},{"appid":220,"success":1,"visible":true}]}}`), []int64{228980, 220})
	if err != nil {
		t.Fatal(err)
	}
	if value(b.Items[228980]["appid"]) != 228980 || len(b.Missing) != 0 {
		t.Fatalf("%+v", b)
	}
	if status, _ := Evaluate(b.Items[228980], ""); status != "locked" {
		t.Fatal(status)
	}
	b, err = ParseResponse([]byte(`{"response":{"store_items":[{"appid":220,"success":1,"visible":true}]}}`), []int64{228980, 220})
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(b.Missing, []int64{228980}) || b.Items[228980] != nil {
		t.Fatalf("%+v", b)
	}
	if _, err := ParseResponse([]byte(`{"error":"bad"}`), []int64{220}); err == nil {
		t.Fatal("invalid envelope accepted")
	}
}

func TestFetchAtUsesSharedHTTPAndUnescapedRequest(t *testing.T) {
	observed := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		observed = true
		if r.Method != "GET" || !strings.Contains(r.RequestURI, `"ids"`) {
			t.Error(r.Method, r.RequestURI)
		}
		var req struct {
			Context map[string]any `json:"context"`
		}
		if err := json.Unmarshal([]byte(r.URL.Query().Get("input_json")), &req); err != nil {
			t.Error(err)
		}
		if req.Context["country_code"] != "JP" || req.Context["language"] != "english" {
			t.Error(req)
		}
		_, _ = w.Write([]byte(`{"response":{"store_items":[{"appid":220,"success":1,"visible":true,"is_free":true}]}}`))
	}))
	defer srv.Close()
	client := steamhttp.New(steamhttp.Config{MaxRetries: 1})
	b, err := FetchAt(context.Background(), client, srv.URL, []int64{220}, "jp", "english", true, "direct")
	if err != nil || !observed || b.BodyBytes == 0 {
		t.Fatalf("%+v %v", b, err)
	}
	rows, status := PriceRows(220, "jp", "JPY", b.Items[220], "")
	if status != "free" || len(rows) != 1 || *rows[0].Price != 0 || *rows[0].Currency != "JPY" || *rows[0].RegionCode != "JP" {
		t.Fatal(rows, status)
	}
}

func TestOptionsKeepOriginalUnitsAndPackageSemantics(t *testing.T) {
	item := itemJSON(t, `{"appid":1113000,"name":"Persona 4 Golden","success":1,"visible":true,"purchase_options":[
	{"packageid":447601,"package_group":"default","purchase_option_name":"Persona 4 Golden","final_price_in_cents":"699","original_price_in_cents":"22900","discount_pct":"97","active_discounts":[{"discount_end_date":"1800000000","discount_description":"Weekly Deal"}]},
	{"packageid":376686,"package_group":"default","purchase_option_name":"Persona 4 Golden - Digital Deluxe Edition","final_price_in_cents":"15600","original_price_in_cents":"15600"},
	{"packageid":447601,"package_group":"default","final_price_in_cents":"1"},
	{"bundleid":880,"package_group":"default","purchase_option_name":"Persona bundle","final_price_in_cents":"100"},
	{"packageid":1,"package_group":"other","final_price_in_cents":"1"}]}`)
	opts := ParseOptions(item, "")
	if len(opts) != 3 || *opts[0].Price != 699 || *opts[0].Original != 22900 || opts[0].Discount != 97 || opts[1].VersionSuffix != "Digital Deluxe Edition" || !opts[2].IsBundle {
		t.Fatalf("%+v", opts)
	}
	rows, status := PriceRows(1113000, "cn", "CNY", item, "")
	if status != "ok" || len(rows) != 2 || *rows[0].DiscountEndTS != 1800000000 || *rows[0].DiscountDesc != "Weekly Deal" || rows[0].VersionSuffix != nil {
		t.Fatalf("%+v %s", rows, status)
	}
	if *rows[0].Price != 699 || *rows[0].OriginalPrice != 22900 {
		t.Fatal("price units changed")
	}
	item = itemJSON(t, `{"name":"Gori","success":1,"visible":true,"best_purchase_option":{"bundleid":"91","final_price_in_cents":"10","original_price_in_cents":"20"}}`)
	rows, status = PriceRows(1, "us", "USD", item, "")
	if len(rows) != 1 || !*rows[0].IsBundle || *rows[0].DiscountPercent != 50 || status != "ok" {
		t.Fatal(rows, status)
	}
}

func TestFreeGiveawayLockedAndMissing(t *testing.T) {
	item := itemJSON(t, `{"success":1,"visible":true,"name":"Game","is_free":true,"is_free_temporarily":true,"best_purchase_option":{"is_free_to_keep":true,"free_to_keep_ends":"1800000000"},"purchase_options":[{"packageid":1,"purchase_option_name":"Game","final_price_in_cents":"999","original_price_in_cents":"999"},{"packageid":2,"purchase_option_name":"Game Limited Free Promotional Package","final_price_in_cents":"0","original_price_in_cents":"999","discount_pct":"100"}]}`)
	rows, status := PriceRows(1, "us", "USD", item, "")
	if status != "free_promo" || len(rows) != 1 || *rows[0].SubID != 2 || *rows[0].Price != 0 || *rows[0].PromoEndTS != 1800000000 || rows[0].VersionSuffix != nil || *rows[0].SteamAnswer != "free" {
		t.Fatal(rows, status)
	}
	delete(item, "purchase_options")
	if rows, status = PriceRows(1, "us", "USD", item, ""); status != "missing" || len(rows) != 0 {
		t.Fatal(rows, status)
	}
	for _, body := range []string{`{"success":15,"visible":false}`, `{"success":1,"visible":true}`, `{"success":1,"visible":true,"is_free":true,"unvailable_for_country_restriction":true}`} {
		rows, status = PriceRows(1, "us", "USD", itemJSON(t, body), "")
		if status != "locked" || len(rows) != 1 || rows[0].Price != nil || *rows[0].SteamAnswer != "locked" {
			t.Fatal(rows, status)
		}
	}
	if rows, status = PriceRows(1, "us", "USD", nil, ""); status != "missing" || len(rows) != 0 {
		t.Fatal(rows, status)
	}
}

func TestMetadataPreservesUnavailableFieldsAndReviewSummary(t *testing.T) {
	keep := dbwriter.GameData{Name: ptr("旧名"), NameEn: ptr("Persona 4 Golden"), ChineseSupport: ptr("full"), PositiveRate: ptr(int64(9700)), ReviewCount: ptr(int64(71207)), PositiveReviews: ptr(int64(69071)), IsVisualNovel: ptr(true), ReleaseDate: ptr("2020-06-13")}
	zh := itemJSON(t, `{"name":"女神异闻录４ 黄金版","type":0,"visible":true,"categories":{"feature_categoryids":[62,29]},"content_descriptorids":[1,2,5],"basic_info":{"developers":[{"name":"ATLUS"}],"publishers":[{"name":"SEGA"}]},"assets":{"asset_url_format":"steam/apps/1113000/${FILENAME}","header":"header.jpg"},"tags":[{"tagid":122,"weight":"100"}],"reviews":{"summary_filtered":{"review_count":"0","percent_positive":"0"}}}`)
	now := time.Date(2026, 11, 6, 0, 0, 0, 0, time.UTC)
	g, ok := BuildGame(1113000, zh, nil, &keep, now)
	if !ok || *g.NameEn != "Persona 4 Golden" || *g.ChineseSupport != "full" || *g.PositiveRate != 9700 || *g.ReviewCount != 71207 || !*g.FamilySharing || !*g.TradingCards || *g.IsAdult || !*g.IsVisualNovel || *g.ReleaseDate != "2020-06-13" || !strings.HasSuffix(*g.HeaderImage, "steam/apps/1113000/header.jpg") {
		t.Fatalf("%+v", g)
	}
	zh["reviews"] = map[string]any{"summary_filtered": map[string]any{"review_count": "1001", "percent_positive": "85"}}
	zh["release"] = map[string]any{"steam_release_date": "1592006400"}
	zh["content_descriptorids"] = []any{float64(3)}
	g, ok = BuildGame(1113000, zh, nil, &keep, now)
	if !ok || *g.PositiveRate != 8500 || *g.PositiveReviews != 851 || !*g.IsAdult || *g.ReleaseDate != "2020-06-13" {
		t.Fatalf("%+v", g)
	}
	if !reflect.DeepEqual(Tags(zh), [][2]int64{{122, 100}}) {
		t.Fatal(Tags(zh))
	}
	if _, ok = BuildGame(1, nil, nil, nil, now); ok {
		t.Fatal("empty game invented")
	}
	zh["type"] = float64(3)
	if _, status, ok := BuildEntry(1, "us", "USD", zh, zh, nil, nil, now); ok || status != "skip" {
		t.Fatal(status, ok)
	}
}

func TestEditionNamesAndLocalizedPriceTails(t *testing.T) {
	for _, tail := range []string{"¥ 37.00", "259 руб.", "1 850₸", "169₴", "$5.49 USD", "CLP$ 4.400", "P289.95", "1.95 KD", "R 79.00", "S/.22.00", "CHF 25.--", "35,99 zł", "120.000₫", "Rp 69 999", "₩ 10,500"} {
		t.Run(tail, func(t *testing.T) {
			if got := ExtractVersionSuffix("Game - Deluxe Edition - "+tail, "Game"); got != "Deluxe Edition" {
				t.Fatal(got)
			}
		})
	}
	for _, pair := range [][3]string{{"Game：Ultimate Edition", "Game", "Ultimate Edition"}, {"Game (English/Chinese/Japanese)", "Game", ""}, {"Game - 2026", "Game", "2026"}, {"Game <span>¥ 50</span><span>¥ 10</span>", "Game", ""}, {"Other complete edition", "Game", "Complete Edition"}} {
		if got := ExtractVersionSuffix(pair[0], pair[1]); got != pair[2] {
			t.Fatal(pair, got)
		}
	}
	if IsGoldEdition("Gold Edition Commercial License") || !IsGoldEdition("Game 黄金版") {
		t.Fatal("gold classification")
	}
}

func TestClassifyKeepsCurrentNoOptionsAndMissingSemantics(t *testing.T) {
	game := dbwriter.GameData{AppID: 1, NameEn: ptr("Game"), Type: ptr("GAME"), ReleaseDate: ptr("2999-01-01")}
	item := itemJSON(t, `{"success":1,"visible":true,"is_coming_soon":true}`)
	entry, status, keep := Classify(1, "cn", "CNY", item, game)
	if !keep || status != "locked" || *entry.Prices[0].SteamAnswer != "locked" || *entry.Game.Type != "GAME" {
		t.Fatal(entry, status, keep)
	}
	for _, typ := range []string{"DEMO", "MOD", "TOOL", "COMING_SOON", "IMPORT"} {
		game.Type = ptr(typ)
		entry, status, keep = Classify(1, "cn", "CNY", item, game)
		if keep || status != "skip" || len(entry.Prices) != 0 || *entry.Game.Type != typ {
			t.Fatal(entry, status, keep)
		}
		_, status, keep = Classify(1, "cn", "CNY", nil, game)
		if keep || status != "missing" {
			t.Fatal("truncated reply must remain missing", status, keep)
		}
	}
	game.Type = ptr("GAME")
	item["is_free_temporarily"] = true
	_, status, keep = Classify(1, "cn", "CNY", item, game)
	if keep || status != "missing" {
		t.Fatal("invalid giveaway must remain missing despite future release", status, keep)
	}
}

func TestFollowParentOptionsDeduplicatesFetchAndPreservesSharedInputs(t *testing.T) {
	child := func(parent int64) Item {
		return Item{"success": float64(1), "visible": true, "name": "Child", "related_items": Item{"parent_appid": parent}}
	}
	parent := itemJSON(t, `{"success":1,"visible":true,"best_purchase_option":{"packageid":"99","purchase_option_name":"Child","final_price_in_cents":"199","original_price_in_cents":"299"}}`)
	items := map[int64]Item{1: child(100), 2: child(100), 3: child(200), 4: child(300), 5: child(500), 6: child(6), 100: parent, 500: nil}
	items[4]["is_free"] = true
	before, _ := json.Marshal(items)
	calls := 0
	out, err := FollowParentOptions(context.Background(), items, []int64{1, 2, 3, 4, 5, 6}, func(_ context.Context, ids []int64) (map[int64]Item, error) {
		calls++
		if !reflect.DeepEqual(ids, []int64{200}) {
			t.Fatal(ids)
		}
		return map[int64]Item{200: parent}, nil
	})
	if err != nil || calls != 1 {
		t.Fatal(calls, err)
	}
	after, _ := json.Marshal(items)
	if string(before) != string(after) {
		t.Fatal("shared source map changed")
	}
	for _, id := range []int64{1, 2, 3} {
		rows, status := PriceRows(id, "us", "USD", out[id], "Child")
		if status != "ok" || len(rows) != 1 || *rows[0].SubID != 99 || *rows[0].Price != 199 {
			t.Fatal(rows, status)
		}
	}
	if _, inherited := out[4]["parent_appid"]; inherited {
		t.Fatal("free child inherited a parent option")
	}
	out[1]["name"] = "Changed"
	if items[1]["name"] != "Child" {
		t.Fatal("modified child aliases shared metadata")
	}
	if _, err = FollowParentOptions(context.Background(), map[int64]Item{3: child(200)}, []int64{3}, func(context.Context, []int64) (map[int64]Item, error) { return nil, errors.New("failed") }); err == nil {
		t.Fatal("parent fetch failure hidden")
	}
}

func TestTagsFallbackRetainsOrder(t *testing.T) {
	item := itemJSON(t, `{"tags":[{"tagid":2,"weight":"9"},{"tagid":1,"weight":"8"}],"tagids":[1,2]}`)
	if got := Tags(item); !reflect.DeepEqual(got, [][2]int64{{2, 9}, {1, 8}}) {
		t.Fatal(got)
	}
	delete(item, "tags")
	if got := Tags(item); !reflect.DeepEqual(got, [][2]int64{{1, 0}, {2, 0}}) {
		t.Fatal(got)
	}
}
