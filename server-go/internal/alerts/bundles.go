package alerts

import (
	"cmp"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"slices"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/account"
	"holdexar/server-go/internal/games"
	"holdexar/server-go/internal/notifications"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

const (
	bundleDealMinDiscount      = 50
	bundleDealMinIntervalHours = 20.0
	bundleMinCnyFen            = 500
	bundleDealAtKey            = "alerts.bundle_deal_at"
)

type bundleRegionRow struct {
	region   string
	currency string
	price    int64
	hasPrice bool
	cny      int64
	discount int64
	count    int
}

type bundleGroup struct {
	id     int64
	name   string
	kind   int64
	url    string
	prices []bundleRegionRow
}

func bundleURL(rawURL string, bundleID, itemKind int64, region string) string {
	kind := "bundle"
	if itemKind == 1 {
		kind = "sub"
	}
	base := strings.TrimSpace(rawURL)
	if base == "" {
		base = fmt.Sprintf("https://store.steampowered.com/%s/%d/", kind, bundleID)
	}
	cc := strings.ToLower(strings.TrimSpace(region))
	if cc == "" {
		return base
	}
	sep := "?"
	if strings.Contains(base, "?") {
		sep = "&"
	}
	return base + sep + "cc=" + cc
}

func pickBundleRegion(prices []bundleRegionRow, ownerRegion string) bundleRegionRow {
	want := strings.ToUpper(strings.TrimSpace(ownerRegion))
	if want != "" {
		for _, p := range prices {
			if p.region == want {
				return p
			}
		}
	}
	best := prices[0]
	for _, p := range prices[1:] {
		if p.discount > best.discount || (p.discount == best.discount && p.cny < best.cny) {
			best = p
		}
	}
	return best
}

func bundleDealCooldownActive(ctx context.Context, q interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}, now time.Time) (bool, error) {
	var raw string
	err := q.QueryRowContext(ctx, `SELECT COALESCE((SELECT value_json FROM app_settings WHERE key=?),'null')`, bundleDealAtKey).Scan(&raw)
	if err != nil {
		return false, err
	}
	var last float64
	if json.Unmarshal([]byte(raw), &last) != nil || last <= 0 {
		return false, nil
	}
	nowSec := float64(now.UnixMilli()) / 1000.0
	return (nowSec-last)/3600.0 < bundleDealMinIntervalHours, nil
}

func bundleJSONLen(raw sql.NullString) int {
	if !raw.Valid || strings.TrimSpace(raw.String) == "" || raw.String == "null" {
		return 0
	}
	var items []any
	if json.Unmarshal([]byte(raw.String), &items) != nil {
		return 0
	}
	return len(items)
}

func loadRegionDisplayNames(ctx context.Context, q query) map[string]string {
	out := map[string]string{}
	rows, err := q.QueryContext(ctx, `SELECT code, name FROM crawl_regions`)
	if err != nil {
		return out
	}
	defer rows.Close()
	for rows.Next() {
		var code, name string
		if rows.Scan(&code, &name) == nil {
			out[strings.ToUpper(strings.TrimSpace(code))] = name
		}
	}
	return out
}

func (s *Service) CheckBundleDeals(ctx context.Context) ([]map[string]any, error) {
	now := s.now()
	active, err := bundleDealCooldownActive(ctx, s.db, now)
	if err != nil {
		return nil, err
	}
	if active {
		return []map[string]any{}, nil
	}

	rows, err := s.db.QueryContext(ctx, `
		SELECT b.bundle_id, b.name, b.item_kind, COALESCE(b.url, ''),
		       p.region_code, COALESCE(p.currency, ''), p.price, p.cny_fen, p.discount_percent, p.app_ids
		FROM bundles b
		JOIN bundle_region_prices p ON p.bundle_id = b.bundle_id
		WHERE p.price_status = 'ok'
		  AND p.discount_percent >= ?
		  AND p.cny_fen IS NOT NULL
		  AND p.cny_fen >= ?`, bundleDealMinDiscount, bundleMinCnyFen)
	if err != nil {
		return nil, err
	}
	grouped := map[int64]*bundleGroup{}
	for rows.Next() {
		var (
			bid, kind, cny, discount int64
			name, rawURL, reg, cur   string
			price                    sql.NullInt64
			appIDs                   sql.NullString
		)
		if err := rows.Scan(&bid, &name, &kind, &rawURL, &reg, &cur, &price, &cny, &discount, &appIDs); err != nil {
			rows.Close()
			return nil, err
		}
		upperReg := strings.ToUpper(strings.TrimSpace(reg))
		if cur == "" {
			cur = notifications.CurrencyForRegion(upperReg)
		}
		g := grouped[bid]
		if g == nil {
			g = &bundleGroup{id: bid, name: name, kind: kind, url: rawURL}
			grouped[bid] = g
		}
		g.prices = append(g.prices, bundleRegionRow{
			region:   upperReg,
			currency: cur,
			price:    price.Int64,
			hasPrice: price.Valid,
			cny:      cny,
			discount: discount,
			count:    bundleJSONLen(appIDs),
		})
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return nil, err
	}
	rows.Close()
	if len(grouped) == 0 {
		return []map[string]any{}, nil
	}

	ownerRegion, _ := account.PrimaryWalletRegion(ctx, s.db)
	regionNames := loadRegionDisplayNames(ctx, s.db)

	picks := make([]map[string]any, 0, len(grouped))
	for _, g := range grouped {
		bestDiscount := g.prices[0].discount
		for _, p := range g.prices[1:] {
			if p.discount > bestDiscount {
				bestDiscount = p.discount
			}
		}
		chosen := pickBundleRegion(g.prices, ownerRegion)
		var cnRow *bundleRegionRow
		for i := range g.prices {
			if g.prices[i].region == "CN" {
				cnRow = &g.prices[i]
				break
			}
		}
		priceText := ""
		if chosen.hasPrice && chosen.currency != "" {
			priceText = games.FormatMinorUnits(chosen.price, chosen.currency)
		}
		cnPriceText := "—"
		if cnRow != nil {
			cnPriceText = games.FormatMinorUnits(cnRow.cny, "CNY")
		}
		regionName := regionNames[chosen.region]
		if regionName == "" {
			regionName = chosen.region
		}
		condition := ""
		if chosen.count > 0 {
			condition = fmt.Sprintf("包内 %d 项", chosen.count)
		}
		picks = append(picks, map[string]any{
			"bundle_id":     g.id,
			"name":          g.name,
			"url":           bundleURL(g.url, g.id, g.kind, chosen.region),
			"region_code":   chosen.region,
			"region_name":   regionName,
			"price_text":    priceText,
			"cn_price_text": cnPriceText,
			"max_discount":  bestDiscount,
			"condition":     condition,
		})
	}
	slices.SortFunc(picks, func(a, b map[string]any) int {
		di, dj := a["max_discount"].(int64), b["max_discount"].(int64)
		if c := cmp.Compare(dj, di); c != 0 {
			return c
		}
		return cmp.Compare(a["bundle_id"].(int64), b["bundle_id"].(int64))
	})

	var fresh []map[string]any
	err = s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		active, err := bundleDealCooldownActive(ctx, conn, now)
		if err != nil || active {
			return err
		}
		nowSec := json.RawMessage(strconv.FormatFloat(float64(now.UnixMilli())/1000.0, 'f', -1, 64))
		if err := settings.WriteValue(ctx, conn, bundleDealAtKey, nowSec); err != nil {
			return err
		}
		facts := make([]notifications.Fact, 0, len(picks))
		for _, p := range picks {
			bid := p["bundle_id"].(int64)
			discount := p["max_discount"].(int64)
			factKey := fmt.Sprintf("bundle:%d:%d", bid, discount)
			data := map[string]any{
				"appid":       0,
				"title":       fmt.Sprintf("%s -%d%%", p["name"], discount),
				"labels":      []string{"捆绑包精选"},
				"bundleId":    bid,
				"priceText":   p["price_text"],
				"cnPriceText": p["cn_price_text"],
				"regionCode":  p["region_code"],
				"regionName":  p["region_name"],
				"url":         p["url"],
				"condition":   p["condition"],
			}
			encoded, err := json.Marshal(data)
			if err != nil {
				return err
			}
			var id int64
			err = conn.QueryRowContext(ctx, `INSERT INTO fact_notices(source, kind, fact_key, data, occurred_at)
				VALUES('bundles', 'discount_pick', ?, ?, ?) ON CONFLICT(fact_key) DO NOTHING RETURNING id`,
				factKey, string(encoded), stamp(now)).Scan(&id)
			if err == sql.ErrNoRows {
				continue
			}
			if err != nil {
				return err
			}
			payload := copyMap(data)
			payload["eventType"] = "BUNDLE_DEAL"
			payload["source"] = "bundles"
			payload["kind"] = "discount_pick"
			payload["observedAt"] = now.Format(time.RFC3339)
			facts = append(facts, notifications.Fact{
				ID:         id,
				AppID:      0,
				Region:     nil,
				EventType:  "BUNDLE_DEAL",
				Payload:    payload,
				OccurredAt: now,
			})
			fresh = append(fresh, p)
		}
		if len(facts) > 0 {
			if _, err := notifications.EnqueueFactsTx(ctx, conn, "fact_notice", facts, now); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	if len(fresh) > 0 {
		s.opts.Log("捆绑包精选新折扣已落账 │ 合格包=%d 新增=%d", len(grouped), len(fresh))
		if s.opts.Dispatch != nil {
			if dErr := s.opts.Dispatch(ctx); dErr != nil {
				s.opts.Log("捆绑包精选已保存，本次发送未完成：%v", dErr)
			}
		}
	} else {
		fresh = []map[string]any{}
	}
	return fresh, nil
}
