package plugins

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
)

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
func failure(w http.ResponseWriter, e error) {
	writeJSON(w, http.StatusBadRequest, map[string]string{"detail": e.Error()})
}
func decode(w http.ResponseWriter, r *http.Request, out any) error {
	b, e := io.ReadAll(http.MaxBytesReader(w, r.Body, maxManifest))
	if e != nil {
		return errors.New("plugin request is too large")
	}
	return strictJSON(b, out)
}

func catalogPresentation(entries []Listing) []map[string]any {
	result := make([]map[string]any, 0, len(entries))
	for _, entry := range entries {
		result = append(result, map[string]any{"manifest": entry.Manifest, "sha256": entry.SHA256, "url": entry.URL, "compatible": entry.Manifest.Validate() == nil, "trust": "unsigned", "enabled": false, "configured": false})
	}
	return result
}

func (m *Manager) status(ctx context.Context) (map[string]any, error) {
	installed, e := m.List(ctx)
	if e != nil {
		return nil, e
	}
	catalog, e := m.readCatalog(ctx)
	if e != nil {
		return nil, e
	}
	if catalog.Plugins == nil {
		catalog.Plugins = []Listing{}
	}
	var builtin any = []any{}
	if m.options.BuiltinList != nil {
		builtin, e = m.options.BuiltinList(ctx)
		if e != nil {
			return nil, e
		}
	}
	return map[string]any{"installed": installed, "catalog": catalogPresentation(catalog.Plugins), "catalogSource": catalog.Source, "catalogError": catalog.Error, "builtin": builtin}, nil
}

func (m *Manager) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/plugins", func(w http.ResponseWriter, r *http.Request) {
		value, e := m.status(r.Context())
		if e != nil {
			failure(w, e)
			return
		}
		writeJSON(w, 200, value)
	})
	mux.HandleFunc("GET /api/v1/plugins/discover", func(w http.ResponseWriter, r *http.Request) {
		value, e := m.Discover(r.Context(), r.URL.Query().Get("source"), r.URL.Query().Get("q"))
		if e != nil {
			failure(w, e)
			return
		}
		writeJSON(w, 200, map[string]any{"items": catalogPresentation(value.Plugins), "source": value.Source, "cached": value.Cached, "error": value.Error})
	})
	mux.HandleFunc("POST /api/v1/plugins/install", func(w http.ResponseWriter, r *http.Request) {
		var input Selection
		if e := decode(w, r, &input); e != nil {
			failure(w, e)
			return
		}
		value, e := m.Install(r.Context(), input)
		if e != nil {
			failure(w, e)
			return
		}
		writeJSON(w, 200, value)
	})
	mux.HandleFunc("POST /api/v1/plugins/install-local", func(w http.ResponseWriter, r *http.Request) {
		raw, e := io.ReadAll(http.MaxBytesReader(w, r.Body, maxPackage))
		if e != nil {
			failure(w, errors.New("plugin package is too large"))
			return
		}
		value, e := m.InstallLocal(r.Context(), raw)
		if e != nil {
			failure(w, e)
			return
		}
		writeJSON(w, 200, value)
	})
	mux.HandleFunc("PUT /api/v1/plugins/builtin/{id}", func(w http.ResponseWriter, r *http.Request) {
		var input struct {
			Enabled *bool `json:"enabled"`
		}
		if e := decode(w, r, &input); e != nil {
			failure(w, e)
			return
		}
		if input.Enabled == nil || m.options.BuiltinSet == nil {
			failure(w, errors.New("built-in feature control is unavailable"))
			return
		}
		if e := m.options.BuiltinSet(r.Context(), r.PathValue("id"), *input.Enabled); e != nil {
			failure(w, e)
			return
		}
		value, e := m.status(r.Context())
		if e != nil {
			failure(w, e)
			return
		}
		writeJSON(w, 200, value)
	})
	mux.HandleFunc("PUT /api/v1/plugins/{id}", func(w http.ResponseWriter, r *http.Request) {
		var input struct {
			Enabled *bool          `json:"enabled"`
			Config  map[string]any `json:"config"`
		}
		if e := decode(w, r, &input); e != nil {
			failure(w, e)
			return
		}
		value, e := m.Update(r.Context(), r.PathValue("id"), input.Enabled, input.Config)
		if e != nil {
			failure(w, e)
			return
		}
		writeJSON(w, 200, value)
	})
	mux.HandleFunc("DELETE /api/v1/plugins/{id}", func(w http.ResponseWriter, r *http.Request) {
		if e := m.Remove(r.Context(), r.PathValue("id")); e != nil {
			failure(w, e)
			return
		}
		writeJSON(w, 200, map[string]bool{"removed": true})
	})
	mux.HandleFunc("POST /api/v1/plugins/{id}/test", func(w http.ResponseWriter, r *http.Request) {
		active, e := m.Active(r.Context())
		if e != nil {
			failure(w, e)
			return
		}
		if active == nil || active.Manifest.ID != r.PathValue("id") {
			failure(w, errors.New("enable and configure this plugin before testing"))
			return
		}
		if m.options.Test == nil {
			failure(w, errors.New("notification test is unavailable"))
			return
		}
		if e = m.options.Test(r.Context(), active.Manifest.ID); e != nil {
			failure(w, errors.New("notification test failed; check its target and configuration"))
			return
		}
		writeJSON(w, 200, map[string]bool{"ok": true})
	})
}
