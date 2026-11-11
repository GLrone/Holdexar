package account

import "net/http"

func (s *Service) registerLogin(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/account/login/status", func(w http.ResponseWriter, r *http.Request) {
		accountResponse(w, 200, s.LoginStatus())
	})
	mux.HandleFunc("POST /api/v1/account/login/start", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Account  *string `json:"account_name"`
			Password *string `json:"password"`
		}
		if !accountBody(w, r, &body) {
			return
		}
		if body.Account == nil || body.Password == nil {
			accountResponse(w, 422, map[string]any{"detail": "缺少账号或密码"})
			return
		}
		result, err := s.StartLogin(r.Context(), *body.Account, *body.Password)
		if err != nil {
			accountHTTPError(w, err)
			return
		}
		if result["busy"] == true {
			accountResponse(w, 409, map[string]any{"detail": result["error"]})
			return
		}
		accountResponse(w, 200, result)
	})
	mux.HandleFunc("POST /api/v1/account/login/code", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Code *string `json:"code"`
		}
		if !accountBody(w, r, &body) {
			return
		}
		if body.Code == nil {
			accountResponse(w, 422, map[string]any{"detail": "缺少验证码"})
			return
		}
		result, err := s.SubmitLoginCode(r.Context(), *body.Code)
		if err != nil {
			accountHTTPError(w, err)
			return
		}
		if result["no_session"] == true {
			accountResponse(w, 409, map[string]any{"detail": result["error"]})
			return
		}
		accountResponse(w, 200, result)
	})
	mux.HandleFunc("POST /api/v1/account/login/cancel", func(w http.ResponseWriter, r *http.Request) {
		accountResponse(w, 200, s.CancelLogin())
	})
}
