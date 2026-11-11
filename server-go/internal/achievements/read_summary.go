package achievements

import (
	"cmp"
	"context"
	"database/sql"
	"slices"
	"time"
)

var readRarityBands = [...]struct {
	Tier  string
	Upper float64
}{
	{"ultra", 1}, {"very_rare", 5}, {"rare", 20}, {"uncommon", 50},
}

func rarityTier(percent *float64) string {
	if percent == nil {
		return "unknown"
	}
	for _, band := range readRarityBands {
		if *percent < band.Upper {
			return band.Tier
		}
	}
	return "common"
}

type readUnlock struct {
	AppID         int64    `json:"appid"`
	UnlockTime    int64    `json:"unlockTime"`
	Name          string   `json:"name"`
	ImageName     string   `json:"imageName"`
	Icon          *string  `json:"icon"`
	GlobalPercent *float64 `json:"globalPercent"`
	GameName      string   `json:"gameName"`
}

type readPlatinum struct {
	readGameArt
	Date int64 `json:"date"`
}

type readNearCompletion struct {
	readGameArt
	readGameProgress
	Remaining int64 `json:"remaining"`
}

type readRarityBucket struct {
	Tier  string `json:"tier"`
	Count int    `json:"count"`
}

type readPlaytime struct {
	readGameName
	PlaytimeMin int64 `json:"playtimeMin"`
}

type readTimelineMonth struct {
	Month string `json:"month"`
	Count int    `json:"count"`
}

type readSummary struct {
	HasCredential         bool                 `json:"hasCredential"`
	SteamID               string               `json:"steamid"`
	Platinum              int                  `json:"platinum"`
	GamesWithAchievements int                  `json:"gamesWithAchievements"`
	PlayedGames           int                  `json:"playedGames"`
	TotalAchievements     int64                `json:"totalAchievements"`
	UnlockedAchievements  int64                `json:"unlockedAchievements"`
	CompletionRate        float64              `json:"completionRate"`
	TotalPlaytimeMin      int64                `json:"totalPlaytimeMin"`
	ExternalGames         int                  `json:"externalGames"`
	ExternalUnlocked      int64                `json:"externalUnlocked"`
	ExternalPlatinum      int                  `json:"externalPlatinum"`
	LastSyncedAt          *string              `json:"lastSyncedAt"`
	Platinums             []readPlatinum       `json:"platinums"`
	Rarest                []readUnlock         `json:"rarest"`
	RecentUnlocks         []readUnlock         `json:"recentUnlocks"`
	NearCompletion        []readNearCompletion `json:"nearCompletion"`
	RarityBuckets         []readRarityBucket   `json:"rarityBuckets"`
	PlaytimeTop           []readPlaytime       `json:"playtimeTop"`
	UnlockTimeline        []readTimelineMonth  `json:"unlockTimeline"`
}

func (r Reader) summary(ctx context.Context, target string) (readSummary, error) {
	result := readSummary{
		Platinums: []readPlatinum{}, Rarest: []readUnlock{}, RecentUnlocks: []readUnlock{},
		NearCompletion: []readNearCompletion{}, RarityBuckets: []readRarityBucket{},
		PlaytimeTop: []readPlaytime{}, UnlockTimeline: []readTimelineMonth{},
	}
	sid, hasCredential, err := r.readAccount(ctx, target)
	if err != nil {
		return result, err
	}
	result.SteamID = sid
	result.HasCredential = sid != "" && hasCredential
	var running bool
	if r.Snapshot != nil {
		owner, syncedAt, isRunning, err := r.Snapshot(ctx)
		if err != nil {
			return result, err
		}
		running = isRunning
		if syncedAt != "" && (owner == "" || owner == sid) {
			result.LastSyncedAt = &syncedAt
		}
	}
	if sid == "" {
		return result, nil
	}
	games, achieved, err := r.readSummaryFacts(ctx, sid)
	if err != nil {
		return result, err
	}
	now := time.Now()
	result.fillSummary(now, games, achieved)
	if r.Warm != nil && result.HasCredential && !running && result.LastSyncedAt != nil {
		if synced, err := time.Parse(time.RFC3339Nano, *result.LastSyncedAt); err == nil && now.Sub(synced) > 6*time.Hour {
			r.Warm(sid)
		}
	}
	return result, nil
}

func (r Reader) readSummaryFacts(ctx context.Context, sid string) ([]readGame, []readUnlock, error) {
	tx, err := r.DB.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, nil, err
	}
	defer tx.Rollback()
	games, err := readGames(ctx, tx, sid)
	if err != nil {
		return nil, nil, err
	}
	rows, err := tx.QueryContext(ctx, `SELECT s.appid,s.unlock_time,COALESCE(d.name,''),d.image_name,d.icon_url,d.global_percent,COALESCE(g.name,'')
		FROM achievement_states s
		JOIN achievement_defs d ON d.appid=s.appid AND d.image_name=s.image_name
		JOIN achievement_games g ON g.appid=s.appid AND g.steamid=s.steamid
		WHERE s.steamid=? AND s.achieved IS 1 ORDER BY s.appid,s.image_name`, sid)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	var achieved []readUnlock
	for rows.Next() {
		var a readUnlock
		if err := rows.Scan(&a.AppID, &a.UnlockTime, &a.Name, &a.ImageName, &a.Icon, &a.GlobalPercent, &a.GameName); err != nil {
			return nil, nil, err
		}
		achieved = append(achieved, a)
	}
	return games, achieved, rows.Err()
}

func (result *readSummary) fillSummary(now time.Time, games []readGame, achieved []readUnlock) {
	lastUnlock := map[int64]int64{}
	bucketCounts := map[string]int{}
	monthCounts := map[string]int{}
	for _, a := range achieved {
		lastUnlock[a.AppID] = max(lastUnlock[a.AppID], a.UnlockTime)
		bucketCounts[rarityTier(a.GlobalPercent)]++
		if a.GlobalPercent != nil {
			result.Rarest = append(result.Rarest, a)
		}
		if a.UnlockTime > 0 {
			result.RecentUnlocks = append(result.RecentUnlocks, a)
			monthCounts[time.Unix(a.UnlockTime, 0).In(now.Location()).Format("2006-01")]++
		}
	}
	for _, band := range readRarityBands {
		result.RarityBuckets = append(result.RarityBuckets, readRarityBucket{band.Tier, bucketCounts[band.Tier]})
	}
	for _, tier := range []string{"common", "unknown"} {
		result.RarityBuckets = append(result.RarityBuckets, readRarityBucket{tier, bucketCounts[tier]})
	}
	monthStart := time.Date(now.Year(), now.Month(), 1, 0, 0, 0, 0, now.Location())
	for offset := -23; offset <= 0; offset++ {
		month := monthStart.AddDate(0, offset, 0).Format("2006-01")
		result.UnlockTimeline = append(result.UnlockTimeline, readTimelineMonth{month, monthCounts[month]})
	}
	for _, g := range games {
		result.TotalPlaytimeMin += g.PlaytimeMin
		if g.PlaytimeMin > 0 {
			result.PlayedGames++
		}
		if g.Total <= 0 {
			continue
		}
		result.GamesWithAchievements++
		result.TotalAchievements += g.Total
		result.UnlockedAchievements += g.Unlocked
		if !g.Owned {
			result.ExternalGames++
			result.ExternalUnlocked += g.Unlocked
		}
		if g.Platinum {
			result.Platinum++
			if !g.Owned {
				result.ExternalPlatinum++
			}
			result.Platinums = append(result.Platinums, readPlatinum{g.readGameArt, lastUnlock[g.AppID]})
		} else if float64(g.Unlocked)*100/float64(g.Total) >= 75 {
			result.NearCompletion = append(result.NearCompletion, readNearCompletion{
				readGameArt:      g.readGameArt,
				readGameProgress: readGameProgress{Unlocked: g.Unlocked, Percent: readPercent(g.Unlocked, g.Total)},
				Remaining:        g.Total - g.Unlocked,
			})
		}
	}
	result.CompletionRate = readPercent(result.UnlockedAchievements, result.TotalAchievements)
	slices.SortStableFunc(result.Platinums, func(a, b readPlatinum) int { return cmp.Compare(b.Date, a.Date) })
	slices.SortStableFunc(result.Rarest, func(a, b readUnlock) int { return cmp.Compare(*a.GlobalPercent, *b.GlobalPercent) })
	slices.SortStableFunc(result.RecentUnlocks, func(a, b readUnlock) int { return cmp.Compare(b.UnlockTime, a.UnlockTime) })
	slices.SortStableFunc(result.NearCompletion, func(a, b readNearCompletion) int {
		return cmp.Or(cmp.Compare(b.Percent, a.Percent), cmp.Compare(a.Remaining, b.Remaining))
	})
	result.Platinums = result.Platinums[:min(len(result.Platinums), 12)]
	result.Rarest = result.Rarest[:min(len(result.Rarest), 12)]
	result.RecentUnlocks = result.RecentUnlocks[:min(len(result.RecentUnlocks), 12)]
	result.NearCompletion = result.NearCompletion[:min(len(result.NearCompletion), 12)]
	slices.SortStableFunc(games, func(a, b readGame) int { return cmp.Compare(b.PlaytimeMin, a.PlaytimeMin) })
	for _, g := range games[:min(len(games), 10)] {
		result.PlaytimeTop = append(result.PlaytimeTop, readPlaytime{g.readGameName, g.PlaytimeMin})
	}
}
