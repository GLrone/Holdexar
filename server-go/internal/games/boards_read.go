package games

import (
	"net/http"
	"strconv"
	"strings"
)

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
