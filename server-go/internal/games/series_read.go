package games

import (
	"context"
	"database/sql"
	"net/http"
	"strings"
)

type seriesMember struct {
	AppID          int64   `json:"appid"`
	Name           string  `json:"name"`
	HeaderImage    *string `json:"headerImage"`
	IsSelf         bool    `json:"isSelf"`
	Type           string  `json:"type"`
	CnPriceFen     *int64  `json:"cnPriceFen"`
	CnOriginalFen  *int64  `json:"cnOriginalFen"`
	CnDiscount     int64   `json:"cnDiscount"`
	LowestPriceFen *int64  `json:"lowestPriceFen"`
	SavingsFen     int64   `json:"savingsFen"`
}

type seriesInfo struct {
	SeriesID   string         `json:"seriesId"`
	SeriesName string         `json:"seriesName"`
	Members    []seriesMember `json:"members"`
}

func (h *handler) handleSeries(w http.ResponseWriter, r *http.Request) {
	appid, err := parseAppID(r.PathValue("appid"))
	if err != nil {
		writeJSON(w, http.StatusUnprocessableEntity, intParseDetail(r.PathValue("appid")))
		return
	}
	info, err := h.seriesMembers(r.Context(), appid)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	if info == nil {
		writeJSON(w, http.StatusNotFound, map[string]any{"detail": "未识别到同系列游戏"})
		return
	}
	writeJSON(w, http.StatusOK, info)
}

func (h *handler) seriesMembers(ctx context.Context, appid int64) (*seriesInfo, error) {
	var seriesID sql.NullString
	e := h.db.QueryRowContext(ctx, `SELECT series_id FROM games WHERE appid=?`, appid).Scan(&seriesID)
	if e == sql.ErrNoRows {
		return nil, nil
	}
	if e != nil {
		return nil, e
	}
	sid := seriesID.String
	if sid == "" {
		return nil, nil
	}

	rows, e := h.db.QueryContext(ctx,
		`SELECT g.appid, g.name, g.header_image, g.type, g.min_cny_fen, g.diff_fen,
		        cp.price, cp.original_price, cp.discount_percent, cp.price_status
		 FROM games g
		 LEFT JOIN game_current_prices cp ON cp.appid = g.appid AND cp.region_code = 'CN'
		 WHERE g.series_id = ?
		 ORDER BY g.appid`, sid)
	if e != nil {
		return nil, e
	}
	defer rows.Close()

	type seriesRow struct {
		appid         int64
		name          string
		headerImage   *string
		typ           *string
		minCnyFen     *int64
		diffFen       *int64
		price         *int64
		originalPrice *int64
		discount      *int64
		priceStatus   *string
	}
	var list []seriesRow
	selfIdx := -1
	for rows.Next() {
		var it seriesRow
		if e = rows.Scan(&it.appid, &it.name, &it.headerImage, &it.typ, &it.minCnyFen, &it.diffFen,
			&it.price, &it.originalPrice, &it.discount, &it.priceStatus); e != nil {
			return nil, e
		}
		if it.appid == appid {
			selfIdx = len(list)
		}
		list = append(list, it)
	}
	if e = rows.Err(); e != nil {
		return nil, e
	}

	row := func(it seriesRow) seriesMember {
		cnOK := it.priceStatus != nil && *it.priceStatus == "ok" && it.price != nil
		var cnFen, cnOriginal, lowest *int64
		if cnOK {
			cnFen = it.price
			if it.originalPrice != nil {
				cnOriginal = it.originalPrice
			}
		}
		if it.minCnyFen != nil {
			lowest = it.minCnyFen
		} else {
			lowest = cnFen
		}
		cnDiscount := int64(0)
		if cnOK && it.discount != nil {
			cnDiscount = *it.discount
		}
		var savings int64
		if it.diffFen != nil && *it.diffFen > 0 {
			savings = *it.diffFen
		}
		typ := "game"
		if it.typ != nil && *it.typ != "" {
			typ = *it.typ
		}
		return seriesMember{
			AppID: it.appid, Name: it.name, HeaderImage: it.headerImage,
			IsSelf: it.appid == appid, Type: typ,
			CnPriceFen: cnFen, CnOriginalFen: cnOriginal, CnDiscount: cnDiscount,
			LowestPriceFen: lowest, SavingsFen: savings,
		}
	}

	members := make([]seriesMember, 0, len(list))
	if selfIdx >= 0 {
		members = append(members, row(list[selfIdx]))
	}
	for i, it := range list {
		if i == selfIdx {
			continue
		}
		members = append(members, row(it))
	}

	// 按 rune 截断 series_id 至多 100 字符，避免截断 UTF-8 序列。
	trimmed := []rune(strings.TrimSpace(sid))
	if len(trimmed) > 100 {
		trimmed = trimmed[:100]
	}
	seriesKey := string(trimmed)
	names := make([]string, len(members))
	for i, m := range members {
		names[i] = m.Name
	}
	return &seriesInfo{
		SeriesID:   seriesKey,
		SeriesName: seriesDisplayName(names, seriesKey),
		Members:    members,
	}, nil
}

// seriesDisplayName 取成员名称仅保留汉字后的最长公共前缀（≥2 字），否则回落系列标识。
func seriesDisplayName(names []string, fallback string) string {
	runes := make([][]rune, len(names))
	for i, n := range names {
		runes[i] = cjkRunes(n)
	}
	var prefix []rune
	for i := 0; ; i++ {
		var cand rune
		ok := true
		for _, cs := range runes {
			if i >= len(cs) {
				ok = false
				break
			}
			if cand == 0 {
				cand = cs[i]
			} else if cs[i] != cand {
				ok = false
				break
			}
		}
		if !ok || cand == 0 {
			break
		}
		prefix = append(prefix, cand)
	}
	if len(prefix) >= 2 {
		return string(prefix)
	}
	return fallback
}

func cjkRunes(s string) []rune {
	out := []rune{}
	for _, r := range s {
		if r >= 0x4e00 && r <= 0x9fff {
			out = append(out, r)
		}
	}
	return out
}
