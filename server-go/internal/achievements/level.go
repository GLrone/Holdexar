package achievements

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/store"
)

type levelProfile struct {
	SteamID        string           `json:"steamid"`
	Level          int              `json:"level"`
	Tier           string           `json:"tier"`
	NextTier       *string          `json:"nextTier"`
	TierProgress   *float64         `json:"tierProgress"`
	TotalXP        int              `json:"totalXp"`
	XPIntoLevel    int              `json:"xpIntoLevel"`
	XPForNextLevel int              `json:"xpForNextLevel"`
	XPRemaining    int              `json:"xpRemaining"`
	LevelProgress  float64          `json:"levelProgress"`
	LastLevelCost  int              `json:"lastLevelCost"`
	NextCostGrowth float64          `json:"nextCostGrowth"`
	CurvePreview   []levelCurveStep `json:"curvePreview"`
	Milestones     []levelMilestone `json:"milestones"`
	ActiveBonuses  []levelBonus     `json:"activeBonuses"`
	RecentEvents   []levelRecentXP  `json:"recentEvents"`
}

type levelCurveStep struct {
	Level        int `json:"level"`
	CostXP       int `json:"costXp"`
	CumulativeXP int `json:"cumulativeXp"`
}

type levelMilestone struct {
	Level   int    `json:"level"`
	Tier    string `json:"tier"`
	Reached bool   `json:"reached"`
}

type levelBonus struct {
	Kind       string  `json:"kind"`
	Multiplier float64 `json:"multiplier"`
	StreakDays int     `json:"streakDays"`
}

type levelRecentXP struct {
	ID         int64   `json:"id"`
	Source     string  `json:"source"`
	XP         int     `json:"xp"`
	Multiplier float64 `json:"multiplier"`
	CreatedAt  *string `json:"createdAt"`
}

func (s LevelService) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/achievements/level", func(w http.ResponseWriter, r *http.Request) {
		profile, err := s.profile(r.Context(), strings.TrimSpace(readParam(r, "steamid")))
		if err != nil {
			http.Error(w, "Internal Server Error", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(profile)
	})
}

func (s LevelService) profile(ctx context.Context, sid string) (levelProfile, error) {
	if sid == "" && s.Account != nil {
		primary, _, err := s.Account(ctx, "")
		if err != nil {
			return levelProfile{}, err
		}
		sid = primary
	}
	if sid == "" {
		return makeLevelProfile("", 0), nil
	}
	if err := s.ensureLevelBaseline(ctx, sid); err != nil {
		return levelProfile{}, err
	}

	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return levelProfile{}, err
	}
	defer tx.Rollback()
	var total int
	if err := tx.QueryRowContext(ctx, "SELECT coalesce(sum(xp),0) FROM xp_events WHERE steamid=?", sid).Scan(&total); err != nil {
		return levelProfile{}, err
	}
	profile := makeLevelProfile(sid, total)
	streakDays, err := profileStreakDays(ctx, tx, sid)
	if err != nil {
		return levelProfile{}, err
	}
	if streakDays >= 2 {
		profile.ActiveBonuses = append(profile.ActiveBonuses, levelBonus{"streak", levelStreakMultiplier(streakDays), streakDays})
	}
	profile.RecentEvents, err = recentLevelXP(ctx, tx, sid)
	if err != nil {
		return levelProfile{}, err
	}
	return profile, tx.Commit()
}

func (s LevelService) ensureLevelBaseline(ctx context.Context, sid string) error {
	const existsQuery = "SELECT 1 FROM xp_events WHERE steamid=? LIMIT 1"
	var exists int
	err := s.DB.QueryRowContext(ctx, existsQuery, sid).Scan(&exists)
	if !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	return s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		err := conn.QueryRowContext(ctx, existsQuery, sid).Scan(&exists)
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		entries, _, err := syncLevelXP(ctx, conn, sid, nil)
		if err != nil {
			return err
		}
		return recordLevelXP(ctx, conn, sid, entries, s.now())
	})
}

func profileStreakDays(ctx context.Context, q interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}, sid string) (int, error) {
	rows, err := q.QueryContext(ctx, "SELECT DISTINCT unlock_time FROM achievement_states WHERE steamid=? AND achieved=1 AND unlock_time>0", sid)
	if err != nil {
		return 0, err
	}
	defer rows.Close()
	days := make(map[time.Time]struct{})
	for rows.Next() {
		var ts int64
		if err := rows.Scan(&ts); err != nil {
			return 0, err
		}
		days[levelUnlockDay(ts)] = struct{}{}
	}
	return levelCurrentStreak(days), rows.Err()
}

func recentLevelXP(ctx context.Context, tx *sql.Tx, sid string) ([]levelRecentXP, error) {
	rows, err := tx.QueryContext(ctx, `SELECT id,source,xp,multiplier,created_at FROM xp_events
		WHERE steamid=? ORDER BY created_at DESC,id DESC LIMIT 10`, sid)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	recent := make([]levelRecentXP, 0)
	for rows.Next() {
		var event levelRecentXP
		var when sql.NullTime
		if err := rows.Scan(&event.ID, &event.Source, &event.XP, &event.Multiplier, &when); err != nil {
			return nil, err
		}
		event.CreatedAt = levelISOTime(when)
		recent = append(recent, event)
	}
	return recent, rows.Err()
}

func levelISOTime(when sql.NullTime) *string {
	if !when.Valid {
		return nil
	}
	stamp := when.Time.Format("2006-01-02T15:04:05")
	if when.Time.Nanosecond() >= 1000 {
		stamp = when.Time.Format("2006-01-02T15:04:05.000000")
	}
	return &stamp
}

var levelTierSteps = [...]struct {
	name  string
	start int
}{
	{"bronze", 1}, {"silver", 10}, {"gold", 20}, {"platinum", 40}, {"legend", 70},
}

func levelCost(n int) int {
	return int(math.Ceil(30 * math.Pow(float64(n), 1.5)))
}

func levelForXP(total int) (level, into, cost int) {
	level, into, cost = 1, total, levelCost(1)
	for into >= cost {
		into -= cost
		level++
		cost = levelCost(level)
	}
	return level, into, cost
}

func levelTierIndex(level int) int {
	tier := 0
	for i, step := range levelTierSteps {
		if level >= step.start {
			tier = i
		}
	}
	return tier
}

func levelDecimalRound(value float64, places int) float64 {
	rounded, _ := strconv.ParseFloat(strconv.FormatFloat(value, 'f', places, 64), 64)
	return rounded
}

func makeLevelProfile(sid string, total int) levelProfile {
	level, into, cost := levelForXP(total)
	tier := levelTierIndex(level)
	out := levelProfile{
		SteamID: sid, Level: level, Tier: levelTierSteps[tier].name,
		TotalXP: total, XPIntoLevel: into, XPForNextLevel: cost, XPRemaining: cost - into,
		LevelProgress: levelDecimalRound(float64(into)/float64(cost), 3),
		ActiveBonuses: []levelBonus{}, RecentEvents: []levelRecentXP{},
	}
	if tier+1 < len(levelTierSteps) {
		start, next := levelTierSteps[tier].start, levelTierSteps[tier+1]
		progress := float64(level-start) / float64(next.start-start)
		out.NextTier, out.TierProgress = &next.name, &progress
	}
	if level > 1 {
		out.LastLevelCost = levelCost(level - 1)
		out.NextCostGrowth = levelDecimalRound(float64(cost-out.LastLevelCost)/float64(out.LastLevelCost), 3)
	}

	cumulative := total
	for target := level + 1; target <= level+6; target++ {
		step := levelCost(target - 1)
		cumulative += step
		out.CurvePreview = append(out.CurvePreview, levelCurveStep{target, step, cumulative})
	}
	threshold, next := 0, 1
	for _, milestone := range [...]int{10, 25, 50, 75, 100, 150, 200} {
		for ; next < milestone; next++ {
			threshold += levelCost(next)
		}
		out.Milestones = append(out.Milestones, levelMilestone{milestone, levelTierSteps[levelTierIndex(milestone)].name, total >= threshold})
	}
	return out
}
