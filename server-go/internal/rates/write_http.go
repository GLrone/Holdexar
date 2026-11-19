package rates

import (
	"errors"
	"net/http"
	"strings"

	"holdexar/server-go/internal/httpx"
)

func (s *Service) registerWrites(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/v1/rates/refresh", func(w http.ResponseWriter, r *http.Request) {
		result, e := s.Refresh(r.Context())
		if e != nil {
			status := 500
			if errors.Is(e, ErrSourcesUnavailable) {
				status = 502
			}
			httpx.WriteJSON(w, status, map[string]any{"detail": e.Error()})
			return
		}
		httpx.WriteJSON(w, 200, result)
	})
	mux.HandleFunc("POST /api/v1/rates/history/repair", func(w http.ResponseWriter, r *http.Request) {
		query := r.URL.Query()
		dryRun := false
		if values, ok := query["dry_run"]; ok {
			switch strings.ToLower(values[0]) {
			case "1", "true", "t", "on", "y", "yes":
				dryRun = true
			case "0", "false", "f", "off", "n", "no":
			default:
				httpx.WriteValidationError(w, httpx.ValErr{
					Type:  "bool_parsing",
					Loc:   []any{"query", "dry_run"},
					Msg:   "Input should be a valid boolean, unable to interpret input",
					Input: values[0],
				})
				return
			}
		}
		limitN, limitGiven, limitErr := httpx.QueryInt(query, "max_windows", 0, nil, nil)
		if limitErr != nil {
			httpx.WriteValidationError(w, *limitErr)
			return
		}
		var limit *int
		if limitGiven {
			n := int(limitN)
			limit = &n
		}
		result, e := s.Repair(r.Context(), dryRun, limit)
		if e != nil {
			httpx.WriteJSON(w, 500, map[string]any{"detail": e.Error()})
			return
		}
		httpx.WriteJSON(w, 200, result)
	})
}
