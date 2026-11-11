package notifications

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"sort"

	"holdexar/server-go/internal/store"
)

type eventScan struct {
	State  string         `json:"state"`
	Cursor int64          `json:"cursor"`
	Upper  int64          `json:"upper,omitempty"`
	Prefs  map[string]any `json:"prefs,omitempty"`
	AppIDs []int64        `json:"appids,omitempty"`
}

func readEventScan(ctx context.Context, q SettingsQuery, id int64) (eventScan, bool, error) {
	var raw sql.NullString
	e := q.QueryRowContext(ctx, `SELECT notification_scan_json FROM price_cycles WHERE id=?`, id).Scan(&raw)
	if errors.Is(e, sql.ErrNoRows) {
		return eventScan{}, false, nil
	}
	if e != nil {
		return eventScan{}, false, e
	}
	var scan eventScan
	if raw.Valid && raw.String != "null" {
		e = json.Unmarshal([]byte(raw.String), &scan)
	}
	return scan, true, e
}
func writeEventScan(ctx context.Context, conn *sql.Conn, id int64, scan eventScan) error {
	raw, e := deliveryJSON(scan)
	if e != nil {
		return e
	}
	_, e = conn.ExecContext(ctx, `UPDATE price_cycles SET notification_scan_json=? WHERE id=?`, raw, id)
	return e
}
func scanUpper(ctx context.Context, q SettingsQuery, id int64) (int64, error) {
	var upper int64
	e := q.QueryRowContext(ctx, `SELECT coalesce(MAX(id),0) FROM price_events WHERE cycle_id=?`, id).Scan(&upper)
	return upper, e
}

func (s *DeliveryService) openEventScan(ctx context.Context, id int64, prefs map[string]any, watched []int64, recipient string) (*eventScan, bool, error) {
	var opened *eventScan
	owned := false
	e := s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		scan, found, e := readEventScan(ctx, conn, id)
		if e != nil {
			return e
		}
		owned = found
		if !found {
			return nil
		}
		if scan.State == "scanning" {
			opened = &scan
			return nil
		}
		if scan.State == "done" {
			return nil
		}
		upper, e := scanUpper(ctx, conn, id)
		if e != nil {
			return e
		}
		upper = max(upper, scan.Cursor)
		if !truthy(prefs["enabled"]) || recipient == "" {
			return writeEventScan(ctx, conn, id, eventScan{State: "done", Cursor: upper})
		}
		raw, e := deliveryJSON(watched)
		if e != nil {
			return e
		}
		rows, e := conn.QueryContext(ctx, `SELECT DISTINCT appid FROM price_events WHERE cycle_id=? AND id>? AND id<=? AND appid IN(SELECT value FROM json_each(?)) ORDER BY appid`, id, scan.Cursor, upper, raw)
		if e != nil {
			return e
		}
		scope := []int64{}
		for rows.Next() {
			var appid int64
			if e = rows.Scan(&appid); e != nil {
				rows.Close()
				return e
			}
			scope = append(scope, appid)
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return e
		}
		scan = eventScan{State: "scanning", Cursor: scan.Cursor, Upper: upper, Prefs: prefs, AppIDs: scope}
		if e = writeEventScan(ctx, conn, id, scan); e != nil {
			return e
		}
		opened = &scan
		return nil
	})
	return opened, owned, e
}
func (s *DeliveryService) scanPage(ctx context.Context, id int64, scan eventScan) ([]Fact, error) {
	raw, e := deliveryJSON(scan.AppIDs)
	if e != nil {
		return nil, e
	}
	rows, e := s.db.QueryContext(ctx, `SELECT id,appid,region_code,event_type,CAST(occurred_at AS TEXT) FROM price_events WHERE cycle_id=? AND id>? AND id<=? AND appid IN(SELECT value FROM json_each(?)) ORDER BY id LIMIT 1000`, id, scan.Cursor, scan.Upper, raw)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	facts := []Fact{}
	for rows.Next() {
		f := Fact{CycleID: id}
		var occurred sql.NullString
		if e = rows.Scan(&f.ID, &f.AppID, &f.Region, &f.EventType, &occurred); e != nil {
			return nil, e
		}
		if occurred.Valid {
			f.OccurredAt, e = parseFactTime(occurred.String)
			if e != nil {
				return nil, e
			}
		}
		facts = append(facts, f)
	}
	return facts, rows.Err()
}
func (s *DeliveryService) commitEventPage(ctx context.Context, id int64, scan eventScan, owned bool, facts []Fact) (int64, error) {
	if len(facts) == 0 {
		return 0, nil
	}
	var created int64
	e := s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		current := scan
		if owned {
			saved, found, e := readEventScan(ctx, conn, id)
			if e != nil {
				return e
			}
			if !found || saved.State != "scanning" || saved.Upper != scan.Upper || saved.Cursor >= facts[len(facts)-1].ID {
				return nil
			}
			current = saved
		}
		recipient, e := savedRecipient(ctx, conn)
		if e != nil {
			return e
		}
		payloads := []map[string]any{}
		status := "pending"
		if InQuietWindow(s.options.Now(), scan.Prefs) {
			status = "suppressed"
		}
		for _, f := range facts {
			if f.ID > current.Cursor && TypeEnabled(scan.Prefs, f.EventType) {
				occurred := f.OccurredAt
				if occurred.IsZero() {
					occurred = s.options.Now()
				}
				due := DeliveryDue(occurred, CategoryFrequency(scan.Prefs, f.EventType), scan.Prefs)
				payloads = append(payloads, map[string]any{"id": f.ID, "appid": f.AppID, "region": f.Region, "event": f.EventType, "category": CategoryOf(f.EventType), "status": status, "due": deliveryStamp(due)})
			}
		}
		if len(payloads) > 0 {
			if recipient == "" && owned {
				return fmt.Errorf("提醒收件邮箱已清空，保留未消费事件等待配置恢复")
			}
			if recipient != "" {
				raw, e := deliveryJSON(payloads)
				if e != nil {
					return e
				}
				now := deliveryStamp(s.options.Now())
				r, e := conn.ExecContext(ctx, `INSERT INTO notification_candidates(source_kind,event_id,cycle_id,appid,region_code,event_type,category,recipient,status,attempts,created_at,updated_at,due_at)
 SELECT 'price_event',value->>'id',?,value->>'appid',value->>'region',value->>'event',value->>'category',?,value->>'status',0,?,?,value->>'due' FROM json_each(?)
 WHERE EXISTS(SELECT 1 FROM price_events f WHERE f.id=value->>'id' AND f.cycle_id=? AND f.appid=value->>'appid' AND f.region_code IS value->>'region' AND f.event_type=value->>'event')
 AND NOT EXISTS(SELECT 1 FROM notification_candidates n WHERE n.source_kind='price_event' AND n.event_id=value->>'id')
 ON CONFLICT(source_kind,event_id,recipient) DO NOTHING`, id, recipient, now, now, raw, id)
				if e != nil {
					return e
				}
				created, e = r.RowsAffected()
				if e != nil {
					return e
				}
			}
		}
		if owned {
			current.Cursor = facts[len(facts)-1].ID
			return writeEventScan(ctx, conn, id, current)
		}
		return nil
	})
	return created, e
}
func (s *DeliveryService) completeEventScan(ctx context.Context, id int64, scan eventScan) error {
	return s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		current, found, e := readEventScan(ctx, conn, id)
		if e != nil {
			return e
		}
		if !found || current.State != "scanning" || current.Upper != scan.Upper {
			return nil
		}
		upper, e := scanUpper(ctx, conn, id)
		if e != nil {
			return e
		}
		state := "done"
		if upper > scan.Upper {
			state = "pending"
		}
		return writeEventScan(ctx, conn, id, eventScan{State: state, Cursor: scan.Upper})
	})
}
func (s *DeliveryService) ResumeEventScans(ctx context.Context, cycleID *int64) (int64, error) {
	rows, e := s.db.QueryContext(ctx, `SELECT id FROM price_cycles WHERE json_extract(notification_scan_json,'$.state') IN('pending','scanning') ORDER BY id`)
	if e != nil {
		return 0, e
	}
	ids := []int64{}
	seen := map[int64]bool{}
	for rows.Next() {
		var id int64
		if e = rows.Scan(&id); e != nil {
			rows.Close()
			return 0, e
		}
		ids = append(ids, id)
		seen[id] = true
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return 0, e
	}
	if cycleID != nil && !seen[*cycleID] {
		ids = append(ids, *cycleID)
	}
	if len(ids) == 0 {
		return 0, nil
	}
	prefs, e := LoadPrefs(ctx, s.db)
	if e != nil {
		return 0, e
	}
	recipient, e := savedRecipient(ctx, s.db)
	if e != nil {
		return 0, e
	}
	var watched []int64
	if truthy(prefs["enabled"]) && recipient != "" {
		if s.options.Watched == nil {
			return 0, fmt.Errorf("监控范围读取入口未接入")
		}
		watched, e = s.options.Watched(ctx)
		if e != nil {
			return 0, e
		}
		sort.Slice(watched, func(i, j int) bool { return watched[i] < watched[j] })
	}
	var created int64
	for _, id := range ids {
		scan, owned, e := s.openEventScan(ctx, id, prefs, watched, recipient)
		if e != nil {
			return created, e
		}
		if owned && scan == nil {
			continue
		}
		if scan == nil {
			upper, e := scanUpper(ctx, s.db, id)
			if e != nil {
				return created, e
			}
			scan = &eventScan{Upper: upper, Prefs: prefs, AppIDs: watched}
		}
		for scan.Cursor < scan.Upper {
			page, e := s.scanPage(ctx, id, *scan)
			if e != nil {
				return created, e
			}
			if len(page) == 0 {
				break
			}
			n, e := s.commitEventPage(ctx, id, *scan, owned, page)
			if e != nil {
				return created, e
			}
			created += n
			scan.Cursor = page[len(page)-1].ID
		}
		if owned {
			if e = s.completeEventScan(ctx, id, *scan); e != nil {
				return created, e
			}
		}
	}
	return created, nil
}
