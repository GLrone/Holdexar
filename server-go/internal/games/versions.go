package games

import (
	"cmp"
	"context"
	"database/sql"
	"net/http"
	"slices"
	"strconv"
)

// 按 (region_code, sub_id) 聚合最新 ok 行，剔除 is_bundle 行。

type versionRegionPrice struct {
	Cents     int64  `json:"cents"`
	Formatted string `json:"formatted"`
	Discount  int64  `json:"discount"`
	CnyFen    *int64 `json:"cnyFen"`
}

type gameVersionPrices struct {
	SubID   int64                         `json:"subId"`
	Suffix  *string                       `json:"suffix"`
	IsGold  bool                          `json:"isGold"`
	Regions map[string]versionRegionPrice `json:"regions"`
}

type versionHistoryRow struct {
	region   string
	subID    int64
	suffix   *string
	isGold   bool
	isBundle bool
	price    int64
	discount *int64
	currency *string
	cnyFen   *int64
}

func (h *handler) handleVersions(w http.ResponseWriter, r *http.Request) {
	appid, err := parseAppID(r.PathValue("appid"))
	if err != nil {
		writeJSON(w, http.StatusUnprocessableEntity, intParseDetail(r.PathValue("appid")))
		return
	}
	resp, err := h.gameVersions(r.Context(), appid)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (h *handler) gameVersions(ctx context.Context, appid int64) (map[string]any, error) {
	rows, err := h.db.QueryContext(ctx,
		`SELECT region_code, sub_id, is_gold, version_suffix, is_bundle, price,
		        discount_percent, currency, cny_fen
		 FROM game_price_history
		 WHERE appid=? AND price_status='ok' AND price IS NOT NULL AND price > 0 AND sub_id IS NOT NULL
		 ORDER BY snapshot_at ASC, id ASC`, appid)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	type pairKey struct {
		region string
		subID  int64
	}
	type subMeta struct {
		suffix   *string
		isGold   bool
		isBundle bool
	}
	latest := map[pairKey]versionHistoryRow{}
	metas := map[int64]*subMeta{}
	var subOrder []int64
	for rows.Next() {
		var it versionHistoryRow
		var gold, bundle sql.NullBool
		var subID sql.NullInt64
		if err = rows.Scan(&it.region, &subID, &gold, &it.suffix, &bundle, &it.price,
			&it.discount, &it.currency, &it.cnyFen); err != nil {
			return nil, err
		}
		it.isGold, it.isBundle = gold.Bool, bundle.Bool
		it.subID = subID.Int64
		latest[pairKey{it.region, it.subID}] = it
		meta, ok := metas[it.subID]
		if !ok {
			meta = &subMeta{suffix: it.suffix, isGold: it.isGold, isBundle: it.isBundle}
			metas[it.subID] = meta
			if !meta.isBundle {
				subOrder = append(subOrder, it.subID)
			}
		} else if it.suffix != nil && *it.suffix != "" && (meta.suffix == nil || *meta.suffix == "") {
			meta.suffix = it.suffix
		}
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}

	versions := make([]gameVersionPrices, 0, len(subOrder))
	idx := make(map[int64]int, len(subOrder))
	for _, subID := range subOrder {
		meta := metas[subID]
		idx[subID] = len(versions)
		versions = append(versions, gameVersionPrices{
			SubID: subID, Suffix: meta.suffix, IsGold: meta.isGold,
			Regions: map[string]versionRegionPrice{},
		})
	}
	rates, err := h.loadRates(ctx)
	if err != nil {
		return nil, err
	}
	for key, row := range latest {
		i, ok := idx[key.subID]
		if !ok {
			continue
		}
		cnyFen := row.cnyFen
		if cnyFen == nil {
			currency := ""
			if row.currency != nil && *row.currency != "" {
				currency = *row.currency
			} else {
				currency = regionToCurrency[key.region]
			}
			if currency != "" {
				cnyFen = convertMinorToCnyFen(row.price, currency, rates)
			}
		}
		discount := int64(0)
		if row.discount != nil {
			discount = *row.discount
		}
		formatted := strconv.FormatInt(row.price, 10)
		if currency := regionToCurrency[key.region]; currency != "" {
			formatted = FormatMinorUnits(row.price, currency)
		}
		versions[i].Regions[key.region] = versionRegionPrice{
			Cents: row.price, Formatted: formatted, Discount: discount, CnyFen: cnyFen,
		}
	}

	// 排序：标准版优先，其余按 (suffix, isGold) 升序，稳定保留首现序。
	slices.SortStableFunc(versions, func(a, b gameVersionPrices) int {
		ka, kb := versionPriceKey(a), versionPriceKey(b)
		if c := cmp.Compare(ka.first, kb.first); c != 0 {
			return c
		}
		if c := cmp.Compare(ka.suffix, kb.suffix); c != 0 {
			return c
		}
		if ka.gold != kb.gold {
			if !ka.gold {
				return -1
			}
			return 1
		}
		return 0
	})
	return map[string]any{"versions": versions}, nil
}

type versionPriceSortKey struct {
	first  int
	suffix string
	gold   bool
}

func versionPriceKey(v gameVersionPrices) versionPriceSortKey {
	k := versionPriceSortKey{first: 1, gold: v.IsGold}
	if v.Suffix != nil {
		k.suffix = *v.Suffix
	}
	if k.suffix == "" && !v.IsGold {
		k.first = 0
	}
	return k
}
