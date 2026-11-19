package notifications

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"net/http"
	"strings"

	"holdexar/server-go/internal/httpx"
)

// factsLimitCap 单次增量读取上限（轮询节拍 60s 下充裕；防异常积压拖垮响应）。
const factsLimitCap = 100

type factItem struct {
	ID         int64           `json:"id"`
	Source     *string         `json:"source"`
	Kind       *string         `json:"kind"`
	Data       json.RawMessage `json:"data"`
	OccurredAt *string         `json:"occurredAt"`
}

type factsResponse struct {
	LatestID int64      `json:"latestId"`
	Items    []factItem `json:"items"`
}

func (h *handler) listFacts(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	zero, one, capLimit := int64(0), int64(1), int64(factsLimitCap)
	afterID, hasAfter, verr := httpx.QueryInt(q, "afterId", 0, &zero, nil)
	if verr != nil {
		httpx.WriteValidationError(w, *verr)
		return
	}
	limit, _, verr := httpx.QueryInt(q, "limit", 50, &one, &capLimit)
	if verr != nil {
		httpx.WriteValidationError(w, *verr)
		return
	}

	var latest sql.NullInt64
	if err := h.db.QueryRowContext(r.Context(),
		"SELECT MAX(id) FROM fact_notices").Scan(&latest); err != nil {
		httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
		return
	}
	items := []factItem{}
	if hasAfter {
		rows, err := h.db.QueryContext(r.Context(),
			"SELECT id, source, kind, data, CAST(occurred_at AS TEXT)"+
				" FROM fact_notices WHERE id > ? ORDER BY id LIMIT ?", afterID, limit)
		if err != nil {
			httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
			return
		}
		defer rows.Close()
		for rows.Next() {
			var id int64
			var source, kind sql.NullString
			var data sql.NullString
			var occurredAt sql.NullString
			if err := rows.Scan(&id, &source, &kind, &data, &occurredAt); err != nil {
				httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
				return
			}
			items = append(items, factItem{
				ID:         id,
				Source:     nullTextPtr(source),
				Kind:       nullTextPtr(kind),
				Data:       rawNullable(data),
				OccurredAt: isoformatPtr(occurredAt),
			})
		}
		if err := rows.Err(); err != nil {
			httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
			return
		}
	}
	httpx.WriteJSON(w, http.StatusOK, factsResponse{LatestID: latestOrZero(latest), Items: items})
}

func rawNullable(v sql.NullString) json.RawMessage {
	if !v.Valid || strings.TrimSpace(v.String) == "" {
		return json.RawMessage("null")
	}
	var buf bytes.Buffer
	if err := json.Compact(&buf, []byte(v.String)); err != nil {
		return json.RawMessage("null")
	}
	return buf.Bytes()
}

func latestOrZero(v sql.NullInt64) int64 {
	if !v.Valid {
		return 0
	}
	return v.Int64
}
