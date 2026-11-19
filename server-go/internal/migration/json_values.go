package migration

import (
	"encoding/json"
	"fmt"
	"io"
	"strconv"
	"strings"
)

func migrationDecodeJSON(raw string) (any, error) {
	decoder := json.NewDecoder(strings.NewReader(raw))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			err = fmt.Errorf("配置含多段JSON值")
		}
		return nil, err
	}
	return value, nil
}

func migrationTruthy(value any) bool {
	switch v := value.(type) {
	case nil:
		return false
	case bool:
		return v
	case string:
		return v != ""
	case []any:
		return len(v) > 0
	case map[string]any:
		return len(v) > 0
	case json.Number:
		number, err := strconv.ParseFloat(v.String(), 64)
		return err != nil || number != 0
	default:
		return true
	}
}

func migrationString(value any) string {
	switch v := value.(type) {
	case nil:
		return "None"
	case string:
		return v
	case bool:
		if v {
			return "True"
		}
		return "False"
	case json.Number:
		return v.String()
	default:
		if raw, err := json.Marshal(v); err == nil {
			return string(raw)
		}
		return fmt.Sprint(v)
	}
}
