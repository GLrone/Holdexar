package settings

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"math/big"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/store"
)

const EpicFreeScheduleKey = "fetch.epic_free_schedule"
const HumbleChoiceScheduleKey = "fetch.hb_choice_schedule"

var fetchKeys = map[string]string{"epic_free": "fetch.epic_free", "hb_choice": "fetch.hb_choice", "boards": "fetch.boards", "bundle_counts": "fetch.bundle_counts", "fx_auto": "crawl.auto_refresh_rates", "fx_history": "fetch.fx_history"}
var kickJobs = map[string]string{"fetch.epic_free": "epic_free", "fetch.hb_choice": "hb_choice", "fetch.boards": "board_topsellers", "fetch.bundle_counts": "bartervg_bundles", "crawl.auto_refresh_rates": "fx_refresh", "fetch.fx_history": "fx_history_repair"}
var scheduleTimes = map[string]string{"hb_schedule": "02:30", "epic_schedule": "23:05"}
var scheduleKeys = map[string]string{"hb_schedule": HumbleChoiceScheduleKey, "epic_schedule": EpicFreeScheduleKey}

func (h *handler) notify(ctx context.Context, writes []kvWrite, kicks []string) {
	notify(ctx, h.options, writes, kicks)
}

func notify(ctx context.Context, options Options, writes []kvWrite, kicks []string) {
	if len(writes) == 0 {
		return
	}
	keys := make([]string, 0, len(writes))
	for _, write := range writes {
		keys = append(keys, write.key)
	}
	sort.Strings(keys)
	if options.Bus != nil {
		options.Bus.Publish("settings.changed", events.KV{Key: "keys", Val: keys})
	}
	if options.Changed != nil {
		options.Changed(ctx, keys, kicks)
	}
}

func SetValues(ctx context.Context, gate *store.WriteGate, options Options, values map[string]any) error {
	if len(values) == 0 {
		return nil
	}
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	writes := make([]kvWrite, 0, len(keys))
	for _, key := range keys {
		raw, e := json.Marshal(values[key])
		if e != nil {
			return e
		}
		writes = append(writes, kvWrite{key: key, value: string(raw)})
	}
	if e := gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error { return applyWrites(ctx, conn, writes) }); e != nil {
		return e
	}
	notify(ctx, options, writes, nil)
	return nil
}

var integerText = regexp.MustCompile(`^[+-]?[0-9](?:_?[0-9])*$`)
var requestIntegerText = regexp.MustCompile(`^[+-]?[0-9](?:_?[0-9])*(?:\.0+)?$`)

func fetchInteger(v any, strict bool) (*big.Int, bool) {
	var text string
	switch x := v.(type) {
	case json.Number:
		text = string(x)
		if strings.ContainsAny(text, ".eE") {
			value, e := strconv.ParseFloat(text, 64)
			if e != nil || math.IsInf(value, 0) || math.IsNaN(value) {
				return nil, false
			}
			if strict && (math.Trunc(value) != value || value < -9223372036854775808.0 || value >= 9223372036854775808.0) {
				return nil, false
			}
			integer, _ := new(big.Float).SetFloat64(value).Int(nil)
			return integer, true
		}
	case string:
		text = strings.TrimSpace(x)
		pattern := integerText
		if strict {
			pattern = requestIntegerText
		}
		if !pattern.MatchString(text) {
			return nil, false
		}
		text, _, _ = strings.Cut(text, ".")
		text = strings.ReplaceAll(text, "_", "")
	case bool:
		if x {
			text = "1"
		} else {
			text = "0"
		}
	default:
		return nil, false
	}
	if len(text) > 4300 {
		return nil, false
	}
	return new(big.Int).SetString(text, 10)
}
func clippedInteger(v any, minValue, maxValue int, strict bool) (int, error) {
	integer, ok := fetchInteger(v, strict)
	if !ok {
		return 0, fmt.Errorf("需要整数")
	}
	if integer.Cmp(big.NewInt(int64(minValue))) < 0 {
		return minValue, nil
	}
	if integer.Cmp(big.NewInt(int64(maxValue))) > 0 {
		return maxValue, nil
	}
	return int(integer.Int64()), nil
}
func normalizedTime(value any) (string, error) {
	text, ok := value.(string)
	if !ok {
		return "", fmt.Errorf("需要时刻")
	}
	parts := strings.Split(strings.TrimSpace(text), ":")
	if len(parts) != 2 || len(parts[0]) < 1 || len(parts[0]) > 2 || len(parts[1]) != 2 {
		return "", fmt.Errorf("时刻无效")
	}
	for _, part := range parts {
		for _, digit := range part {
			if digit < '0' || digit > '9' {
				return "", fmt.Errorf("时刻无效")
			}
		}
	}
	hour, e := strconv.Atoi(parts[0])
	if e != nil || hour > 23 {
		return "", fmt.Errorf("时刻无效")
	}
	minute, e := strconv.Atoi(parts[1])
	if e != nil || minute > 59 {
		return "", fmt.Errorf("时刻无效")
	}
	return fmt.Sprintf("%02d:%02d", hour, minute), nil
}
func normalizedSchedule(raw any, field string, strict bool) (map[string]any, error) {
	fallback := map[string]any{"time": scheduleTimes[field], "days": 1}
	object, ok := raw.(map[string]any)
	if !ok {
		if strict {
			return nil, fmt.Errorf("刷新节奏无效")
		}
		return fallback, nil
	}
	at, e := normalizedTime(object["time"])
	if e != nil {
		if strict {
			return nil, e
		}
		return fallback, nil
	}
	days := 1
	if value, exists := object["days"]; exists {
		if !strict && !pyBool(value) {
			value = json.Number("1")
		}
		days, e = clippedInteger(value, 1, 30, strict)
		if e != nil {
			if strict {
				return nil, e
			}
			days = 1
		}
	}
	return map[string]any{"time": at, "days": days}, nil
}

func ContentSchedule(ctx context.Context, db *sql.DB, field string) (hour, minute, days int, err error) {
	value, err := (&handler{db: db}).kvValue(ctx, scheduleKeys[field])
	schedule, _ := normalizedSchedule(value.val, field, false)
	at, _ := time.Parse("15:04", schedule["time"].(string))
	return at.Hour(), at.Minute(), schedule["days"].(int), err
}

func EpicFreeEnabled(ctx context.Context, db *sql.DB) (bool, error) {
	return ContentEnabled(ctx, db, "epic_free")
}

func BackupEnabled(ctx context.Context, db *sql.DB) (bool, error) {
	return (&handler{db: db}).fetchEnabled(ctx, keyBackupAuto)
}

func ContentEnabled(ctx context.Context, db *sql.DB, source string) (bool, error) {
	return (&handler{db: db}).fetchEnabled(ctx, fetchKeys[source])
}

func (h *handler) fetchEnabled(ctx context.Context, key string) (bool, error) {
	value, err := h.kvValue(ctx, key)
	return !value.present || pyBool(value.val), err
}

func (h *handler) fetchRead(ctx context.Context) (map[string]any, error) {
	result := map[string]any{}
	for field, key := range fetchKeys {
		enabled, e := h.fetchEnabled(ctx, key)
		if e != nil {
			return nil, e
		}
		result[field] = enabled
	}
	result["price_interval_hours"] = 6
	if value, e := h.kvValue(ctx, "crawl.price_interval_hours"); e != nil {
		return nil, e
	} else if value.present {
		if number, ok := fetchInteger(value.val, false); ok {
			result["price_interval_hours"] = json.Number(number.String())
		}
	}
	strategy, e := h.kvValue(ctx, "proxy.strategy")
	if e != nil {
		return nil, e
	}
	catalogDefault := strategy.present && strategy.val != "direct_only"
	for _, field := range []string{"catalog_refresh", "queue_missing", "queue_pool", "queue_discounts"} {
		value, e := h.kvValue(ctx, "crawl."+field)
		if e != nil {
			return nil, e
		}
		fallback := true
		if field == "catalog_refresh" {
			fallback = catalogDefault
		}
		if value.present {
			fallback = pyBool(value.val)
		}
		result[field] = fallback
	}
	for field, key := range scheduleKeys {
		value, e := h.kvValue(ctx, key)
		if e != nil {
			return nil, e
		}
		schedule, e := normalizedSchedule(value.val, field, false)
		if e != nil {
			return nil, e
		}
		result[field] = schedule
	}
	return result, nil
}
func (h *handler) handleFetchGet(w http.ResponseWriter, r *http.Request) {
	payload, e := h.fetchRead(r.Context())
	if e != nil {
		writeJSON(w, 500, detailNull())
		return
	}
	writeJSON(w, 200, payload)
}

func (h *handler) handleFetchPut(w http.ResponseWriter, r *http.Request) {
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	decoder.UseNumber()
	var body map[string]any
	if e := decoder.Decode(&body); e != nil || body == nil {
		writeJSON(w, 422, map[string]any{"detail": "抓取设置无效"})
		return
	}
	var extra any
	if e := decoder.Decode(&extra); e != io.EOF {
		writeJSON(w, 422, map[string]any{"detail": "抓取设置无效"})
		return
	}
	writes := []kvWrite{}
	push := func(key string, value any) {
		raw, _ := json.Marshal(value)
		writes = append(writes, kvWrite{key: key, value: string(raw)})
	}
	boolKeys := map[string]string{}
	for field, key := range fetchKeys {
		boolKeys[field] = key
	}
	for _, field := range []string{"catalog_refresh", "queue_missing", "queue_pool", "queue_discounts"} {
		boolKeys[field] = "crawl." + field
	}
	newEnabled := map[string]bool{}
	for field, key := range boolKeys {
		raw, exists := body[field]
		if !exists || raw == nil {
			continue
		}
		value, e := wantBool(raw, field)
		if e != nil {
			writeJSON(w, 422, map[string]any{"detail": []any{e}})
			return
		}
		push(key, value)
		if _, source := kickJobs[key]; source {
			newEnabled[key] = value
		}
	}
	if value, exists := body["price_interval_hours"]; exists && value != nil {
		hours, e := clippedInteger(value, 1, 72, true)
		if e != nil {
			writeJSON(w, 422, map[string]any{"detail": "更新间隔需要整数"})
			return
		}
		push("crawl.price_interval_hours", hours)
	}
	for field, key := range scheduleKeys {
		if value, exists := body[field]; exists && value != nil {
			schedule, e := normalizedSchedule(value, field, true)
			if e != nil {
				writeJSON(w, 422, map[string]any{"detail": e.Error()})
				return
			}
			push(key, schedule)
		}
	}
	kicks := []string{}
	if len(writes) > 0 {
		err := h.gate.WithWrite(r.Context(), store.PriorityInteractive, func(conn *sql.Conn) error {
			for key, enabled := range newEnabled {
				if !enabled {
					continue
				}
				var raw sql.NullString
				e := conn.QueryRowContext(r.Context(), "SELECT value_json FROM app_settings WHERE key=?", key).Scan(&raw)
				if e != nil && e != sql.ErrNoRows {
					return e
				}
				old := true
				if raw.Valid {
					var value any
					dec := json.NewDecoder(strings.NewReader(raw.String))
					dec.UseNumber()
					if e = dec.Decode(&value); e != nil {
						return e
					}
					old = pyBool(value)
				}
				if !old {
					kicks = append(kicks, kickJobs[key])
				}
			}
			return applyWrites(r.Context(), conn, writes)
		})
		if err != nil {
			writeJSON(w, 500, detailNull())
			return
		}
		sort.Strings(kicks)
		h.notify(r.Context(), writes, kicks)
	}
	h.handleFetchGet(w, r)
}
