package rates

import (
	"errors"
	"net/http"
	"strconv"
	"strings"
)

func (s *Service) registerWrites(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/v1/rates/refresh", func(w http.ResponseWriter, r *http.Request) {
		result, e := s.Refresh(r.Context())
		if e != nil {
			status := 500
			if errors.Is(e, ErrSourcesUnavailable) {
				status = 502
			}
			writeJSON(w, status, map[string]any{"detail": e.Error()})
			return
		}
		writeJSON(w, 200, result)
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
				writeJSON(w, 422, map[string]any{"detail": []map[string]any{{"type": "bool_parsing", "loc": []string{"query", "dry_run"}, "msg": "Input should be a valid boolean, unable to interpret input", "input": values[0]}}})
				return
			}
		}
		var limit *int
		if values, ok := query["max_windows"]; ok {
			n, e := strconv.Atoi(values[0])
			if e != nil {
				writeJSON(w, 422, intParseError("max_windows", values[0]))
				return
			}
			limit = &n
		}
		result, e := s.Repair(r.Context(), dryRun, limit)
		if e != nil {
			writeJSON(w, 500, map[string]any{"detail": e.Error()})
			return
		}
		writeJSON(w, 200, result)
	})
}
