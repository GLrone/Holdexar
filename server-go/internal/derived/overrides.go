package derived

import (
	"encoding/json"
	"io"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

func LoadSeriesOverrides(dataDir string) (map[string][]int64, []string) {
	empty := func() (map[string][]int64, []string) { return map[string][]int64{}, nil }
	file, err := os.Open(filepath.Join(dataDir, "series_overrides.json"))
	if err != nil {
		return empty()
	}
	defer file.Close()
	decoder := json.NewDecoder(file)
	decoder.UseNumber()
	start, err := decoder.Token()
	if err != nil || start != json.Delim('{') {
		return empty()
	}
	raw := map[string]any{}
	var rawOrder []string
	for decoder.More() {
		key, err := decoder.Token()
		if err != nil {
			return empty()
		}
		var value any
		if err := decoder.Decode(&value); err != nil {
			return empty()
		}
		name := key.(string)
		if _, exists := raw[name]; !exists {
			rawOrder = append(rawOrder, name)
		}
		raw[name] = value
	}
	if end, err := decoder.Token(); err != nil || end != json.Delim('}') {
		return empty()
	}
	if _, err := decoder.Token(); err != io.EOF {
		return empty()
	}
	out := map[string][]int64{}
	var order []string
	for _, key := range rawOrder {
		name := strings.TrimSpace(key)
		runes := []rune(name)
		if len(runes) > 100 {
			name = string(runes[:100])
		}
		items, ok := raw[key].([]any)
		if !ok || name == "" {
			continue
		}
		set := map[int64]bool{}
		for _, item := range items {
			if id, ok := overrideID(item); ok && id > 0 {
				set[id] = true
			}
		}
		if len(set) == 0 {
			continue
		}
		ids := make([]int64, 0, len(set))
		for id := range set {
			ids = append(ids, id)
		}
		sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
		if _, exists := out[name]; !exists {
			order = append(order, name)
		}
		out[name] = ids
	}
	return out, order
}

func overrideID(value any) (int64, bool) {
	switch value := value.(type) {
	case bool:
		if value {
			return 1, true
		}
		return 0, true
	case string:
		id, err := strconv.ParseInt(strings.TrimSpace(value), 10, 64)
		return id, err == nil
	case json.Number:
		if id, err := value.Int64(); err == nil {
			return id, true
		}
		if number, err := value.Float64(); err == nil && !math.IsNaN(number) && !math.IsInf(number, 0) && number >= math.MinInt64 && number < 9223372036854775808.0 {
			return int64(number), true
		}
	}
	return 0, false
}
