package notifications

import (
	"bytes"
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"fmt"
	"html/template"
	"sort"
	"strings"

	"holdexar/server-go/internal/config"
	"holdexar/server-go/internal/games"
)

//go:embed mail_layout.html
var mailLayout string
var mailTemplates = template.Must(template.New("mail").Parse(mailLayout))

func mailPart(name string, data any) (string, error) {
	var out bytes.Buffer
	e := mailTemplates.ExecuteTemplate(&out, name, data)
	return out.String(), e
}
func mailMap(raw sql.NullString) (map[string]any, error) {
	if !raw.Valid || raw.String == "null" {
		return map[string]any{}, nil
	}
	var out map[string]any
	d := json.NewDecoder(strings.NewReader(raw.String))
	d.UseNumber()
	e := d.Decode(&out)
	return out, e
}
func mailMinor(v any, currency string) string {
	if currency == "" {
		return ""
	}
	var amount int64
	switch n := v.(type) {
	case int64:
		amount = n
	case json.Number:
		var e error
		amount, e = n.Int64()
		if e != nil {
			return ""
		}
	default:
		return ""
	}
	return games.FormatMinorUnits(amount, currency)
}
func describePriceEvent(kind string, region *string, previous, current map[string]any, names map[string]string, game string) string {
	parts := []string{eventLabel(kind)}
	code := strings.ToUpper(pointerText(region))
	if code == "" {
		code = strings.ToUpper(stringValue(current["region"]))
	}
	if code != "" {
		label := names[code]
		if label == "" {
			label = code
		}
		parts = append(parts, label)
	}
	currency := CurrencyForRegion(strings.ToUpper(pointerText(region)))
	from, to := "", ""
	switch kind {
	case "PRICE_DROP", "PRICE_INCREASE":
		from, to = mailMinor(previous["price"], currency), mailMinor(current["price"], currency)
	case "NEW_HISTORICAL_LOW":
		from, to = mailMinor(previous["low"], currency), mailMinor(current["price"], currency)
	case "HISTORICAL_LOW_MATCH":
		v := previous["price"]
		if !truthy(v) {
			v = previous["low"]
		}
		from, to = mailMinor(v, currency), mailMinor(current["price"], currency)
	case "PERMANENT_PRICE_CHANGE":
		from, to = mailMinor(previous["originalPrice"], currency), mailMinor(current["originalPrice"], currency)
	case "REGION_LOCKED", "REGION_UNLOCKED", "PRICE_UNAVAILABLE", "PRICE_RESTORED":
		states := map[string]string{"ok": "可购买", "locked": "锁区", "missing": "待补抓", "blocked": "暂无价格"}
		from, to = states[stringValue(previous["status"])], states[stringValue(current["status"])]
	}
	change := []string{}
	if from != "" {
		change = append(change, from)
	}
	if to != "" {
		change = append(change, to)
	}
	if len(change) > 0 {
		parts = append(parts, strings.Join(change, " → "))
	}
	if game != "" {
		parts = append(parts, game)
	}
	return strings.Join(parts, " · ")
}
func (s *DeliveryService) render(ctx context.Context, candidates []DeliveryCandidate, cycleID *int64, prefs map[string]any) (string, string, error) {
	limit := min(len(candidates), 12)
	ids, priceIDs := []int64{}, []int64{}
	for _, c := range candidates[:limit] {
		if c.AppID != 0 {
			ids = append(ids, c.AppID)
		}
		if c.SourceKind == "price_event" {
			priceIDs = append(priceIDs, c.EventID)
		}
	}
	raw, e := deliveryJSON(ids)
	if e != nil {
		return "", "", e
	}
	rows, e := s.db.QueryContext(ctx, `SELECT appid,name FROM games WHERE appid IN(SELECT value FROM json_each(?))`, raw)
	if e != nil {
		return "", "", e
	}
	names := map[int64]string{}
	for rows.Next() {
		var id int64
		var name string
		if e = rows.Scan(&id, &name); e != nil {
			rows.Close()
			return "", "", e
		}
		names[id] = name
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return "", "", e
	}
	type snapshots struct{ previous, current map[string]any }
	events := map[int64]snapshots{}
	if len(priceIDs) > 0 {
		raw, e = deliveryJSON(priceIDs)
		if e != nil {
			return "", "", e
		}
		rows, e = s.db.QueryContext(ctx, `SELECT id,previous_json,current_json FROM price_events WHERE id IN(SELECT value FROM json_each(?))`, raw)
		if e != nil {
			return "", "", e
		}
		for rows.Next() {
			var id int64
			var p, c sql.NullString
			if e = rows.Scan(&id, &p, &c); e != nil {
				rows.Close()
				return "", "", e
			}
			prev, e := mailMap(p)
			if e != nil {
				rows.Close()
				return "", "", e
			}
			cur, e := mailMap(c)
			if e != nil {
				rows.Close()
				return "", "", e
			}
			events[id] = snapshots{prev, cur}
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return "", "", e
		}
	}
	regionNames := map[string]string{}
	rows, e = s.db.QueryContext(ctx, `SELECT code,name FROM crawl_regions`)
	if e == nil {
		for rows.Next() {
			var code, name string
			if e = rows.Scan(&code, &name); e != nil {
				break
			}
			regionNames[strings.ToUpper(code)] = name
		}
		if rows.Err() != nil {
			regionNames = map[string]string{}
		}
		rows.Close()
	} else {
		s.options.Log("读取地区名称失败，提醒保留地区代码")
	}
	details := []string{}
	for _, c := range candidates[:limit] {
		p := c.Payload
		game := stringValue(p["gameName"])
		if game == "" {
			game = names[c.AppID]
		}
		parts := []string{}
		switch c.SourceKind {
		case "alert_event":
			kind := stringValue(p["eventType"])
			if kind == "" {
				kind = "RULE_PRICE"
			}
			parts = append(parts, eventLabel(kind))
			if code := strings.ToUpper(stringValue(p["region"])); code != "" {
				name := regionNames[code]
				if name == "" {
					name = code
				}
				parts = append(parts, name)
			}
			if amount := mailMinor(p["price"], stringValue(p["currency"])); amount != "" {
				parts = append(parts, amount)
			}
			if game != "" {
				parts = append(parts, game)
			}
		case "fact_notice":
			if title := stringValue(p["title"]); title != "" {
				parts = append(parts, title)
			}
			labels, _ := p["labels"].([]any)
			for _, label := range labels {
				text := stringValue(label)
				if !containsText(parts, text) {
					parts = append(parts, text)
				}
			}
			if game != "" && !containsText(parts, game) {
				parts = append(parts, game)
			}
		default:
			ev := events[c.EventID]
			parts = append(parts, describePriceEvent(c.EventType, c.Region, ev.previous, ev.current, regionNames, game))
		}
		details = append(details, strings.Join(parts, " · "))
	}
	if v, exists := prefs["includeDetails"]; exists && !truthy(v) {
		details = nil
	}
	counts := map[string]int{}
	order := []string{}
	gamesSet := map[int64]bool{}
	deferred := 0
	for _, c := range candidates {
		label := eventLabel(c.EventType)
		for _, category := range categoryLabels {
			if category.key == c.Category {
				label = category.label
				break
			}
		}
		if counts[label] == 0 {
			order = append(order, label)
		}
		counts[label]++
		gamesSet[c.AppID] = true
		if cycleID == nil || c.CycleID != *cycleID {
			deferred++
		}
	}
	sort.SliceStable(order, func(i, j int) bool { return counts[order[i]] > counts[order[j]] })
	subtitle := fmt.Sprintf("这一轮价格刷新共产生 %d 条变化，涉及 %d 款游戏。", len(candidates), len(gamesSet))
	if deferred > 0 {
		subtitle += fmt.Sprintf("其中 %d 条来自更早的轮次（静默期顺延）。", deferred)
	}
	card, e := mailPart("hero", map[string]any{"Badge": template.HTML("&#9432;"), "Color": "#66c0f4", "Title": "本轮价格更新", "Subtitle": subtitle, "Eyebrow": "PRICE REFRESH"})
	if e != nil {
		return "", "", e
	}
	if len(order) > 0 {
		grid := `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 -6px;">`
		for i, label := range order {
			if i%2 == 0 {
				grid += "<tr>"
			}
			cell, e := mailPart("cell", map[string]any{"Label": label, "Value": fmt.Sprintf("%d 条", counts[label]), "Color": "#66c0f4"})
			if e != nil {
				return "", "", e
			}
			grid += cell
			if i%2 == 1 {
				grid += "</tr>"
			} else if i == len(order)-1 {
				grid += `<td width="50%">&nbsp;</td></tr>`
			}
		}
		card += `<tr><td style="padding:0 24px;">` + grid + `</table></td></tr>`
	}
	if len(details) > 0 {
		divider, e := mailPart("divider", nil)
		if e != nil {
			return "", "", e
		}
		bullets, e := mailPart("bullets", details)
		if e != nil {
			return "", "", e
		}
		card += divider + `<tr><td style="padding:14px 24px 4px;"><div style="font-size:12px;font-weight:700;letter-spacing:.4px;color:#8f98a0;">变化明细</div></td></tr>` + bullets
	}
	if len(candidates) > len(details) {
		notice, e := mailPart("notice", fmt.Sprintf("其余 %d 条变化不再逐条列出，可在游戏详情页查看。", len(candidates)-len(details)))
		if e != nil {
			return "", "", e
		}
		card += notice
	}
	notice, e := mailPart("notice", "同一轮的变化合并成一封邮件发送；静默期内产生的变化随下一轮一并送达。（"+s.options.Now().Format("2006-01-02 15:04")+"）")
	if e != nil {
		return "", "", e
	}
	card += notice
	subject := fmt.Sprintf("Holdexar 本轮价格更新：%d 条变化", len(candidates))
	body, e := mailPart("page", map[string]any{"App": config.AppName(), "Card": template.HTML(card), "Preheader": fmt.Sprintf("%s 本轮价格更新：%d 条变化", config.AppName(), len(candidates))})
	return subject, body, e
}
func containsText(list []string, text string) bool {
	for _, v := range list {
		if v == text {
			return true
		}
	}
	return false
}
func (s *DeliveryService) testMailHTML(cfg SMTPConfig) (string, error) {
	encryption := "STARTTLS"
	if cfg.UseSSL {
		encryption = "SSL/TLS"
	}
	return mailPart("test", map[string]any{"App": config.AppName(), "Host": cfg.Host, "Port": cfg.Port, "User": cfg.User, "To": cfg.ToAddr, "Encryption": encryption, "When": s.options.Now().Format("2006-01-02 15:04:05")})
}
