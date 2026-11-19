package games

import (
	"context"
	"database/sql"
	"net/http"

	"holdexar/server-go/internal/httpx"
	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/store"
	_ "holdexar/server-go/internal/store" // name_bigram UDF 注册（影子表触发器与检索共用）
)

type WriteDeps struct {
	Gate     *store.WriteGate
	Monitor  *monitoring.Service
	StartJob func(ctx context.Context, scope string, ids []int64, kind string) (map[string]any, error)
	Log      func(string, ...any)
}

type Options struct {
	BoardGet        func(context.Context, string) ([]int64, error)
	Proxy           func(context.Context) (string, error)
	Writes          *WriteDeps
	MaintainDerived func(context.Context)
}

// Register 注册 games 域端点；两级子路径统一经通配路由分发并优先匹配 boards 字面段。
func Register(mux *http.ServeMux, db *sql.DB, backfill *HistoryBackfill, opts Options) {
	h := &handler{db: db, backfill: backfill, boardGet: opts.BoardGet, writes: opts.Writes,
		cdk: newCDKFetcher(db, opts.Proxy), maintainDerived: opts.MaintainDerived}
	mux.HandleFunc("GET /api/v1/games", h.handleList)
	mux.HandleFunc("GET /api/v1/games/top100", h.handleTop100)
	mux.HandleFunc("GET /api/v1/games/{appid}", h.handleDetail)
	mux.HandleFunc("GET /api/v1/games/{appid}/{action}", h.handleGameSubresource)
	mux.HandleFunc("POST /api/v1/games/price-context-batch", h.handlePriceContextBatch)
	mux.HandleFunc("POST /api/v1/games/remove", h.handleRemove)
	mux.HandleFunc("POST /api/v1/games/restore", h.handleRestore)
	mux.HandleFunc("POST /api/v1/games/{appid}/{action}", h.handleGameSubresourcePost)
	mux.HandleFunc("POST /api/v1/games/{appid}/history/backfill", h.handleBackfill)
}

func (h *handler) handleGameSubresource(w http.ResponseWriter, r *http.Request) {
	appid := r.PathValue("appid")
	action := r.PathValue("action")
	if appid == "boards" {
		h.handleBoard(w, r, action)
		return
	}
	switch action {
	case "history":
		h.handleHistory(w, r)
	case "price-context":
		h.handlePriceContext(w, r)
	case "versions":
		h.handleVersions(w, r)
	case "bundles":
		h.handleBundles(w, r)
	case "cdk":
		h.handleCDK(w, r)
	case "series":
		h.handleSeries(w, r)
	default:
		http.NotFound(w, r)
	}
}

func (h *handler) handleGameSubresourcePost(w http.ResponseWriter, r *http.Request) {
	switch r.PathValue("action") {
	case "retry-removed":
		h.handleRetryRemoved(w, r)
	default:
		http.NotFound(w, r)
	}
}

type handler struct {
	db              *sql.DB
	backfill        *HistoryBackfill
	boardGet        func(context.Context, string) ([]int64, error)
	writes          *WriteDeps
	cdk             *cdkFetcher
	maintainDerived func(context.Context)
}

func (h *handler) handleList(w http.ResponseWriter, r *http.Request) {
	if h.maintainDerived != nil {
		h.maintainDerived(r.Context())
	}
	p, errs := parseListParams(r)
	if len(errs) > 0 {
		httpx.WriteValidationError(w, errs...)
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

func intParseDetail(input string) map[string]any {
	return map[string]any{"detail": []httpx.ValErr{{
		Type:  "int_parsing",
		Loc:   []any{"path", "appid"},
		Msg:   httpx.IntParseMsg,
		Input: input,
	}}}
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	httpx.WriteJSON(w, status, payload)
}

func detailNull() map[string]any {
	return httpx.DetailNull()
}
