package alerts

import (
	"context"
	"database/sql"
	"fmt"
	"sort"

	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/notifications"
	"holdexar/server-go/internal/store"
)

func boolean(v any) bool {
	if b, ok := v.(bool); ok {
		return b
	}
	n, ok := number(v)
	return ok && n != 0
}
func memberships(ctx context.Context, q query, currents []map[string]any) (map[int64]map[string]any, error) {
	members := map[int64]map[string]any{}
	ids := []int64{}
	for _, current := range currents {
		id, _ := number(current["appid"])
		ids = append(ids, int64(id))
		members[int64(id)] = map[string]any{"sources": []any{}, "owned": false, "wishlisted": false, "ignored": false, "priority": 0}
	}
	if len(ids) == 0 {
		return members, nil
	}
	raw, e := jsonText(ids)
	if e != nil {
		return nil, e
	}
	sources, e := maps(ctx, q, `SELECT target_id,source,priority FROM monitor_sources WHERE target_type='game' AND active=1 AND target_id IN(SELECT value FROM json_each(?))`, raw)
	if e != nil {
		return nil, e
	}
	for _, row := range sources {
		id, _ := number(row["target_id"])
		member := members[int64(id)]
		member["sources"] = append(member["sources"].([]any), row["source"])
		priority, _ := number(row["priority"])
		current, _ := number(member["priority"])
		if priority > current {
			member["priority"] = priority
		}
	}
	excluded, e := maps(ctx, q, `SELECT target_id FROM monitor_exclusions WHERE target_type='game' AND active=1 AND target_id IN(SELECT value FROM json_each(?))`, raw)
	if e != nil {
		return nil, e
	}
	for _, row := range excluded {
		id, _ := number(row["target_id"])
		members[int64(id)]["ignored"] = true
	}
	accounts, e := maps(ctx, q, `SELECT appid,owned,wishlisted FROM wishlist_items WHERE active=1 AND appid IN(SELECT value FROM json_each(?))`, raw)
	if e != nil {
		return nil, e
	}
	for _, row := range accounts {
		id, _ := number(row["appid"])
		member := members[int64(id)]
		member["owned"] = boolean(member["owned"]) || boolean(row["owned"])
		member["wishlisted"] = boolean(member["wishlisted"]) || boolean(row["wishlisted"])
	}
	return members, nil
}
func featuredValue(result map[string]any, sortKey string) float64 {
	current, member := obj(result["observed"]), obj(result["membership"])
	n := func(key string) float64 { v, _ := number(current[key]); return v }
	switch sortKey {
	case "price":
		return n("price")
	case "savings":
		return -(n("originalPrice") - n("price"))
	case "quality":
		return -n("positiveRate")
	case "followed":
		v, _ := number(member["priority"])
		return -v
	case "wishlist":
		if member["wishlisted"] == true {
			return -1
		}
		for _, source := range sourceList(member["sources"]) {
			if source == "family_wishlist" {
				return -1
			}
		}
		return 0
	default:
		return -n("discount")
	}
}
func (s *Service) featuredResults(ctx context.Context, q query, config map[string]any, region string, ids []int64) ([]map[string]any, error) {
	currents, e := inputs(ctx, q, ids, region, sourceList(config["scope"]))
	if e != nil {
		return nil, e
	}
	members, e := memberships(ctx, q, currents)
	if e != nil {
		return nil, e
	}
	histories, e := derived.HistoryContext(ctx, q, currents, s.now(), window(map[string]any{"config": config}))
	if e != nil {
		return nil, e
	}
	results := []map[string]any{}
	for _, current := range currents {
		history := histories[derived.ComparisonKey(current)]
		current["currentKnown"] = history["currentKnown"] == true
		current["observationId"] = history["observationId"]
		id, _ := number(current["appid"])
		result, e := EvaluateFeatured(config, current, history, members[int64(id)])
		if e != nil {
			return nil, e
		}
		results = append(results, result)
	}
	sort.SliceStable(results, func(i, j int) bool {
		a, b := results[i], results[j]
		if a["matched"] != b["matched"] {
			return a["matched"] == true
		}
		av, bv := featuredValue(a, text(config["sort"])), featuredValue(b, text(config["sort"]))
		if av != bv {
			return av < bv
		}
		ai, _ := number(obj(a["observed"])["appid"])
		bi, _ := number(obj(b["observed"])["appid"])
		return ai < bi
	})
	return results, nil
}
func featuredConfig(raw map[string]any) map[string]any {
	if value, exists := raw["featured"]; exists {
		return obj(value)
	}
	if value, exists := raw["config"]; exists {
		return obj(value)
	}
	return raw
}
func previewAppIDs(raw map[string]any) ([]int64, error) {
	value, exists := raw["appids"]
	if !exists || value == nil {
		return nil, nil
	}
	list, ok := value.([]any)
	if !ok {
		return nil, fmt.Errorf("游戏编号须为列表")
	}
	ids := []int64{}
	for _, v := range list {
		n, ok := number(v)
		if !ok || float64(int64(n)) != n {
			return nil, fmt.Errorf("游戏编号须为整数")
		}
		ids = append(ids, int64(n))
	}
	return ids, nil
}
func (s *Service) PreviewFeatured(ctx context.Context, raw map[string]any) (map[string]any, error) {
	prefs, e := notifications.LoadPrefs(ctx, s.db)
	if e != nil {
		return nil, e
	}
	config, e := notifications.ValidateFeatured(featuredConfig(raw))
	if e != nil {
		return nil, e
	}
	selected, e := notifications.EffectiveRegion(ctx, s.db, text(raw["region"]), prefs, nil)
	if e != nil {
		return nil, e
	}
	if selected.Region == nil {
		return map[string]any{"items": []map[string]any{}, "matched": 0, "total": 0, "region": selected, "config": config, "exclusions": []map[string]any{{"code": "region_unknown"}}}, nil
	}
	ids, e := previewAppIDs(raw)
	if e != nil {
		return nil, e
	}
	results, e := s.featuredResults(ctx, s.db, config, *selected.Region, ids)
	if e != nil {
		return nil, e
	}
	matched := 0
	for _, result := range results {
		if result["matched"] == true {
			matched++
		}
	}
	return map[string]any{"items": results, "matched": matched, "total": len(results), "region": selected, "config": config}, nil
}
func (s *Service) CheckFeatured(ctx context.Context, ids []int64) (int, error) {
	count := 0
	e := s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		prefs, e := notifications.LoadPrefs(ctx, conn)
		if e != nil {
			return e
		}
		if !notifications.TypeEnabled(prefs, "FEATURED") {
			return nil
		}
		config, e := notifications.ValidateFeatured(obj(prefs["featured"]))
		if e != nil {
			return e
		}
		selected, e := notifications.EffectiveRegion(ctx, conn, "", prefs, nil)
		if e != nil {
			return e
		}
		if selected.Region == nil {
			s.opts.Log("精选通知地区尚未确定，本轮保留价格结果")
			return nil
		}
		results, e := s.featuredResults(ctx, conn, config, *selected.Region, ids)
		if e != nil {
			return e
		}
		matches := []evaluationMatch{}
		for _, result := range results {
			if result["matched"] == true {
				matches = append(matches, evaluationMatch{Result: result, EventType: "FEATURED", Metadata: map[string]any{"featured": config}})
			}
		}
		records, e := appendMatchesTx(ctx, conn, matches, s.now())
		count = len(records)
		return e
	})
	if e == nil {
		s.after(ctx, count)
	}
	return count, e
}
