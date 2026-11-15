// Package games：games 域 HTTP 面（列表 / 详情 / 历史 / 榜单 / 价格上下文 /
// 版本 / 系列 / CDK 查价 / 目录写链）。写链统一走注入的写闸事务与监控服务，
// 爬取触发经注入的 StartJob 出口；CDK 查价不读本地业务表。
package games

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"

	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/store"
	_ "holdexar/server-go/internal/store" // name_bigram UDF 注册（影子表触发器与检索共用）
)

// WriteDeps games 域写链依赖：目录移除 / 恢复 / 下架复探共用一份。
type WriteDeps struct {
	Gate    *store.WriteGate
	Monitor *monitoring.Service
	// StartJob 爬取派发出口（crawl.Runtime.StartJob 的 appids 收窄形态）。
	StartJob func(ctx context.Context, scope string, ids []int64, kind string) (map[string]any, error)
	Log      func(string, ...any)
}

// Options games 域装配选项：boardGet 为榜单读出口（boards.Client.Get 同形），
// proxy 为出网代理解析（CDK 查价注入，netenv.ResolveSystemProxy 口径），
// writes 为写链依赖（nil = 写端点按未装配处理），maintainDerived 为列表
// 入口的派生列日切换维护钩子（nil = 跳过，对齐 Python list_games 入口）。
type Options struct {
	BoardGet        func(context.Context, string) ([]int64, error)
	Proxy           func(context.Context) (string, error)
	Writes          *WriteDeps
	MaintainDerived func(context.Context)
}

// Register 注册 games 域端点。backfill 为社区历史源回填管理器（nil =
// 数据源未安装，历史只读照常，回填端点显式拒绝）。
//
// 两级子路径（/{appid}/{action} 与 boards/{key}）共用一个通配符分发器：
// ServeMux 不允许 {appid}/history 与 boards/{key} 歧义共存，而 Python
// 声明序保证 boards 字面段优先，分发器按同序裁决。
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

// handleGameSubresource 两级 GET 子路径分发：boards 字面段优先（与 Python
// 路由声明序一致），其余按 action 分派；未识别 action 回 404（与改动前
// ServeMux 默认行为一致）。
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

// handleGameSubresourcePost 两级 POST 子路径分发（目录写三件中的单对象件）。
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
	// 派生列日切换维护（版本一致零重算；失败只记日志不拖垮读取面）
	if h.maintainDerived != nil {
		h.maintainDerived(r.Context())
	}
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
