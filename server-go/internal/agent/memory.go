//go:build windows

// memory.go 用户长期画像偏好账本（agent_memories）：查询/写入/画像装配/
// 从文本启发式提取。语义对齐 agent/memory.py。
package agent

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"time"
)

func newMemoryID() string {
	buf := make([]byte, 16)
	_, _ = rand.Read(buf)
	return hex.EncodeToString(buf)
}

func memoryStamp() string {
	return time.Now().UTC().Add(8 * time.Hour).Format("2006-01-02 15:04:05.000000")
}

// UpsertMemory 写入或更新记忆项（category+key 唯一确定一行）。
func UpsertMemory(ctx context.Context, db *sql.DB, category, key string,
	value any, confidence float64, source string) (map[string]any, error) {
	if key == "" {
		return nil, fmt.Errorf("memory key required")
	}
	// Python val_dict = value if isinstance(value, dict) else {"val": value}
	wrapped := value
	if _, ok := value.(map[string]any); !ok {
		wrapped = map[string]any{"val": value}
	}
	payload, err := json.Marshal(wrapped)
	if err != nil {
		return nil, err
	}
	ts := memoryStamp()
	id := newMemoryID()
	_, err = db.ExecContext(ctx, `
INSERT INTO agent_memories(id,category,"key",value,confidence,source,created_at,updated_at)
VALUES(?,?,?,?,?,?,?,?)
ON CONFLICT(category,"key") DO UPDATE SET
	value=excluded.value, confidence=excluded.confidence,
	source=CASE WHEN excluded.source!='' THEN excluded.source ELSE agent_memories.source END,
	updated_at=excluded.updated_at`,
		id, category, key, string(payload), confidence, source, ts, ts)
	if err != nil {
		return nil, err
	}
	return memoryRow(ctx, db, category, key)
}

func memoryRow(ctx context.Context, db *sql.DB, category, key string) (map[string]any, error) {
	var id, value, createdAt, updatedAt string
	var confidence float64
	var source sql.NullString
	err := db.QueryRowContext(ctx,
		`SELECT id, value, confidence, COALESCE(source,''), created_at, updated_at
		 FROM agent_memories WHERE category=? AND "key"=?`, category, key).
		Scan(&id, &value, &confidence, &source, &createdAt, &updatedAt)
	if err != nil {
		return nil, err
	}
	var parsed any
	_ = json.Unmarshal([]byte(value), &parsed)
	return map[string]any{
		"id": id, "category": category, "key": key, "value": parsed,
		"confidence": confidence, "source": source.String,
		"created_at": createdAt, "updated_at": updatedAt,
	}, nil
}

// ListMemories 记忆清单（按 category 与 key 排序）。
func ListMemories(ctx context.Context, db *sql.DB) ([]map[string]any, error) {
	rows, err := db.QueryContext(ctx, `
SELECT id, category, "key", value, confidence, COALESCE(source,''), created_at, updated_at
FROM agent_memories ORDER BY category, "key"`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []map[string]any{}
	for rows.Next() {
		var id, category, key, value string
		var confidence float64
		var source, createdAt, updatedAt sql.NullString
		if err := rows.Scan(&id, &category, &key, &value, &confidence, &source, &createdAt, &updatedAt); err != nil {
			return nil, err
		}
		var parsed any
		_ = json.Unmarshal([]byte(value), &parsed)
		out = append(out, map[string]any{
			"id": id, "category": category, "key": key, "value": parsed,
			"confidence": confidence, "source": source.String,
			"created_at": createdAt.String, "updated_at": updatedAt.String,
		})
	}
	return out, rows.Err()
}

var profileLabels = map[string]string{
	"region_preference": "常用关注区服",
	"genre_preference":  "游戏类型与标签偏好",
	"budget_habit":      "价格与预算习惯",
	"user_profile":      "用户习惯",
}

// BuildProfileContext 高置信度偏好 → 系统提示背景文本（无偏好返回空）。
func BuildProfileContext(ctx context.Context, db *sql.DB) string {
	memories, err := ListMemories(ctx, db)
	if err != nil || len(memories) == 0 {
		return ""
	}
	grouped := map[string][]string{}
	order := []string{}
	for _, m := range memories {
		conf, _ := m["confidence"].(float64)
		if conf < 0.5 {
			continue
		}
		category, _ := m["category"].(string)
		key, _ := m["key"].(string)
		label := profileLabels[category]
		if label == "" {
			label = category
		}
		if _, seen := grouped[label]; !seen {
			order = append(order, label)
		}
		grouped[label] = append(grouped[label], fmt.Sprintf("%s: %s", key, preferenceValueText(m["value"])))
	}
	if len(order) == 0 {
		return ""
	}
	lines := []string{"【用户已知长期偏好】（仅供决策与回答参考，不作为硬性限制）："}
	for _, label := range order {
		lines = append(lines, fmt.Sprintf("- %s：%s", label, strings.Join(grouped[label], "; ")))
	}
	return strings.Join(lines, "\n")
}

// preferenceValueText 偏好值 → 平文本（与 Python 的 value 展开同语义）。
func preferenceValueText(value any) string {
	switch v := value.(type) {
	case map[string]any:
		if inner, ok := v["val"]; ok {
			return preferenceValueText(inner)
		}
		if items, ok := v["items"].([]any); ok {
			parts := make([]string, 0, len(items))
			for _, item := range items {
				parts = append(parts, preferenceValueText(item))
			}
			return strings.Join(parts, ", ")
		}
		parts := make([]string, 0, len(v))
		for k, item := range v {
			parts = append(parts, fmt.Sprintf("%s: %s", k, preferenceValueText(item)))
		}
		return strings.Join(parts, ", ")
	case []any:
		parts := make([]string, 0, len(v))
		for _, item := range v {
			parts = append(parts, preferenceValueText(item))
		}
		return strings.Join(parts, ", ")
	default:
		return fmt.Sprintf("%v", value)
	}
}

var regionNameMap = map[string]string{
	"阿根廷": "AR", "土耳其": "TR", "国区": "CN", "日区": "JP",
	"美区": "US", "港区": "HK", "俄区": "RU", "巴西": "BR",
}

var genreKeywords = []string{
	"肉鸽", "Roguelike", "魂类", "魂系", "二次元", "开放世界",
	"动作", "模拟经营", "策略", "解谜", "恐怖", "FPS", "独立游戏",
}

var budgetRe = regexp.MustCompile(`(?:预算|价格)(?:不超过|低于|在)\s*(\d+)\s*(?:块|元)?`)

// ExtractPreferencesFromText 从文本启发式提取显式偏好并写入记忆账本。
func ExtractPreferencesFromText(ctx context.Context, db *sql.DB, text, source string) []map[string]any {
	if text == "" {
		return nil
	}
	var extracted []map[string]any
	// 区服偏好：中文区名或区码词面
	seen := map[string]bool{}
	var regions []string
	for zh, code := range regionNameMap {
		if strings.Contains(text, zh) || regionCodeMatch(text, code) {
			if !seen[code] {
				seen[code] = true
				regions = append(regions, code)
			}
		}
	}
	if len(regions) > 0 {
		if rec, err := UpsertMemory(ctx, db, "region_preference", "preferred_regions",
			regions, 0.85, source); err == nil {
			extracted = append(extracted, rec)
		}
	}
	// 类型偏好
	var genres []string
	for _, g := range genreKeywords {
		if strings.Contains(text, g) {
			genres = append(genres, g)
		}
	}
	if len(genres) > 0 {
		if rec, err := UpsertMemory(ctx, db, "genre_preference", "preferred_genres",
			genres, 0.8, source); err == nil {
			extracted = append(extracted, rec)
		}
	}
	// 史低 / 预算偏好
	if strings.Contains(text, "史低") {
		if rec, err := UpsertMemory(ctx, db, "budget_habit", "deal_timing",
			"史低优先", 0.8, source); err == nil {
			extracted = append(extracted, rec)
		}
	}
	if m := budgetRe.FindStringSubmatch(text); m != nil {
		limit := pyIntOf(m[1])
		if rec, err := UpsertMemory(ctx, db, "budget_habit", "max_budget_cny",
			limit, 0.9, source); err == nil {
			extracted = append(extracted, rec)
		}
	}
	return extracted
}

// regionCodeMatch 区码作为独立词出现（Python \b{code}\b 的 ASCII 词界等价）。
func regionCodeMatch(text, code string) bool {
	for _, field := range strings.FieldsFunc(text, func(r rune) bool {
		return !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '_')
	}) {
		if strings.EqualFold(field, code) {
			return true
		}
	}
	return false
}

func pyIntOf(s string) int {
	n := 0
	for _, r := range s {
		if r < '0' || r > '9' {
			return 0
		}
		n = n*10 + int(r-'0')
	}
	return n
}

var _ = sha256.Sum256
