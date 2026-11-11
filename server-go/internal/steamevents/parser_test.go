package steamevents

import (
	"strings"
	"testing"
	"time"
)

func TestParserHeadingDates(t *testing.T) {
	for _, tc := range []struct {
		name, heading, category, start, end string
	}{
		{"day first", "<strong>Autumn Sale 2026</strong> | 1 October, 2026 - 8 October, 2026", "seasonal_sale", "2026-10-01", "2026-10-08"},
		{"month first across years", "<strong>Winter Sale 2026</strong> | December 17, 2026 - January 4, 2027", "seasonal_sale", "2026-12-17", "2027-01-04"},
		{"Chinese date", "秋季特卖 | 2026 年 10 月 1 日 - 10 月 8 日（PT）", "seasonal_sale", "2026-10-01", "2026-10-08"},
		{"official summer title", "2027 年夏日特卖 | 2027 年 6 月 24 日 - 2027 年 7 月 8 日（PT）", "seasonal_sale", "2027-06-24", "2027-07-08"},
		{"shared month and year", "Next Fest | June 14 - 21, 2027", "next_fest", "2027-06-14", "2027-06-21"},
		{"Chinese short tail", "新品节 | 2026 年 10 月 19 日 - 26 日（PT）", "next_fest", "2026-10-19", "2026-10-26"},
		{"no year in title", "Spring Sale | March 19 - 26, 2026 (ENDED)", "seasonal_sale", "2026-03-19", "2026-03-26"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			raw := "<h2>Autumn Sale 2027 | TBD</h2><h2>" + tc.heading + "</h2><h2>" + tc.heading + "</h2>"
			rows := parseUpcoming(raw)
			if len(rows) != 1 {
				t.Fatalf("expected one deduplicated event, got %+v", rows)
			}
			row := rows[0]
			if row.Category != tc.category || row.Start.Format(time.DateOnly) != tc.start || row.End.Format(time.DateOnly) != tc.end || row.Slug != "" {
				t.Fatalf("unexpected event: %+v", row)
			}
			if tc.name == "no year in title" && row.Name != "Spring Sale" {
				t.Fatalf("unexpected title: %q", row.Name)
			}
		})
	}
}

func TestParserRejectsInvalidDates(t *testing.T) {
	raw := "<h2>Spring Sale | March 30 - 29, 2027</h2>" +
		"<h2>Summer Sale | April 31 - May 7, 2027</h2>" +
		"<h2>Winter Sale | 12 Marching, 2027 - 17 Marching, 2027</h2>"
	if rows := parseUpcoming(raw); len(rows) != 0 {
		t.Fatalf("accepted invalid dates: %+v", rows)
	}
}

func TestParserThemedTables(t *testing.T) {
	english := "<table><tr><td>Oct 12<br>Oct 19</td><td>Cooking Fest</td>" +
		"<td><a href='/doc/marketing/upcoming_events/themed_sales/cooking_2026'>More info</a></td></tr>" +
		"<tr><td>12 Nov<br>19 Nov</td><td>Sample &amp; Example Fest</td></tr></table>"
	if rows := parseUpcoming(english + "<h2>2026 Fests</h2>"); len(rows) != 0 {
		t.Fatalf("table without preceding year: %+v", rows)
	}
	rows := parseUpcoming("<h2>2026 Fests</h2>" + english)
	if len(rows) != 2 || rows[0].Category != "themed_fest" || rows[0].Slug != "themed_sales/cooking_2026" ||
		rows[0].Start.Format(time.DateOnly) != "2026-10-12" || rows[0].End.Format(time.DateOnly) != "2026-10-19" ||
		rows[1].Name != "Sample & Example Fest" || rows[1].Start.Format(time.DateOnly) != "2026-11-12" {
		t.Fatalf("unexpected English tables: %+v", rows)
	}

	chinese := "<h2><strong>2027 年各游戏节</strong></h2>" +
		"<table><tr><td>1 月 14 日 -<br>1 月 18 日（PT）</td><td>桌面伴侣游戏节</td>" +
		"<td><a href='/doc/marketing/upcoming_events/themed_sales/desktop_companion_2027'>更多信息</a></td></tr></table>" +
		"<a href='/doc/marketing/upcoming_events/themed_sales/desktop_companion_2027'><span>Steam 桌面伴侣游戏节</span></a>" +
		"<a href='/doc/marketing/upcoming_events/themed_sales/desktop_companion_2027'>重复译名</a>" +
		"<a href='/doc/marketing/upcoming_events/themed_sales/not_a_name'>注册详情</a>"
	rows = parseUpcoming(chinese)
	if len(rows) != 1 || rows[0].Name != "桌面伴侣游戏节" || rows[0].Start.Format(time.DateOnly) != "2027-01-14" || rows[0].End.Format(time.DateOnly) != "2027-01-18" {
		t.Fatalf("unexpected Chinese tables: %+v", rows)
	}
	if names := parseZHNames(chinese); len(names) != 1 || names["themed_sales/desktop_companion_2027"] != "Steam 桌面伴侣游戏节" {
		t.Fatalf("unexpected Chinese navigation names: %+v", names)
	}
}

func TestParserValidation(t *testing.T) {
	today := time.Date(2026, 9, 27, 12, 0, 0, 0, time.FixedZone("Beijing", 8*60*60))
	ok := parseUpcoming("<h2>Autumn Sale | October 1 - 8, 2026</h2><h2>Winter Sale | December 17, 2026 - January 4, 2027</h2>")
	if err := validateEvents(ok, today); err != nil {
		t.Fatal(err)
	}
	bad := []parsedEvent{
		{Category: "seasonal_sale", Name: "A", Start: today.AddDate(-1, 0, -1), End: today.AddDate(-1, 0, 0)},
		{Category: "seasonal_sale", Name: "B", Start: today.AddDate(-1, 0, 0), End: today.AddDate(-1, 1, 0)},
		{Category: "themed_fest", Name: "C", Start: today, End: today.AddDate(0, 0, -1)},
	}
	err := validateEvents(bad, today)
	if err == nil {
		t.Fatal("validation passed for invalid events")
	}
	for _, problem := range []string{"abnormal", "more than a year ago", "overlaps", "end < start"} {
		if !strings.Contains(err.Error(), problem) {
			t.Errorf("validation omitted %q: %v", problem, err)
		}
	}
	if err := validateEvents(nil, today); err == nil {
		t.Fatal("validation accepted an empty calendar")
	}
}
