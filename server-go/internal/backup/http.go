package backup

import (
	"encoding/json"
	"errors"
	"mime"
	"net/http"
	"os"

	"holdexar/server-go/internal/store"
)

func (s *Service) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/system/backup", func(w http.ResponseWriter, r *http.Request) {
		items, err := s.List()
		respond(w, map[string]any{"items": items}, err)
	})
	mux.HandleFunc("POST /api/v1/system/backup", func(w http.ResponseWriter, r *http.Request) {
		var req struct{ Label string }
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8192)).Decode(&req); err != nil {
			respond(w, nil, ErrName)
			return
		}
		result, err := s.Create(r.Context(), req.Label)
		respond(w, result, err)
	})
	mux.HandleFunc("POST /api/v1/system/backup/{name}/verify", func(w http.ResponseWriter, r *http.Request) {
		var req struct{ Deep bool }
		if r.ContentLength != 0 {
			if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8192)).Decode(&req); err != nil {
				respond(w, nil, ErrName)
				return
			}
		}
		result, err := s.Verify(r.Context(), r.PathValue("name"), req.Deep)
		respond(w, result, err)
	})
	mux.HandleFunc("GET /api/v1/system/backup/{name}/download", func(w http.ResponseWriter, r *http.Request) {
		name := r.PathValue("name")
		file, err := s.open(name)
		if err != nil {
			respond(w, nil, err)
			return
		}
		defer file.Close()
		info, err := file.Stat()
		if err != nil {
			respond(w, nil, err)
			return
		}
		w.Header().Set("Content-Disposition", mime.FormatMediaType("attachment", map[string]string{"filename": name}))
		w.Header().Set("Content-Type", "application/octet-stream")
		http.ServeContent(w, r, name, info.ModTime(), file)
	})
	mux.HandleFunc("DELETE /api/v1/system/backup/{name}", func(w http.ResponseWriter, r *http.Request) {
		name := r.PathValue("name")
		if !backupName(name) {
			respond(w, nil, ErrName)
			return
		}
		end, err := store.BeginBackupMaintenance(s.dbPath)
		if err != nil {
			respond(w, nil, err)
			return
		}
		err = s.remove(name)
		respond(w, map[string]bool{"removed": err == nil}, errors.Join(err, end()))
	})
}

func respond(w http.ResponseWriter, value any, err error) {
	status := http.StatusOK
	if err != nil {
		status = http.StatusInternalServerError
		detail := "备份操作未完成"
		switch {
		case errors.Is(err, ErrName):
			status, detail = http.StatusBadRequest, ErrName.Error()
		case errors.Is(err, os.ErrNotExist):
			status, detail = http.StatusNotFound, "备份不存在"
		case errors.Is(err, store.ErrMaintenanceProtected):
			detail = store.ErrMaintenanceProtected.Error()
		}
		value = map[string]string{"detail": detail}
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
