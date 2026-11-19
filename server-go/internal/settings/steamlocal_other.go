//go:build !windows

package settings

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
)

func registerSteamLocalRoutes(mux *http.ServeMux, _ *handler) {
	mux.HandleFunc("GET /api/v1/settings/steam-local-accounts", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"accounts": []any{}})
	})
	mux.HandleFunc("POST /api/v1/settings/steam-local-accounts/import", func(w http.ResponseWriter, r *http.Request) {
		if _, verr := decodeSteamIDImport(w, r); verr != nil {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []valErr{*verr}})
			return
		}
		writeJSON(w, http.StatusNotFound, map[string]any{"detail": "本机 Steam 账号记录已不可用"})
	})
}

func decodeSteamIDImport(w http.ResponseWriter, r *http.Request) (string, *valErr) {
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20))
	if err != nil {
		return "", &valErr{Type: "missing", Loc: []any{"body"}, Msg: "Field required"}
	}
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" {
		return "", &valErr{Type: "missing", Loc: []any{"body"}, Msg: "Field required", Input: nil}
	}
	var top any
	if err := json.Unmarshal([]byte(trimmed), &top); err != nil {
		return "", &valErr{Type: "json_invalid", Loc: []any{"body"}, Msg: "JSON decode error", Input: map[string]any{}}
	}
	fields, ok := top.(map[string]any)
	if !ok {
		return "", &valErr{Type: "model_attributes_type", Loc: []any{"body"},
			Msg: "Input should be a valid dictionary or object to extract fields from", Input: top}
	}
	value, present := fields["steam_id"]
	if !present || value == nil {
		return "", &valErr{Type: "missing", Loc: []any{"body", "steam_id"}, Msg: "Field required", Input: fields}
	}
	text, ok := value.(string)
	if !ok {
		return "", &valErr{Type: "string_type", Loc: []any{"body", "steam_id"},
			Msg: "Input should be a valid string", Input: value}
	}
	if len(text) < 17 {
		return "", &valErr{Type: "string_too_short", Loc: []any{"body", "steam_id"},
			Msg: "String should have at least 17 characters", Input: text, Ctx: map[string]any{"min_length": 17}}
	}
	if len(text) > 17 {
		return "", &valErr{Type: "string_too_long", Loc: []any{"body", "steam_id"},
			Msg: "String should have at most 17 characters", Input: text, Ctx: map[string]any{"max_length": 17}}
	}
	return text, nil
}
