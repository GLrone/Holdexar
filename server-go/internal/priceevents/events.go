// Package priceevents records changes supported by reliable regional Steam observations.
package priceevents

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"holdexar/server-go/internal/crawler/dbwriter"
	"holdexar/server-go/internal/store"
)

const (
	PriceDrop            = "PRICE_DROP"
	PriceIncrease        = "PRICE_INCREASE"
	NewHistoricalLow     = "NEW_HISTORICAL_LOW"
	HistoricalLowMatch   = "HISTORICAL_LOW_MATCH"
	PermanentPriceChange = "PERMANENT_PRICE_CHANGE"
	RegionLocked         = "REGION_LOCKED"
	RegionUnlocked       = "REGION_UNLOCKED"
	PriceUnavailable     = "PRICE_UNAVAILABLE"
	PriceRestored        = "PRICE_RESTORED"
	FreePromo            = "FREE_PROMO"
	Removed              = "REMOVED"
)

type Event struct {
	ID         int64          `json:"id"`
	CycleID    int64          `json:"cycleId"`
	AppID      int64          `json:"appid"`
	Region     *string        `json:"region"`
	Type       string         `json:"eventType"`
	Previous   map[string]any `json:"previous"`
	Current    map[string]any `json:"current"`
	OccurredAt string         `json:"occurredAt"`
	CreatedAt  string         `json:"-"`
}

type Detector struct {
	db   *sql.DB
	gate *store.WriteGate
}

func New(db *sql.DB, gate *store.WriteGate) *Detector { return &Detector{db: db, gate: gate} }

func (d *Detector) Detect(ctx context.Context, cycleID int64) ([]Event, error) {
	var started, ended, expected sql.NullString
	err := d.db.QueryRowContext(ctx, `SELECT CAST(started_at AS TEXT),CAST(finished_at AS TEXT),expected_json FROM price_cycles WHERE id=?`, cycleID).Scan(&started, &ended, &expected)
	if errors.Is(err, sql.ErrNoRows) || err == nil && !started.Valid {
		return []Event{}, nil
	}
	if err != nil {
		return nil, err
	}
	var scope struct {
		AppIDs  []int64  `json:"appids"`
		Regions []string `json:"regions"`
	}
	if expected.Valid && expected.String != "null" {
		if err = json.Unmarshal([]byte(expected.String), &scope); err != nil {
			return nil, fmt.Errorf("cycle expected scope: %w", err)
		}
	}
	start, err := parseTime(started.String)
	if err != nil {
		return nil, err
	}
	end := time.Now()
	if ended.Valid {
		end, err = parseTime(ended.String)
		if err != nil {
			return nil, err
		}
	}
	return d.DetectWindow(ctx, scope.AppIDs, scope.Regions, start, end, cycleID)
}

func (d *Detector) DetectWindow(ctx context.Context, appIDs []int64, regions []string, start, end time.Time, cycleID int64) ([]Event, error) {
	if len(appIDs) == 0 || len(regions) == 0 {
		return []Event{}, nil
	}
	regionSet := map[string]bool{}
	for _, r := range regions {
		if r = strings.ToUpper(strings.TrimSpace(r)); r != "" {
			regionSet[r] = true
		}
	}
	regions = make([]string, 0, len(regionSet))
	for r := range regionSet {
		regions = append(regions, r)
	}
	sort.Strings(regions)
	if len(regions) == 0 {
		return []Event{}, nil
	}
	apps, _ := json.Marshal(appIDs)
	regs, _ := json.Marshal(regions)
	written := []Event{}
	err := d.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		observed, err := observations(ctx, conn, string(apps), string(regs), start, end)
		if err != nil {
			return err
		}
		snaps, prev, had, err := evidence(ctx, conn, observed, start, end)
		if err != nil {
			return err
		}
		states, err := previousStates(ctx, conn, string(apps))
		if err != nil {
			return err
		}
		pending := []Event{}
		appendEvent := func(a int64, r string, kind string, before, after map[string]any, at string) {
			var region *string
			if r != "" {
				rr := r
				region = &rr
			}
			pending = append(pending, Event{CycleID: cycleID, AppID: a, Region: region, Type: kind, Previous: before, Current: after, OccurredAt: iso(at)})
		}
		for _, cur := range observed {
			key := unit{cur.app, cur.region}
			snap, hasSnap := snaps[key]
			prior, hasPrev := prev[key]
			if hasSnap && cur.status == "ok" {
				if hasPrev && positive(prior.price) && positive(cur.price) {
					if cur.price.Int64 < prior.price.Int64 {
						appendEvent(cur.app, cur.region, PriceDrop, prior.payload(cur.region), snap.payload(cur.region), snap.at)
					} else if cur.price.Int64 > prior.price.Int64 {
						appendEvent(cur.app, cur.region, PriceIncrease, prior.payload(cur.region), snap.payload(cur.region), snap.at)
					}
				}
				if hasPrev && positive(cur.price) && positive(prior.low) {
					kind := ""
					if cur.price.Int64 < prior.low.Int64 {
						kind = NewHistoricalLow
					} else if cur.price.Int64 == prior.low.Int64 && prior.price.Int64 != prior.low.Int64 {
						kind = HistoricalLowMatch
					}
					if kind != "" {
						p := prior.payload(cur.region)
						p["low"] = prior.low.Int64
						appendEvent(cur.app, cur.region, kind, p, snap.payload(cur.region), snap.at)
					}
				}
				if hasPrev && snap.original.Valid && prior.original.Valid && snap.original.Int64 != prior.original.Int64 {
					appendEvent(cur.app, cur.region, PermanentPriceChange, prior.payload(cur.region), snap.payload(cur.region), snap.at)
				}
			}
			state := states[key]
			if state == "" && had[key] {
				state = "ok"
			}
			kind := ""
			switch {
			case state == "ok" && cur.status == "locked":
				kind = RegionLocked
			case state == "locked" && cur.status == "ok":
				kind = RegionUnlocked
			case (state == "ok" || state == "locked") && failed(cur.status):
				kind = PriceUnavailable
			case failed(state) && cur.status == "ok":
				kind = PriceRestored
			}
			if kind != "" {
				payload := cur.payload(cur.region)
				payload["status"] = cur.status
				appendEvent(cur.app, cur.region, kind, map[string]any{"status": state}, payload, cur.at)
			}
		}
		for _, a := range appIDs {
			candidates := append([]string{"CN"}, regions...)
			for _, r := range candidates {
				key := unit{a, r}
				snap, ok := snaps[key]
				if !ok || !snap.price.Valid || snap.price.Int64 != 0 || !positive(snap.original) {
					continue
				}
				if _, ok := prev[key]; ok {
					p := snap.payload(r)
					p["freeKind"] = "promo"
					appendEvent(a, "", FreePromo, map[string]any{"freeKind": nil}, p, snap.at)
				}
				break
			}
		}
		rows, err := conn.QueryContext(ctx, `SELECT appid,CAST(removed_at AS TEXT) FROM games WHERE appid IN(SELECT value FROM json_each(?)) AND removed_at>=? AND removed_at<=?`, string(apps), stamp(start), stamp(end))
		if err != nil {
			return err
		}
		for rows.Next() {
			var a int64
			var at string
			if err = rows.Scan(&a, &at); err != nil {
				rows.Close()
				return err
			}
			appendEvent(a, "", Removed, map[string]any{"removedAt": nil}, map[string]any{"removedAt": iso(at)}, at)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		written, err = persist(ctx, conn, pending, cycleID)
		return err
	})
	if err != nil {
		return nil, err
	}
	return written, nil
}

type unit struct {
	app    int64
	region string
}
type observation struct {
	app                                  int64
	region, status, currency, at, source string
	price, original, cny, sub, low       sql.NullInt64
}

func (o observation) payload(region string) map[string]any {
	return map[string]any{"price": nullable(o.price), "originalPrice": nullable(o.original), "cnyFen": nullable(o.cny), "currency": o.currency, "region": region, "subId": nullable(o.sub), "source": o.source, "observedAt": iso(o.at)}
}
func nullable(v sql.NullInt64) any {
	if v.Valid {
		return v.Int64
	}
	return nil
}
func positive(v sql.NullInt64) bool { return v.Valid && v.Int64 > 0 }
func failed(s string) bool          { return s == "missing" || s == "blocked" }
func stamp(t time.Time) string      { return dbwriter.FormatNaive(t) }
func parseTime(s string) (time.Time, error) {
	for _, layout := range []string{"2006-01-02 15:04:05.999999999", time.RFC3339Nano, "2006-01-02T15:04:05.999999999"} {
		if t, e := time.Parse(layout, s); e == nil {
			return t, nil
		}
	}
	return time.Time{}, fmt.Errorf("invalid observation time %q", s)
}
func iso(s string) string {
	if t, e := parseTime(s); e == nil {
		if t.Nanosecond() == 0 {
			return t.Format("2006-01-02T15:04:05")
		}
		return t.Format("2006-01-02T15:04:05.000000")
	}
	return s
}

func observations(ctx context.Context, c *sql.Conn, apps, regions string, start, end time.Time) ([]observation, error) {
	rows, err := c.QueryContext(ctx, `SELECT appid,upper(trim(region_code)),price,cny_fen,lower(trim(price_status)),sub_id,CAST(updated_at AS TEXT),upper(trim(COALESCE(currency,''))) FROM game_current_prices WHERE appid IN(SELECT value FROM json_each(?)) AND upper(trim(region_code)) IN(SELECT value FROM json_each(?)) AND updated_at>=? AND updated_at<=? AND attempt_outcome='success' AND last_success_at=updated_at AND (price_status!='ok' OR steam_answer IN('ok','free')) ORDER BY appid,region_code`, apps, regions, stamp(start), stamp(end))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []observation{}
	for rows.Next() {
		o := observation{source: "steam"}
		if err = rows.Scan(&o.app, &o.region, &o.price, &o.cny, &o.status, &o.sub, &o.at, &o.currency); err != nil {
			return nil, err
		}
		out = append(out, o)
	}
	return out, rows.Err()
}

func standard(alias string) string {
	return "(" + alias + ".is_gold IS NULL OR " + alias + ".is_gold=0) AND (" + alias + ".version_suffix IS NULL OR " + alias + ".version_suffix='') AND (" + alias + ".is_bundle IS NULL OR " + alias + ".is_bundle=0) AND (" + alias + ".product_unknown IS NULL OR " + alias + ".product_unknown=0)"
}
func reliable(alias string) string {
	return alias + ".price_status='ok' AND " + alias + ".price>0 AND " + standard(alias) + " AND (" + alias + ".source='steam' OR (" + alias + ".source='community' AND EXISTS(SELECT 1 FROM game_price_history k WHERE k.appid=" + alias + ".appid AND k.region_code=" + alias + ".region_code AND k.sub_id=" + alias + ".sub_id AND k.currency=" + alias + ".currency AND k.source='steam' AND k.price_status='ok' AND k.price>0 AND k.snapshot_at<=w.b AND " + standard("k") + ")))"
}

func evidence(ctx context.Context, c *sql.Conn, observed []observation, start, end time.Time) (map[unit]observation, map[unit]observation, map[unit]bool, error) {
	snaps := map[unit]observation{}
	previous := map[unit]observation{}
	had := map[unit]bool{}
	if len(observed) == 0 {
		return snaps, previous, had, nil
	}
	want := []map[string]any{}
	units := []map[string]any{}
	current := map[unit]observation{}
	for _, o := range observed {
		key := unit{o.app, o.region}
		current[key] = o
		units = append(units, map[string]any{"a": o.app, "r": o.region})
		if positive(o.sub) && o.currency != "" {
			want = append(want, map[string]any{"a": o.app, "r": o.region, "s": o.sub.Int64, "c": o.currency, "t": o.at})
		}
	}
	if len(want) > 0 {
		wanted, _ := json.Marshal(want)
		rows, err := c.QueryContext(ctx, `WITH wanted AS(SELECT value->>'a' a,value->>'r' r,value->>'s' s,value->>'c' c,value->>'t' t FROM json_each(?)) SELECT w.a,w.r,h.price,h.original_price,h.cny_fen,CAST(h.snapshot_at AS TEXT),h.currency,h.sub_id FROM wanted w JOIN game_price_history h ON h.id=(SELECT h2.id FROM game_price_history h2 WHERE h2.appid=w.a AND h2.region_code=w.r AND h2.sub_id=w.s AND h2.currency=w.c AND h2.source='steam' AND h2.price_status='ok' AND h2.snapshot_at>=? AND h2.snapshot_at<=? AND h2.snapshot_at<=w.t AND `+standard("h2")+` ORDER BY h2.snapshot_at DESC,h2.id DESC LIMIT 1)`, string(wanted), stamp(start), stamp(end))
		if err != nil {
			return nil, nil, nil, err
		}
		for rows.Next() {
			o := observation{source: "steam"}
			if err = rows.Scan(&o.app, &o.region, &o.price, &o.original, &o.cny, &o.at, &o.currency, &o.sub); err != nil {
				rows.Close()
				return nil, nil, nil, err
			}
			key := unit{o.app, o.region}
			if o.price == current[key].price {
				snaps[key] = o
			}
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return nil, nil, nil, err
		}
		for _, w := range want {
			key := unit{w["a"].(int64), w["r"].(string)}
			w["b"] = w["t"]
			if s, ok := snaps[key]; ok {
				w["b"] = s.at
			}
		}
		wanted, _ = json.Marshal(want)
		rows, err = c.QueryContext(ctx, `WITH wanted AS(SELECT value->>'a' a,value->>'r' r,value->>'s' s,value->>'c' c,value->>'b' b FROM json_each(?)) SELECT w.a,w.r,p.price,p.original_price,p.cny_fen,CAST(p.snapshot_at AS TEXT),p.currency,p.sub_id,p.source,(SELECT MIN(h.price) FROM game_price_history h WHERE h.appid=w.a AND h.region_code=w.r AND h.sub_id=w.s AND h.currency=w.c AND h.snapshot_at<w.b AND `+reliable("h")+`) FROM wanted w JOIN game_price_history p ON p.id=(SELECT h2.id FROM game_price_history h2 WHERE h2.appid=w.a AND h2.region_code=w.r AND h2.sub_id=w.s AND h2.currency=w.c AND h2.snapshot_at<w.b AND `+reliable("h2")+` ORDER BY h2.snapshot_at DESC,h2.id DESC LIMIT 1)`, string(wanted))
		if err != nil {
			return nil, nil, nil, err
		}
		for rows.Next() {
			var o observation
			if err = rows.Scan(&o.app, &o.region, &o.price, &o.original, &o.cny, &o.at, &o.currency, &o.sub, &o.source, &o.low); err != nil {
				rows.Close()
				return nil, nil, nil, err
			}
			previous[unit{o.app, o.region}] = o
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return nil, nil, nil, err
		}
	}
	unitJSON, _ := json.Marshal(units)
	rows, err := c.QueryContext(ctx, `SELECT value->>'a',value->>'r' FROM json_each(?) w WHERE EXISTS(SELECT 1 FROM game_price_history h WHERE h.appid=w.value->>'a' AND h.region_code=w.value->>'r' AND h.snapshot_at<? AND h.sub_id>0 AND COALESCE(h.currency,'')!='' AND h.price>0 AND h.source='steam' AND h.price_status='ok' AND `+standard("h")+`)`, string(unitJSON), stamp(start))
	if err != nil {
		return nil, nil, nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var k unit
		if err = rows.Scan(&k.app, &k.region); err != nil {
			return nil, nil, nil, err
		}
		had[k] = true
	}
	return snaps, previous, had, rows.Err()
}

func previousStates(ctx context.Context, c *sql.Conn, apps string) (map[unit]string, error) {
	rows, err := c.QueryContext(ctx, `SELECT appid,upper(trim(COALESCE(region_code,''))),json_extract(current_json,'$.status') FROM price_events WHERE id IN(SELECT MAX(id) FROM price_events WHERE appid IN(SELECT value FROM json_each(?)) AND event_type IN('REGION_LOCKED','REGION_UNLOCKED','PRICE_UNAVAILABLE','PRICE_RESTORED') AND COALESCE(json_extract(current_json,'$.status'),'')!='' GROUP BY appid,region_code)`, apps)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[unit]string{}
	for rows.Next() {
		var k unit
		var state string
		if err = rows.Scan(&k.app, &k.region, &state); err != nil {
			return nil, err
		}
		out[k] = state
	}
	return out, rows.Err()
}

func persist(ctx context.Context, c *sql.Conn, pending []Event, cycleID int64) ([]Event, error) {
	written := []Event{}
	now := stamp(time.Now())
	for offset := 0; offset < len(pending); offset += 500 {
		upper := offset + 500
		if upper > len(pending) {
			upper = len(pending)
		}
		batch := pending[offset:upper]
		values := make([]string, len(batch))
		args := make([]any, 0, len(batch)*8)
		for i, e := range batch {
			before, err := json.Marshal(e.Previous)
			if err != nil {
				return nil, err
			}
			after, err := json.Marshal(e.Current)
			if err != nil {
				return nil, err
			}
			at, err := parseTime(e.OccurredAt)
			if err != nil {
				return nil, err
			}
			values[i] = "(?,?,?,?,?,?,?,?)"
			args = append(args, e.CycleID, e.AppID, e.Region, e.Type, string(before), string(after), stamp(at), now)
		}
		rows, err := c.QueryContext(ctx, `INSERT INTO price_events(cycle_id,appid,region_code,event_type,previous_json,current_json,occurred_at,created_at) VALUES `+strings.Join(values, ",")+` ON CONFLICT DO NOTHING RETURNING id,cycle_id,appid,region_code,event_type,previous_json,current_json,CAST(occurred_at AS TEXT),CAST(created_at AS TEXT)`, args...)
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var e Event
			var region sql.NullString
			var before, after string
			if err = rows.Scan(&e.ID, &e.CycleID, &e.AppID, &region, &e.Type, &before, &after, &e.OccurredAt, &e.CreatedAt); err != nil {
				rows.Close()
				return nil, err
			}
			if region.Valid {
				e.Region = &region.String
			}
			if err = json.Unmarshal([]byte(before), &e.Previous); err != nil {
				rows.Close()
				return nil, err
			}
			if err = json.Unmarshal([]byte(after), &e.Current); err != nil {
				rows.Close()
				return nil, err
			}
			e.OccurredAt = iso(e.OccurredAt)
			written = append(written, e)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return nil, err
		}
	}
	if len(written) > 0 && cycleID > 0 {
		_, err := c.ExecContext(ctx, `UPDATE price_cycles SET notification_scan_json=json_object('state','pending','cursor',COALESCE(json_extract(notification_scan_json,'$.cursor'),0)) WHERE id=? AND COALESCE(json_extract(notification_scan_json,'$.state'),'') NOT IN('pending','scanning')`, cycleID)
		if err != nil {
			return nil, err
		}
	}
	return written, nil
}
