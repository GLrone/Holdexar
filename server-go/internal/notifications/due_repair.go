package notifications

import (
	"context"
	"database/sql"
	"fmt"

	"holdexar/server-go/internal/store"
)

func (s *DeliveryService) fillMissingDue(ctx context.Context) error {
	return s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		prefs, e := LoadPrefs(ctx, conn)
		if e != nil || !truthy(prefs["enabled"]) {
			return e
		}
		rows, e := conn.QueryContext(ctx, `SELECT n.id,n.event_type,CAST(coalesce(CASE n.source_kind WHEN 'price_event' THEN pe.occurred_at WHEN 'alert_event' THEN ae.triggered_at WHEN 'fact_notice' THEN fn.occurred_at END,n.created_at) AS TEXT) FROM notification_candidates n LEFT JOIN price_events pe ON n.source_kind='price_event' AND n.event_id=pe.id LEFT JOIN alert_events ae ON n.source_kind='alert_event' AND n.event_id=ae.id LEFT JOIN fact_notices fn ON n.source_kind='fact_notice' AND n.event_id=fn.id WHERE n.due_at IS NULL AND n.status IN('pending','suppressed','failed') AND coalesce(n.reason,'')!='rule_paused' AND `+matchingFactSQL)
		if e != nil {
			return e
		}
		payloads := []map[string]any{}
		for rows.Next() {
			var id int64
			var kind string
			var at sql.NullString
			if e = rows.Scan(&id, &kind, &at); e != nil {
				rows.Close()
				return e
			}
			if !TypeEnabled(prefs, kind) {
				continue
			}
			occurred := s.options.Now()
			if at.Valid {
				occurred, e = parseFactTime(at.String)
				if e != nil {
					rows.Close()
					return fmt.Errorf("提醒%d的发生时间无效：%w", id, e)
				}
			}
			due := DeliveryDue(occurred, CategoryFrequency(prefs, kind), prefs)
			payloads = append(payloads, map[string]any{"id": id, "due": deliveryStamp(due)})
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return e
		}
		if len(payloads) == 0 {
			return nil
		}
		raw, e := deliveryJSON(payloads)
		if e != nil {
			return e
		}
		_, e = conn.ExecContext(ctx, `UPDATE notification_candidates AS n SET due_at=j.value->>'due' FROM json_each(?) j WHERE n.id=j.value->>'id' AND n.due_at IS NULL AND n.status IN('pending','suppressed','failed')`, raw)
		return e
	})
}
