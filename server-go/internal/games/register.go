// Package games：games 域只读路径（列表 / 详情 / 目录检索 / 历史走势）。
// 写路径、爬取触发与外部抓取端点不在本包；历史回填派发只改历史事实与
// 同步状态，统一写入仍在 dbwriter。
package games

import (
	"database/sql"
	"encoding/json"
	"net/http"

	_ "holdexar/server-go/internal/store" // name_bigram UDF 注册（影子表触发器与检索共用）
)

// Register 注册 games 域端点。backfill 为社区历史源回填管理器（nil =
// 数据源未安装，历史只读照常，回填端点显式拒绝）。
func Register(mux *http.ServeMux, db *sql.DB, backfill *HistoryBackfill) {
	h := &handler{db: db, backfill: backfill}
	mux.HandleFunc("GET /api/v1/games", h.handleList)
	mux.HandleFunc("GET /api/v1/games/{appid}", h.handleDetail)
	mux.HandleFunc("GET /api/v1/games/{appid}/history", h.handleHistory)
	mux.HandleFunc("POST /api/v1/games/{appid}/history/backfill", h.handleBackfill)
}

type handler struct {
	db       *sql.DB
	backfill *HistoryBackfill
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
