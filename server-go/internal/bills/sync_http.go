package bills

import "net/http"

func (s *Service) Register(mux *http.ServeMux) {
	RegisterViews(mux, s.db, s.gate)
	mux.HandleFunc("GET "+billViewPrefix+"/sync", func(w http.ResponseWriter, r *http.Request) {
		snapshot, err := s.Status(r.Context())
		writeBillView(w, snapshot, err, "")
	})
	mux.HandleFunc("POST "+billViewPrefix+"/sync", func(w http.ResponseWriter, r *http.Request) {
		result, err := s.Sync(r.Context(), billQueryValue(r.URL.Query()["steam_id"]), true)
		switch {
		case err != nil:
			writeBillJSON(w, http.StatusBadGateway, map[string]string{"detail": "账单同步失败：" + err.Error()})
		case result["status"] == "no_cookie":
			writeBillJSON(w, http.StatusConflict, map[string]string{"detail": "尚未绑定 Steam Cookie，无法同步账单"})
		default:
			writeBillJSON(w, http.StatusOK, result)
		}
	})
}
