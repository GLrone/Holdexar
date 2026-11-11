package epicfree

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/sched"
)

func (s *Service) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/metadata/epic/offers", func(w http.ResponseWriter, r *http.Request) {
		var force bool
		if r.URL.Query().Has("force") {
			switch strings.ToLower(r.URL.Query().Get("force")) {
			case "1", "true", "on", "yes", "t", "y":
				force = true
			case "0", "false", "off", "no", "f", "n":
			default:
				s.reply(w, http.StatusUnprocessableEntity, map[string]string{"detail": "刷新参数无效"})
				return
			}
		}
		result, err := s.Offers(r.Context(), force)
		s.respond(w, result, err)
	})
	mux.HandleFunc("POST /api/v1/metadata/epic/refresh", func(w http.ResponseWriter, r *http.Request) {
		result, err := s.Refresh(r.Context())
		s.respond(w, result, err)
	})
	mux.HandleFunc("POST /api/v1/metadata/epic/list", s.importList)
	mux.HandleFunc("GET /api/v1/metadata/steam/offers", func(w http.ResponseWriter, r *http.Request) {
		result, err := s.steamOffers(r.Context())
		s.respond(w, result, err)
	})
}

func (s *Service) importList(w http.ResponseWriter, r *http.Request) {
	ctx, done, err := s.begin(r.Context())
	if err != nil {
		s.respond(w, nil, err)
		return
	}
	defer done()
	var payload struct {
		Games []struct {
			AppID json.RawMessage `json:"appid"`
			Title string          `json:"title"`
			Start string          `json:"free_start"`
		} `json:"games"`
	}
	d := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<20))
	if err := d.Decode(&payload); err != nil || payload.Games == nil || d.Decode(new(any)) != io.EOF {
		s.reply(w, http.StatusUnprocessableEntity, map[string]string{"detail": "Epic 名单格式无效"})
		return
	}
	tags := make([]gameTag, 0, len(payload.Games))
	skipped := 0
	for _, game := range payload.Games {
		idText := string(game.AppID)
		if strings.HasPrefix(idText, `"`) {
			if err := json.Unmarshal(game.AppID, &idText); err != nil {
				s.reply(w, http.StatusUnprocessableEntity, map[string]string{"detail": "Steam AppID 格式无效"})
				return
			}
		}
		id, err := strconv.ParseInt(strings.TrimSpace(idText), 10, 64)
		if err != nil {
			s.reply(w, http.StatusUnprocessableEntity, map[string]string{"detail": "Steam AppID 格式无效"})
			return
		}
		if id <= 0 {
			skipped++
			continue
		}
		tags = append(tags, gameTag{AppID: id, Name: game.Title, Start: normalizeEpicDate(game.Start)})
	}
	marked, err := s.mark(ctx, tags, s.opts.Now().In(sched.DefaultLoc))
	if err != nil {
		s.respond(w, nil, err)
		return
	}
	changed := make([]map[string]any, 0, len(marked))
	for _, tag := range marked {
		changed = append(changed, map[string]any{"appid": tag.AppID, "freeStart": tag.Start})
	}
	s.respond(w, map[string]any{"source": "epic-list", "ok": true, "received": len(payload.Games), "marked": changed, "skipped": skipped}, nil)
}

func (s *Service) steamOffers(ctx context.Context) (map[string]any, error) {
	type offer struct {
		AppID            int64   `json:"appid"`
		Name             string  `json:"name"`
		HeaderImage      *string `json:"headerImage"`
		OriginalPriceFen *int64  `json:"originalPriceFen"`
		EndTS            int64   `json:"endTs"`
	}
	now := s.opts.Now().In(sched.DefaultLoc)
	rows, err := s.db.QueryContext(ctx, `SELECT g.appid,COALESCE(NULLIF(g.name,''),CAST(g.appid AS TEXT)),g.header_image,p.original_price,g.promo_end_at
		FROM games g LEFT JOIN game_current_prices p ON p.appid=g.appid AND p.region_code='CN' AND p.price_status='ok'
		WHERE g.free_kind='promo' AND g.promo_end_at>? ORDER BY g.promo_end_at,g.appid`, now.Unix())
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []offer{}
	for rows.Next() {
		var item offer
		if err := rows.Scan(&item.AppID, &item.Name, &item.HeaderImage, &item.OriginalPriceFen, &item.EndTS); err != nil {
			return nil, err
		}
		out = append(out, item)
	}
	return map[string]any{"source": "steam-free", "ok": true, "fetchedAt": now.Format(time.RFC3339), "offers": out}, rows.Err()
}

func (s *Service) respond(w http.ResponseWriter, result any, err error) {
	if errors.Is(err, ErrDisabled) {
		s.reply(w, http.StatusConflict, map[string]string{"detail": ErrDisabled.Error()})
		return
	}
	if err != nil {
		s.opts.Log("免费促销请求未完成 │ 原因=%v", err)
		s.reply(w, http.StatusBadGateway, map[string]string{"detail": "免费促销数据暂不可用"})
		return
	}
	s.reply(w, http.StatusOK, result)
}

func (s *Service) reply(w http.ResponseWriter, code int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		s.opts.Log("免费促销响应未发送 │ 原因=%v", err)
	}
}
