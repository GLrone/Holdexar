package achievements

import (
	"cmp"
	"context"
	"database/sql"
	"fmt"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

type careerObject = map[string]any

type careerGame struct {
	readGame
	catalogName, series, chinese, developers, publishers string
	release                                              *string
	hasCatalog                                           bool
	price, positiveRate                                  int64
}

type careerUnlock struct {
	readUnlock
	description string
}

type careerTag struct {
	name   string
	weight int64
}

func (r Reader) RegisterCareer(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/achievements/career", func(w http.ResponseWriter, req *http.Request) {
		body, err := r.Career(req.Context(), readParam(req, "steamid"))
		readReply(w, body, err)
	})
}

func (r Reader) Career(ctx context.Context, target string) (map[string]any, error) {
	sid, hasCredential, err := r.readAccount(ctx, target)
	if err != nil {
		return nil, err
	}
	var syncedAt any
	if r.Snapshot != nil {
		owner, stamp, _, err := r.Snapshot(ctx)
		if err != nil {
			return nil, err
		}
		if stamp != "" && (owner == "" || owner == sid) {
			syncedAt = stamp
		}
	}
	var games []careerGame
	var unlocked []careerUnlock
	var tags map[int64][]careerTag
	if sid != "" {
		games, unlocked, tags, err = r.careerFacts(ctx, sid)
		if err != nil {
			return nil, err
		}
	}
	owned := make([]careerGame, 0, len(games))
	for _, game := range games {
		if game.Owned {
			owned = append(owned, game)
		}
	}
	playtime, totalMinutes, spotlight, unfinished, unfinishedCount, dormant := careerPlaytime(owned)
	trophy, completed := careerTrophy(games, unlocked, totalMinutes)
	activity, yearly, milestones, records, lastUnlocks := careerActivity(games, unlocked, sid != "")
	platinum, biggest := careerPlatinum(games, lastUnlocks, tags)
	records["biggestPlatinum"] = biggest
	return careerObject{
		"steamid": sid, "hasCredential": sid != "" && hasCredential, "lastSyncedAt": syncedAt,
		"playtime": playtime, "trophy": trophy, "platinum": platinum, "activity": activity,
		"yearly": yearly, "milestones": milestones, "quotes": careerQuotes(unlocked),
		"unfinished": unfinished, "unfinishedCount": unfinishedCount, "dormant": dormant,
		"library": careerLibrary(owned, totalMinutes), "taste": careerTaste(owned, tags, time.Now().Year()),
		"series": careerSeries(owned), "records": records, "spotlight": spotlight, "completedGames": completed,
	}, nil
}

func (r Reader) careerFacts(ctx context.Context, sid string) ([]careerGame, []careerUnlock, map[int64][]careerTag, error) {
	tx, err := r.DB.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, nil, nil, err
	}
	defer tx.Rollback()
	games, err := careerReadGames(ctx, tx, sid)
	if err != nil {
		return nil, nil, nil, err
	}
	if len(games) == 0 {
		return games, nil, nil, nil
	}
	unlocked, err := careerReadUnlocks(ctx, tx, sid)
	if err != nil {
		return nil, nil, nil, err
	}
	tags, err := careerReadTags(ctx, tx, sid)
	return games, unlocked, tags, err
}

func careerReadGames(ctx context.Context, tx *sql.Tx, sid string) ([]careerGame, error) {
	rows, err := tx.QueryContext(ctx, `SELECT a.appid, COALESCE(a.name,''), COALESCE(g.header_image,''),
		a.playtime_min, a.last_played, a.total_achievements, a.unlocked, COALESCE(NULLIF(a.source,''),'owned'),
		COALESCE(g.name,''), COALESCE(g.series_id,''), g.release_date, g.appid IS NOT NULL, COALESCE(g.chinese_support,''),
		COALESCE(g.developers,''), COALESCE(g.publishers,''), COALESCE(g.min_cny_fen,0), COALESCE(g.positive_rate,0)
		FROM achievement_games a LEFT JOIN games g ON g.appid=a.appid WHERE a.steamid=? ORDER BY a.appid`, sid)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []careerGame
	for rows.Next() {
		var g careerGame
		if err := rows.Scan(&g.AppID, &g.Name, &g.HeaderImage, &g.PlaytimeMin, &g.LastPlayed, &g.Total, &g.Unlocked, &g.Source,
			&g.catalogName, &g.series, &g.release, &g.hasCatalog, &g.chinese, &g.developers, &g.publishers, &g.price, &g.positiveRate); err != nil {
			return nil, err
		}
		if g.HeaderImage == "" {
			g.HeaderImage = fmt.Sprintf("https://cdn.cloudflare.steamstatic.com/steam/apps/%d/header.jpg", g.AppID)
		}
		g.Owned = g.Source == "owned"
		g.Platinum = g.Total > 0 && g.Unlocked >= g.Total
		g.series = strings.TrimSpace(g.series)
		if g.catalogName == "" {
			g.catalogName = g.Name
		}
		out = append(out, g)
	}
	return out, rows.Err()
}

func careerReadUnlocks(ctx context.Context, tx *sql.Tx, sid string) ([]careerUnlock, error) {
	rows, err := tx.QueryContext(ctx, `SELECT s.appid,s.unlock_time,COALESCE(d.name,''),d.image_name,d.icon_url,d.global_percent,COALESCE(g.name,''),COALESCE(d.description,'')
		FROM achievement_states s JOIN achievement_defs d ON d.appid=s.appid AND d.image_name=s.image_name
		JOIN achievement_games g ON g.appid=s.appid AND g.steamid=s.steamid
		WHERE s.steamid=? AND s.achieved IS 1 ORDER BY s.appid,s.image_name`, sid)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []careerUnlock
	for rows.Next() {
		var a careerUnlock
		if err := rows.Scan(&a.AppID, &a.UnlockTime, &a.Name, &a.ImageName, &a.Icon, &a.GlobalPercent, &a.GameName, &a.description); err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

func careerReadTags(ctx context.Context, tx *sql.Tx, sid string) (map[int64][]careerTag, error) {
	rows, err := tx.QueryContext(ctx, `SELECT gt.appid,t.name_zh,gt.weight FROM achievement_games g
		JOIN game_tags gt ON gt.appid=g.appid JOIN tags t ON t.tagid=gt.tagid
		WHERE g.steamid=? AND t.name_zh<>'' ORDER BY gt.appid,gt.weight DESC,gt.tagid`, sid)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make(map[int64][]careerTag)
	for rows.Next() {
		var appID int64
		var tag careerTag
		if err := rows.Scan(&appID, &tag.name, &tag.weight); err != nil {
			return nil, err
		}
		out[appID] = append(out[appID], tag)
	}
	return out, rows.Err()
}

func (g careerGame) brief() careerObject {
	return careerObject{"appid": g.AppID, "name": g.Name, "headerImage": g.HeaderImage, "playtimeMin": g.PlaytimeMin,
		"unlocked": g.Unlocked, "total": g.Total, "platinum": g.Platinum}
}

func careerRound(value float64, places int) float64 {
	rounded, _ := strconv.ParseFloat(strconv.FormatFloat(value, 'f', places, 64), 64)
	return rounded
}

func careerMedian(values []int64) int64 {
	if len(values) == 0 {
		return 0
	}
	slices.Sort(values)
	mid := len(values) / 2
	if len(values)%2 == 1 {
		return values[mid]
	}
	return (values[mid-1] + values[mid]) / 2
}

func careerPlaytime(games []careerGame) (careerObject, int64, []careerObject, []careerObject, int, []careerObject) {
	histogram := [6]int{}
	var total int64
	var played []int64
	var top *careerGame
	var pending, stale []careerGame
	thresholds := [5]int64{600, 1200, 3000, 6000, 12000}
	var counts [5]int
	for i, game := range games {
		minutes := game.PlaytimeMin
		total += minutes
		bucket := 0
		for _, bound := range [...]int64{1, 60, 300, 1200, 3000} {
			if minutes >= bound {
				bucket++
			}
		}
		if minutes < 0 {
			bucket = 5
		}
		histogram[bucket]++
		for j, threshold := range thresholds {
			if minutes >= threshold {
				counts[j]++
			}
		}
		if top == nil || minutes > top.PlaytimeMin {
			top = &games[i]
		}
		if minutes > 0 {
			played = append(played, minutes)
			if game.LastPlayed > 0 {
				stale = append(stale, game)
			}
		}
		if game.Unlocked > 0 && game.Unlocked < game.Total {
			pending = append(pending, game)
		}
	}
	var avg, maxMin int64
	var topGame careerObject
	if len(played) > 0 {
		avg = total / int64(len(played))
	}
	if top != nil {
		maxMin, topGame = top.PlaytimeMin, top.brief()
	}
	playtime := careerObject{"totalMin": total, "playedGames": len(played), "avgMin": avg, "medianMin": careerMedian(played),
		"maxMin": maxMin, "maxGame": topGame, "over10h": counts[0], "over20h": counts[1], "over50h": counts[2],
		"over100h": counts[3], "over200h": counts[4], "idleGames": histogram[1], "untouchedGames": histogram[0], "histogram": histogram}
	ordered := slices.Clone(games)
	slices.SortStableFunc(ordered, func(a, b careerGame) int {
		return cmp.Or(cmp.Compare(b.PlaytimeMin, a.PlaytimeMin), cmp.Compare(strings.ToLower(a.Name), strings.ToLower(b.Name)))
	})
	slices.SortStableFunc(pending, func(a, b careerGame) int {
		return cmp.Or(cmp.Compare(a.Total-a.Unlocked, b.Total-b.Unlocked), cmp.Compare(float64(b.Unlocked)/float64(b.Total), float64(a.Unlocked)/float64(a.Total)))
	})
	slices.SortStableFunc(stale, func(a, b careerGame) int { return cmp.Compare(a.LastPlayed, b.LastPlayed) })
	spotlight, unfinished, dormant := []careerObject{}, []careerObject{}, []careerObject{}
	for _, game := range ordered[:min(18, len(ordered))] {
		spotlight = append(spotlight, game.brief())
	}
	for _, game := range pending[:min(12, len(pending))] {
		brief := game.brief()
		brief["remaining"] = game.Total - game.Unlocked
		unfinished = append(unfinished, brief)
	}
	for _, game := range stale[:min(12, len(stale))] {
		brief := game.brief()
		brief["lastPlayed"] = game.LastPlayed
		dormant = append(dormant, brief)
	}
	return playtime, total, spotlight, unfinished, len(pending), dormant
}

func careerTrophy(games []careerGame, achieved []careerUnlock, minutes int64) (careerObject, int) {
	var total, unlocked int64
	var trophyGames, perfect int
	for _, game := range games {
		if game.Total > 0 {
			total += game.Total
			unlocked += game.Unlocked
			trophyGames++
			if game.Platinum {
				perfect++
			}
		}
	}
	rarity := map[string]int{"ultra": 0, "very_rare": 0, "rare": 0, "uncommon": 0, "common": 0, "unknown": 0}
	var rated float64
	var ratedCount int
	for _, a := range achieved {
		rarity[rarityTier(a.GlobalPercent)]++
		if a.GlobalPercent != nil {
			rated += *a.GlobalPercent
			ratedCount++
		}
	}
	rare := rarity["ultra"] + rarity["very_rare"]
	avgRarity := 0.0
	if ratedCount > 0 {
		avgRarity = careerRound(rated/float64(ratedCount), 1)
	}
	return careerObject{"total": total, "unlocked": unlocked, "rate": readPercent(unlocked, total), "gamesWithAchievements": trophyGames,
		"perfect": perfect, "platinumRate": readPercent(int64(perfect), int64(trophyGames)), "rareCount": rare,
		"rareShare": readPercent(int64(rare), int64(len(achieved))), "avgRarity": avgRarity,
		"perHour": careerRound(float64(unlocked)/max(1, float64(minutes)/60), 2), "rarity": rarity}, perfect
}

func careerQuotes(unlocked []careerUnlock) []careerObject {
	out := []careerObject{}
	ordered := slices.Clone(unlocked)
	slices.SortStableFunc(ordered, func(a, b careerUnlock) int {
		return cmp.Compare(readDetailRarity(a.GlobalPercent), readDetailRarity(b.GlobalPercent))
	})
	replacer := strings.NewReplacer("<br/>", " ", "<br>", " ", "&nbsp;", " ")
	seen := make(map[string]bool)
	for _, row := range ordered[:min(160, len(ordered))] {
		text := strings.Join(strings.Fields(replacer.Replace(row.description)), " ")
		if utf8.RuneCountInString(text) < 8 || seen[text] {
			continue
		}
		seen[text] = true
		var percent any
		if row.GlobalPercent != nil {
			percent = careerRound(*row.GlobalPercent, 2)
		}
		out = append(out, careerObject{"name": row.Name, "text": text, "icon": careerIcon(row.Icon), "globalPercent": percent, "appid": row.AppID, "gameName": row.GameName})
		if len(out) == 12 {
			break
		}
	}
	return out
}

func careerIcon(icon *string) string {
	if icon == nil {
		return ""
	}
	return *icon
}
