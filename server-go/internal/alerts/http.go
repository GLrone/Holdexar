package alerts

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/games"
	"holdexar/server-go/internal/netenv"
	"holdexar/server-go/internal/notifications"
	"holdexar/server-go/internal/steamhttp"
	"holdexar/server-go/internal/store"
)

func reply(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
func failure(w http.ResponseWriter, status int, e error) {
	reply(w, status, map[string]any{"detail": e.Error()})
}
func decode(w http.ResponseWriter, r *http.Request) (map[string]any, bool) {
	d := json.NewDecoder(r.Body)
	d.UseNumber()
	var v map[string]any
	if e := d.Decode(&v); e != nil || v == nil {
		failure(w, 422, fmt.Errorf("请求内容必须是对象"))
		return nil, false
	}
	return v, true
}
func pathID(w http.ResponseWriter, r *http.Request) (int64, bool) {
	id, e := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if e != nil {
		failure(w, 422, fmt.Errorf("提醒编号无效"))
		return 0, false
	}
	return id, true
}
func (s *Service) Register(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/v1/alerts/featured/preview", func(w http.ResponseWriter, r *http.Request) {
		raw, ok := decode(w, r)
		if !ok {
			return
		}
		if _, e := notifications.ValidateFeatured(featuredConfig(raw)); e != nil {
			failure(w, 400, e)
			return
		}
		if _, e := notifications.ResolveRegion(text(raw["region"]), nil, "", nil); e != nil {
			failure(w, 400, e)
			return
		}
		if _, e := previewAppIDs(raw); e != nil {
			failure(w, 400, e)
			return
		}
		value, e := s.PreviewFeatured(r.Context(), raw)
		if e != nil {
			failure(w, 500, e)
			return
		}
		reply(w, 200, value)
	})
	mux.HandleFunc("GET /api/v1/alerts", func(w http.ResponseWriter, r *http.Request) {
		v, e := s.List(r.Context())
		if e != nil {
			failure(w, 500, e)
			return
		}
		reply(w, 200, v)
	})
	mux.HandleFunc("POST /api/v1/alerts", func(w http.ResponseWriter, r *http.Request) {
		raw, ok := decode(w, r)
		if !ok {
			return
		}
		if _, e := ValidateRule(raw); e != nil {
			failure(w, 400, e)
			return
		}
		v, e := s.Add(r.Context(), raw)
		if e != nil {
			failure(w, 500, e)
			return
		}
		reply(w, 200, v)
	})
	mux.HandleFunc("POST /api/v1/alerts/preview", func(w http.ResponseWriter, r *http.Request) {
		raw, ok := decode(w, r)
		if !ok {
			return
		}
		if _, e := ValidateRule(raw); e != nil {
			failure(w, 400, e)
			return
		}
		v, e := s.Preview(r.Context(), raw)
		if e != nil {
			failure(w, 500, e)
			return
		}
		reply(w, 200, v)
	})
	mux.HandleFunc("PUT /api/v1/alerts/{id}", func(w http.ResponseWriter, r *http.Request) {
		id, ok := pathID(w, r)
		if !ok {
			return
		}
		raw, ok := decode(w, r)
		if !ok {
			return
		}
		v, e := s.Update(r.Context(), id, raw)
		if e != nil {
			if e.Error() == "提醒不存在" {
				failure(w, 404, e)
			} else {
				failure(w, 400, e)
			}
			return
		}
		reply(w, 200, v)
	})
	mux.HandleFunc("DELETE /api/v1/alerts/{id}", func(w http.ResponseWriter, r *http.Request) {
		id, ok := pathID(w, r)
		if !ok {
			return
		}
		removed, e := s.Delete(r.Context(), id)
		if e != nil {
			failure(w, 500, e)
			return
		}
		if !removed {
			failure(w, 404, fmt.Errorf("提醒不存在"))
			return
		}
		reply(w, 200, map[string]any{"removed": true})
	})
	mux.HandleFunc("GET /api/v1/alerts/events", func(w http.ResponseWriter, r *http.Request) {
		limit := 50
		if r.URL.Query().Has("limit") {
			var e error
			limit, e = strconv.Atoi(r.URL.Query().Get("limit"))
			if e != nil {
				failure(w, 422, e)
				return
			}
		}
		v, e := s.Events(r.Context(), limit)
		if e != nil {
			failure(w, 500, e)
			return
		}
		reply(w, 200, v)
	})
	mux.HandleFunc("DELETE /api/v1/alerts/events/{id}", func(w http.ResponseWriter, r *http.Request) {
		id, ok := pathID(w, r)
		if !ok {
			return
		}
		removed, e := s.DeleteEvent(r.Context(), id)
		if e != nil {
			failure(w, 409, e)
			return
		}
		if !removed {
			failure(w, 404, fmt.Errorf("触发记录不存在"))
			return
		}
		reply(w, 200, map[string]any{"removed": true})
	})
	mux.HandleFunc("DELETE /api/v1/alerts/events", func(w http.ResponseWriter, r *http.Request) {
		v, e := s.ClearEvents(r.Context())
		if e != nil {
			failure(w, 500, e)
			return
		}
		reply(w, 200, v)
	})
	mux.HandleFunc("GET /api/v1/alerts/search", func(w http.ResponseWriter, r *http.Request) {
		region := r.URL.Query().Get("region")
		if region == "" {
			region = "CN"
		}
		v, e := s.Search(r.Context(), r.URL.Query().Get("q"), region)
		if e != nil {
			failure(w, 500, e)
			return
		}
		reply(w, 200, v)
	})
}
func (s *Service) Events(ctx context.Context, limit int) ([]map[string]any, error) {
	rows, e := maps(ctx, s.db, `SELECT a.id,a.alert_id AS alertId,a.appid,coalesce(g.name,'') AS gameName,coalesce(nullif(g.header_image,''),'https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/'||a.appid||'/header.jpg') AS gameHeader,a.region,a.price,a.price_cny AS priceCny,CAST(a.triggered_at AS TEXT) AS triggeredAt,a.notified,a.details_json AS details FROM alert_events a LEFT JOIN games g ON g.appid=a.appid ORDER BY a.id DESC LIMIT ?`, limit)
	for _, r := range rows {
		r["notified"] = r["notified"] == int64(1) || r["notified"] == true
		r["triggeredAt"] = iso(r["triggeredAt"])
		var details any
		_ = json.Unmarshal([]byte(text(r["details"])), &details)
		r["details"] = details
		n, _ := number(r["price"])
		r["priceText"] = ""
		if n == 0 {
			r["price"] = nil
		} else {
			currency := notifications.CurrencyForRegion(text(r["region"]))
			if currency == "" {
				currency = "CNY"
			}
			r["priceText"] = games.FormatMinorUnits(int64(n), currency)
		}
		if n, _ := number(r["priceCny"]); n == 0 {
			r["priceCny"] = nil
		}
	}
	return rows, e
}
func (s *Service) ClearEvents(ctx context.Context) (map[string]any, error) {
	var protected, removed int64
	e := s.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		e := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM alert_events WHERE id IN(SELECT event_id FROM notification_candidates WHERE source_kind='alert_event')`).Scan(&protected)
		if e != nil {
			return e
		}
		r, e := conn.ExecContext(ctx, `DELETE FROM alert_events WHERE id NOT IN(SELECT event_id FROM notification_candidates WHERE source_kind='alert_event')`)
		if e != nil {
			return e
		}
		removed, e = r.RowsAffected()
		return e
	})
	return map[string]any{"removed": removed, "protected": protected}, e
}
func (s *Service) Search(ctx context.Context, q, region string) (map[string]any, error) {
	q = strings.TrimSpace(q)
	empty := []map[string]any{}
	if q == "" {
		return map[string]any{"items": empty}, nil
	}
	id, idErr := strconv.ParseInt(q, 10, 64)
	rows, e := maps(ctx, s.db, `SELECT appid,name,coalesce(name_en,'') AS nameEn FROM games WHERE name LIKE ? OR name_en LIKE ? OR appid=? LIMIT 20`, "%"+q+"%", "%"+q+"%", id)
	if e != nil {
		return nil, e
	}
	for _, r := range rows {
		r["isFree"] = false
	}
	if len(rows) > 0 {
		return map[string]any{"items": rows}, nil
	}
	if idErr != nil {
		return map[string]any{"items": empty, "error": "输入不是有效的 AppID，且本地无匹配游戏记录"}, nil
	}
	proxy, e := netenv.ResolveSystemProxy(ctx)
	if e != nil {
		return nil, e
	}
	client := steamhttp.New(steamhttp.Config{Timeout: 15 * time.Second, MaxRetries: 1, ProxyURL: proxy})
	endpoint := "https://store.steampowered.com/api/appdetails?appids=" + strconv.FormatInt(id, 10) + "&cc=" + url.QueryEscape(strings.ToLower(region)) + "&l=schinese"
	_, body, e := client.GetJSON(ctx, endpoint, "")
	if e != nil {
		return map[string]any{"items": empty, "error": "在线查询失败: " + e.Error()}, nil
	}
	var response map[string]struct {
		Data map[string]any `json:"data"`
	}
	if e = json.Unmarshal(body, &response); e != nil {
		return nil, e
	}
	data := response[strconv.FormatInt(id, 10)].Data
	if len(data) == 0 {
		return map[string]any{"items": empty, "error": fmt.Sprintf("AppID %d 在 Steam 不存在或已下架", id)}, nil
	}
	if data["is_free"] == true {
		return map[string]any{"items": empty, "error": fmt.Sprintf("《%s》是免费游戏，无需价格提醒", text(data["name"]))}, nil
	}
	return map[string]any{"items": []map[string]any{{"appid": id, "name": data["name"], "nameEn": "", "isFree": false}}}, nil
}
