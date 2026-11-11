package wishlist

import (
	"encoding/json"
	"io"
	"net/http"
	"strconv"
)

func poolReply(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
func (p *Pool) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/follows", func(w http.ResponseWriter, r *http.Request) {
		ids, e := FollowedAppIDs(r.Context(), p.DB)
		if e != nil {
			poolReply(w, 500, map[string]any{"detail": e.Error()})
			return
		}
		poolReply(w, 200, map[string]any{"appids": ids})
	})
	for _, method := range []string{"PUT", "DELETE"} {
		mux.HandleFunc(method+" /api/v1/follows/{appid}", func(w http.ResponseWriter, r *http.Request) {
			id, e := strconv.ParseInt(r.PathValue("appid"), 10, 64)
			if e != nil {
				poolReply(w, 422, map[string]any{"detail": "游戏编号无效"})
				return
			}
			value, e := p.Follow(r.Context(), id, r.Method == "PUT")
			if e != nil {
				poolReply(w, 500, map[string]any{"detail": e.Error()})
				return
			}
			poolReply(w, 200, value)
		})
	}
	mux.HandleFunc("GET /api/v1/wishlist", func(w http.ResponseWriter, r *http.Request) {
		var steamid *string
		if r.URL.Query().Has("steamid") {
			value := r.URL.Query().Get("steamid")
			steamid = &value
		}
		value, e := p.Items(r.Context(), steamid)
		if e != nil {
			poolReply(w, 500, map[string]any{"detail": e.Error()})
			return
		}
		poolReply(w, 200, value)
	})
	mux.HandleFunc("GET /api/v1/wishlist/appids", func(w http.ResponseWriter, r *http.Request) {
		value, e := p.AppIDs(r.Context())
		if e != nil {
			poolReply(w, 500, map[string]any{"detail": e.Error()})
			return
		}
		poolReply(w, 200, value)
	})
	for _, path := range []string{"/pool/items", "/pool/items/remove"} {
		mux.HandleFunc("POST /api/v1"+path, func(w http.ResponseWriter, r *http.Request) {
			var body struct {
				AppIDs []int64 `json:"appids"`
				Source *string `json:"source"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 128<<10))
			if e := decoder.Decode(&body); e != nil || len(body.AppIDs) < 1 || len(body.AppIDs) > 500 || body.Source != nil && len([]rune(*body.Source)) > 80 {
				poolReply(w, 422, map[string]any{"detail": "关注参数无效"})
				return
			}
			var extra any
			if e := decoder.Decode(&extra); e != io.EOF {
				poolReply(w, 422, map[string]any{"detail": "关注参数无效"})
				return
			}
			var result map[string]any
			var e error
			if path == "/pool/items/remove" {
				result, e = p.Remove(r.Context(), body.AppIDs)
			} else {
				source := ""
				if body.Source != nil {
					source = *body.Source
				}
				result, e = p.Add(r.Context(), body.AppIDs, source)
			}
			if e != nil {
				poolReply(w, 500, map[string]any{"detail": e.Error()})
				return
			}
			poolReply(w, 200, result)
		})
	}
}
