package humblechoice

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"

	"holdexar/server-go/internal/sched"
)

type game struct {
	AppID            int64   `json:"appid"`
	Name             string  `json:"name"`
	HeaderImage      *string `json:"headerImage"`
	PriceFen         *int64  `json:"priceFen"`
	OriginalPriceFen *int64  `json:"originalPriceFen"`
	Discount         int     `json:"discount"`
	LowestCnyFen     *int64  `json:"lowestCnyFen"`
}

func (s *Service) Offers(ctx context.Context) (map[string]any, error) {
	previous, err := s.state(ctx)
	if err != nil {
		return nil, err
	}
	label := monthLabel(month{Machine: previous.Machine, Product: previous.Product})
	if previous.Machine == "" {
		now := s.opts.Now().In(sched.DefaultLoc)
		label = fmt.Sprintf("HB慈善包%02d年%d月包", now.Year()%100, now.Month())
	}
	rows, err := s.db.QueryContext(ctx, `SELECT g.appid,coalesce(nullif(g.name,''),cast(g.appid AS TEXT)),g.header_image,
		p.price,p.original_price,coalesce(p.discount_percent,0),g.min_cny_fen FROM games g
		LEFT JOIN game_current_prices p ON p.appid=g.appid AND p.region_code='CN' AND p.price_status='ok'
		WHERE g.is_hb=1 AND instr(g.hb_data,?)>0 ORDER BY coalesce(g.review_count,0) DESC,g.appid`, label)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	games := []game{}
	for rows.Next() {
		var g game
		if err := rows.Scan(&g.AppID, &g.Name, &g.HeaderImage, &g.PriceFen, &g.OriginalPriceFen, &g.Discount, &g.LowestCnyFen); err != nil {
			return nil, err
		}
		games = append(games, g)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if len(games) == 0 {
		return map[string]any{"source": "hb-offers", "ok": false, "error": "当月包尚未入库（等待每日抓取或手动触发 /metadata/hb/refresh）"}, nil
	}
	return map[string]any{
		"source": "hb-offers", "ok": true, "machineName": optionalText(previous.Machine), "productName": optionalText(previous.Product),
		"label": label, "monthUrl": monthURL(previous.Machine), "skipUrl": "https://www.humblebundle.com/user/skip-month",
		"settingsUrl": "https://www.humblebundle.com/user/settings", "games": games,
	}, nil
}

func optionalText(text string) *string {
	if text == "" {
		return nil
	}
	return &text
}

func (s *Service) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/metadata/hb/offers", func(w http.ResponseWriter, r *http.Request) {
		result, err := s.Offers(r.Context())
		s.respond(w, result, err)
	})
	mux.HandleFunc("POST /api/v1/metadata/hb/refresh", func(w http.ResponseWriter, r *http.Request) {
		result, err := s.Run(r.Context(), true)
		s.respond(w, result, err)
	})
}

func (s *Service) respond(w http.ResponseWriter, result map[string]any, err error) {
	w.Header().Set("Content-Type", "application/json")
	if err != nil {
		s.opts.Log("Humble Choice 请求未完成：%v", err)
		w.WriteHeader(http.StatusInternalServerError)
		result = map[string]any{"detail": "暂时无法更新 Humble Choice"}
	}
	_ = json.NewEncoder(w).Encode(result)
}
