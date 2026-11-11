package notifications

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"holdexar/server-go/internal/plugins"
	"holdexar/server-go/internal/store"
)

type DeliveryCandidate struct {
	ID         int64          `json:"id"`
	SourceKind string         `json:"sourceKind"`
	EventID    int64          `json:"eventId"`
	CycleID    int64          `json:"cycleId"`
	AppID      int64          `json:"appid"`
	Region     *string        `json:"region"`
	EventType  string         `json:"eventType"`
	Category   string         `json:"category"`
	Recipient  string         `json:"recipient"`
	Status     string         `json:"status"`
	Reason     *string        `json:"reason"`
	Attempts   int            `json:"attempts"`
	LastError  *string        `json:"lastError"`
	CreatedAt  *string        `json:"createdAt"`
	UpdatedAt  *string        `json:"updatedAt"`
	DueAt      *string        `json:"dueAt"`
	Payload    map[string]any `json:"payload"`
	History    []any          `json:"history"`
	prior      *DeliveryCandidate
}
type candidateQuery interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}

const candidateColumns = `id,source_kind,event_id,cycle_id,appid,region_code,event_type,category,recipient,status,reason,attempts,last_error,CAST(created_at AS TEXT),CAST(updated_at AS TEXT),CAST(due_at AS TEXT),payload_json,delivery_history_json`

func readCandidates(ctx context.Context, q candidateQuery, suffix string, args ...any) ([]DeliveryCandidate, error) {
	rows, e := q.QueryContext(ctx, `SELECT `+candidateColumns+` FROM notification_candidates `+suffix, args...)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []DeliveryCandidate{}
	for rows.Next() {
		var c DeliveryCandidate
		var payload, history sql.NullString
		e = rows.Scan(&c.ID, &c.SourceKind, &c.EventID, &c.CycleID, &c.AppID, &c.Region, &c.EventType, &c.Category, &c.Recipient, &c.Status, &c.Reason, &c.Attempts, &c.LastError, &c.CreatedAt, &c.UpdatedAt, &c.DueAt, &payload, &history)
		if e != nil {
			return nil, e
		}
		if payload.Valid && payload.String != "null" {
			d := json.NewDecoder(strings.NewReader(payload.String))
			d.UseNumber()
			if e = d.Decode(&c.Payload); e != nil {
				return nil, e
			}
		}
		if history.Valid && history.String != "null" {
			if e = json.Unmarshal([]byte(history.String), &c.History); e != nil {
				return nil, e
			}
		}
		out = append(out, c)
	}
	return out, rows.Err()
}
func deliveryStamp(t time.Time) string { return t.Format("2006-01-02 15:04:05.000000") }
func pointerText(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
func deliveryJSON(v any) (string, error) { b, e := json.Marshal(v); return string(b), e }

const matchingFactSQL = `(
 (n.source_kind='price_event' AND EXISTS(SELECT 1 FROM price_events f WHERE f.id=n.event_id AND f.cycle_id=n.cycle_id AND f.appid=n.appid AND f.event_type=n.event_type AND f.region_code IS n.region_code AND f.created_at<=n.created_at)) OR
 (n.source_kind='alert_event' AND EXISTS(SELECT 1 FROM alert_events f WHERE f.id=n.event_id AND f.appid=n.appid AND f.region=n.region_code AND f.triggered_at<=n.created_at AND json_extract(f.details_json,'$.eventType')=n.event_type)) OR
 (n.source_kind='fact_notice' AND EXISTS(SELECT 1 FROM fact_notices f WHERE f.id=n.event_id AND f.occurred_at<=n.created_at AND coalesce(json_extract(f.data,'$.appid'),0)=n.appid AND n.region_code IS NULL AND n.event_type=CASE f.source||':'||f.kind WHEN 'hb_choice:bundle_changed' THEN 'HB_CHOICE' WHEN 'epic_free:free_rotation' THEN 'EPIC_FREE' WHEN 'bundles:discount_pick' THEN 'BUNDLE_DEAL' WHEN 'alerts:proxy_health' THEN 'PROXY_ALERT' WHEN 'alerts:system_alert' THEN 'SYSTEM_ALERT' END))
 )`

func recoverStuckSending(ctx context.Context, conn *sql.Conn, now time.Time) (int64, error) {
	r, err := conn.ExecContext(ctx, `UPDATE notification_candidates SET status='unknown',reason='投递中断，外部是否已接收无法确认',last_error='sending 超时，投递结果未知',updated_at=? WHERE status='sending' AND (updated_at IS NULL OR updated_at<?)`, deliveryStamp(now), deliveryStamp(now.Add(-30*time.Minute)))
	if err != nil {
		return 0, err
	}
	return r.RowsAffected()
}

func (s *DeliveryService) Recover(ctx context.Context) (int64, error) {
	var total int64
	e := s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		now := s.options.Now()
		n, e := recoverStuckSending(ctx, conn, now)
		if e != nil {
			return e
		}
		total += n
		r, e := conn.ExecContext(ctx, `UPDATE notification_candidates AS n SET status='unknown',reason='提醒依据缺失或不一致，已停止发送并保留记录',last_error='price event identity mismatch',updated_at=? WHERE status IN('pending','suppressed','failed','sending') AND NOT `+matchingFactSQL, deliveryStamp(now))
		if e != nil {
			return e
		}
		n, e = r.RowsAffected()
		total += n
		return e
	})
	return total, e
}

func mutable(status string) bool {
	return status == "pending" || status == "suppressed" || status == "failed"
}
func retargetTx(ctx context.Context, conn *sql.Conn, recipient string, now time.Time) (int, error) {
	if recipient == "" {
		return 0, nil
	}
	rows, e := readCandidates(ctx, conn, `WHERE event_id IN(SELECT event_id FROM notification_candidates WHERE status IN('pending','suppressed','failed')) ORDER BY id`)
	if e != nil {
		return 0, e
	}
	targets := map[string]*DeliveryCandidate{}
	key := func(c *DeliveryCandidate) string { return c.SourceKind + ":" + fmt.Sprint(c.EventID) }
	for i := range rows {
		row := &rows[i]
		target := targets[key(row)]
		if target == nil || !mutable(row.Status) && mutable(target.Status) || mutable(target.Status) && row.Recipient == recipient {
			targets[key(row)] = row
		}
	}
	changed := map[int64]*DeliveryCandidate{}
	deleted := []int64{}
	moved := 0
	at := deliveryStamp(now)
	for i := range rows {
		row := &rows[i]
		if !mutable(row.Status) {
			continue
		}
		target := targets[key(row)]
		if target.ID == row.ID && row.Recipient == recipient {
			continue
		}
		snapshotAt := any(nil)
		if row.UpdatedAt != nil {
			snapshotAt = strings.Replace(pointerText(row.UpdatedAt), " ", "T", 1)
		}
		history := append(append([]any{}, row.History...), map[string]any{"recipient": row.Recipient, "status": row.Status, "attempts": row.Attempts, "lastError": row.LastError, "reason": row.Reason, "at": snapshotAt})
		moved++
		if target.ID == row.ID {
			row.Recipient = recipient
			row.History = history
			row.UpdatedAt = &at
			changed[row.ID] = row
			continue
		}
		target.History = append(target.History, history...)
		if mutable(target.Status) {
			target.Attempts += row.Attempts
			if row.Status == "failed" {
				if target.Status != "failed" || isRetryable(pointerText(target.LastError)) {
					target.Reason, target.LastError = row.Reason, row.LastError
				}
				target.Status = "failed"
			}
			target.UpdatedAt = &at
		}
		changed[target.ID] = target
		deleted = append(deleted, row.ID)
	}
	if len(deleted) > 0 {
		raw, e := deliveryJSON(deleted)
		if e != nil {
			return 0, e
		}
		if _, e = conn.ExecContext(ctx, `DELETE FROM notification_candidates WHERE id IN(SELECT value FROM json_each(?))`, raw); e != nil {
			return 0, e
		}
	}
	updates := []*DeliveryCandidate{}
	deletedSet := map[int64]bool{}
	for _, id := range deleted {
		deletedSet[id] = true
	}
	for id, c := range changed {
		if !deletedSet[id] {
			updates = append(updates, c)
		}
	}
	if len(updates) > 0 {
		raw, e := deliveryJSON(updates)
		if e != nil {
			return 0, e
		}
		_, e = conn.ExecContext(ctx, `UPDATE notification_candidates AS n SET recipient=j.value->>'recipient',status=j.value->>'status',reason=j.value->>'reason',last_error=j.value->>'lastError',attempts=j.value->>'attempts',updated_at=j.value->>'updatedAt',delivery_history_json=j.value->'history' FROM json_each(?) j WHERE n.id=j.value->>'id'`, raw)
		if e != nil {
			return 0, e
		}
	}
	return moved, nil
}
func savedRecipient(ctx context.Context, q SettingsQuery) (string, error) {
	if target, err := plugins.Recipient(ctx, q); err != nil || target != "" {
		return target, err
	}
	var raw sql.NullString
	e := q.QueryRowContext(ctx, `SELECT value_json FROM app_settings WHERE key='smtp.to_addr'`).Scan(&raw)
	if e == sql.ErrNoRows {
		return "", nil
	}
	if e != nil {
		return "", e
	}
	if !raw.Valid || raw.String == "null" {
		return "", nil
	}
	var recipient string
	e = json.Unmarshal([]byte(raw.String), &recipient)
	return recipient, e
}
func eligible(c DeliveryCandidate, prefs map[string]any, now time.Time) bool {
	now = time.Date(now.Year(), now.Month(), now.Day(), now.Hour(), now.Minute(), now.Second(), now.Nanosecond(), time.UTC)
	if pointerText(c.Reason) == "rule_paused" {
		return false
	}
	if c.DueAt != nil {
		at, e := parseFactTime(*c.DueAt)
		if e != nil || at.After(now) {
			return false
		}
	}
	if prefs != nil && !TypeEnabled(prefs, c.EventType) {
		return false
	}
	return c.Status == "pending" || c.Status == "suppressed" || c.Status == "failed" && c.Attempts < maxAttempts && isRetryable(pointerText(c.LastError))
}
func (s *DeliveryService) claim(ctx context.Context) ([]DeliveryCandidate, error) {
	claimed := []DeliveryCandidate{}
	e := s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		prefs, e := LoadPrefs(ctx, conn)
		if e != nil {
			return e
		}
		if InQuietWindow(s.options.Now(), prefs) {
			return nil
		}
		recipient, e := savedRecipient(ctx, conn)
		if e != nil || recipient == "" {
			return e
		}
		if _, e = retargetTx(ctx, conn, recipient, s.options.Now()); e != nil {
			return e
		}
		rows, e := readCandidates(ctx, conn, `WHERE recipient=? AND (status IN('pending','suppressed') OR (status='failed' AND attempts<3)) ORDER BY id`, recipient)
		if e != nil {
			return e
		}
		selected := []DeliveryCandidate{}
		now := s.options.Now()
		for _, c := range rows {
			if eligible(c, prefs, now) {
				selected = append(selected, c)
			}
		}
		if len(selected) == 0 {
			return nil
		}
		raw, e := deliveryJSON(selected)
		if e != nil {
			return e
		}
		at := deliveryStamp(now)
		result, e := conn.QueryContext(ctx, `UPDATE notification_candidates AS n SET status='sending',attempts=n.attempts+1,updated_at=? WHERE EXISTS(SELECT 1 FROM json_each(?) j WHERE n.id=j.value->>'id' AND n.event_id=j.value->>'eventId' AND n.recipient=j.value->>'recipient' AND n.status=j.value->>'status' AND n.attempts=j.value->>'attempts' AND n.updated_at IS j.value->>'updatedAt' AND n.last_error IS j.value->>'lastError') AND `+matchingFactSQL+` RETURNING id`, at, raw)
		if e != nil {
			return e
		}
		ids := map[int64]bool{}
		for result.Next() {
			var id int64
			if e = result.Scan(&id); e != nil {
				result.Close()
				return e
			}
			ids[id] = true
		}
		e = result.Err()
		result.Close()
		if e != nil {
			return e
		}
		for _, c := range selected {
			if ids[c.ID] {
				prior := c
				c.prior = &prior
				c.Attempts++
				c.UpdatedAt = &at
				claimed = append(claimed, c)
			}
		}
		return nil
	})
	if e != nil {
		return nil, e
	}
	return claimed, nil
}

const claimedIdentity = `EXISTS(SELECT 1 FROM json_each(?) j WHERE n.id=j.value->>'id' AND n.event_id=j.value->>'eventId' AND n.recipient=j.value->>'recipient' AND n.attempts=j.value->>'attempts' AND n.updated_at IS j.value->>'updatedAt')`

func (s *DeliveryService) settle(ctx context.Context, candidates []DeliveryCandidate, status string, reason, lastError *string) (int64, error) {
	if len(candidates) == 0 {
		return 0, nil
	}
	raw, e := deliveryJSON(candidates)
	if e != nil {
		return 0, e
	}
	var changed int64
	e = s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		now := deliveryStamp(s.options.Now())
		r, e := conn.ExecContext(ctx, `UPDATE notification_candidates AS n SET status=?,reason=?,last_error=?,updated_at=?,delivered_at=CASE WHEN ?='delivered' THEN ? ELSE delivered_at END WHERE status='sending' AND `+claimedIdentity, status, reason, lastError, now, status, now, raw)
		if e != nil {
			return e
		}
		changed, e = r.RowsAffected()
		return e
	})
	return changed, e
}
func (s *DeliveryService) release(ctx context.Context, candidates []DeliveryCandidate) error {
	payloads := []map[string]any{}
	for _, c := range candidates {
		if c.prior != nil {
			payloads = append(payloads, map[string]any{"claim": c, "prior": c.prior})
		}
	}
	if len(payloads) == 0 {
		return nil
	}
	raw, e := deliveryJSON(payloads)
	if e != nil {
		return e
	}
	return s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, e := conn.ExecContext(ctx, `UPDATE notification_candidates AS n SET status=j.value->>'$.prior.status',reason=j.value->>'$.prior.reason',last_error=j.value->>'$.prior.lastError',attempts=j.value->>'$.prior.attempts',updated_at=j.value->>'$.prior.updatedAt' FROM json_each(?) j WHERE n.id=j.value->>'$.claim.id' AND n.event_id=j.value->>'$.claim.eventId' AND n.recipient=j.value->>'$.claim.recipient' AND n.status='sending' AND n.attempts=j.value->>'$.claim.attempts' AND n.updated_at IS j.value->>'$.claim.updatedAt'`, raw)
		return e
	})
}
