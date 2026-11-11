package monitoring

import (
	"encoding/json"
	"io"
	"net/http"
	"strconv"
)

func reply(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
func (s *Service) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/monitoring/stats", func(w http.ResponseWriter, r *http.Request) {
		v, e := s.Stats(r.Context())
		if e != nil {
			reply(w, 500, map[string]any{"detail": e.Error()})
			return
		}
		reply(w, 200, v)
	})
	mux.HandleFunc("GET /api/v1/monitoring/state", func(w http.ResponseWriter, r *http.Request) {
		typ := r.URL.Query().Get("target_type")
		if typ == "" {
			typ = "game"
		}
		if !ValidType(typ) {
			reply(w, 400, map[string]any{"detail": "未知 target_type: " + typ})
			return
		}
		var id int64
		var e error
		if raw := r.URL.Query().Get("target_id"); raw != "" {
			id, e = strconv.ParseInt(raw, 10, 64)
			if e != nil {
				reply(w, 422, map[string]any{"detail": "对象编号无效"})
				return
			}
		}
		v, e := s.Snapshot(r.Context(), typ, id)
		if e != nil {
			reply(w, 500, map[string]any{"detail": e.Error()})
			return
		}
		reply(w, 200, v)
	})
	for _, action := range []string{"exclude", "include", "stop", "track"} {
		mux.HandleFunc("POST /api/v1/monitoring/"+action, func(w http.ResponseWriter, r *http.Request) {
			var body struct {
				TargetType string `json:"target_type"`
				TargetID   *int64 `json:"target_id"`
				Reason     string `json:"reason"`
				Source     string `json:"source"`
			}
			body.TargetType = "game"
			body.Source = "manual"
			decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10))
			if e := decoder.Decode(&body); e != nil || body.TargetID == nil || !ValidType(body.TargetType) {
				reply(w, 422, map[string]any{"detail": "监控参数无效"})
				return
			}
			var trailing any
			if e := decoder.Decode(&trailing); e != io.EOF {
				reply(w, 422, map[string]any{"detail": "监控参数无效"})
				return
			}
			id := *body.TargetID
			on, off := true, false
			var change Change
			switch action {
			case "exclude":
				change = Change{Excluded: &on, Reason: body.Reason}
			case "include":
				change = Change{Excluded: &off}
			case "stop":
				change = Change{Sources: map[int64][]string{}}
			case "track":
				change = Change{Excluded: &off, Sources: map[int64][]string{id: {body.Source}}}
			}
			states, e := s.Apply(r.Context(), body.TargetType, []int64{id}, change)
			if e != nil {
				reply(w, 500, map[string]any{"detail": e.Error()})
				return
			}
			reply(w, 200, map[string]any{"ok": true, "state": states[id]})
		})
	}
}
