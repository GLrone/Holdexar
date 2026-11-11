package storebrowse

import (
	"fmt"
	"math"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/crawler/dbwriter"
)

func ptr[T any](v T) *T { return &v }
func text(v any) string {
	if v == nil {
		return ""
	}
	return fmt.Sprint(v)
}
func obj(v any) Item {
	switch x := v.(type) {
	case Item:
		return x
	case map[string]any:
		return x
	}
	return nil
}
func list(v any) []any { x, _ := v.([]any); return x }
func truth(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case float64:
		return x != 0
	case int64:
		return x != 0
	case int:
		return x != 0
	}
	return true
}
func integer(v any) *int64 {
	if v == nil || v == "" {
		return nil
	}
	switch x := v.(type) {
	case int64:
		return ptr(x)
	case int:
		return ptr(int64(x))
	}
	f, err := strconv.ParseFloat(text(v), 64)
	if err != nil || math.IsInf(f, 0) || math.IsNaN(f) || f >= math.Exp2(63) || f < -math.Exp2(63) {
		return nil
	}
	return ptr(int64(f))
}
func value(v any) int64 {
	n := integer(v)
	if n == nil {
		return 0
	}
	return *n
}
func stringPtr(v string) *string {
	if v == "" {
		return nil
	}
	return &v
}

type Option struct {
	Price, Original               *int64
	Discount                      int64
	SubID                         int64
	IsGold, IsBundle              bool
	VersionSuffix, Name           string
	DiscountEndTS, FreeToKeepEnds *int64
	DiscountDesc                  *string
	BundleID, BundleDiscountPct   *int64
}

func ParseOptions(item Item, nameEn string) []Option {
	if nameEn == "" {
		nameEn = text(item["name"])
	}
	raw := list(item["purchase_options"])
	best := obj(item["best_purchase_option"])
	if len(raw) == 0 && len(best) > 0 {
		raw = []any{best}
	}
	hasDefault := false
	for _, v := range raw {
		if text(obj(v)["package_group"]) == "default" {
			hasDefault = true
		}
	}
	seen := map[int64]bool{}
	var out []Option
	for _, v := range raw {
		o := obj(v)
		if hasDefault && text(o["package_group"]) != "default" {
			continue
		}
		pkg, bundle := value(o["packageid"]), value(o["bundleid"])
		key := pkg
		if key == 0 {
			key = -bundle
		}
		if key == 0 || seen[key] {
			continue
		}
		seen[key] = true
		price, original := integer(o["final_price_in_cents"]), integer(o["original_price_in_cents"])
		if original == nil {
			original = price
		}
		discount := value(o["discount_pct"])
		if discount <= 0 && price != nil && original != nil && *price != 0 && *original > *price {
			discount = int64(math.RoundToEven(float64(*original-*price) / float64(*original) * 100))
		}
		name := text(o["purchase_option_name"])
		option := Option{Price: price, Original: original, Discount: discount, SubID: pkg, IsGold: IsGoldEdition(name), IsBundle: pkg == 0 && bundle != 0, VersionSuffix: ExtractVersionSuffix(name, nameEn), Name: name, FreeToKeepEnds: integer(o["free_to_keep_ends"]), BundleID: integer(o["bundleid"]), BundleDiscountPct: integer(o["bundle_discount_pct"])}
		if option.SubID == 0 {
			option.SubID = bundle
		}
		if option.FreeToKeepEnds == nil {
			option.FreeToKeepEnds = integer(best["free_to_keep_ends"])
		}
		if discounts := list(o["active_discounts"]); len(discounts) > 0 {
			first := obj(discounts[0])
			option.DiscountEndTS = integer(first["discount_end_date"])
			desc := []rune(text(first["discount_description"]))
			if len(desc) > 60 {
				desc = desc[:60]
			}
			option.DiscountDesc = stringPtr(string(desc))
		}
		out = append(out, option)
	}
	return out
}

func Evaluate(item Item, nameEn string) (string, []Option) {
	if item == nil || value(item["success"]) != 1 || !truth(item["visible"]) || truth(item["unvailable_for_country_restriction"]) {
		return "locked", nil
	}
	if truth(item["is_free_temporarily"]) || truth(obj(item["best_purchase_option"])["is_free_to_keep"]) {
		return "free_promo", ParseOptions(item, nameEn)
	}
	if truth(item["is_free"]) {
		return "free", nil
	}
	opts := ParseOptions(item, nameEn)
	if len(opts) == 0 {
		return "locked", nil
	}
	return "ok", opts
}

// Missing responses and incomplete giveaways must use the existing failure writer.
func PriceRows(appid int64, cc, currency string, item Item, nameEn string) ([]dbwriter.PriceRow, string) {
	if item == nil {
		return nil, "missing"
	}
	status, options := Evaluate(item, nameEn)
	base := dbwriter.PriceRow{AppID: appid, RegionCode: ptr(strings.ToUpper(cc)), Currency: ptr(currency), PriceStatus: ptr("ok"), SteamAnswer: ptr("ok")}
	var rows []dbwriter.PriceRow
	switch status {
	case "ok":
		var pkgs []Option
		for _, o := range options {
			if !o.IsBundle {
				pkgs = append(pkgs, o)
			}
		}
		if len(pkgs) > 0 {
			options = pkgs
		}
		for _, o := range options {
			r := base
			r.Price = o.Price
			r.OriginalPrice = o.Original
			r.DiscountPercent = ptr(o.Discount)
			r.SubID = ptr(o.SubID)
			r.IsGold = ptr(o.IsGold)
			r.IsBundle = ptr(o.IsBundle)
			r.VersionSuffix = stringPtr(o.VersionSuffix)
			r.DiscountEndTS = o.DiscountEndTS
			r.DiscountDesc = o.DiscountDesc
			r.BundleID = o.BundleID
			r.BundleDiscountPct = o.BundleDiscountPct
			rows = append(rows, r)
		}
	case "free_promo":
		for _, o := range options {
			if o.Price == nil || *o.Price != 0 || o.Original == nil || *o.Original <= 0 {
				continue
			}
			r := base
			r.Price = ptr(int64(0))
			r.OriginalPrice = o.Original
			r.DiscountPercent = ptr(o.Discount)
			if o.Discount == 0 {
				r.DiscountPercent = ptr(int64(100))
			}
			r.SubID = ptr(o.SubID)
			r.IsGold = ptr(false)
			r.IsBundle = ptr(false)
			r.SteamAnswer = ptr("free")
			r.PromoEndTS = o.FreeToKeepEnds
			if r.PromoEndTS == nil || *r.PromoEndTS == 0 {
				r.PromoEndTS = o.DiscountEndTS
			}
			r.DiscountEndTS = r.PromoEndTS
			rows = append(rows, r)
		}
		if len(rows) == 0 {
			return nil, "missing"
		}
	case "free":
		r := base
		r.Price = ptr(int64(0))
		r.OriginalPrice = ptr(int64(0))
		r.DiscountPercent = ptr(int64(0))
		r.SubID = ptr(int64(0))
		r.SteamAnswer = ptr("free")
		rows = append(rows, r)
	default:
		r := base
		r.PriceStatus = ptr(status)
		r.SteamAnswer = ptr("locked")
		rows = append(rows, r)
	}
	return rows, status
}

func HeaderImage(item Item) string {
	assets := obj(item["assets"])
	format, file := text(assets["asset_url_format"]), text(assets["header"])
	if format == "" || file == "" {
		return ""
	}
	return "https://shared.akamai.steamstatic.com/store_item_assets/" + strings.ReplaceAll(format, "${FILENAME}", file)
}

func Tags(item Item) [][2]int64 {
	var out [][2]int64
	for _, v := range list(item["tags"]) {
		o := obj(v)
		if id := value(o["tagid"]); id != 0 {
			out = append(out, [2]int64{id, value(o["weight"])})
		}
	}
	if len(out) > 0 {
		return out
	}
	for _, v := range list(item["tagids"]) {
		if id := value(v); id != 0 {
			out = append(out, [2]int64{id, 0})
		}
	}
	return out
}

func GameType(item Item) string {
	switch value(item["type"]) {
	case 1:
		return "DEMO"
	case 2:
		return "MOD"
	case 3:
		return "TOOL"
	case 4:
		return "DLC"
	}
	return "GAME"
}

func ComingSoon(item Item) bool {
	return truth(item["is_coming_soon"]) || truth(obj(item["release"])["is_coming_soon"])
}
func IsVisible(item Item) bool {
	return item != nil && value(item["success"]) == 1 && truth(item["visible"])
}

func BuildGame(appid int64, zh, en Item, preserved *dbwriter.GameData, now time.Time) (dbwriter.GameData, bool) {
	g := dbwriter.GameData{AppID: appid, Developers: []any{}, Publishers: []any{}, IsVisualNovel: ptr(false), ReviewCount: ptr(int64(0)), PositiveReviews: ptr(int64(0))}
	if preserved != nil {
		g = *preserved
		g.AppID = appid
	}
	src := zh
	if src == nil {
		src = en
	}
	if src == nil && preserved == nil {
		return g, false
	}
	g.CreatedAt = &now
	g.UpdatedAt = &now
	if src == nil {
		return g, true
	}
	nameEn := strings.TrimSpace(text(en["name"]))
	if nameEn != "" {
		g.NameEn = &nameEn
	}
	name := strings.TrimSpace(text(zh["name"]))
	if name != "" {
		g.Name = &name
	} else if g.Name == nil || *g.Name == "" {
		g.Name = g.NameEn
	}
	g.Type = ptr(GameType(src))
	if image := HeaderImage(src); image != "" {
		g.HeaderImage = &image
	}
	features := list(obj(src["categories"])["feature_categoryids"])
	family, cards, adult := false, false, false
	for _, v := range features {
		if value(v) == 62 {
			family = true
		}
		if value(v) == 29 {
			cards = true
		}
	}
	for _, v := range list(src["content_descriptorids"]) {
		if value(v) == 3 || value(v) == 4 {
			adult = true
		}
	}
	g.FamilySharing = &family
	g.TradingCards = &cards
	g.IsAdult = &adult
	release := value(obj(src["release"])["steam_release_date"])
	if release > 0 && release <= 253402300799 {
		g.ReleaseDate = ptr(time.Unix(release, 0).UTC().Format("2006-01-02"))
	}
	basic := obj(src["basic_info"])
	for key, dest := range map[string]*[]any{"developers": &g.Developers, "publishers": &g.Publishers} {
		var names []any
		for _, v := range list(basic[key]) {
			if n := text(obj(v)["name"]); n != "" {
				names = append(names, n)
			}
		}
		if len(names) > 0 {
			*dest = names
		} else if *dest == nil {
			*dest = []any{}
		}
	}
	reviews := obj(obj(src["reviews"])["summary_filtered"])
	count, pct := value(reviews["review_count"]), value(reviews["percent_positive"])
	if count > 0 && pct > 0 {
		g.ReviewCount = &count
		g.PositiveRate = ptr(pct * 100)
		g.PositiveReviews = ptr(int64(math.RoundToEven(float64(count) * float64(pct) / 100)))
	}
	return g, true
}

func BuildEntry(appid int64, cc, currency string, item, zh, en Item, preserved *dbwriter.GameData, now time.Time) (dbwriter.BatchEntry, string, bool) {
	g, ok := BuildGame(appid, zh, en, preserved, now)
	if !ok {
		return dbwriter.BatchEntry{}, "skip", false
	}
	return Classify(appid, cc, currency, item, g)
}

func Classify(appid int64, cc, currency string, item Item, game dbwriter.GameData) (dbwriter.BatchEntry, string, bool) {
	g := game
	g.AppID = appid
	if item == nil {
		return dbwriter.BatchEntry{Game: g}, "missing", false
	}
	if g.Type != nil && *g.Type != "GAME" && *g.Type != "DLC" {
		return dbwriter.BatchEntry{Game: g}, "skip", false
	}
	nameEn := ""
	if g.NameEn != nil {
		nameEn = *g.NameEn
	}
	prices, status := PriceRows(appid, cc, currency, item, nameEn)
	return dbwriter.BatchEntry{Game: g, Prices: prices}, status, len(prices) > 0
}
