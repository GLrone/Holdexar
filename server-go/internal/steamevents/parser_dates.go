package steamevents

import (
	"regexp"
	"strconv"
	"time"
)

const monthWord = `(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)`

var (
	dateYearRE     = regexp.MustCompile(`20\d\d`)
	dateFragmentRE = regexp.MustCompile(`(?i)\b(?:` +
		`(?:(20\d\d)\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日|` +
		`(\d{1,2})\s+(` + monthWord + `)\b(?:,?\s+(20\d\d))?|` +
		`(` + monthWord + `)\s+(\d{1,2})\b(?:,?\s+(20\d\d))?|` +
		`(\d{1,2}),?\s+(20\d\d)|` +
		`(\d{1,2})\s*日)`)
)

type eventDate struct {
	year  int
	month time.Month
	day   int
}

func eventDateFragments(text string, yearHint int) []eventDate {
	if yearHint == 0 {
		years := dateYearRE.FindAllString(text, -1)
		if len(years) > 0 {
			yearHint = dateNumber(years[len(years)-1])
		}
	}

	var dates []eventDate
	var month time.Month
	for _, part := range dateFragmentRE.FindAllStringSubmatch(text, -1) {
		var year, day int
		switch {
		case part[2] != "":
			year, month, day = dateNumber(part[1]), time.Month(dateNumber(part[2])), dateNumber(part[3])
		case part[4] != "":
			year, month, day = dateNumber(part[6]), englishEventMonth(part[5]), dateNumber(part[4])
		case part[7] != "":
			year, month, day = dateNumber(part[9]), englishEventMonth(part[7]), dateNumber(part[8])
		case part[10] != "":
			year, day = dateNumber(part[11]), dateNumber(part[10])
		default:
			day = dateNumber(part[12])
		}
		if year == 0 {
			year = yearHint
		}
		dates = append(dates, eventDate{year, month, day})
	}
	return dates
}

func (value eventDate) date() time.Time {
	if value.year == 0 || value.month < time.January || value.month > time.December || value.day == 0 {
		return time.Time{}
	}
	date := time.Date(value.year, value.month, value.day, 0, 0, 0, 0, time.UTC)
	if date.Month() != value.month {
		return time.Time{}
	}
	return date
}

func dateNumber(text string) int {
	value, _ := strconv.Atoi(text)
	return value
}

func englishEventMonth(word string) time.Month {
	date, _ := time.Parse("Jan", word[:3])
	return date.Month()
}
