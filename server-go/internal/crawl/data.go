package crawl

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"holdexar/server-go/internal/crawler/dbwriter"
	"holdexar/server-go/internal/store"
)

func dataMarks(n int) string { return strings.TrimSuffix(strings.Repeat("?,", n), ",") }

func (r *Runtime) setting(ctx context.Context, key string, fallback any) (any, error) {
	return r.planner.setting(ctx, key, fallback)
}

func dataTime(value sql.NullString) (*time.Time, error) {
	if !value.Valid || value.String == "" {
		return nil, nil
	}
	for _, layout := range []string{"2006-01-02 15:04:05.999999999", "2006-01-02T15:04:05.999999999", time.RFC3339Nano, "2006-01-02 15:04:05.999999999-07:00"} {
		if t, err := time.ParseInLocation(layout, value.String, beijing); err == nil {
			return &t, nil
		}
	}
	return nil, fmt.Errorf("invalid timestamp %q", value.String)
}

func (r *Runtime) loadMetadata(ctx context.Context, ids []int64) (map[int64]dbwriter.GameData, error) {
	out := map[int64]dbwriter.GameData{}
	for start := 0; start < len(ids); start += 500 {
		part := ids[start:min(start+500, len(ids))]
		args := make([]any, len(part))
		for i, id := range part {
			args[i] = id
		}
		rows, err := r.db.QueryContext(ctx, `SELECT appid,name,name_en,type,header_image,store_url,chinese_support,family_sharing,trading_cards,is_adult,is_visual_novel,release_date,developers,publishers,positive_rate,positive_reviews,review_count,CAST(created_at AS TEXT),CAST(updated_at AS TEXT) FROM games WHERE appid IN (`+dataMarks(len(part))+`)`, args...)
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var g dbwriter.GameData
			var developers, publishers, created, updated sql.NullString
			err = rows.Scan(&g.AppID, &g.Name, &g.NameEn, &g.Type, &g.HeaderImage, &g.StoreURL, &g.ChineseSupport, &g.FamilySharing, &g.TradingCards, &g.IsAdult, &g.IsVisualNovel, &g.ReleaseDate, &developers, &publishers, &g.PositiveRate, &g.PositiveReviews, &g.ReviewCount, &created, &updated)
			if err != nil {
				rows.Close()
				return nil, err
			}
			for raw, dest := range map[*sql.NullString]*[]any{&developers: &g.Developers, &publishers: &g.Publishers} {
				if raw.Valid && raw.String != "" {
					if err = json.Unmarshal([]byte(raw.String), dest); err != nil {
						rows.Close()
						return nil, fmt.Errorf("game %d metadata: %w", g.AppID, err)
					}
				}
			}
			if g.CreatedAt, err = dataTime(created); err != nil {
				rows.Close()
				return nil, err
			}
			if g.UpdatedAt, err = dataTime(updated); err != nil {
				rows.Close()
				return nil, err
			}
			out[g.AppID] = g
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return nil, err
		}
	}
	return out, nil
}

func (r *Runtime) loadTaskCurrencies(ctx context.Context, tasks []TargetBatch, currencies map[string]string) error {
	missing := map[string]bool{}
	for _, task := range tasks {
		code := strings.ToLower(strings.TrimSpace(task.Region))
		if currencies[code] == "" {
			missing[code] = true
		}
	}
	if len(missing) == 0 {
		return nil
	}
	rows, err := r.db.QueryContext(ctx, "SELECT code,currency FROM crawl_regions")
	if err != nil {
		return err
	}
	for rows.Next() {
		var code, currency string
		if err = rows.Scan(&code, &currency); err != nil {
			rows.Close()
			return err
		}
		code = strings.ToLower(strings.TrimSpace(code))
		if missing[code] && currency != "" {
			currencies[code] = currency
			delete(missing, code)
		}
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	if len(missing) > 0 {
		return fmt.Errorf("抓取地区没有货币配置：%v", uniqueStrings(dataKeys(missing)))
	}
	return nil
}

func dataKeys(values map[string]bool) []string {
	out := make([]string, 0, len(values))
	for k := range values {
		out = append(out, k)
	}
	return out
}

func coverageBucket(status, outcome, answer string) string {
	status = strings.ToLower(strings.TrimSpace(status))
	if outcome == "failed" {
		if status == "blocked" {
			return "blocked"
		}
		return "missing"
	}
	if outcome == "success" && (answer == "locked" || answer == "no_options") {
		return "locked"
	}
	switch status {
	case "ok", "locked", "missing", "blocked":
		return status
	}
	return "missing"
}

func (r *Runtime) freezeStats(ctx context.Context, cycleID int64, _ []int64, _ []Region, start, end time.Time) error {
	return r.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		var raw sql.NullString
		if err := conn.QueryRowContext(ctx, "SELECT expected_json FROM price_cycles WHERE id=?", cycleID).Scan(&raw); err != nil {
			return err
		}
		var expected struct {
			AppIDs  []int64  `json:"appids"`
			Regions []string `json:"regions"`
		}
		if raw.Valid && raw.String != "" {
			if err := json.Unmarshal([]byte(raw.String), &expected); err != nil {
				return err
			}
		}
		regions := map[string]bool{}
		for _, code := range expected.Regions {
			regions[strings.ToLower(strings.TrimSpace(code))] = true
		}
		active := map[string]bool{}
		rows, err := conn.QueryContext(ctx, "SELECT code FROM crawl_regions WHERE enabled=1")
		if err != nil {
			return err
		}
		for rows.Next() {
			var code string
			if err = rows.Scan(&code); err != nil {
				rows.Close()
				return err
			}
			active[strings.ToLower(code)] = true
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		counts := map[string]int64{"ok": 0, "locked": 0, "missing": 0, "blocked": 0}
		observed := map[int64]int{}
		oldestAge := map[int64]float64{}
		for offset := 0; offset < len(expected.AppIDs); offset += 500 {
			ids := expected.AppIDs[offset:min(offset+500, len(expected.AppIDs))]
			args := []any{}
			for _, id := range ids {
				args = append(args, id)
			}
			rows, err = conn.QueryContext(ctx, `SELECT appid,region_code,price_status,attempt_outcome,steam_answer,CAST(updated_at AS TEXT),CAST(last_success_at AS TEXT) FROM game_current_prices WHERE appid IN (`+dataMarks(len(ids))+`)`, args...)
			if err != nil {
				return err
			}
			for rows.Next() {
				var id int64
				var region string
				var status, outcome, answer sql.NullString
				var updated, success sql.NullString
				if err = rows.Scan(&id, &region, &status, &outcome, &answer, &updated, &success); err != nil {
					rows.Close()
					return err
				}
				region = strings.ToLower(region)
				updatedAt, parseErr := dataTime(updated)
				if parseErr != nil {
					rows.Close()
					return parseErr
				}
				lastSuccess, parseErr := dataTime(success)
				if parseErr != nil {
					rows.Close()
					return parseErr
				}
				inWindow := updatedAt != nil && !updatedAt.Before(start) && !updatedAt.After(end)
				if inWindow && regions[region] {
					counts[coverageBucket(status.String, outcome.String, answer.String)]++
					observed[id]++
				}
				if lastSuccess != nil && (len(active) == 0 || active[region]) {
					age := end.Sub(*lastSuccess).Hours()
					if old, ok := oldestAge[id]; !ok || age > old {
						oldestAge[id] = age
					}
				}
			}
			err = rows.Err()
			rows.Close()
			if err != nil {
				return err
			}
		}
		done, stale := int64(0), int64(0)
		for _, id := range expected.AppIDs {
			if len(regions) > 0 && observed[id] >= len(regions) {
				done++
			}
			if age, ok := oldestAge[id]; ok && age >= 12 {
				stale++
			}
		}
		total := int64(len(expected.AppIDs))
		units := total * int64(len(regions))
		if units == 0 {
			total = 0
			done = 0
		}
		_, err = conn.ExecContext(ctx, `UPDATE price_cycles SET targets_total=?,targets_done=?,units_expected=?,units_ok=?,units_locked=?,units_failed=?,units_missing=?,units_blocked=?,units_unobserved=?,stale_count=? WHERE id=?`, total, done, units, counts["ok"], counts["locked"], counts["missing"]+counts["blocked"], counts["missing"], counts["blocked"], units-counts["ok"]-counts["locked"]-counts["missing"]-counts["blocked"], stale, cycleID)
		return err
	})
}
