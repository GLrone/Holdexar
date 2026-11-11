package storebrowse

import "context"

func FollowParentOptions(ctx context.Context, items map[int64]Item, appids []int64, fetch func(context.Context, []int64) (map[int64]Item, error)) (map[int64]Item, error) {
	out := make(map[int64]Item, len(items))
	for id, item := range items {
		out[id] = item
	}
	var parents [][2]int64
	seen := map[int64]bool{}
	var missing []int64
	for _, id := range appids {
		item := items[id]
		if !IsVisible(item) || truth(item["is_free"]) || len(list(item["purchase_options"])) > 0 {
			continue
		}
		parent := value(obj(item["related_items"])["parent_appid"])
		if parent == 0 || parent == id {
			continue
		}
		parents = append(parents, [2]int64{id, parent})
		if _, known := items[parent]; !known && !seen[parent] {
			seen[parent] = true
			missing = append(missing, parent)
		}
	}
	for _, batch := range PlanBatches(missing, "us", "english", true, DefaultBatchSize) {
		fetched, err := fetch(ctx, batch)
		if err != nil {
			return nil, err
		}
		for id, item := range fetched {
			out[id] = item
		}
	}
	for _, pair := range parents {
		id, parent := pair[0], pair[1]
		item := out[parent]
		if !IsVisible(item) || (len(list(item["purchase_options"])) == 0 && len(obj(item["best_purchase_option"])) == 0) {
			continue
		}
		child := make(Item, len(items[id])+3)
		for key, value := range items[id] {
			child[key] = value
		}
		child["purchase_options"] = item["purchase_options"]
		child["best_purchase_option"] = item["best_purchase_option"]
		child["parent_appid"] = parent
		out[id] = child
	}
	return out, nil
}
