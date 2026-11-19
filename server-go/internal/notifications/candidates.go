package notifications

import (
	"database/sql"
	"encoding/json"
	"net/http"
	"strings"

	"holdexar/server-go/internal/httpx"
)

type candidateDetail struct {
	Title         any `json:"title"`
	Price         any `json:"price"`
	Currency      any `json:"currency"`
	OriginalPrice any `json:"originalPrice"`
	Discount      any `json:"discount"`
	ObservedAt    any `json:"observedAt"`
	Labels        any `json:"labels"`
	Summary       any `json:"summary"`
}

type candidateItem struct {
	ID              *int64          `json:"id"`
	EventID         *int64          `json:"eventId"`
	CycleID         *int64          `json:"cycleId"`
	AppID           *int64          `json:"appid"`
	Region          *string         `json:"region"`
	SourceKind      *string         `json:"sourceKind"`
	EventType       *string         `json:"eventType"`
	EventLabel      string          `json:"eventLabel"`
	Category        *string         `json:"category"`
	Recipient       *string         `json:"recipient"`
	Status          *string         `json:"status"`
	Reason          *string         `json:"reason"`
	Attempts        *int64          `json:"attempts"`
	DeliveredAt     *string         `json:"deliveredAt"`
	DeliveryHistory json.RawMessage `json:"deliveryHistory"`
	Payload         json.RawMessage `json:"payload"`
	CreatedAt       *string         `json:"createdAt"`
	DueAt           *string         `json:"dueAt"`
	Detail          candidateDetail `json:"detail"`
}

func (h *handler) listCandidates(w http.ResponseWriter, r *http.Request) {
	minLimit, maxLimit := int64(1), int64(500)
	limit, _, verr := httpx.QueryInt(r.URL.Query(), "limit", 50, &minLimit, &maxLimit)
	if verr != nil {
		httpx.WriteValidationError(w, *verr)
		return
	}
	rows, err := h.db.QueryContext(r.Context(),
		"SELECT id, event_id, cycle_id, appid, region_code, source_kind, event_type, category,"+
			" recipient, status, reason, attempts, CAST(delivered_at AS TEXT),"+
			" delivery_history_json, payload_json, CAST(created_at AS TEXT), CAST(due_at AS TEXT)"+
			" FROM notification_candidates ORDER BY id DESC LIMIT ?", limit)
	if err != nil {
		httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
		return
	}
	defer rows.Close()

	type candidate struct {
		id, eventID, cycleID, appid sql.NullInt64
		region                      sql.NullString
		sourceKind                  sql.NullString
		eventType                   sql.NullString
		category                    sql.NullString
		recipient                   sql.NullString
		status                      sql.NullString
		reason                      sql.NullString
		attempts                    sql.NullInt64
		deliveredAt                 sql.NullString
		deliveryHistory             sql.NullString
		payload                     sql.NullString
		createdAt                   sql.NullString
		dueAt                       sql.NullString
	}
	var cands []candidate
	for rows.Next() {
		var c candidate
		if err := rows.Scan(&c.id, &c.eventID, &c.cycleID, &c.appid, &c.region,
			&c.sourceKind, &c.eventType, &c.category, &c.recipient, &c.status,
			&c.reason, &c.attempts, &c.deliveredAt, &c.deliveryHistory,
			&c.payload, &c.createdAt, &c.dueAt); err != nil {
			httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
			return
		}
		cands = append(cands, c)
	}
	if err := rows.Err(); err != nil {
		httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
		return
	}

	appids := map[int64]bool{}
	var priceIDs []int64
	for _, c := range cands {
		if c.appid.Valid && c.appid.Int64 != 0 {
			appids[c.appid.Int64] = true
		}
		if c.sourceKind.Valid && c.sourceKind.String == "price_event" {
			priceIDs = append(priceIDs, c.eventID.Int64)
		}
	}
	names, err := h.gameNames(r, appids)
	if err != nil {
		httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
		return
	}
	events, err := h.eventCurrents(r, priceIDs)
	if err != nil {
		httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
		return
	}

	items := make([]candidateItem, 0, len(cands))
	for _, c := range cands {
		var detail candidateDetail
		if c.sourceKind.Valid && c.sourceKind.String == "price_event" {
			var current map[string]any
			if raw, ok := events[c.eventID.Int64]; ok {
				if parsed, ok := parseJSONNumber(raw).(map[string]any); ok {
					current = parsed
				}
			}
			detail = candidateDetail{
				Title:         names[appidValue(c.appid)],
				Price:         current["price"],
				Currency:      current["currency"],
				OriginalPrice: current["originalPrice"],
				Discount:      current["discount"],
				ObservedAt:    jsonFieldOr(current["observedAt"], current["at"]),
				Labels:        []any{},
				Summary:       nil,
			}
		} else {
			payload, _ := parseJSONNumber(c.payload.String).(map[string]any)
			fallback := any(nil)
			if name, ok := names[appidValue(c.appid)]; ok {
				fallback = name
			}
			var labels any = []any{}
			if truthy(payload["labels"]) {
				labels = payload["labels"]
			}
			detail = candidateDetail{
				Title:         jsonFieldOr(payload["gameName"], payload["title"], fallback),
				Price:         payload["price"],
				Currency:      payload["currency"],
				OriginalPrice: payload["originalPrice"],
				Discount:      payload["discount"],
				ObservedAt:    payload["observedAt"],
				Labels:        labels,
				Summary:       payload["summary"],
			}
		}
		eventType := ""
		if c.eventType.Valid {
			eventType = c.eventType.String
		}
		items = append(items, candidateItem{
			ID:              nullIntPtr(c.id),
			EventID:         nullIntPtr(c.eventID),
			CycleID:         nullIntPtr(c.cycleID),
			AppID:           nullIntPtr(c.appid),
			Region:          nullTextPtr(c.region),
			SourceKind:      nullTextPtr(c.sourceKind),
			EventType:       nullTextPtr(c.eventType),
			EventLabel:      eventLabel(eventType),
			Category:        nullTextPtr(c.category),
			Recipient:       nullTextPtr(c.recipient),
			Status:          nullTextPtr(c.status),
			Reason:          nullTextPtr(c.reason),
			Attempts:        nullIntPtr(c.attempts),
			DeliveredAt:     isoformatPtr(c.deliveredAt),
			DeliveryHistory: rawOrFallback(c.deliveryHistory.String, "[]"),
			Payload:         rawOrFallback(c.payload.String, "{}"),
			CreatedAt:       isoformatPtr(c.createdAt),
			DueAt:           isoformatPtr(c.dueAt),
			Detail:          detail,
		})
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": items})
}

func appidValue(v sql.NullInt64) int64 {
	if !v.Valid {
		return -1
	}
	return v.Int64
}

func (h *handler) gameNames(r *http.Request, appids map[int64]bool) (map[int64]any, error) {
	names := map[int64]any{}
	if len(appids) == 0 {
		return names, nil
	}
	ids := make([]int64, 0, len(appids))
	for id := range appids {
		ids = append(ids, id)
	}
	query := "SELECT appid, name FROM games WHERE appid IN (" + placeholders(len(ids)) + ")"
	args := make([]any, len(ids))
	for i, id := range ids {
		args[i] = id
	}
	rows, err := h.db.QueryContext(r.Context(), query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var appid int64
		var name sql.NullString
		if err := rows.Scan(&appid, &name); err != nil {
			return nil, err
		}
		if name.Valid {
			names[appid] = name.String
		} else {
			names[appid] = nil
		}
	}
	return names, rows.Err()
}

func (h *handler) eventCurrents(r *http.Request, ids []int64) (map[int64]string, error) {
	out := map[int64]string{}
	if len(ids) == 0 {
		return out, nil
	}
	query := "SELECT id, CAST(current_json AS TEXT) FROM price_events WHERE id IN (" +
		placeholders(len(ids)) + ")"
	args := make([]any, len(ids))
	for i, id := range ids {
		args[i] = id
	}
	rows, err := h.db.QueryContext(r.Context(), query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var id int64
		var current sql.NullString
		if err := rows.Scan(&id, &current); err != nil {
			return nil, err
		}
		out[id] = current.String
	}
	return out, rows.Err()
}

func placeholders(n int) string {
	if n <= 0 {
		return ""
	}
	return strings.TrimSuffix(strings.Repeat("?,", n), ",")
}
