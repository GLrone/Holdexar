package main

import (
	"testing"
	"time"
)

// Trigger 语义边界：严格晚于锚点、网格不漂移、cron 跨日。

func TestIntervalTriggerNext(t *testing.T) {
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	tr := IntervalTrigger{Start: start, Period: time.Hour}
	cases := []struct {
		after time.Time
		want  time.Time
	}{
		{start.Add(-time.Minute), start},                                 // 早于锚点 → 首拍即锚点
		{start, start.Add(time.Hour)},                                    // 恰在锚点 → 严格晚一拍
		{start.Add(2*time.Hour + time.Second), start.Add(3 * time.Hour)}, // 锚定步进不漂移
		{start.Add(47 * time.Hour), start.Add(48 * time.Hour)},
	}
	for _, c := range cases {
		if got := tr.Next(c.after); !got.Equal(c.want) {
			t.Fatalf("Next(%s) = %s, want %s", c.after, got, c.want)
		}
	}
}

func TestCronTriggerNext(t *testing.T) {
	tr := CronTrigger{Hour: 3, Minute: 0, Loc: locBJ}
	cases := []struct {
		after string
		want  string
	}{
		{"2026-11-01 00:00", "2026-11-01 03:00"},
		{"2026-11-01 03:00", "2026-11-02 03:00"}, // 恰在拍点 → 次日
		{"2026-11-01 23:59", "2026-11-02 03:00"},
	}
	for _, c := range cases {
		got := tr.Next(parseBJ(c.after))
		if !got.Equal(parseBJ(c.want)) {
			t.Fatalf("Next(%s) = %s, want %s", c.after, ftShort(got), c.want)
		}
	}
}

func TestGridNextDST(t *testing.T) {
	step := 6 * time.Hour
	// 夏令时（太平洋 10:00 PDT = 北京 01:00）
	got := gridNext(time.Date(2026, 10, 31, 17, 0, 0, 0, locBJ), step)
	if !got.Equal(parseBJ("2026-10-31 19:00")) {
		t.Fatalf("PDT 网格下一拍 = %s, want 10-31 19:00", ftShort(got))
	}
	// 秋季回拨后（太平洋 10:00 PST = 北京 02:00）：19:00 触发重锚 → 当日 20:00 追赶拍
	got = gridNext(time.Date(2026, 11, 1, 19, 0, 0, 0, locBJ), step)
	if !got.Equal(parseBJ("2026-11-01 20:00")) {
		t.Fatalf("PST 重锚下一拍 = %s, want 11-01 20:00", ftShort(got))
	}
	got = gridNext(time.Date(2026, 11, 1, 20, 0, 0, 0, locBJ), step)
	if !got.Equal(parseBJ("2026-11-02 02:00")) {
		t.Fatalf("追赶拍后下一拍 = %s, want 11-02 02:00", ftShort(got))
	}
	if h := anchorHour(time.Date(2026, 11, 1, 13, 0, 0, 0, locBJ)); h != 1 {
		t.Fatalf("回拨前锚点 = %d, want 1", h)
	}
	if h := anchorHour(time.Date(2026, 11, 1, 19, 0, 0, 0, locBJ)); h != 2 {
		t.Fatalf("回拨后锚点 = %d, want 2", h)
	}
}
