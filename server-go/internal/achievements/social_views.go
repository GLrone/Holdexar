package achievements

import (
	"context"
	"database/sql"
	"errors"
	"strings"

	"holdexar/server-go/internal/store"
)

type socialCompareAccount struct {
	SteamID             string             `json:"steamid"`
	PersonaName         string             `json:"personaName"`
	AvatarURL           string             `json:"avatarUrl"`
	Relation            string             `json:"relation"`
	Level               int                `json:"level"`
	Tier                string             `json:"tier"`
	TotalXP             int                `json:"totalXp"`
	UnlockedCount       int                `json:"unlockedCount"`
	PlatinumCount       int                `json:"platinumCount"`
	PlaytimeMinutes     int64              `json:"playtimeMinutes"`
	ChallengesCompleted int                `json:"challengesCompleted"`
	RarityBuckets       []readRarityBucket `json:"rarityBuckets"`
}

func (s SocialService) compare(ctx context.Context, raw string) ([]socialCompareAccount, error) {
	roster, err := (Reader{DB: s.DB}).accounts(ctx)
	if err != nil {
		return nil, err
	}
	profiles := map[string]readDisplayAccount{}
	for _, a := range roster {
		profiles[a.SteamID] = a
	}
	wanted := socialCompareIDs(raw)
	if len(wanted) == 0 {
		for _, a := range roster[:min(len(roster), 4)] {
			wanted = append(wanted, a.SteamID)
		}
	}
	if len(wanted) == 0 {
		return []socialCompareAccount{}, nil
	}
	if err := s.ensureSocialBaselines(ctx, wanted); err != nil {
		return nil, err
	}
	tx, err := s.DB.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	accounts, err := readSocialCompareTotals(ctx, tx, wanted, profiles)
	if err != nil {
		return nil, err
	}
	if err := fillSocialCompareRarity(ctx, tx, accounts, wanted); err != nil {
		return nil, err
	}
	if err := fillSocialCompareChallenges(ctx, tx, accounts, wanted); err != nil {
		return nil, err
	}
	return accounts, nil
}

func socialCompareIDs(raw string) []string {
	var wanted []string
	seen := map[string]bool{}
	for part := range strings.SplitSeq(raw, ",") {
		sid := strings.TrimSpace(part)
		if sid != "" && !seen[sid] {
			seen[sid] = true
			wanted = append(wanted, sid)
			if len(wanted) == 6 {
				break
			}
		}
	}
	return wanted
}

func socialBindings(wanted []string) (string, []any) {
	args := make([]any, len(wanted))
	for i, sid := range wanted {
		args[i] = sid
	}
	return strings.TrimSuffix(strings.Repeat("?,", len(wanted)), ","), args
}

func (s SocialService) ensureSocialBaselines(ctx context.Context, wanted []string) error {
	bind, args := socialBindings(wanted)
	rows, err := s.DB.QueryContext(ctx, `SELECT DISTINCT steamid FROM xp_events WHERE steamid IN (`+bind+`)`, args...)
	if err != nil {
		return err
	}
	existing := map[string]bool{}
	for rows.Next() {
		var sid string
		if err := rows.Scan(&sid); err != nil {
			rows.Close()
			return err
		}
		existing[sid] = true
	}
	rows.Close()
	if err := rows.Err(); err != nil || len(existing) == len(wanted) {
		return err
	}
	now := (LevelService{Now: s.Now}).now()
	return s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		for _, sid := range wanted {
			if existing[sid] {
				continue
			}
			var marker int
			err := conn.QueryRowContext(ctx, `SELECT 1 FROM xp_events WHERE steamid=? LIMIT 1`, sid).Scan(&marker)
			if err == nil {
				continue
			}
			if !errors.Is(err, sql.ErrNoRows) {
				return err
			}
			entries, _, err := syncLevelXP(ctx, conn, sid, nil)
			if err != nil {
				return err
			}
			if err := recordLevelXP(ctx, conn, sid, entries, now); err != nil {
				return err
			}
		}
		return nil
	})
}

func readSocialCompareTotals(ctx context.Context, tx *sql.Tx, wanted []string, profiles map[string]readDisplayAccount) ([]socialCompareAccount, error) {
	args := make([]any, 0, len(wanted)*2)
	holders := make([]string, len(wanted))
	for i, sid := range wanted {
		holders[i] = "(?,?)"
		args = append(args, i, sid)
	}
	rows, err := tx.QueryContext(ctx, `WITH wanted(pos,steamid) AS (VALUES `+strings.Join(holders, ",")+`),
		game_stats AS (
			SELECT g.steamid, SUM(g.playtime_min) playtime, SUM(g.total_achievements>0 AND g.unlocked>=g.total_achievements) platinum
			FROM achievement_games g JOIN wanted w ON w.steamid=g.steamid GROUP BY g.steamid
		)
		SELECT w.steamid, COALESCE(g.playtime,0), COALESCE(g.platinum,0),
			(SELECT COUNT(*) FROM achievement_states s WHERE s.steamid=w.steamid AND s.achieved IS 1),
			(SELECT COALESCE(SUM(xp),0) FROM xp_events x WHERE x.steamid=w.steamid)
		FROM wanted w LEFT JOIN game_stats g ON w.steamid=g.steamid ORDER BY w.pos`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]socialCompareAccount, 0, len(wanted))
	for rows.Next() {
		var a socialCompareAccount
		if err := rows.Scan(&a.SteamID, &a.PlaytimeMinutes, &a.PlatinumCount, &a.UnlockedCount, &a.TotalXP); err != nil {
			return nil, err
		}
		a.Level, _, _ = levelForXP(a.TotalXP)
		a.Tier = levelTierSteps[levelTierIndex(a.Level)].name
		profile := profiles[a.SteamID]
		a.PersonaName, a.AvatarURL, a.Relation = profile.PersonaName, profile.AvatarURL, profile.Relation
		out = append(out, a)
	}
	return out, rows.Err()
}

func fillSocialCompareRarity(ctx context.Context, tx *sql.Tx, accounts []socialCompareAccount, wanted []string) error {
	bind, args := socialBindings(wanted)
	rows, err := tx.QueryContext(ctx, `SELECT s.steamid,d.global_percent,COUNT(*) FROM achievement_states s
		JOIN achievement_defs d ON d.appid=s.appid AND d.image_name=s.image_name
		WHERE s.achieved IS 1 AND s.steamid IN (`+bind+`) GROUP BY s.steamid,d.global_percent`, args...)
	if err != nil {
		return err
	}
	defer rows.Close()
	counts := make(map[string]map[string]int, len(wanted))
	for rows.Next() {
		var sid string
		var percent *float64
		var count int
		if err := rows.Scan(&sid, &percent, &count); err != nil {
			return err
		}
		if counts[sid] == nil {
			counts[sid] = map[string]int{}
		}
		counts[sid][rarityTier(percent)] += count
	}
	if err := rows.Err(); err != nil {
		return err
	}
	for i := range accounts {
		a := &accounts[i]
		bucket := counts[a.SteamID]
		for _, band := range readRarityBands {
			a.RarityBuckets = append(a.RarityBuckets, readRarityBucket{band.Tier, bucket[band.Tier]})
		}
		a.RarityBuckets = append(a.RarityBuckets, readRarityBucket{"common", bucket["common"]}, readRarityBucket{"unknown", bucket["unknown"]})
	}
	return nil
}

func fillSocialCompareChallenges(ctx context.Context, tx *sql.Tx, accounts []socialCompareAccount, wanted []string) error {
	var exists bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE type='table' AND name='challenge_progress')`).Scan(&exists); err != nil || !exists {
		return err
	}
	bind, args := socialBindings(wanted)
	rows, err := tx.QueryContext(ctx, `SELECT steamid, COUNT(*) FROM challenge_progress
		WHERE status IN ('completed','claimed') AND steamid IN (`+bind+`) GROUP BY steamid`, args...)
	if err != nil {
		return err
	}
	defer rows.Close()
	counts := make(map[string]int, len(wanted))
	for rows.Next() {
		var sid string
		var count int
		if err := rows.Scan(&sid, &count); err != nil {
			return err
		}
		counts[sid] = count
	}
	for i := range accounts {
		accounts[i].ChallengesCompleted = counts[accounts[i].SteamID]
	}
	return rows.Err()
}

type socialFeedItem struct {
	AppID         int64    `json:"appid"`
	ImageName     string   `json:"imageName"`
	Name          string   `json:"name"`
	IconURL       *string  `json:"iconUrl"`
	IconGrayURL   *string  `json:"iconGrayUrl"`
	GameName      string   `json:"gameName"`
	GlobalPercent *float64 `json:"globalPercent"`
	Rarity        string   `json:"rarity"`
	UnlockTime    int64    `json:"unlockTime"`
}

func (s SocialService) feed(ctx context.Context, target string, limit int64) ([]socialFeedItem, error) {
	items := []socialFeedItem{}
	sid, err := s.socialSteamID(ctx, target)
	if err != nil || sid == "" {
		return items, err
	}
	if limit == 0 {
		limit = 30
	}
	rows, err := s.DB.QueryContext(ctx, `SELECT s.appid,s.image_name,COALESCE(NULLIF(d.name,''),s.image_name),d.icon_url,d.icon_gray_url,
		COALESCE(g.name,''),d.global_percent,s.unlock_time FROM achievement_states s
		LEFT JOIN achievement_defs d ON d.appid=s.appid AND d.image_name=s.image_name
		LEFT JOIN achievement_games g ON g.steamid=s.steamid AND g.appid=s.appid
		WHERE s.steamid=? AND s.achieved IS 1 AND s.unlock_time>0 ORDER BY s.unlock_time DESC,s.image_name LIMIT ?`, sid, max(1, min(limit, 100)))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var a socialFeedItem
		if err := rows.Scan(&a.AppID, &a.ImageName, &a.Name, &a.IconURL, &a.IconGrayURL, &a.GameName, &a.GlobalPercent, &a.UnlockTime); err != nil {
			return nil, err
		}
		a.Rarity = rarityTier(a.GlobalPercent)
		items = append(items, a)
	}
	return items, rows.Err()
}
