package clashcfg

import (
	"fmt"
)

func Truthy(v any) bool { return truthy(v) }

func truthy(v any) bool {
	switch val := v.(type) {
	case nil:
		return false
	case bool:
		return val
	case int:
		return val != 0
	case int64:
		return val != 0
	case float64:
		return val != 0
	case string:
		return val != ""
	case []any:
		return len(val) > 0
	case map[string]any:
		return len(val) > 0
	case *Doc:
		return val.Len() > 0
	default:
		return true
	}
}

func pyStr(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	if b, ok := v.(bool); ok {
		if b {
			return "True"
		}
		return "False"
	}
	return fmt.Sprint(v)
}

func toStringSlice(v any) []string {
	items, ok := v.([]any)
	if !ok {
		if truthy(v) {
			return []string{pyStr(v)}
		}
		return nil
	}
	out := make([]string, 0, len(items))
	for _, item := range items {
		out = append(out, pyStr(item))
	}
	return out
}

func toAnySlice(items []string) []any {
	out := make([]any, 0, len(items))
	for _, item := range items {
		out = append(out, item)
	}
	return out
}

func toAnySliceMaps(items []map[string]any) []any {
	out := make([]any, 0, len(items))
	for _, item := range items {
		out = append(out, item)
	}
	return out
}

func dedupStrings(items []string) []string {
	seen := map[string]bool{}
	out := make([]string, 0, len(items))
	for _, item := range items {
		if !seen[item] {
			seen[item] = true
			out = append(out, item)
		}
	}
	return out
}

func firstOf(groupIDs, nodeIDs []int) int {
	if len(groupIDs) > 0 {
		return groupIDs[0]
	}
	return nodeIDs[0]
}

func deepCopyMap(m map[string]any) map[string]any {
	out, _ := copyYAMLMapWithLimit(m, 0, nil)
	return out
}

func deepCopyAny(v any) any {
	out, _ := copyYAMLValueWithLimit(v, 0, nil)
	return out
}

func copyYAMLMapWithLimit(m map[string]any, depth int, budget *yamlWalkBudget) (map[string]any, error) {
	if budget != nil {
		if err := budget.enter(depth); err != nil {
			return nil, err
		}
	}
	out := make(map[string]any, len(m))
	for k, v := range m {
		copied, err := copyYAMLValueWithLimit(v, depth+1, budget)
		if err != nil {
			return nil, err
		}
		out[k] = copied
	}
	return out, nil
}

func copyYAMLValueWithLimit(v any, depth int, budget *yamlWalkBudget) (any, error) {
	if budget != nil {
		if err := budget.enter(depth); err != nil {
			return nil, err
		}
	}
	switch val := v.(type) {
	case map[string]any:
		return copyYAMLMapWithLimit(val, depth, budget)
	case []any:
		out := make([]any, len(val))
		for i, item := range val {
			copied, err := copyYAMLValueWithLimit(item, depth+1, budget)
			if err != nil {
				return nil, err
			}
			out[i] = copied
		}
		return out, nil
	default:
		return val, nil
	}
}

func providerKeys(providers map[string]map[string]any) []string {
	out := make([]string, 0, len(providers))
	for name := range providers {
		out = append(out, name)
	}
	sortStrings(out)
	return out
}

func sortStrings(items []string) {
	for i := 1; i < len(items); i++ {
		for j := i; j > 0 && items[j] < items[j-1]; j-- {
			items[j], items[j-1] = items[j-1], items[j]
		}
	}
}

func docFromMap(m map[string]any) *Doc {
	out := NewDoc()
	for _, key := range sortedMapKeys(m) {
		out.Set(key, m[key])
	}
	return out
}

func docToMap(d *Doc) map[string]any {
	out := make(map[string]any, d.Len())
	for _, key := range d.Keys() {
		v, _ := d.Get(key)
		out[key] = docToAny(v)
	}
	return out
}

func docToAny(v any) any {
	switch val := v.(type) {
	case *Doc:
		return docToMap(val)
	case []any:
		out := make([]any, len(val))
		for i, item := range val {
			out[i] = docToAny(item)
		}
		return out
	default:
		return val
	}
}
