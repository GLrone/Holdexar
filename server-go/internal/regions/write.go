package regions

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"time"

	"holdexar/server-go/internal/bundles"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

type regionRequestError struct {
	status  int
	message string
}

func (e *regionRequestError) Error() string { return e.message }
func regionError(w http.ResponseWriter, err error) {
	status := 500
	var requestErr *regionRequestError
	if errors.As(err, &requestErr) {
		status = requestErr.status
	}
	writeJSON(w, status, map[string]any{"detail": err.Error()})
}

func (h *handler) requested(r *http.Request, field string, owned bool) ([]string, bool, error) {
	decoder := json.NewDecoder(http.MaxBytesReader(nil, r.Body, 64<<10))
	var body map[string]json.RawMessage
	if e := decoder.Decode(&body); e != nil || body == nil {
		return nil, false, &regionRequestError{422, "地区参数无效"}
	}
	var extra any
	if e := decoder.Decode(&extra); e != io.EOF {
		return nil, false, &regionRequestError{422, "地区参数无效"}
	}
	raw, exists := body[field]
	if !exists || string(raw) == "null" {
		return nil, true, nil
	}
	var codes []string
	if e := json.Unmarshal(raw, &codes); e != nil {
		return nil, false, &regionRequestError{422, "地区参数无效"}
	}
	items, e := h.regionItems(r.Context())
	if e != nil {
		return nil, false, e
	}
	valid := map[string]bool{}
	for _, item := range items {
		valid[item.Code] = true
	}
	seen := map[string]bool{}
	wanted := []string{}
	bad := []string{}
	for _, code := range codes {
		code = strings.ToLower(strings.TrimSpace(code))
		if owned && code == "" {
			continue
		}
		if seen[code] {
			continue
		}
		seen[code] = true
		if !valid[code] {
			bad = append(bad, code)
		}
		wanted = append(wanted, code)
	}
	if len(bad) > 0 {
		sort.Strings(bad)
		return nil, false, &regionRequestError{400, fmt.Sprintf("未知区服代码: %s", strings.Join(bad, ", "))}
	}
	if owned && len(wanted) == 0 {
		return nil, false, &regionRequestError{400, "已购抓取地区不能为空：请至少勾选一个区服，或改回「跟随监控地区」"}
	}
	return wanted, false, nil
}

func (h *handler) updateEnabled(w http.ResponseWriter, r *http.Request) {
	wanted, all, e := h.requested(r, "enabled", false)
	if e != nil {
		regionError(w, e)
		return
	}
	if all {
		items, e := h.regionItems(r.Context())
		if e != nil {
			writeJSON(w, 500, map[string]any{"detail": e.Error()})
			return
		}
		for _, item := range items {
			wanted = append(wanted, item.Code)
		}
	}
	raw, e := json.Marshal(wanted)
	if e != nil {
		writeJSON(w, 500, map[string]any{"detail": e.Error()})
		return
	}
	e = h.options.Gate.WithWrite(r.Context(), store.PriorityInteractive, func(conn *sql.Conn) error {
		_, e := conn.ExecContext(r.Context(), `UPDATE crawl_regions SET enabled=EXISTS(SELECT 1 FROM json_each(?) WHERE value=crawl_regions.code),updated_at=? WHERE enabled IS NOT EXISTS(SELECT 1 FROM json_each(?) WHERE value=crawl_regions.code)`, string(raw), time.Now().UTC().Add(8*time.Hour).Format("2006-01-02 15:04:05.000000"), string(raw))
		if e != nil {
			return e
		}
		_, e = bundles.RefreshSortCache(r.Context(), conn, nil, wanted)
		return e
	})
	if e != nil {
		writeJSON(w, 500, map[string]any{"detail": e.Error()})
		return
	}
	items, e := h.regionItems(r.Context())
	if e != nil {
		writeJSON(w, 500, map[string]any{"detail": e.Error()})
		return
	}
	writeJSON(w, 200, map[string]any{"regions": items})
}

func (h *handler) updateOwned(w http.ResponseWriter, r *http.Request) {
	wanted, follow, e := h.requested(r, "regions", true)
	if e != nil {
		regionError(w, e)
		return
	}
	var value any = wanted
	if follow {
		value = nil
	}
	if e = settings.SetValues(r.Context(), h.options.Gate, h.options.Settings, map[string]any{ownedRegionsKey: value}); e != nil {
		writeJSON(w, 500, map[string]any{"detail": e.Error()})
		return
	}
	writeJSON(w, 200, map[string]any{"ownedRegions": value})
}
