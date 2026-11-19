package timeutil

import (
	"testing"
	"time"
)

func TestTimeutilLocationsAndFormats(t *testing.T) {
	if Beijing == nil {
		t.Fatal("Beijing location is nil")
	}
	now := Now()
	if _, offset := now.Zone(); offset != 8*3600 {
		t.Fatalf("Now() offset = %d, want %d", offset, 8*3600)
	}

	sample := time.Date(2026, time.November, 18, 9, 5, 6, 123456000, Beijing)
	if got := FormatMicro(sample); got != "2026-11-18 09:05:06.123456" {
		t.Fatalf("FormatMicro = %q", got)
	}
	if got := FormatSec(sample); got != "2026-11-18 09:05:06" {
		t.Fatalf("FormatSec = %q", got)
	}

	pac := Pacific()
	if pac == nil {
		t.Fatal("Pacific() returned nil")
	}
	summer := time.Date(2026, time.July, 15, 12, 0, 0, 0, time.UTC).In(pac)
	winter := time.Date(2026, time.January, 15, 12, 0, 0, 0, time.UTC).In(pac)
	if !summer.IsDST() || winter.IsDST() {
		t.Fatalf("unexpected Pacific DST flags: summer=%v winter=%v", summer.IsDST(), winter.IsDST())
	}
}
