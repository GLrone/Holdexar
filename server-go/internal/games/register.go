// Package games：games 域只读路径（列表 / 详情 / 目录检索）。
// 写路径、爬取触发、回填与外部抓取端点不在本包。
package games

import (
	"database/sql"
	"encoding/json"
	"net/http"

	_ "holdexar/server-go/internal/store" // name_bigram UDF 注册（影子表触发器与检索共用）
)

// Register 注册 games 域只读端点。其余端点（榜单 / 历史 / 回填 / 移除等）
// 不在此注册，由装配方决定兜底行为。
func Register(mux *http.ServeMux, db *sql.DB) {
	h := &handler{db: db}
	mux.HandleFunc("GET /api/v1/games", h.handleList)
	mux.HandleFunc("GET /api/v1/games/{appid}", h.handleDetail)
}

type handler struct {
	db *sql.DB
}

func (h *handler) handleList(w http.ResponseWriter, r *http.Request) {
	p, errs := parseListParams(r)
	if len(errs) > 0 {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": errs})
		return
	}
	resp, err := h.listGames(r.Context(), p)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (h *handler) handleDetail(w http.ResponseWriter, r *http.Request) {
	appid, err := parseAppID(r.PathValue("appid"))
	if err != nil {
		writeJSON(w, http.StatusUnprocessableEntity, intParseDetail(r.PathValue("appid")))
		return
	}
	d, err := h.gameDetail(r.Context(), appid)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	if d == nil {
		writeJSON(w, http.StatusNotFound, map[string]any{"detail": "游戏不存在"})
		return
	}
	writeJSON(w, http.StatusOK, d)
}

// intParseDetail 对齐路径参数整数解析失败的校验错误形状。
func intParseDetail(input string) map[string]any {
	return map[string]any{"detail": []any{map[string]any{
		"type":  "int_parsing",
		"loc":   []any{"path", "appid"},
		"msg":   "Input should be a valid integer, unable to parse string as an integer",
		"input": input,
	}}}
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	body, err := json.Marshal(payload)
	if err != nil {
		http.Error(w, "响应序列化失败", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

// detailNull 错误响应体：{"detail": null}。
func detailNull() map[string]any {
	return map[string]any{"detail": nil}
}
