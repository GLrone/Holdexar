package achievements

import (
	"cmp"
	"slices"
	"time"
)

type careerSpan struct {
	game     careerGame
	from, to int64
	count    int
}

func (s *careerSpan) brief() careerObject {
	if s == nil {
		return nil
	}
	out := s.game.brief()
	out["spanSec"], out["spanMin"], out["fromTime"], out["toTime"], out["unlocks"] = s.to-s.from, (s.to-s.from)/60, s.from, s.to, s.count
	return out
}

func careerActivity(games []careerGame, unlocked []careerUnlock, selected bool) (careerObject, []careerObject, []careerObject, careerObject, map[int64]int64) {
	byID := make(map[int64]careerGame, len(games))
	for _, game := range games {
		byID[game.AppID] = game
	}
	timed := make([]careerUnlock, 0, len(unlocked))
	dayCounts, monthCounts := map[string]int{}, map[string]int{}
	var dayOrder []string
	var hours [24]int
	var weekdays [7]int
	hourWeekday := []int{}
	if selected {
		hourWeekday = make([]int, 7*24)
	}
	spans := map[int64]*careerSpan{}
	var spanOrder []int64
	years := map[int]int{}
	yearGames := map[int]map[int64]bool{}
	for _, unlock := range unlocked {
		if unlock.UnlockTime <= 0 {
			continue
		}
		timed = append(timed, unlock)
		date := time.Unix(unlock.UnlockTime, 0).In(time.Local)
		day := date.Format(time.DateOnly)
		if dayCounts[day] == 0 {
			dayOrder = append(dayOrder, day)
		}
		dayCounts[day]++
		monthCounts[date.Format("2006-01")]++
		hour, weekday := date.Hour(), (int(date.Weekday())+6)%7
		hours[hour]++
		weekdays[weekday]++
		hourWeekday[weekday*24+hour]++
		span := spans[unlock.AppID]
		if span == nil {
			span = &careerSpan{game: byID[unlock.AppID], from: unlock.UnlockTime, to: unlock.UnlockTime}
			spans[unlock.AppID] = span
			spanOrder = append(spanOrder, unlock.AppID)
		}
		span.from, span.to, span.count = min(span.from, unlock.UnlockTime), max(span.to, unlock.UnlockTime), span.count+1
		year := date.Year()
		years[year]++
		if yearGames[year] == nil {
			yearGames[year] = map[int64]bool{}
		}
		yearGames[year][unlock.AppID] = true
	}
	var busiestDay careerObject
	var busiestCount int
	for _, day := range dayOrder {
		if dayCounts[day] > busiestCount {
			busiestCount = dayCounts[day]
			busiestDay = careerObject{"date": day, "count": busiestCount}
		}
	}
	slices.Sort(dayOrder)
	firstDate, lastDate, spanDays, current, longest, longestEnd, maxGap := careerStreaks(dayOrder)
	months := make([]string, 0, len(monthCounts))
	for month := range monthCounts {
		months = append(months, month)
	}
	slices.Sort(months)
	monthly := make([]careerObject, 0, len(months))
	for _, month := range months {
		monthly = append(monthly, careerObject{"month": month, "count": monthCounts[month]})
	}
	slices.SortStableFunc(timed, func(a, b careerUnlock) int { return cmp.Compare(a.UnlockTime, b.UnlockTime) })
	var firstUnlock any
	if len(timed) > 0 {
		first := timed[0]
		firstUnlock = struct {
			readUnlock
			HeaderImage string `json:"headerImage"`
		}{first.readUnlock, byID[first.AppID].HeaderImage}
	}
	var busiestHour int
	for hour, count := range hours {
		if count > hours[busiestHour] {
			busiestHour = hour
		}
	}
	activity := careerObject{"days": dayCounts, "firstDate": firstDate, "lastDate": lastDate, "activeDays": len(dayOrder),
		"totalUnlocks": len(timed), "currentStreak": current, "longestStreak": longest, "longestStreakEnd": longestEnd,
		"busiestDay": busiestDay, "hourHistogram": hours, "weekdayHistogram": weekdays,
		"nightUnlocks": careerSum(hours[:5]), "morningUnlocks": careerSum(hours[5:9]), "dayUnlocks": careerSum(hours[9:18]),
		"eveningUnlocks": careerSum(hours[18:]), "weekendUnlocks": weekdays[5] + weekdays[6], "firstUnlock": firstUnlock,
		"spanDays": spanDays, "hourWeekday": hourWeekday, "monthly": monthly, "maxGapDays": maxGap}
	var fastest, slowest, most *careerSpan
	lastUnlocks := make(map[int64]int64, len(spans))
	var platinumYears = map[int]int{}
	for _, appID := range spanOrder {
		span := spans[appID]
		lastUnlocks[appID] = span.to
		if span.game.Platinum {
			platinumYears[time.Unix(span.to, 0).In(time.Local).Year()]++
		}
		if most == nil || span.count > most.count {
			most = span
		}
		if span.count < 5 || span.game.PlaytimeMin < 60 {
			continue
		}
		if fastest == nil || span.to-span.from < fastest.to-fastest.from {
			fastest = span
		}
		if slowest == nil || span.to-span.from > slowest.to-slowest.from {
			slowest = span
		}
	}
	var mostGame careerObject
	if most != nil {
		mostGame = most.game.brief()
		mostGame["unlocked"] = most.count
	}
	records := careerObject{"fastestComplete": fastest.brief(), "slowestComplete": slowest.brief(), "marathonDay": busiestDay,
		"busiestHour": busiestHour, "mostUnlocksGame": mostGame}
	orderedYears := make([]int, 0, len(years))
	for year := range years {
		orderedYears = append(orderedYears, year)
	}
	slices.Sort(orderedYears)
	yearly := make([]careerObject, 0, len(orderedYears))
	for _, year := range orderedYears {
		yearly = append(yearly, careerObject{"year": year, "unlocks": years[year], "games": len(yearGames[year]), "platinum": platinumYears[year]})
	}
	return activity, yearly, careerMilestones(timed, byID), records, lastUnlocks
}

func careerSum(values []int) int {
	var sum int
	for _, value := range values {
		sum += value
	}
	return sum
}

func careerStreaks(days []string) (first, last string, span, current, longest int, end int64, gap int) {
	if len(days) == 0 {
		return
	}
	first, last = days[0], days[len(days)-1]
	start, _ := time.Parse(time.DateOnly, first)
	previous, longestDay := start, start
	current, longest = 1, 1
	for _, day := range days[1:] {
		at, _ := time.Parse(time.DateOnly, day)
		difference := int(at.Sub(previous) / (24 * time.Hour))
		gap = max(gap, difference)
		if difference == 1 {
			current++
		} else {
			current = 1
		}
		if current > longest {
			longest, longestDay = current, at
		}
		previous = at
	}
	span = int(previous.Sub(start)/(24*time.Hour)) + 1
	end = time.Date(longestDay.Year(), longestDay.Month(), longestDay.Day(), 0, 0, 0, 0, time.Local).Unix()
	return
}

func careerMilestones(timed []careerUnlock, games map[int64]careerGame) []careerObject {
	type milestone struct {
		at     int64
		object careerObject
	}
	entry := func(a careerUnlock, kind string, index int) milestone {
		return milestone{a.UnlockTime, careerObject{"kind": kind, "index": index, "at": a.UnlockTime, "name": a.Name, "icon": careerIcon(a.Icon),
			"gameName": a.GameName, "appid": a.AppID, "headerImage": games[a.AppID].HeaderImage}}
	}
	var entries []milestone
	for _, step := range [...]int{1, 10, 50, 100, 250, 500, 1000} {
		if len(timed) < step {
			break
		}
		entries = append(entries, entry(timed[step-1], "count", step))
	}
	for _, unlock := range timed {
		if unlock.GlobalPercent != nil && *unlock.GlobalPercent != 0 && *unlock.GlobalPercent < 5 {
			milestone := entry(unlock, "rarest", 0)
			milestone.object["globalPercent"] = careerRound(*unlock.GlobalPercent, 2)
			entries = append(entries, milestone)
			break
		}
	}
	slices.SortStableFunc(entries, func(a, b milestone) int { return cmp.Compare(a.at, b.at) })
	out := make([]careerObject, 0, len(entries))
	for _, entry := range entries {
		out = append(out, entry.object)
	}
	return out
}

func careerPlatinum(games []careerGame, last map[int64]int64, tags map[int64][]careerTag) (careerObject, careerObject) {
	var entries, played []careerGame
	var biggest *careerGame
	var firstDate, lastDate int64
	byYear := map[int]int{}
	byTag := map[string]int{}
	var tagOrder []string
	for i, game := range games {
		if !game.Platinum {
			continue
		}
		entries = append(entries, game)
		if game.PlaytimeMin > 0 {
			played = append(played, game)
		}
		if biggest == nil || game.Total > biggest.Total {
			biggest = &games[i]
		}
		if date := last[game.AppID]; date > 0 {
			byYear[time.Unix(date, 0).In(time.Local).Year()]++
			if firstDate == 0 || date < firstDate {
				firstDate = date
			}
			lastDate = max(lastDate, date)
		}
		if list := tags[game.AppID]; len(list) > 0 {
			if byTag[list[0].name] == 0 {
				tagOrder = append(tagOrder, list[0].name)
			}
			byTag[list[0].name]++
		}
	}
	if len(played) == 0 {
		played = entries
	}
	var fastest, slowest careerObject
	var avg int64
	var minutes []int64
	if len(played) > 0 {
		lo, hi := played[0], played[0]
		var sum int64
		for _, game := range played {
			if game.PlaytimeMin < lo.PlaytimeMin {
				lo = game
			}
			if game.PlaytimeMin > hi.PlaytimeMin {
				hi = game
			}
			minutes = append(minutes, game.PlaytimeMin)
			sum += game.PlaytimeMin
		}
		avg = sum / int64(len(played))
		fastest, slowest = careerDatedGame(lo, last), careerDatedGame(hi, last)
	}
	orderedYears := make([]int, 0, len(byYear))
	for year := range byYear {
		orderedYears = append(orderedYears, year)
	}
	slices.Sort(orderedYears)
	perYear := make([]careerObject, 0, len(orderedYears))
	for _, year := range orderedYears {
		perYear = append(perYear, careerObject{"year": year, "count": byYear[year]})
	}
	slices.SortStableFunc(tagOrder, func(a, b string) int { return cmp.Compare(byTag[b], byTag[a]) })
	topTags := []careerObject{}
	for _, tag := range tagOrder[:min(12, len(tagOrder))] {
		topTags = append(topTags, careerObject{"tag": tag, "count": byTag[tag]})
	}
	var biggestGame careerObject
	if biggest != nil {
		biggestGame = careerDatedGame(*biggest, last)
	}
	return careerObject{"count": len(entries), "avgMin": avg, "medianMin": careerMedian(minutes), "fastest": fastest, "slowest": slowest,
		"tags": topTags, "spanDays": (lastDate - firstDate) / 86400, "perYear": perYear, "firstDate": firstDate, "lastDate": lastDate}, biggestGame
}

func careerDatedGame(game careerGame, last map[int64]int64) careerObject {
	brief := game.brief()
	brief["date"] = last[game.AppID]
	return brief
}
