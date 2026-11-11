package wishlist

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"
)

func trackingBody(w http.ResponseWriter, r *http.Request, v any) bool {
	d := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10))
	e := d.Decode(v)
	var extra any
	if e != nil || d.Decode(&extra) != io.EOF {
		poolReply(w, 422, map[string]any{"detail": "账户参数无效"})
		return false
	}
	return true
}
func trackingReply(w http.ResponseWriter, result any, e error, missing int) {
	if e == nil {
		poolReply(w, 200, result)
		return
	}
	status := 500
	var input *InputError
	if errors.As(e, &input) {
		status = 400
	}
	if errors.Is(e, ErrAccountMissing) {
		status = missing
	}
	poolReply(w, status, map[string]any{"detail": e.Error()})
}
func (s *Service) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/accounts", func(w http.ResponseWriter, r *http.Request) {
		v, e := s.Accounts(r.Context())
		trackingReply(w, v, e, 400)
	})
	mux.HandleFunc("POST /api/v1/accounts", func(w http.ResponseWriter, r *http.Request) {
		var b struct {
			SteamID *string         `json:"steamid"`
			Label   string          `json:"label"`
			Kinds   map[string]bool `json:"kinds"`
		}
		if !trackingBody(w, r, &b) {
			return
		}
		if b.SteamID == nil {
			poolReply(w, 422, map[string]any{"detail": "请填写 Steam 账户"})
			return
		}
		v, e := s.AddAccount(r.Context(), *b.SteamID, b.Label, b.Kinds)
		trackingReply(w, v, e, 400)
	})
	mux.HandleFunc("PUT /api/v1/accounts/{steamid}/kinds", func(w http.ResponseWriter, r *http.Request) {
		var b struct {
			Wishlist *bool `json:"wishlist"`
			Owned    *bool `json:"owned"`
		}
		if !trackingBody(w, r, &b) {
			return
		}
		patch := map[string]bool{}
		if b.Wishlist != nil {
			patch["wishlist"] = *b.Wishlist
		}
		if b.Owned != nil {
			patch["owned"] = *b.Owned
		}
		v, e := s.UpdateKinds(r.Context(), r.PathValue("steamid"), patch)
		trackingReply(w, v, e, 400)
	})
	mux.HandleFunc("DELETE /api/v1/accounts/{steamid}", func(w http.ResponseWriter, r *http.Request) {
		v, e := s.RemoveAccount(r.Context(), r.PathValue("steamid"))
		trackingReply(w, v, e, 404)
	})
	mux.HandleFunc("POST /api/v1/accounts/{steamid}/sync", func(w http.ResponseWriter, r *http.Request) {
		auto := true
		if r.URL.Query().Has("autoCrawl") {
			var e error
			auto, e = strconv.ParseBool(r.URL.Query().Get("autoCrawl"))
			if e != nil {
				poolReply(w, 422, map[string]any{"detail": "自动更新价格参数无效"})
				return
			}
		}
		v, e := s.SyncAccount(r.Context(), r.PathValue("steamid"), auto)
		trackingReply(w, v, e, 400)
	})
	mux.HandleFunc("GET /api/v1/owned-library", func(w http.ResponseWriter, r *http.Request) {
		v, e := s.OwnedLibrary(r.Context())
		trackingReply(w, v, e, 400)
	})
	mux.HandleFunc("GET /api/v1/ownership", func(w http.ResponseWriter, r *http.Request) {
		if !r.URL.Query().Has("appids") {
			poolReply(w, 422, map[string]any{"detail": "请提供游戏编号"})
			return
		}
		ids := []int64{}
		for _, raw := range strings.Split(r.URL.Query().Get("appids"), ",") {
			raw = strings.TrimSpace(raw)
			digits := raw != ""
			for _, c := range raw {
				if c < '0' || c > '9' {
					digits = false
					break
				}
			}
			if digits {
				if id, e := strconv.ParseInt(raw, 10, 64); e == nil {
					ids = append(ids, id)
				}
			}
			if len(ids) == 200 {
				break
			}
		}
		if len(ids) == 0 {
			poolReply(w, 400, map[string]any{"detail": "appids 不能为空"})
			return
		}
		v, e := s.Ownership(r.Context(), ids)
		trackingReply(w, v, e, 400)
	})
}
