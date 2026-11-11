package achievements

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"time"

	"holdexar/server-go/internal/store"
)

type savedChallenge struct {
	status                 string
	completedAt, claimedAt sql.NullTime
}

func (s ChallengeService) recompute(ctx context.Context, sid string) (challengeOverview, error) {
	roster, err := (Reader{DB: s.DB}).accounts(ctx)
	if err != nil {
		return challengeOverview{}, err
	}
	var overview challengeOverview
	var notices []levelFact
	var now time.Time
	err = s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		measured, err := measureChallenges(ctx, conn, sid, roster)
		if err != nil {
			return err
		}
		saved, err := savedChallenges(ctx, conn, sid)
		if err != nil {
			return err
		}
		now = s.now()
		overview = assembleChallenges(sid, measured, saved, sql.NullTime{Time: now.UTC(), Valid: true})
		notices, err = saveChallenges(ctx, conn, overview, saved, now)
		return err
	})
	if err != nil {
		return challengeOverview{}, err
	}
	s.publishChallengeFacts(ctx, notices, now)
	return overview, nil
}

func measureChallenges(ctx context.Context, conn *sql.Conn, sid string, roster []readDisplayAccount) ([measureCount]int, error) {
	var counts [measureCount]int
	if err := measureChallengeStates(ctx, conn, sid, roster, &counts); err != nil {
		return counts, err
	}
	if err := measureChallengeGames(ctx, conn, sid, &counts); err != nil {
		return counts, err
	}
	days, err := profileStreakDays(ctx, conn, sid)
	if err != nil {
		return counts, err
	}
	counts[measureStreak] = days
	err = conn.QueryRowContext(ctx, "SELECT count(*) FROM achievement_notes WHERE steamid=?", sid).Scan(&counts[measureNotes])
	return counts, err
}

func measureChallengeStates(ctx context.Context, conn *sql.Conn, sid string, roster []readDisplayAccount, counts *[measureCount]int) error {
	ids := make([]string, 0, len(roster))
	members := make(map[string]bool, len(roster))
	for _, account := range roster {
		ids = append(ids, account.SteamID)
		members[account.SteamID] = true
	}
	encoded, err := json.Marshal(ids)
	if err != nil {
		return err
	}
	rows, err := conn.QueryContext(ctx, `SELECT steamid,count(*) FROM achievement_states
		WHERE achieved=1 AND (steamid=? OR steamid IN (SELECT value FROM json_each(?))) GROUP BY steamid`, sid, string(encoded))
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var owner string
		var unlocked int
		if err := rows.Scan(&owner, &unlocked); err != nil {
			return err
		}
		if owner == sid {
			counts[measureStates] = unlocked
		}
		if members[owner] {
			counts[measureSocial]++
			if unlocked >= 10 {
				counts[measureSocialTen]++
			}
		}
	}
	return rows.Err()
}

func measureChallengeGames(ctx context.Context, conn *sql.Conn, sid string, counts *[measureCount]int) error {
	rows, err := conn.QueryContext(ctx, `SELECT coalesce(playtime_min,0),coalesce(total_achievements,0),coalesce(unlocked,0)
		FROM achievement_games WHERE steamid=?`, sid)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var minutes, total, unlocked int
		if err := rows.Scan(&minutes, &total, &unlocked); err != nil {
			return err
		}
		counts[measurePlaytime] += minutes
		counts[measureBuckets] += levelPlaytimeBucket(minutes)
		if unlocked > 0 {
			counts[measureGamesWithUnlocks]++
		}
		if total > 0 && unlocked >= total {
			counts[measurePlatinum]++
		}
	}
	return rows.Err()
}

func savedChallenges(ctx context.Context, conn *sql.Conn, sid string) (map[string]savedChallenge, error) {
	rows, err := conn.QueryContext(ctx, "SELECT challenge_id,status,completed_at,claimed_at FROM challenge_progress WHERE steamid=?", sid)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make(map[string]savedChallenge, len(challengeCatalog))
	for rows.Next() {
		var id string
		var saved savedChallenge
		if err := rows.Scan(&id, &saved.status, &saved.completedAt, &saved.claimedAt); err != nil {
			return nil, err
		}
		out[id] = saved
	}
	return out, rows.Err()
}

func assembleChallenges(sid string, measured [measureCount]int, saved map[string]savedChallenge, now sql.NullTime) challengeOverview {
	out := challengeOverview{
		SteamID: sid, Challenges: make([]challengeItem, 0, len(challengeCatalog)),
		Summary: challengeSummary{Total: len(challengeCatalog)},
	}
	updated := levelISOTime(now)
	for _, spec := range challengeCatalog {
		old := saved[spec.id]
		item := challengeItem{
			ID: spec.id, Tier: spec.tier, Category: spec.category, Target: spec.target, RewardXP: challengeReward[spec.tier],
			Progress: measured[spec.measure], Status: challengeActive, ClaimedAt: levelISOTime(old.claimedAt), UpdatedAt: updated,
		}
		switch {
		case old.status == challengeCompleted || old.status == challengeClaimed:
			item.Status, item.CompletedAt = old.status, levelISOTime(old.completedAt)
		case item.Progress >= spec.target:
			item.Status, item.CompletedAt = challengeCompleted, levelISOTime(old.completedAt)
			if item.CompletedAt == nil {
				item.CompletedAt = updated
			}
		}
		item.Claimable = item.Status == challengeCompleted
		if item.Claimable || item.Status == challengeClaimed {
			out.Summary.Completed++
		}
		if item.Status == challengeClaimed {
			out.Summary.Claimed++
		}
		out.Challenges = append(out.Challenges, item)
	}
	out.Summary.Claimable = out.Summary.Completed - out.Summary.Claimed
	return out
}

func saveChallenges(ctx context.Context, conn *sql.Conn, overview challengeOverview, saved map[string]savedChallenge, now time.Time) ([]levelFact, error) {
	var notices []levelFact
	holders := make([]string, 0, len(overview.Challenges))
	args := make([]any, 0, len(overview.Challenges)*6)
	stamp := now.UTC().Format(levelSQLTime)
	for _, item := range overview.Challenges {
		old := saved[item.ID]
		var completedAt any
		if item.CompletedAt != nil {
			completedAt = strings.Replace(*item.CompletedAt, "T", " ", 1)
		}
		if item.Status == challengeCompleted && old.status != challengeCompleted {
			notices = append(notices, levelFact{
				kind: "challenge_completed", key: overview.SteamID + ":challenge:" + item.ID,
				data: map[string]any{"challengeId": item.ID, "rewardXp": item.RewardXP},
			})
		}
		holders = append(holders, "(?,?,?,?,?,?)")
		args = append(args, overview.SteamID, item.ID, item.Progress, item.Status, completedAt, stamp)
	}
	_, err := conn.ExecContext(ctx, `INSERT INTO challenge_progress (steamid,challenge_id,progress,status,completed_at,updated_at) VALUES `+
		strings.Join(holders, ",")+` ON CONFLICT (steamid,challenge_id) DO UPDATE
		SET progress=excluded.progress,status=excluded.status,updated_at=excluded.updated_at,
			completed_at=CASE WHEN challenge_progress.status IN ('completed','claimed') THEN challenge_progress.completed_at ELSE excluded.completed_at END`, args...)
	return notices, err
}

func (s ChallengeService) claim(ctx context.Context, sid, id string) (challengeClaim, error) {
	var reward int
	for _, spec := range challengeCatalog {
		if spec.id == id {
			reward = challengeReward[spec.tier]
			break
		}
	}
	if reward == 0 {
		return challengeClaim{}, errChallengeUnknown
	}
	sid, err := s.account(ctx, sid)
	if err != nil {
		return challengeClaim{}, err
	}
	if sid == "" {
		return challengeClaim{}, errChallengeNoAccount
	}
	if _, err := s.recompute(ctx, sid); err != nil {
		return challengeClaim{}, err
	}
	var result challengeClaim
	var notices []levelFact
	var now time.Time
	err = s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		var status string
		err := conn.QueryRowContext(ctx, "SELECT status FROM challenge_progress WHERE steamid=? AND challenge_id=?", sid, id).Scan(&status)
		switch {
		case errors.Is(err, sql.ErrNoRows):
			return errChallengeIncomplete
		case err != nil:
			return err
		case status == challengeClaimed:
			return errChallengeClaimed
		case status != challengeCompleted:
			return errChallengeIncomplete
		}

		var total int
		var duplicate bool
		if err := conn.QueryRowContext(ctx, `SELECT coalesce(sum(xp),0),coalesce(max(source=? AND source_ref=?),0)
			FROM xp_events WHERE steamid=?`, xpChallenge, id, sid).Scan(&total, &duplicate); err != nil {
			return err
		}
		if duplicate {
			return errChallengeClaimed
		}
		now = s.now()
		entry := []levelXPEvent{{levelXPKey: levelXPKey{xpChallenge, id}, xp: reward, multiplier: 1, note: &id}}
		if err := recordLevelXP(ctx, conn, sid, entry, now); err != nil {
			return err
		}
		stamp := now.UTC().Format(levelSQLTime)
		if _, err := conn.ExecContext(ctx, `UPDATE challenge_progress SET status=?,claimed_at=?,updated_at=?
			WHERE steamid=? AND challenge_id=?`, challengeClaimed, stamp, stamp, sid, id); err != nil {
			return err
		}
		before, _, _ := levelForXP(total)
		after, _, _ := levelForXP(total + reward)
		result = challengeClaim{
			ID: id, Status: challengeClaimed, XPGranted: reward, TotalXP: total + reward,
			Level: challengeClaimLevel{Previous: before, Current: after, LeveledUp: after > before},
		}
		notices = levelSyncFacts(sid, total, entry, nil)
		return nil
	})
	if err != nil {
		return challengeClaim{}, err
	}
	s.publishChallengeFacts(ctx, notices, now)
	return result, nil
}

func (s ChallengeService) publishChallengeFacts(ctx context.Context, notices []levelFact, now time.Time) {
	if len(notices) == 0 {
		return
	}
	if err := s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		return recordLevelFacts(ctx, conn, notices, now)
	}); err != nil {
		slog.Warn("挑战变化未能保存到通知", "error", err)
	}
}
