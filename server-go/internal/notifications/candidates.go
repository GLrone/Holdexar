package notifications

import (
	"database/sql"
	"net/http"
	"strings"
)

// listCandidates 最近的通知候选（投递账本，只读），按 id 倒序 + limit 截断。
func (h *handler) listCandidates(w http.ResponseWriter, r *http.Request) {
	limit, errBody := queryInt(r.URL.Query(), "limit", 50, 1, 500)
	if errBody != nil {
		writeJSON(w, http.StatusUnprocessableEntity, errBody)
		return
	}
	rows, err := h.db.QueryContext(r.Context(),
		"SELECT id, event_id, cycle_id, appid, region_code, source_kind, event_type, category,"+
			" recipient, status, reason, attempts, CAST(delivered_at AS TEXT),"+
			" delivery_history_json, payload_json, CAST(created_at AS TEXT), CAST(due_at AS TEXT)"+
			" FROM notification_candidates ORDER BY id DESC LIMIT ?", limit)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
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
			writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
			return
		}
		cands = append(cands, c)
	}
	if err := rows.Err(); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
		return
	}

	// 游戏名与价格事件 current 快照只对命中的 id 批量取（Python 同构：缺席不回查）
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
		writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
		return
	}
	events, err := h.eventCurrents(r, priceIDs)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
		return
	}

	items := make([]*orderedObj, 0, len(cands))
	for _, c := range cands {
		var detail *orderedObj
		if c.sourceKind.Valid && c.sourceKind.String == "price_event" {
			var current map[string]any
			if raw, ok := events[c.eventID.Int64]; ok {
				if parsed, ok := parseJSONNumber(raw).(map[string]any); ok {
					current = parsed
				}
			}
			detail = kvObj(
				"title", names[appidValue(c.appid)],
				"price", current["price"],
				"currency", current["currency"],
				"originalPrice", current["originalPrice"],
				"discount", current["discount"],
				"observedAt", jsonFieldOr(current["observedAt"], current["at"]),
				"labels", []any{},
				"summary", nil,
			)
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
			detail = kvObj(
				"title", jsonFieldOr(payload["gameName"], payload["title"], fallback),
				"price", payload["price"],
				"currency", payload["currency"],
				"originalPrice", payload["originalPrice"],
				"discount", payload["discount"],
				"observedAt", payload["observedAt"],
				"labels", labels,
				"summary", payload["summary"],
			)
		}
		eventType := ""
		if c.eventType.Valid {
			eventType = c.eventType.String
		}
		item := newOrderedObj()
		item.set("id", nullIntPtr(c.id))
		item.set("eventId", nullIntPtr(c.eventID))
		item.set("cycleId", nullIntPtr(c.cycleID))
		item.set("appid", nullIntPtr(c.appid))
		item.set("region", nullTextPtr(c.region))
		item.set("sourceKind", nullTextPtr(c.sourceKind))
		item.set("eventType", nullTextPtr(c.eventType))
		item.set("eventLabel", eventLabel(eventType))
		item.set("category", nullTextPtr(c.category))
		item.set("recipient", nullTextPtr(c.recipient))
		item.set("status", nullTextPtr(c.status))
		item.set("reason", nullTextPtr(c.reason))
		item.set("attempts", nullIntPtr(c.attempts))
		item.set("deliveredAt", isoformatPtr(c.deliveredAt))
		item.set("deliveryHistory", rawOrFallback(c.deliveryHistory.String, "[]"))
		item.set("payload", rawOrFallback(c.payload.String, "{}"))
		item.set("createdAt", isoformatPtr(c.createdAt))
		item.set("dueAt", isoformatPtr(c.dueAt))
		item.set("detail", detail)
		items = append(items, item)
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items})
}

// appidValue 候选 appid 的查表键；NULL 行回不存在键（-1 恒无游戏名）。
func appidValue(v sql.NullInt64) int64 {
	if !v.Valid {
		return -1
	}
	return v.Int64
}

// gameNames appid → 游戏名；NULL 名按缺席处理（Python dict 值 None 同形）。
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

// eventCurrents price_event id → current_json 文本；缺席 = 无该事件（current 视为空）。
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

// placeholders N 个占位符（IN 子句用）。
func placeholders(n int) string {
	if n <= 0 {
		return ""
	}
	return strings.TrimSuffix(strings.Repeat("?,", n), ",")
}
