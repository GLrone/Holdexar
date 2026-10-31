package notifications

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"net/http"
	"strings"
)

// factsLimitCap 单次增量读取上限（轮询节拍 60s 下充裕；防异常积压拖垮响应）。
const factsLimitCap = 100

// listFacts 内容链事实通知的增量出口（灵动岛轮询）：afterId 缺省 = 只对齐，
// 回空增量与 latestId；消费端记下游标后从后续轮询开始取增量。
func (h *handler) listFacts(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	afterID, hasAfter, errBody := queryIntUnbounded(q, "afterId", 0)
	if errBody != nil {
		writeJSON(w, http.StatusUnprocessableEntity, errBody)
		return
	}
	limit, errBody := queryInt(q, "limit", 50, 1, factsLimitCap)
	if errBody != nil {
		writeJSON(w, http.StatusUnprocessableEntity, errBody)
		return
	}

	var latest sql.NullInt64
	if err := h.db.QueryRowContext(r.Context(),
		"SELECT MAX(id) FROM fact_notices").Scan(&latest); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
		return
	}
	items := []*orderedObj{}
	if hasAfter {
		rows, err := h.db.QueryContext(r.Context(),
			"SELECT id, source, kind, data, CAST(occurred_at AS TEXT)"+
				" FROM fact_notices WHERE id > ? ORDER BY id LIMIT ?", afterID, limit)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
			return
		}
		defer rows.Close()
		for rows.Next() {
			var id int64
			var source, kind sql.NullString
			var data sql.NullString
			var occurredAt sql.NullString
			if err := rows.Scan(&id, &source, &kind, &data, &occurredAt); err != nil {
				writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
				return
			}
			item := kvObj(
				"id", id,
				"source", nullTextPtr(source),
				"kind", nullTextPtr(kind),
				// data 原样透传（NULL 保持 null，不做默认填充）
				"data", rawNullable(data),
				"occurredAt", isoformatPtr(occurredAt),
			)
			items = append(items, item)
		}
		if err := rows.Err(); err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
			return
		}
	}
	writeJSON(w, http.StatusOK, kvObj("latestId", latestOrZero(latest), "items", items))
}

// rawNullable JSON 列原样透传：NULL/空 → null，其余紧凑化（保序、数值原样）。
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

// latestOrZero 最大事实 id：空表按 0（Python int(latest or 0) 语义）。
func latestOrZero(v sql.NullInt64) int64 {
	if !v.Valid {
		return 0
	}
	return v.Int64
}
