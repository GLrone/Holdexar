package agent

import (
	"encoding/json"
	"net/http"
	"strings"

	"holdexar/server-go/internal/store"
)

func (h *httpRead) registerMemoryRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/agent/memories", h.listMemoriesHTTP)
	mux.HandleFunc("POST /api/v1/agent/memories", h.upsertMemoryHTTP)
	mux.HandleFunc("DELETE /api/v1/agent/memories/{category}/{key}", h.deleteMemoryHTTP)
	mux.HandleFunc("DELETE /api/v1/agent/memories", h.clearMemoriesHTTP)
}

func (h *httpRead) writeGate() *store.WriteGate {
	if h.deps != nil {
		return h.deps.WriteGate
	}
	return nil
}

func (h *httpRead) listMemoriesHTTP(w http.ResponseWriter, r *http.Request) {
	category := r.URL.Query().Get("category")
	items, err := ListMemoriesByCategory(r.Context(), h.db, category)
	if err != nil {
		writeRunJSON(w, http.StatusInternalServerError, map[string]any{"detail": err.Error()})
		return
	}
	writeRunJSON(w, http.StatusOK, map[string]any{"items": items})
}

func (h *httpRead) upsertMemoryHTTP(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Category   string   `json:"category"`
		Key        string   `json:"key"`
		Value      any      `json:"value"`
		Confidence *float64 `json:"confidence"`
		Source     string   `json:"source"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&body); err != nil {
		writeRunJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": "invalid request body"})
		return
	}
	cat := strings.TrimSpace(body.Category)
	key := strings.TrimSpace(body.Key)
	if cat == "" || len([]rune(cat)) > 32 || key == "" || len([]rune(key)) > 64 || body.Value == nil {
		writeRunJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": "invalid memory fields"})
		return
	}
	conf := 1.0
	if body.Confidence != nil {
		conf = *body.Confidence
		if conf < 0 || conf > 1 {
			writeRunJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": "confidence out of range"})
			return
		}
	}
	if len([]rune(body.Source)) > 64 {
		writeRunJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": "source too long"})
		return
	}
	item, err := UpsertMemory(r.Context(), h.db, h.writeGate(), cat, key, body.Value, conf, body.Source)
	if err != nil {
		writeRunJSON(w, http.StatusInternalServerError, map[string]any{"detail": err.Error()})
		return
	}
	writeRunJSON(w, http.StatusOK, item)
}

func (h *httpRead) deleteMemoryHTTP(w http.ResponseWriter, r *http.Request) {
	ok, err := DeleteMemory(r.Context(), h.db, h.writeGate(), r.PathValue("category"), r.PathValue("key"))
	if err != nil {
		writeRunJSON(w, http.StatusInternalServerError, map[string]any{"detail": err.Error()})
		return
	}
	if !ok {
		writeRunJSON(w, http.StatusNotFound, map[string]any{"detail": "memory not found"})
		return
	}
	writeRunJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (h *httpRead) clearMemoriesHTTP(w http.ResponseWriter, r *http.Request) {
	count, err := ClearMemories(r.Context(), h.db, h.writeGate(), r.URL.Query().Get("category"))
	if err != nil {
		writeRunJSON(w, http.StatusInternalServerError, map[string]any{"detail": err.Error()})
		return
	}
	writeRunJSON(w, http.StatusOK, map[string]any{"ok": true, "count": count})
}
