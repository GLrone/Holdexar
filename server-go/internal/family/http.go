package family

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
)

func reply(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
func result(w http.ResponseWriter, v any, e error) {
	if e == nil {
		reply(w, 200, v)
		return
	}
	status := 502
	var input *InputError
	if errors.As(e, &input) {
		status = 400
	}
	reply(w, status, map[string]any{"detail": e.Error()})
}
func body(w http.ResponseWriter, r *http.Request, v any) bool {
	d := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10))
	e := d.Decode(v)
	var extra any
	if e != nil || d.Decode(&extra) != io.EOF {
		reply(w, 422, map[string]any{"detail": "家庭参数无效"})
		return false
	}
	return true
}
func (s *Service) Register(mux *http.ServeMux) {
	for _, pattern := range []string{"GET /api/v1/family/library", "POST /api/v1/family/library/refresh"} {
		mux.HandleFunc(pattern, func(w http.ResponseWriter, r *http.Request) {
			v, e := s.Library(r.Context(), r.URL.Query().Get("steam_id"), r.Method == http.MethodPost)
			result(w, v, e)
		})
	}
	mux.HandleFunc("GET /api/v1/family/wishlist", func(w http.ResponseWriter, r *http.Request) {
		v, e := s.Wishlist(r.Context(), r.URL.Query().Get("steam_id"))
		result(w, v, e)
	})
	mux.HandleFunc("GET /api/v1/family/resolve", func(w http.ResponseWriter, r *http.Request) {
		if !r.URL.Query().Has("input") {
			reply(w, 422, map[string]any{"detail": "请提供 Steam 身份"})
			return
		}
		v, e := s.Resolve(r.Context(), r.URL.Query().Get("input"))
		result(w, v, e)
	})
	mux.HandleFunc("POST /api/v1/family/bind", func(w http.ResponseWriter, r *http.Request) {
		var b struct {
			Input *string `json:"input"`
		}
		if !body(w, r, &b) {
			return
		}
		if b.Input == nil {
			reply(w, 422, map[string]any{"detail": "请提供 Steam 身份"})
			return
		}
		v, e := s.Bind(r.Context(), *b.Input)
		result(w, v, e)
	})
	mux.HandleFunc("GET /api/v1/family/status", func(w http.ResponseWriter, r *http.Request) { v, e := s.Status(r.Context()); result(w, v, e) })
	mux.HandleFunc("POST /api/v1/family/sync", func(w http.ResponseWriter, r *http.Request) {
		v, e := s.Sync(r.Context(), r.URL.Query().Get("steam_id"))
		result(w, v, e)
	})
	mux.HandleFunc("POST /api/v1/family/members", func(w http.ResponseWriter, r *http.Request) {
		var b struct {
			SID    *string `json:"steamid"`
			Name   string  `json:"personaName"`
			Avatar string  `json:"avatarUrl"`
			Group  string  `json:"groupSteamid"`
		}
		if !body(w, r, &b) {
			return
		}
		if b.SID == nil {
			reply(w, 422, map[string]any{"detail": "请提供成员身份"})
			return
		}
		v, e := s.AddMember(r.Context(), *b.SID, b.Name, b.Avatar, b.Group)
		result(w, v, e)
	})
	mux.HandleFunc("DELETE /api/v1/family/members/{steamid}", func(w http.ResponseWriter, r *http.Request) {
		v, e := s.RemoveMember(r.Context(), r.PathValue("steamid"), r.URL.Query().Get("group_steamid"))
		result(w, v, e)
	})
	mux.HandleFunc("PUT /api/v1/family/member-regions", func(w http.ResponseWriter, r *http.Request) {
		var b struct {
			Regions map[string]string `json:"regions"`
		}
		if !body(w, r, &b) {
			return
		}
		if b.Regions == nil {
			reply(w, 422, map[string]any{"detail": "请提供成员地区"})
			return
		}
		v, e := s.SaveRegions(r.Context(), b.Regions)
		if e != nil {
			result(w, nil, e)
			return
		}
		reply(w, 200, map[string]any{"ok": true, "memberRegions": v})
	})
}
