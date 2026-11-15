package games

import (
	"net/http"
	"strconv"
	"strings"
)

// 榜单读端点：热销 TOP100 与各板原始 appid 清单（调试/观测入口，无写
// 副作用）。拉取完全失败（无缓存无 stale）→ 502 FETCH_FAILED；
// appIds 与 Python 同为字符串数组。

func (h *handler) handleTop100(w http.ResponseWriter, r *http.Request) {
	if h.boardGet == nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	appids, err := h.boardGet(r.Context(), "topsellers")
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	if len(appids) == 0 {
		writeJSON(w, http.StatusBadGateway, map[string]any{"detail": "FETCH_FAILED"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"appIds": appIDStrings(appids), "cached": true})
}

func (h *handler) handleBoard(w http.ResponseWriter, r *http.Request, key string) {
	if h.boardGet == nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	appids, err := h.boardGet(r.Context(), key)
	if err != nil {
		// 未知榜单源：boards.Client 对未注册 key 回同一文案错误，与 Python
		// BOARDS 白名单 404 同径；其余错误按通用 500。
		if strings.HasPrefix(err.Error(), "未知榜单源") {
			writeJSON(w, http.StatusNotFound, map[string]any{"detail": err.Error()})
			return
		}
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	if len(appids) == 0 {
		writeJSON(w, http.StatusBadGateway, map[string]any{"detail": "FETCH_FAILED"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"board": key, "appIds": appIDStrings(appids), "count": len(appids)})
}

func appIDStrings(ids []int64) []string {
	out := make([]string, len(ids))
	for i, id := range ids {
		out[i] = strconv.FormatInt(id, 10)
	}
	return out
}

// handleBundles 关联捆绑包包壳：linkedBundles 已逐字段对齐 Python，零新逻辑。
func (h *handler) handleBundles(w http.ResponseWriter, r *http.Request) {
	appid, err := parseAppID(r.PathValue("appid"))
	if err != nil {
		writeJSON(w, http.StatusUnprocessableEntity, intParseDetail(r.PathValue("appid")))
		return
	}
	items, err := h.linkedBundles(r.Context(), appid)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"bundles": items})
}
