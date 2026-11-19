// 投递状态语义：pending/suppressed 待投递，sending/unknown 禁止自动重发。
package notifications

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"strings"

	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/httpx"
	"holdexar/server-go/internal/store"
)

const prefsKey = "notifications.prefs"

// maxAttempts 单条候选最大投递尝试次数（达到即不再重试）。
const maxAttempts = 3

const statusUnknown = "unknown"

type Options struct {
	Gate     *store.WriteGate
	Bus      *events.Bus
	Delivery *DeliveryService
}

func Register(mux *http.ServeMux, db *sql.DB, options ...Options) {
	h := &handler{db: db}
	if len(options) > 0 {
		h.options = options[0]
	}
	if h.options.Gate != nil {
		mux.HandleFunc("PUT /api/v1/notifications/prefs", h.putPrefs)
	}
	if h.options.Delivery != nil {
		h.registerSMTP(mux)
	}
	mux.HandleFunc("GET /api/v1/notifications/prefs", h.getPrefs)
	mux.HandleFunc("GET /api/v1/notifications/candidates", h.listCandidates)
	mux.HandleFunc("GET /api/v1/notifications/stats", h.getStats)
	mux.HandleFunc("GET /api/v1/notifications/facts", h.listFacts)
	for _, p := range []string{
		"/api/v1/notifications/prefs",
		"/api/v1/notifications/candidates",
		"/api/v1/notifications/stats",
		"/api/v1/notifications/facts",
	} {
		mux.HandleFunc(p, httpx.MethodNotAllowed("GET"))
	}
}

type handler struct {
	db      *sql.DB
	options Options
}

type featuredPrefsView struct {
	Scope           any `json:"scope"`
	MinDiscount     any `json:"minDiscount"`
	MinSavings      any `json:"minSavings"`
	MaxPrice        any `json:"maxPrice"`
	LowMode         any `json:"lowMode"`
	AllowFullPrice  any `json:"allowFullPrice"`
	MinPositiveRate any `json:"minPositiveRate"`
	MinReviews      any `json:"minReviews"`
	MissingReviews  any `json:"missingReviews"`
	IncludeOwned    any `json:"includeOwned"`
	IncludeIgnored  any `json:"includeIgnored"`
	Sort            any `json:"sort"`
	WindowDays      any `json:"windowDays"`
}

type lastDeliveryView struct {
	Status    *string `json:"status"`
	At        *string `json:"at"`
	Attempts  *int64  `json:"attempts"`
	Reason    *string `json:"reason"`
	Retryable bool    `json:"retryable"`
}

type deliveryChannelView struct {
	Kind       string `json:"kind"`
	Configured bool   `json:"configured"`
	Name       string `json:"name,omitempty"`
}

type prefsBaseView struct {
	Enabled        any               `json:"enabled"`
	QuietEnabled   any               `json:"quietEnabled"`
	QuietStart     any               `json:"quietStart"`
	QuietEnd       any               `json:"quietEnd"`
	IncludeDetails any               `json:"includeDetails"`
	MaxAttempts    int               `json:"maxAttempts"`
	ManualRegion   any               `json:"manualRegion"`
	DigestTime     any               `json:"digestTime"`
	DigestWeekday  any               `json:"digestWeekday"`
	Featured       featuredPrefsView `json:"featured"`
	Region         RegionSelection   `json:"region"`
	Types          []typeEntry       `json:"types"`
	Categories     []categoryEntry   `json:"categories"`
}

type prefsGetResponse struct {
	prefsBaseView
	SMTP         *smtpSummaryView     `json:"smtp"`
	LastDelivery *lastDeliveryView    `json:"lastDelivery"`
	Delivery     *deliveryChannelView `json:"delivery,omitempty"`
}

func (h *handler) getPrefs(w http.ResponseWriter, r *http.Request) {
	prefs, err := h.readPrefs(r)
	if err != nil {
		httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
		return
	}
	smtp, err := h.smtpView(r)
	if err != nil {
		httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
		return
	}
	last, err := h.lastDelivery(r)
	if err != nil {
		httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
		return
	}
	region, err := EffectiveRegion(r.Context(), h.db, "", prefs, nil)
	if err != nil {
		httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
		return
	}
	resp := prefsGetResponse{
		prefsBaseView: buildPrefsBaseView(prefs, region),
		SMTP:          smtp,
		LastDelivery:  last,
	}
	if h.options.Delivery != nil && h.options.Delivery.options.Plugins != nil {
		active, problem := h.options.Delivery.options.Plugins.Active(r.Context())
		if problem != nil {
			resp.Delivery = &deliveryChannelView{Kind: "plugin", Configured: false}
		} else if active != nil {
			resp.Delivery = &deliveryChannelView{Kind: "plugin", Configured: true, Name: active.Manifest.Name}
		}
	}
	httpx.WriteJSON(w, http.StatusOK, resp)
}

func buildPrefsBaseView(p map[string]any, region RegionSelection) prefsBaseView {
	return prefsBaseView{
		Enabled:        patched(p, "enabled", false),
		QuietEnabled:   patched(p, "quietEnabled", false),
		QuietStart:     patched(p, "quietStart", "23:00"),
		QuietEnd:       patched(p, "quietEnd", "08:00"),
		IncludeDetails: patched(p, "includeDetails", true),
		MaxAttempts:    maxAttempts,
		ManualRegion:   patched(p, "manualRegion", nil),
		DigestTime:     patched(p, "digestTime", "08:00"),
		DigestWeekday:  patched(p, "digestWeekday", json.Number("0")),
		Featured:       featuredView(p),
		Region:         region,
		Types:          typeEntries(p),
		Categories:     categoryEntries(p),
	}
}

func patched(p map[string]any, key string, def any) any {
	if raw, ok := p[key]; ok {
		return raw
	}
	return def
}

var featuredDefaultKeys = []struct {
	key string
	def any
}{
	{"scope", []any{"followed", "wishlist"}}, {"minDiscount", json.Number("50")},
	{"minSavings", nil}, {"maxPrice", nil}, {"lowMode", "any"},
	{"allowFullPrice", false}, {"minPositiveRate", nil}, {"minReviews", nil},
	{"missingReviews", "include"}, {"includeOwned", false}, {"includeIgnored", false},
	{"sort", "discount"}, {"windowDays", nil},
}

func featuredView(p map[string]any) featuredPrefsView {
	patch, _ := p["featured"].(map[string]any)
	return featuredPrefsView{
		Scope:           patched(patch, "scope", []any{"followed", "wishlist"}),
		MinDiscount:     patched(patch, "minDiscount", json.Number("50")),
		MinSavings:      patched(patch, "minSavings", nil),
		MaxPrice:        patched(patch, "maxPrice", nil),
		LowMode:         patched(patch, "lowMode", "any"),
		AllowFullPrice:  patched(patch, "allowFullPrice", false),
		MinPositiveRate: patched(patch, "minPositiveRate", nil),
		MinReviews:      patched(patch, "minReviews", nil),
		MissingReviews:  patched(patch, "missingReviews", "include"),
		IncludeOwned:    patched(patch, "includeOwned", false),
		IncludeIgnored:  patched(patch, "includeIgnored", false),
		Sort:            patched(patch, "sort", "discount"),
		WindowDays:      patched(patch, "windowDays", nil),
	}
}

type typeEntry struct {
	Key       string `json:"key"`
	Label     string `json:"label"`
	Category  string `json:"category"`
	Enabled   any    `json:"enabled"`
	Frequency any    `json:"frequency"`
}

func typeEntries(p map[string]any) []typeEntry {
	patch, _ := p["types"].(map[string]any)
	out := make([]typeEntry, 0, len(typeLabels))
	for _, t := range typeLabels {
		cfg, _ := patch[t.key].(map[string]any)
		enabled, frequency := any(true), any("immediate")
		if raw, ok := cfg["enabled"]; ok {
			enabled = raw
		}
		if raw, ok := cfg["frequency"]; ok {
			frequency = raw
		}
		out = append(out, typeEntry{Key: t.key, Label: t.label, Category: t.category,
			Enabled: enabled, Frequency: frequency})
	}
	return out
}

type categoryEntry struct {
	Key        string `json:"key"`
	Label      string `json:"label"`
	Enabled    any    `json:"enabled"`
	EventTypes int    `json:"eventTypes"`
}

func categoryEntries(p map[string]any) []categoryEntry {
	patch, _ := p["categories"].(map[string]any)
	out := make([]categoryEntry, 0, len(categoryLabels))
	for _, c := range categoryLabels {
		enabled := any(true)
		if raw, ok := patch[c.key]; ok {
			enabled = raw
		}
		out = append(out, categoryEntry{Key: c.key, Label: c.label, Enabled: enabled,
			EventTypes: c.eventTypes})
	}
	return out
}

func (h *handler) lastDelivery(r *http.Request) (*lastDeliveryView, error) {
	var status, deliveredAt, updatedAt, reason, lastError sql.NullString
	var attempts sql.NullInt64
	filter := ""
	var args []any
	if h.options.Delivery != nil && h.options.Delivery.options.Plugins != nil {
		recipient, e := savedRecipient(r.Context(), h.db)
		if e != nil {
			return nil, e
		}
		filter = " AND recipient=?"
		args = []any{recipient}
	}
	err := h.db.QueryRowContext(r.Context(),
		"SELECT status, CAST(delivered_at AS TEXT), CAST(updated_at AS TEXT),"+
			" attempts, reason, last_error FROM notification_candidates"+
			" WHERE status IN ('delivered', 'failed', 'unknown')"+
			filter+" ORDER BY updated_at DESC LIMIT 1", args...).Scan(&status, &deliveredAt, &updatedAt,
		&attempts, &reason, &lastError)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	at := isoformatPtr(deliveredAt)
	if at == nil {
		at = isoformatPtr(updatedAt)
	}
	return &lastDeliveryView{
		Status:    nullTextPtr(status),
		At:        at,
		Attempts:  nullIntPtr(attempts),
		Reason:    nullTextPtr(reason),
		Retryable: isRetryable(lastError.String),
	}, nil
}

func (h *handler) readPrefs(r *http.Request) (map[string]any, error) {
	return LoadPrefs(r.Context(), h.db)
}

// settingValue 读单个设置的 value_json；行缺席回 (nil, false)，NULL 回 (nil, true)。
func (h *handler) settingValue(ctx context.Context, key string) (any, bool, error) {
	var raw sql.NullString
	err := h.db.QueryRowContext(ctx,
		"SELECT value_json FROM app_settings WHERE key = ?", key).Scan(&raw)
	if err == sql.ErrNoRows {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	if !raw.Valid || raw.String == "" {
		return nil, true, nil
	}
	dec := json.NewDecoder(strings.NewReader(raw.String))
	dec.UseNumber()
	var value any
	if err := dec.Decode(&value); err != nil {
		return nil, true, err
	}
	return value, true, nil
}
