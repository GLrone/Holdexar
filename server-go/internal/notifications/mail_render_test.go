package notifications

import (
	"context"
	"strings"
	"testing"
)

func TestDigestKeepsLayoutAndEscapesStoredFacts(t *testing.T) {
	s, db := deliveryFixture(t, nil)
	_, e := db.Exec(`UPDATE notification_candidates SET payload_json='{"gameName":"<script>alert(1)</script>","price":3500,"currency":"CNY","eventType":"RULE_PRICE"}'`)
	if e != nil {
		t.Fatal(e)
	}
	rows, e := readCandidates(context.Background(), db, `ORDER BY id`)
	if e != nil {
		t.Fatal(e)
	}
	subject, body, e := s.render(context.Background(), rows, nil, map[string]any{"includeDetails": true})
	if e != nil {
		t.Fatal(e)
	}
	if subject != "Holdexar 本轮价格更新：1 条变化" || !strings.Contains(body, "max-width:600px") || !strings.Contains(body, "我的提醒") || strings.Contains(body, "<script>") {
		t.Fatalf("%s %s", subject, body)
	}
	if !strings.Contains(MailText(body), "¥35.00") {
		t.Fatal(MailText(body))
	}
}

func TestPriceDigestReadsExistingRegionNames(t *testing.T) {
	s, db := deliveryFixture(t, nil)
	seedPriceEvents(t, db, 1)
	if _, e := s.ResumeEventScans(context.Background(), nil); e != nil {
		t.Fatal(e)
	}
	rows, e := readCandidates(context.Background(), db, `WHERE source_kind='price_event'`)
	if e != nil || len(rows) != 1 {
		t.Fatalf("%d %v", len(rows), e)
	}
	_, body, e := s.render(context.Background(), rows, nil, nil)
	if e != nil {
		t.Fatal(e)
	}
	if !strings.Contains(MailText(body), "中国") {
		t.Fatal(MailText(body))
	}
}
