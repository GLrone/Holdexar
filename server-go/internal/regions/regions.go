// Package regions：区服元数据只读下发。
// 数据源：crawl_regions 表（区服配置唯一事实）+ app_settings 已购抓取区键。
// 写端点（PUT enabled / PUT owned）未迁移，见 deferred 清单。
package regions

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"
)

// ownedRegionsKey 已购游戏专用抓取区设置键；缺省/空 = 跟随全局启用集。
const ownedRegionsKey = "crawl.owned_regions"

// Register 挂载 regions 只读路由。
func Register(mux *http.ServeMux, db *sql.DB) {
	h := &handler{db: db}
	mux.HandleFunc("GET /api/v1/regions", h.listRegions)
	mux.HandleFunc("/api/v1/regions", methodNotAllowed("GET"))
}

type handler struct {
	db *sql.DB
}

// regionItem 区服元数据行（crawl_regions 投影）。
type regionItem struct {
	Code     string `json:"code"`
	Name     string `json:"name"`
	Currency string `json:"currency"`
	Enabled  bool   `json:"enabled"`
	Sort     int    `json:"sort"`
}

type regionsResponse struct {
	Regions []regionItem `json:"regions"`
	// OwnedRegions null = 跟随全局启用集；数组 = 自定义子集（小写 cc）。
	OwnedRegions *[]string `json:"ownedRegions"`
}

// listRegions 全量区服（含禁用标记，按 sort 排）+ 已购抓取区。
func (h *handler) listRegions(w http.ResponseWriter, r *http.Request) {
	items := []regionItem{}
	rows, err := h.db.QueryContext(r.Context(),
		"SELECT code, name, currency, enabled, sort FROM crawl_regions ORDER BY sort, code")
	if err != nil {
		http.Error(w, fmt.Sprintf("区服查询失败：%v", err), http.StatusInternalServerError)
		return
	}
	defer rows.Close()
	for rows.Next() {
		var it regionItem
		var enabled sql.NullInt64
		if err := rows.Scan(&it.Code, &it.Name, &it.Currency, &enabled, &it.Sort); err != nil {
			http.Error(w, fmt.Sprintf("区服读取失败：%v", err), http.StatusInternalServerError)
			return
		}
		it.Enabled = enabled.Valid && enabled.Int64 != 0
		items = append(items, it)
	}
	if err := rows.Err(); err != nil {
		http.Error(w, fmt.Sprintf("区服读取失败：%v", err), http.StatusInternalServerError)
		return
	}
	owned, err := h.ownedRegions(r.Context())
	if err != nil {
		http.Error(w, fmt.Sprintf("已购抓取区读取失败：%v", err), http.StatusInternalServerError)
		return
	}
	writeJSON(w, http.StatusOK, regionsResponse{Regions: items, OwnedRegions: owned})
}

// ownedRegions 读 app_settings 的已购抓取区键：非空列表归一为小写串，其余为 null。
func (h *handler) ownedRegions(ctx context.Context) (*[]string, error) {
	var raw sql.NullString
	err := h.db.QueryRowContext(ctx,
		"SELECT value_json FROM app_settings WHERE key = ?", ownedRegionsKey).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if !raw.Valid {
		return nil, nil
	}
	var value any
	if err := json.Unmarshal([]byte(raw.String), &value); err != nil {
		return nil, err
	}
	list, ok := value.([]any)
	if !ok || len(list) == 0 {
		return nil, nil
	}
	out := make([]string, 0, len(list))
	for _, el := range list {
		out = append(out, strings.ToLower(strings.TrimSpace(pyStr(el))))
	}
	return &out, nil
}

// pyStr JSON 标量转 Python str() 语义字符串。写入侧保证元素为字符串，
// 数值/布尔/null 为越界数据，按近似语义兜底。
func pyStr(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case bool:
		if x {
			return "True"
		}
		return "False"
	case nil:
		return "None"
	case float64:
		if x == math.Trunc(x) && math.Abs(x) < 1e15 {
			return strconv.FormatFloat(x, 'f', -1, 64)
		}
		return strconv.FormatFloat(x, 'g', -1, 64)
	default:
		return fmt.Sprint(x)
	}
}

// methodNotAllowed 非声明方法的回退响应（对齐 {"detail": ...} 形态）。
func methodNotAllowed(allow string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Allow", allow)
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"detail": "Method Not Allowed"})
	}
}

// writeJSON JSON 响应体不带尾随换行。
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
