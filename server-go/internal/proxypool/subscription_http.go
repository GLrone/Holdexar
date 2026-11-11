package proxypool

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/store"
)

type SubscriptionView struct {
	ID               int64              `json:"id"`
	Kind             string             `json:"kind"`
	URL              string             `json:"url"`
	Label            *string            `json:"label"`
	CreatedAt        *string            `json:"createdAt"`
	LastImportedAt   *string            `json:"lastImportedAt"`
	LastStats        *subscriptionStats `json:"lastStats"`
	Deprecated       bool               `json:"deprecated"`
	DeprecatedAt     *string            `json:"deprecatedAt"`
	DeprecatedReason *string            `json:"deprecatedReason"`
	AutoRefresh      bool               `json:"autoRefresh"`
}

type subscriptionStats struct {
	Fetched     *int    `json:"fetched,omitempty"`
	Added       *int    `json:"added,omitempty"`
	Skipped     *int    `json:"skipped,omitempty"`
	Total       *int    `json:"total,omitempty"`
	Alive       *int    `json:"alive,omitempty"`
	Traffic     *string `json:"traffic,omitempty"`
	Nodes       *int    `json:"nodes,omitempty"`
	Cached      *bool   `json:"cached,omitempty"`
	LabelSource string  `json:"labelSource,omitempty"`
}

const subscriptionViewSQL = `SELECT id,kind,url,label,replace(created_at,' ','T'),replace(last_imported_at,' ','T'),
	CAST(last_stats AS TEXT),COALESCE(deprecated,0),replace(deprecated_at,' ','T'),deprecated_reason,COALESCE(auto_refresh,1)
	FROM proxy_subscriptions`

var (
	subscriptionURLInText = regexp.MustCompile(`(?i)https?://[^\s<>"']+`)
	errSubscriptionKind   = errors.New("当前仅支持管理 Clash 订阅")
)

func parseSubscriptionURL(raw string) *url.URL {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Hostname() == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return nil
	}
	return parsed
}

func maskSubscriptionURL(raw string) string {
	if raw == "" {
		return ""
	}
	if parsed := parseSubscriptionURL(raw); parsed != nil {
		return parsed.Scheme + "://" + parsed.Host + "/***"
	}
	return "***"
}

func safeSubscriptionText(raw string) string {
	return subscriptionURLInText.ReplaceAllStringFunc(raw, maskSubscriptionURL)
}

func scanSubscriptionView(row interface{ Scan(...any) error }, dataDir string, now time.Time) (SubscriptionView, error) {
	var view SubscriptionView
	var storedURL, stats string
	var rawStats sql.NullString
	if err := row.Scan(&view.ID, &view.Kind, &storedURL, &view.Label, &view.CreatedAt, &view.LastImportedAt,
		&rawStats, &view.Deprecated, &view.DeprecatedAt, &view.DeprecatedReason, &view.AutoRefresh); err != nil {
		return view, err
	}
	view.URL = maskSubscriptionURL(OpenURL(dataDir, storedURL))
	if view.Label != nil {
		*view.Label = safeSubscriptionText(*view.Label)
	}
	if view.DeprecatedReason != nil {
		*view.DeprecatedReason = safeSubscriptionText(*view.DeprecatedReason)
	}
	stats = rawStats.String
	if json.Unmarshal([]byte(stats), &view.LastStats) == nil && view.LastStats != nil {
		if view.LastStats.Traffic != nil {
			*view.LastStats.Traffic = safeSubscriptionText(*view.LastStats.Traffic)
		}
		if source := view.LastStats.LabelSource; source != "panel" && source != "user" {
			view.LastStats.LabelSource = ""
		}
	} else {
		view.LastStats = nil
	}
	if reason := SubscriptionBlockReason(stats, now); reason != "" {
		view.Deprecated, view.DeprecatedReason = true, &reason
	}
	return view, nil
}

func ListSubscriptionViews(ctx context.Context, db *sql.DB, dataDir, kind string) ([]SubscriptionView, error) {
	rows, err := db.QueryContext(ctx, subscriptionViewSQL+` WHERE (?='' OR kind=?) ORDER BY id`, kind, kind)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := []SubscriptionView{}
	now := time.Now()
	for rows.Next() {
		view, err := scanSubscriptionView(rows, dataDir, now)
		if err != nil {
			return nil, err
		}
		items = append(items, view)
	}
	return items, rows.Err()
}

type subscriptionHTTP struct {
	db        *sql.DB
	gate      *store.WriteGate
	dataDir   string
	refresh   func(context.Context, int64) (*SyncResult, error)
	onChanged func()
}

type subscriptionEdit struct {
	Kind        string  `json:"kind"`
	URL         *string `json:"url"`
	Label       *string `json:"label"`
	AutoRefresh *bool   `json:"autoRefresh"`
}

func RegisterSubscriptionRoutes(mux *http.ServeMux, db *sql.DB, gate *store.WriteGate, dataDir string,
	refresh func(context.Context, int64) (*SyncResult, error), onChanged func()) {
	h := subscriptionHTTP{db: db, gate: gate, dataDir: dataDir, refresh: refresh, onChanged: onChanged}
	const path = "/api/v1/proxies/subscriptions"
	mux.HandleFunc("GET "+path, h.list)
	mux.HandleFunc("GET "+path+"/{id}/url", h.reveal)
	mux.HandleFunc("POST "+path, h.add)
	mux.HandleFunc("PUT "+path+"/{id}", h.update)
	mux.HandleFunc("POST "+path+"/{id}/refresh", h.refreshRoute(true))
	mux.HandleFunc("POST "+path+"/{id}/sync", h.refreshRoute(false))
}

func subscriptionError(w http.ResponseWriter, status int, detail string) {
	runResponse(w, status, map[string]string{"detail": detail})
}

func subscriptionRouteID(w http.ResponseWriter, r *http.Request) (int64, bool) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		subscriptionError(w, http.StatusUnprocessableEntity, "订阅编号不正确")
	}
	return id, err == nil
}

func readSubscriptionEdit(w http.ResponseWriter, r *http.Request, requireURL bool) (subscriptionEdit, bool) {
	var req *subscriptionEdit
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10))
	if decoder.Decode(&req) != nil || req == nil || decoder.Decode(new(any)) != io.EOF {
		subscriptionError(w, http.StatusUnprocessableEntity, "订阅参数不正确")
		return subscriptionEdit{}, false
	}
	if req.URL != nil {
		*req.URL = strings.TrimSpace(*req.URL)
		parsed := parseSubscriptionURL(*req.URL)
		if parsed == nil || parsed.Path == "/***" {
			subscriptionError(w, http.StatusBadRequest, "请输入完整的 http(s) 订阅链接")
			return subscriptionEdit{}, false
		}
	} else if requireURL {
		subscriptionError(w, http.StatusUnprocessableEntity, "缺少订阅链接")
		return subscriptionEdit{}, false
	}
	if req.Label != nil {
		*req.Label = truncateRunes(strings.TrimSpace(*req.Label), 100)
	}
	return *req, true
}

func (h subscriptionHTTP) list(w http.ResponseWriter, r *http.Request) {
	items, err := ListSubscriptionViews(r.Context(), h.db, h.dataDir, r.URL.Query().Get("kind"))
	if err != nil {
		subscriptionError(w, http.StatusInternalServerError, "暂时无法读取订阅")
		return
	}
	runResponse(w, http.StatusOK, map[string][]SubscriptionView{"items": items})
}

func (h subscriptionHTTP) reveal(w http.ResponseWriter, r *http.Request) {
	id, ok := subscriptionRouteID(w, r)
	if !ok {
		return
	}
	var stored string
	err := h.db.QueryRowContext(r.Context(), `SELECT url FROM proxy_subscriptions WHERE id=?`, id).Scan(&stored)
	if errors.Is(err, sql.ErrNoRows) {
		subscriptionError(w, http.StatusNotFound, "订阅不存在")
		return
	}
	if err != nil {
		subscriptionError(w, http.StatusInternalServerError, "暂时无法读取订阅")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	runResponse(w, http.StatusOK, map[string]any{"id": id, "url": OpenURL(h.dataDir, stored)})
}

func (h subscriptionHTTP) add(w http.ResponseWriter, r *http.Request) {
	req, ok := readSubscriptionEdit(w, r, true)
	if !ok {
		return
	}
	if req.Kind != "clash" {
		subscriptionError(w, http.StatusBadRequest, errSubscriptionKind.Error())
		return
	}
	ctx := r.Context()
	sealed, err := SealURL(h.dataDir, *req.URL)
	if err != nil {
		subscriptionError(w, http.StatusInternalServerError, "订阅链接无法安全保存")
		return
	}
	var label, stats any
	if req.Label != nil && *req.Label != "" {
		label, stats = *req.Label, `{"labelSource":"user"}`
	}
	var id int64
	err = h.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		return conn.QueryRowContext(ctx, `INSERT INTO proxy_subscriptions(kind,url,label,created_at,last_stats,deprecated,admission_status,auto_refresh)
			VALUES ('clash',?,?,?,?,0,?,1) RETURNING id`, sealed, label, pyTimeText(time.Now()), stats, AdmissionCandidate).Scan(&id)
	})
	if err != nil {
		subscriptionError(w, http.StatusInternalServerError, "暂时无法保存订阅")
		return
	}
	response := map[string]any{"id": id, "kind": "clash", "url": maskSubscriptionURL(*req.URL), "label": label}
	if label != nil {
		response["label"] = safeSubscriptionText(label.(string))
	}
	if fields, ok := h.refreshFields(ctx, id); ok {
		copyRefreshFields(response, fields)
		response["cached"] = false
	} else {
		response["warning"] = "订阅已保存，本次下载未完成"
	}
	runResponse(w, http.StatusOK, response)
}

func (h subscriptionHTTP) update(w http.ResponseWriter, r *http.Request) {
	id, ok := subscriptionRouteID(w, r)
	if !ok {
		return
	}
	req, ok := readSubscriptionEdit(w, r, false)
	if !ok {
		return
	}
	ctx := r.Context()
	var urlChanged, enabled bool
	var view SubscriptionView
	err := h.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		var kind, sealed string
		var label, rawStats sql.NullString
		var automatic bool
		if err := conn.QueryRowContext(ctx, `SELECT kind,url,label,CAST(last_stats AS TEXT),COALESCE(auto_refresh,1)
			FROM proxy_subscriptions WHERE id=?`, id).Scan(&kind, &sealed, &label, &rawStats, &automatic); err != nil {
			return err
		}
		if kind != "clash" {
			return errSubscriptionKind
		}
		var stats map[string]any
		if jsonUnmarshalUseNumber([]byte(rawStats.String), &stats) != nil || stats == nil {
			stats = map[string]any{}
		}
		labelChanged := req.Label != nil && *req.Label != label.String
		if labelChanged {
			label = sql.NullString{String: *req.Label, Valid: *req.Label != ""}
			if label.Valid {
				stats["labelSource"] = "user"
			} else {
				delete(stats, "labelSource")
			}
		}
		if req.AutoRefresh != nil {
			enabled = !automatic && *req.AutoRefresh
			automatic = *req.AutoRefresh
		}
		urlChanged = req.URL != nil && !URLMatches(h.dataDir, sealed, *req.URL)
		if urlChanged {
			newURL, err := SealURL(h.dataDir, *req.URL)
			if err != nil {
				return err
			}
			if err := resetSubscriptionURL(ctx, conn, id, newURL); err != nil {
				return err
			}
			source := stats["labelSource"]
			clear(stats)
			switch source {
			case "user":
				stats["labelSource"] = source
			case "panel":
				label = sql.NullString{}
			}
		}
		if labelChanged || urlChanged {
			rawStats = sql.NullString{}
			if len(stats) > 0 {
				encoded, err := json.Marshal(stats)
				if err != nil {
					return err
				}
				rawStats = sql.NullString{String: string(encoded), Valid: true}
			}
		}
		if _, err := conn.ExecContext(ctx, `UPDATE proxy_subscriptions SET label=?,last_stats=?,auto_refresh=? WHERE id=?`, label, rawStats, automatic, id); err != nil {
			return err
		}
		var err error
		view, err = scanSubscriptionView(conn.QueryRowContext(ctx, subscriptionViewSQL+` WHERE id=?`, id), h.dataDir, time.Now())
		return err
	})
	switch {
	case errors.Is(err, sql.ErrNoRows):
		subscriptionError(w, http.StatusBadRequest, "订阅不存在")
	case errors.Is(err, errSubscriptionKind):
		subscriptionError(w, http.StatusBadRequest, errSubscriptionKind.Error())
	case err != nil:
		subscriptionError(w, http.StatusInternalServerError, "暂时无法更新订阅")
	default:
		if urlChanged && h.onChanged != nil {
			h.onChanged()
		}
		response := map[string]any{"id": id, "kind": view.Kind, "url": view.URL, "label": view.Label, "autoRefresh": view.AutoRefresh, "synced": false}
		if urlChanged || enabled {
			if fields, ok := h.refreshFields(ctx, id); ok {
				copyRefreshFields(response, fields)
				response["synced"] = true
			} else {
				response["warning"] = "订阅已更新，本次下载未完成"
			}
		}
		runResponse(w, http.StatusOK, response)
	}
}

func resetSubscriptionURL(ctx context.Context, conn *sql.Conn, id int64, sealed string) error {
	if err := LockSubscriptionMutation(ctx, conn, id); err != nil {
		return err
	}
	rows, err := conn.QueryContext(ctx, `UPDATE proxy_nodes SET subscription_id=NULL WHERE subscription_id=? RETURNING node_id`, id)
	if err != nil {
		return err
	}
	defer rows.Close()
	nodes := map[string]bool{}
	for rows.Next() {
		var node string
		if err := rows.Scan(&node); err != nil {
			return err
		}
		nodes[node] = true
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if _, err := SettleNodesAfterSourceChange(ctx, conn, nodes, time.Now()); err != nil {
		return err
	}
	_, err = conn.ExecContext(ctx, `UPDATE proxy_subscriptions SET url=?,admission_status=?,
		last_imported_at=NULL,last_fetch_at=NULL,last_fetch_status=NULL,last_success_at=NULL,last_error=NULL,
		cache_url_identity=NULL,deprecated=0,deprecated_at=NULL,deprecated_reason=NULL WHERE id=?`, sealed, AdmissionCandidate, id)
	return err
}

func (h subscriptionHTTP) refreshRoute(trafficOnly bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		id, ok := subscriptionRouteID(w, r)
		if !ok {
			return
		}
		ctx := r.Context()
		var kind string
		err := h.db.QueryRowContext(ctx, `SELECT kind FROM proxy_subscriptions WHERE id=?`, id).Scan(&kind)
		switch {
		case errors.Is(err, sql.ErrNoRows):
			subscriptionError(w, http.StatusBadRequest, "订阅不存在")
			return
		case err != nil:
			subscriptionError(w, http.StatusInternalServerError, "暂时无法读取订阅")
			return
		case kind != "clash":
			subscriptionError(w, http.StatusBadRequest, errSubscriptionKind.Error())
			return
		}
		fields, ok := h.refreshFields(ctx, id)
		if !ok {
			subscriptionError(w, http.StatusBadGateway, "本次订阅下载未完成")
			return
		}
		if trafficOnly {
			if fields["traffic"] == nil {
				subscriptionError(w, http.StatusBadGateway, "订阅未返回新的流量信息")
				return
			}
			fields = map[string]any{"id": id, "traffic": fields["traffic"]}
		}
		runResponse(w, http.StatusOK, fields)
	}
}

func (h subscriptionHTTP) refreshFields(ctx context.Context, id int64) (map[string]any, bool) {
	if h.refresh == nil {
		return nil, false
	}
	result, err := h.refresh(ctx, id)
	if err != nil || result == nil || result.Failures[id] != "" {
		return nil, false
	}
	snapshot := result.Snapshots[id]
	count, ok := result.NodeCount[id]
	if !ok || snapshot == nil {
		return nil, false
	}
	view, err := scanSubscriptionView(h.db.QueryRowContext(ctx, subscriptionViewSQL+` WHERE id=?`, id), h.dataDir, time.Now())
	if err != nil {
		return nil, false
	}
	fields := map[string]any{"id": id, "label": view.Label, "nodes": count, "traffic": nil, "alive": 0, "total": 0, "usedCache": false, "restarted": false, "strategySwitched": result.StrategySwitched}
	if snapshot.Userinfo != "" {
		fields["traffic"] = safeSubscriptionText(snapshot.Userinfo)
	}
	if stats := view.LastStats; stats != nil {
		if stats.Alive != nil {
			fields["alive"] = *stats.Alive
		}
		if stats.Total != nil {
			fields["total"] = *stats.Total
		}
	}
	return fields, true
}

func copyRefreshFields(response, fields map[string]any) {
	for _, key := range []string{"label", "nodes", "traffic", "alive", "total", "restarted", "strategySwitched"} {
		if value, exists := fields[key]; exists {
			response[key] = value
		}
	}
}
