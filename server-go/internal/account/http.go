package account

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
)

func accountResponse(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
func accountHTTPError(w http.ResponseWriter, e error) {
	status := 500
	var input *InputError
	if errors.As(e, &input) {
		status = 400
	}
	if errors.Is(e, ErrAccountNotFound) {
		status = 404
	}
	accountResponse(w, status, map[string]any{"detail": e.Error()})
}
func accountBody(w http.ResponseWriter, r *http.Request, dest any) bool {
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	var extra any
	if decoder.Decode(dest) != nil || decoder.Decode(&extra) != io.EOF {
		accountResponse(w, 422, map[string]any{"detail": "账号参数无效"})
		return false
	}
	return true
}
func (s *Service) Register(mux *http.ServeMux) {
	s.registerLogin(mux)
	for _, pattern := range []string{"DELETE /api/v1/account/cookies/{steam_id}", "DELETE /api/v1/account/cookies"} {
		mux.HandleFunc(pattern, func(w http.ResponseWriter, r *http.Request) {
			var e error
			if sid := r.PathValue("steam_id"); sid != "" {
				_, e = s.RemoveAccount(r.Context(), sid)
			} else {
				e = s.ClearCookies(r.Context())
			}
			if e != nil {
				accountHTTPError(w, e)
				return
			}
			status, e := s.Status(r.Context())
			if e != nil {
				accountHTTPError(w, e)
				return
			}
			accountResponse(w, 200, status)
		})
	}
	mux.HandleFunc("GET /api/v1/account", func(w http.ResponseWriter, r *http.Request) {
		s.MarkSeen()
		value, e := s.Status(r.Context())
		if e != nil {
			accountHTTPError(w, e)
			return
		}
		accountResponse(w, 200, value)
	})
	mux.HandleFunc("GET /api/v1/account/list", func(w http.ResponseWriter, r *http.Request) {
		value, e := s.Accounts(r.Context())
		if e != nil {
			accountHTTPError(w, e)
			return
		}
		accountResponse(w, 200, value)
	})
	mux.HandleFunc("PUT /api/v1/account/cookies", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Cookies *string `json:"cookies"`
		}
		if !accountBody(w, r, &body) {
			return
		}
		if body.Cookies == nil {
			accountResponse(w, 422, map[string]any{"detail": "缺少 Cookie 参数"})
			return
		}
		if strings.TrimSpace(*body.Cookies) == "" {
			accountResponse(w, 400, map[string]any{"detail": "Cookie 内容为空"})
			return
		}
		value, e := s.BindCookies(r.Context(), *body.Cookies)
		if e != nil {
			accountHTTPError(w, e)
			return
		}
		accountResponse(w, 200, value)
	})
	mux.HandleFunc("PUT /api/v1/account/active", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			SteamID *string `json:"steam_id"`
		}
		if !accountBody(w, r, &body) {
			return
		}
		if body.SteamID == nil {
			accountResponse(w, 422, map[string]any{"detail": "缺少账号参数"})
			return
		}
		value, e := s.SetActive(r.Context(), strings.TrimSpace(*body.SteamID))
		if e != nil {
			accountHTTPError(w, e)
			return
		}
		accountResponse(w, 200, value)
	})
	mux.HandleFunc("POST /api/v1/account/sync", func(w http.ResponseWriter, r *http.Request) {
		result, e := s.SyncWallet(r.Context(), "", true)
		if e != nil {
			accountHTTPError(w, e)
			return
		}
		value, e := s.Status(r.Context())
		if e != nil {
			accountHTTPError(w, e)
			return
		}
		if result["ok"] != true {
			value["sync_error"] = result["error"]
		}
		accountResponse(w, 200, value)
	})
	mux.HandleFunc("POST /api/v1/account/steam_id/{steam_id}/refresh", func(w http.ResponseWriter, r *http.Request) {
		value, err := s.RefreshAccount(r.Context(), r.PathValue("steam_id"))
		if err != nil {
			accountHTTPError(w, err)
			return
		}
		accountResponse(w, http.StatusOK, value)
	})
}
