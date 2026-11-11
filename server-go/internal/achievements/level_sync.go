package achievements

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/store"
)

const (
	xpUnlock   = "achievement_unlock"
	xpPlatinum = "platinum"
	xpBucket   = "playtime_bucket"
	xpStreak   = "streak_day"
	xpBaseline = "baseline"

	levelSQLTime = "2006-01-02 15:04:05.000000"
)

type LevelService struct {
	DB      *sql.DB
	Gate    *store.WriteGate
	Account func(context.Context, string) (string, bool, error)
	Now     func() time.Time
}

func (s LevelService) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

type levelXPKey struct{ source, ref string }

type levelXPEvent struct {
	levelXPKey
	xp         int
	multiplier float64
	note       *string
}

type levelUnlock struct {
	appID       int64
	image, name string
	percent     sql.NullFloat64
	unlockTime  int64
}

type levelFact struct {
	kind, key string
	data      any
}

func (s LevelService) AwardSync(ctx context.Context, sid string) error {
	now := s.now()
	var facts []levelFact
	if err := s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		total, previous, err := existingLevelXP(ctx, conn, sid)
		if err != nil {
			return err
		}
		events, unlocked, err := syncLevelXP(ctx, conn, sid, previous)
		if err != nil {
			return err
		}
		if err := recordLevelXP(ctx, conn, sid, events, now); err != nil {
			return err
		}
		facts = levelSyncFacts(sid, total, events, unlocked)
		return nil
	}); err != nil {
		return err
	}
	if len(facts) == 0 {
		return nil
	}
	return s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		return recordLevelFacts(ctx, conn, facts, now)
	})
}

func existingLevelXP(ctx context.Context, conn *sql.Conn, sid string) (int, map[levelXPKey]struct{}, error) {
	rows, err := conn.QueryContext(ctx, "SELECT source, source_ref, xp FROM xp_events WHERE steamid=?", sid)
	if err != nil {
		return 0, nil, err
	}
	defer rows.Close()
	var total int
	keys := make(map[levelXPKey]struct{})
	for rows.Next() {
		var key levelXPKey
		var xp int
		if err := rows.Scan(&key.source, &key.ref, &xp); err != nil {
			return 0, nil, err
		}
		keys[key] = struct{}{}
		total += xp
	}
	return total, keys, rows.Err()
}

func syncLevelXP(ctx context.Context, conn *sql.Conn, sid string, existing map[levelXPKey]struct{}) ([]levelXPEvent, []levelUnlock, error) {
	unlocks, err := levelUnlocks(ctx, conn, sid)
	if err != nil {
		return nil, nil, err
	}
	days := make(map[time.Time]struct{})
	for _, unlocked := range unlocks {
		if unlocked.unlockTime > 0 {
			days[levelUnlockDay(unlocked.unlockTime)] = struct{}{}
		}
	}

	baseline := len(existing) == 0
	streak := levelStreakMultiplier(levelCurrentStreak(days))
	var entries []levelXPEvent
	baselineXP := 0
	marker := "baseline marker"
	appendXP := func(source, ref string, base int, rarity float64) bool {
		key := levelXPKey{source, ref}
		if _, found := existing[key]; found {
			return false
		}
		if baseline {
			// Markers reserve the references for facts already included in the baseline.
			baselineXP += int(math.Floor(float64(base)*rarity + 0.5))
			entries = append(entries, levelXPEvent{levelXPKey: key, multiplier: 1, note: &marker})
			return false
		}
		multiplier := rarity * streak
		entries = append(entries, levelXPEvent{
			levelXPKey: key,
			xp:         int(math.Floor(float64(base)*multiplier + 0.5)),
			multiplier: levelDecimalRound(multiplier, 4),
		})
		return true
	}

	var awarded []levelUnlock
	for _, unlocked := range unlocks {
		ref := fmt.Sprintf("%d:%s", unlocked.appID, unlocked.image)
		if appendXP(xpUnlock, ref, 15, levelRarityMultiplier(unlocked.percent)) {
			awarded = append(awarded, unlocked)
		}
	}
	if err := addGameXP(ctx, conn, sid, appendXP); err != nil {
		return nil, nil, err
	}
	if baseline {
		note := "baseline from unlocks/platinum/playtime buckets"
		entry := levelXPEvent{levelXPKey: levelXPKey{xpBaseline, "v1"}, xp: baselineXP, multiplier: 1, note: &note}
		return append([]levelXPEvent{entry}, entries...), nil, nil
	}
	addStreakXP(awarded, days, appendXP)
	return entries, awarded, nil
}

func levelUnlocks(ctx context.Context, conn *sql.Conn, sid string) ([]levelUnlock, error) {
	rows, err := conn.QueryContext(ctx, `SELECT s.appid,s.image_name,coalesce(nullif(d.name,''),s.image_name),d.global_percent,coalesce(s.unlock_time,0)
		FROM achievement_states s LEFT JOIN achievement_defs d ON d.appid=s.appid AND d.image_name=s.image_name
		WHERE s.steamid=? AND s.achieved=1 ORDER BY s.appid,s.image_name`, sid)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []levelUnlock
	for rows.Next() {
		var unlocked levelUnlock
		if err := rows.Scan(&unlocked.appID, &unlocked.image, &unlocked.name, &unlocked.percent, &unlocked.unlockTime); err != nil {
			return nil, err
		}
		out = append(out, unlocked)
	}
	return out, rows.Err()
}

func addGameXP(ctx context.Context, conn *sql.Conn, sid string, add func(string, string, int, float64) bool) error {
	rows, err := conn.QueryContext(ctx, `SELECT appid,coalesce(playtime_min,0),coalesce(total_achievements,0)>0 AND coalesce(unlocked,0)>=total_achievements
		FROM achievement_games WHERE steamid=? ORDER BY appid`, sid)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var appID int64
		var minutes int
		var platinum bool
		if err := rows.Scan(&appID, &minutes, &platinum); err != nil {
			return err
		}
		if platinum {
			add(xpPlatinum, strconv.FormatInt(appID, 10), 200, 1)
		}
		lastBucket := levelPlaytimeBucket(minutes)
		for bucket := 1; bucket <= lastBucket; bucket++ {
			add(xpBucket, fmt.Sprintf("%d:%d", appID, bucket), 40, 1)
		}
	}
	return rows.Err()
}

func addStreakXP(awarded []levelUnlock, days map[time.Time]struct{}, add func(string, string, int, float64) bool) {
	eligible := make(map[time.Time]struct{})
	for _, unlocked := range awarded {
		if unlocked.unlockTime <= 0 {
			continue
		}
		day := levelUnlockDay(unlocked.unlockTime)
		if _, previous := days[day.AddDate(0, 0, -1)]; previous {
			eligible[day] = struct{}{}
		}
	}
	ordered := make([]time.Time, 0, len(eligible))
	for day := range eligible {
		ordered = append(ordered, day)
	}
	sort.Slice(ordered, func(i, j int) bool { return ordered[i].Before(ordered[j]) })
	for _, day := range ordered {
		add(xpStreak, day.Format(time.DateOnly), 20, 1)
	}
}

func levelSyncFacts(sid string, previousXP int, entries []levelXPEvent, unlocked []levelUnlock) []levelFact {
	var facts []levelFact
	if len(unlocked) > 0 {
		app := unlocked[0].appID
		latest := unlocked[0].unlockTime
		singleApp := true
		titles := make([]string, 0, len(unlocked))
		for _, unlocked := range unlocked {
			if unlocked.appID != app {
				singleApp = false
			}
			latest = max(latest, unlocked.unlockTime)
			titles = append(titles, unlocked.name)
		}
		data := map[string]any{"count": len(unlocked), "titles": titles}
		if singleApp {
			data["appid"] = app
		}
		facts = append(facts, levelFact{"achievement_unlocked_batch", fmt.Sprintf("%s:unlock_batch:%d", sid, latest), data})
	}
	before, _, _ := levelForXP(previousXP)
	total := previousXP
	for _, event := range entries {
		total += event.xp
	}
	if after, _, _ := levelForXP(total); after > before {
		facts = append(facts, levelFact{"level_up", fmt.Sprintf("%s:level:%d", sid, after),
			map[string]int{"fromLevel": before, "toLevel": after}})
	}
	return facts
}

func recordLevelXP(ctx context.Context, conn *sql.Conn, sid string, entries []levelXPEvent, now time.Time) error {
	const batchSize = 400
	stamp := now.UTC().Format(levelSQLTime)
	for start := 0; start < len(entries); start += batchSize {
		batch := entries[start:min(start+batchSize, len(entries))]
		holders := make([]string, 0, len(batch))
		args := make([]any, 0, len(batch)*7)
		for _, entry := range batch {
			holders = append(holders, "(?,?,?,?,?,?,?)")
			args = append(args, sid, entry.source, entry.ref, entry.xp, entry.multiplier, entry.note, stamp)
		}
		_, err := conn.ExecContext(ctx, `INSERT INTO xp_events (steamid,source,source_ref,xp,multiplier,note,created_at) VALUES `+
			strings.Join(holders, ",")+" ON CONFLICT (steamid,source,source_ref) DO NOTHING", args...)
		if err != nil {
			return err
		}
	}
	return nil
}

func recordLevelFacts(ctx context.Context, conn *sql.Conn, facts []levelFact, now time.Time) error {
	holders := make([]string, 0, len(facts))
	args := make([]any, 0, len(facts)*4)
	stamp := now.Local().Format(levelSQLTime)
	for _, fact := range facts {
		data, err := json.Marshal(fact.data)
		if err != nil {
			return err
		}
		holders = append(holders, "('achievements',?,?,?,?)")
		args = append(args, fact.kind, fact.key, string(data), stamp)
	}
	_, err := conn.ExecContext(ctx, `INSERT INTO fact_notices (source,kind,fact_key,data,occurred_at) VALUES `+
		strings.Join(holders, ",")+" ON CONFLICT (fact_key) DO NOTHING", args...)
	return err
}

func levelPlaytimeBucket(minutes int) int {
	for i, boundary := range [...]int{1, 60, 300, 1200, 3000} {
		if minutes >= 0 && minutes < boundary {
			return i
		}
	}
	return 5
}

func levelRarityMultiplier(percent sql.NullFloat64) float64 {
	if !percent.Valid {
		return 1
	}
	switch rarityTier(&percent.Float64) {
	case "ultra":
		return 3
	case "very_rare":
		return 2
	case "rare":
		return 1.5
	case "uncommon":
		return 1.25
	default:
		return 1
	}
}

func levelUnlockDay(ts int64) time.Time {
	y, m, d := time.Unix(ts, 0).Local().Date()
	return time.Date(y, m, d, 0, 0, 0, 0, time.UTC)
}

func levelCurrentStreak(days map[time.Time]struct{}) int {
	var latest time.Time
	for day := range days {
		if day.After(latest) {
			latest = day
		}
	}
	length := 0
	for day := latest; ; day = day.AddDate(0, 0, -1) {
		if _, ok := days[day]; !ok {
			return length
		}
		length++
	}
}

func levelStreakMultiplier(days int) float64 {
	switch {
	case days >= 30:
		return 2
	case days >= 14:
		return 1.5
	case days >= 7:
		return 1.25
	case days >= 2:
		return 1.1
	default:
		return 1
	}
}
