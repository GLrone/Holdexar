package achievements

import (
	"cmp"
	"encoding/json"
	"fmt"
	"math"
	"slices"
	"strconv"
	"strings"
)

func careerLibrary(games []careerGame, minutes int64) careerObject {
	var value, rate int64
	var priced, rated int
	var top *careerGame
	for i, game := range games {
		if game.price > 0 {
			value += game.price
			priced++
			if top == nil || game.price > top.price {
				top = &games[i]
			}
		}
		if game.positiveRate > 0 {
			rate += game.positiveRate
			rated++
		}
	}
	var topValue careerObject
	var costPerHour, avgRate float64
	if top != nil {
		topValue = top.brief()
		topValue["priceFen"], topValue["positiveRate"] = top.price, careerRound(float64(top.positiveRate)/100, 1)
		if minutes > 0 {
			costPerHour = careerRound(float64(value)/(float64(minutes)/60), 1)
		}
		if rated > 0 {
			avgRate = careerRound(float64(rate)/float64(rated)/100, 1)
		}
	}
	return careerObject{"valueFen": value, "pricedGames": priced, "costPerHourFen": costPerHour, "avgPositiveRate": avgRate, "topValue": topValue}
}

var careerTagFamilies = map[string]string{
	"动作": "action", "冒险": "adventure", "角色扮演": "rpg", "独立": "indie", "模拟": "sim", "休闲": "casual",
	"策略": "strategy", "抢先体验": "early", "大型多人在线": "mmo", "体育": "sports", "竞速": "racing", "免费开玩": "free",
	"实用工具": "software", "设计与插画": "software", "动画制作和建模": "software", "教育": "software",
	"游戏开发": "software", "视频制作": "software", "照片编辑": "software",
}

type careerRankItem struct {
	name            string
	minutes         float64
	games, platinum int
}

type careerRanking map[string]careerRankItem

func (rank careerRanking) add(name string, minutes float64, platinum bool) {
	row := rank[name]
	row.name = name
	row.minutes += minutes
	row.games++
	if platinum {
		row.platinum++
	}
	rank[name] = row
}

func (rank careerRanking) rows(label string) []careerObject {
	ordered := make([]careerRankItem, 0, len(rank))
	for _, row := range rank {
		ordered = append(ordered, row)
	}
	slices.SortFunc(ordered, func(a, b careerRankItem) int {
		return cmp.Or(cmp.Compare(b.minutes, a.minutes), cmp.Compare(a.name, b.name))
	})
	out := []careerObject{}
	for _, row := range ordered[:min(12, len(ordered))] {
		out = append(out, careerObject{label: row.name, "playtimeMin": int64(math.RoundToEven(row.minutes)), "games": row.games, "platinum": row.platinum})
	}
	return out
}

func careerTaste(games []careerGame, tagsByGame map[int64][]careerTag, currentYear int) careerObject {
	tags, families, developers, publishers, series := careerRanking{}, careerRanking{}, careerRanking{}, careerRanking{}, careerRanking{}
	type decadeStats struct {
		count   int
		minutes int64
	}
	decades := map[string]decadeStats{}
	var chinese, fresh, sumYears, yearCount int
	var oldest, newest *careerGame
	for i, game := range games {
		if game.PlaytimeMin <= 0 {
			continue
		}
		minutes := float64(game.PlaytimeMin)
		var weight int64
		gameTags := tagsByGame[game.AppID]
		for _, tag := range gameTags {
			weight += tag.weight
		}
		seenFamilies := map[string]bool{}
		for _, tag := range gameTags {
			var share float64
			if weight != 0 {
				share = minutes * float64(tag.weight) / float64(weight)
			}
			tags.add(tag.name, share, game.Platinum)
			if family := careerTagFamilies[tag.name]; family != "" {
				seenFamilies[family] = true
			}
		}
		for family := range seenFamilies {
			families.add(family, minutes/float64(len(seenFamilies)), game.Platinum)
		}
		gameDevelopers := careerNames(game.developers)
		for _, name := range gameDevelopers {
			developers.add(name, minutes/float64(len(gameDevelopers)), game.Platinum)
		}
		for _, name := range careerNames(game.publishers) {
			publishers.add(name, minutes, game.Platinum)
		}
		if game.series != "" {
			series.add(game.series, minutes, game.Platinum)
		}
		release := game.releaseText()
		if len(release) >= 4 {
			if year, err := strconv.Atoi(release[:4]); err == nil && release[0] >= '0' && release[0] <= '9' {
				sumYears += year
				yearCount++
				if year >= currentYear-1 {
					fresh++
				}
				decade := strconv.Itoa(year/10*10) + "s"
				stat := decades[decade]
				stat.count++
				stat.minutes += game.PlaytimeMin
				decades[decade] = stat
			}
		}
		if game.chinese != "" {
			chinese++
		}
		if oldest == nil || compareOldRelease(release, oldest.releaseText()) < 0 {
			oldest = &games[i]
		}
		if newest == nil || release > newest.releaseText() {
			newest = &games[i]
		}
	}
	ordered := make([]string, 0, len(decades))
	for decade := range decades {
		ordered = append(ordered, decade)
	}
	slices.Sort(ordered)
	decadeRows := make([]careerObject, 0, len(ordered))
	for _, decade := range ordered {
		stat := decades[decade]
		decadeRows = append(decadeRows, careerObject{"decade": decade, "games": stat.count, "playtimeMin": stat.minutes})
	}
	return careerObject{"tags": tags.rows("tag"), "families": families.rows("family"), "developers": developers.rows("name"),
		"publishers": publishers.rows("name"), "series": series.rows("name"), "decades": decadeRows, "chineseGames": chinese,
		"freshGames": fresh, "avgReleaseYear": sumYears / max(1, yearCount), "oldestGame": oldest.releasedBrief(), "newestGame": newest.releasedBrief()}
}

func compareOldRelease(a, b string) int {
	if a == "" {
		a = "9999"
	}
	if b == "" {
		b = "9999"
	}
	return cmp.Compare(a, b)
}

func (game *careerGame) releasedBrief() careerObject {
	if game == nil {
		return nil
	}
	brief := game.brief()
	if game.hasCatalog {
		brief["releaseDate"] = game.release
	} else {
		brief["releaseDate"] = ""
	}
	return brief
}

func (game careerGame) releaseText() string {
	if game.release == nil {
		return ""
	}
	return *game.release
}

func careerNames(raw string) []string {
	var values []any
	if json.Unmarshal([]byte(raw), &values) != nil {
		return nil
	}
	var names []string
	for _, value := range values {
		var name string
		switch x := value.(type) {
		case nil:
			name = "None"
		case bool:
			if x {
				name = "True"
			} else {
				name = "False"
			}
		default:
			name = fmt.Sprint(x)
		}
		if name = strings.TrimSpace(name); name != "" {
			names = append(names, name)
		}
	}
	return names
}

type careerSeriesRow struct {
	id                                                    string
	names                                                 []string
	owned, played, platinum, completed                    int
	unlocked, total, playedUnlocked, playedTotal, minutes int64
	top                                                   careerGame
	next                                                  *careerGame
}

func careerSeries(games []careerGame) careerObject {
	groups := map[string]*careerSeriesRow{}
	var ordered []*careerSeriesRow
	for i, game := range games {
		if game.series == "" {
			continue
		}
		row := groups[game.series]
		if row == nil {
			row = &careerSeriesRow{id: game.series, top: game}
			groups[game.series] = row
			ordered = append(ordered, row)
		}
		row.names = append(row.names, game.catalogName)
		row.owned++
		row.minutes += game.PlaytimeMin
		row.unlocked += game.Unlocked
		row.total += game.Total
		if game.Platinum {
			row.platinum++
			row.completed++
		}
		if game.PlaytimeMin > 0 {
			row.played++
			if game.Total > 0 {
				row.playedUnlocked += game.Unlocked
				row.playedTotal += game.Total
			}
		}
		if game.PlaytimeMin > row.top.PlaytimeMin {
			row.top = game
		}
		if game.Unlocked > 0 && game.Unlocked < game.Total && (row.next == nil || game.Total-game.Unlocked < row.next.Total-row.next.Unlocked) {
			row.next = &games[i]
		}
	}
	ordered = slices.DeleteFunc(ordered, func(row *careerSeriesRow) bool { return row.owned < 2 })
	slices.SortStableFunc(ordered, func(a, b *careerSeriesRow) int {
		return cmp.Or(cmp.Compare(b.played, a.played), cmp.Compare(b.platinum, a.platinum), cmp.Compare(b.minutes, a.minutes), cmp.Compare(a.names[0], b.names[0]))
	})
	var perfected int
	for _, row := range ordered {
		if row.platinum == row.owned {
			perfected++
		}
	}
	out := []careerObject{}
	for _, row := range ordered[:min(18, len(ordered))] {
		var progress float64
		if row.playedTotal > 0 {
			progress = careerRound(float64(row.playedUnlocked)/float64(row.playedTotal), 3)
		}
		var next careerObject
		if row.next != nil {
			next = row.next.brief()
			next["remaining"] = row.next.Total - row.next.Unlocked
		}
		out = append(out, careerObject{"seriesId": row.id, "name": careerSeriesName(row.names, row.id), "owned": row.owned,
			"played": row.played, "platinum": row.platinum, "completed": row.completed, "unlocked": row.unlocked, "total": row.total,
			"playedUnlocked": row.playedUnlocked, "playedTotal": row.playedTotal, "playtimeMin": row.minutes, "progress": progress,
			"topGame": row.top.brief(), "nextGame": next})
	}
	return careerObject{"rows": out, "seriesTotal": len(ordered), "taggedTotal": len(groups), "perfected": perfected}
}

func careerSeriesName(names []string, fallback string) string {
	var prefix []rune
	for i, name := range names {
		chars := []rune(strings.Map(func(r rune) rune {
			if r >= '\u4e00' && r <= '\u9fff' {
				return r
			}
			return -1
		}, name))
		if i == 0 {
			prefix = chars
			continue
		}
		limit := min(len(prefix), len(chars))
		j := 0
		for j < limit && prefix[j] == chars[j] {
			j++
		}
		prefix = prefix[:j]
		if len(prefix) < 2 {
			return fallback
		}
	}
	if len(prefix) >= 2 {
		return string(prefix)
	}
	return fallback
}
