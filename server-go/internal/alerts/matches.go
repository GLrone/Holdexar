package alerts

import (
	"context"
	"database/sql"
	"encoding/json"
	"time"

	"holdexar/server-go/internal/notifications"
)

type evaluationMatch struct {
	RuleID    int64
	Result    map[string]any
	EventType string
	Metadata  map[string]any
}
type triggerRecord struct {
	RuleID  int64
	Details map[string]any
}

func matchKey(current map[string]any, eventType string) string {
	if eventType == "FEATURED" {
		key, _ := jsonText([]any{current["appid"], current["region"], current["subId"], current["currency"], current["observationId"]})
		return key
	}
	return observationKey(current)
}
func appendMatchesTx(ctx context.Context, conn *sql.Conn, matches []evaluationMatch, now time.Time) ([]triggerRecord, error) {
	records := []triggerRecord{}
	if len(matches) == 0 {
		return records, nil
	}
	requests := []map[string]any{}
	for _, m := range matches {
		requests = append(requests, map[string]any{"id": m.RuleID, "key": matchKey(obj(m.Result["observed"]), m.EventType)})
	}
	raw, e := jsonText(requests)
	if e != nil {
		return nil, e
	}
	existing, e := maps(ctx, conn, `SELECT alert_id,json_extract(details_json,'$.observationKey') AS key FROM alert_events WHERE EXISTS(SELECT 1 FROM json_each(?) j WHERE j.value->>'id'=alert_id AND j.value->>'key'=json_extract(details_json,'$.observationKey'))`, raw)
	if e != nil {
		return nil, e
	}
	seen := map[string]bool{}
	for _, r := range existing {
		seen[text(r["alert_id"])+":"+text(r["key"])] = true
	}
	payloads := []map[string]any{}
	for _, m := range matches {
		current := obj(m.Result["observed"])
		key := matchKey(current, m.EventType)
		identity := text(m.RuleID) + ":" + key
		if seen[identity] {
			continue
		}
		seen[identity] = true
		details := copyMap(m.Metadata)
		var ruleID any
		if m.RuleID != 0 {
			ruleID = m.RuleID
		}
		details["ruleId"] = ruleID
		details["observationKey"] = key
		details["eventType"] = m.EventType
		details["evaluation"] = m.Result
		details["labels"] = m.Result["labels"]
		for _, field := range []string{"price", "currency", "subId", "observedAt", "originalPrice", "discount", "gameName", "headerImage"} {
			details[field] = current[field]
		}
		payloads = append(payloads, map[string]any{"alert": m.RuleID, "appid": current["appid"], "region": current["region"], "price": current["price"], "cny": current["cnyFen"], "details": details})
	}
	if len(payloads) == 0 {
		return records, nil
	}
	raw, e = jsonText(payloads)
	if e != nil {
		return nil, e
	}
	events, e := maps(ctx, conn, `INSERT INTO alert_events(alert_id,appid,region,price,price_cny,triggered_at,notified,details_json) SELECT value->>'alert',value->>'appid',value->>'region',value->>'price',value->>'cny',?,0,value->'details' FROM json_each(?) RETURNING id,alert_id,appid,region,details_json`, stamp(now), raw)
	if e != nil {
		return nil, e
	}
	facts := []notifications.Fact{}
	for _, event := range events {
		var payload map[string]any
		if e = json.Unmarshal([]byte(text(event["details_json"])), &payload); e != nil {
			return nil, e
		}
		id, _ := number(event["id"])
		appid, _ := number(event["appid"])
		ruleID, _ := number(event["alert_id"])
		region := text(event["region"])
		facts = append(facts, notifications.Fact{ID: int64(id), AppID: int64(appid), Region: &region, EventType: text(payload["eventType"]), Payload: payload, OccurredAt: now})
		records = append(records, triggerRecord{RuleID: int64(ruleID), Details: payload})
	}
	if _, e = notifications.EnqueueFactsTx(ctx, conn, "alert_event", facts, now); e != nil {
		return nil, e
	}
	return records, nil
}
