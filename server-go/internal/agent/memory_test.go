//go:build windows

package agent

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"holdexar/server-go/internal/store"
)

func memoryTestDB(t *testing.T) (context.Context, *sql.DB) {
	t.Helper()
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := store.InitializeFreshDatabase(ctx, db); err != nil {
		t.Fatal(err)
	}
	return ctx, db
}

// 记忆账本：写入/更新同键覆盖、清单排序、画像装配阈值。
func TestMemoryUpsertListProfile(t *testing.T) {
	ctx, db := memoryTestDB(t)

	rec, err := UpsertMemory(ctx, db, "region_preference", "preferred_regions",
		[]string{"AR", "TR"}, 0.85, "compact:s1")
	if err != nil {
		t.Fatal(err)
	}
	if rec["category"] != "region_preference" || rec["source"] != "compact:s1" {
		t.Fatalf("写入投影不符: %v", rec)
	}
	// 同键更新：值与来源覆盖，行数不变
	if _, err = UpsertMemory(ctx, db, "region_preference", "preferred_regions",
		[]string{"CN"}, 0.9, "user_explicit"); err != nil {
		t.Fatal(err)
	}
	list, err := ListMemories(ctx, db)
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 1 {
		t.Fatalf("同键应覆盖为一行: %d", len(list))
	}
	value, _ := list[0]["value"].(map[string]any)
	if value == nil || fmtValue(value["val"]) != `["CN"]` {
		t.Fatalf("覆盖值不符: %v", list[0]["value"])
	}
	if list[0]["source"] != "user_explicit" {
		t.Fatalf("来源应更新: %v", list[0]["source"])
	}
	// 低置信度不进画像；高置信度按标签分组
	if _, err = UpsertMemory(ctx, db, "genre_preference", "preferred_genres",
		[]string{"肉鸽"}, 0.3, "auto"); err != nil {
		t.Fatal(err)
	}
	profile := BuildProfileContext(ctx, db)
	if strings.Contains(profile, "肉鸽") {
		t.Fatalf("低置信度偏好不应进画像: %q", profile)
	}
	if !strings.Contains(profile, "常用关注区服") || !strings.Contains(profile, "preferred_regions: CN") {
		t.Fatalf("画像装配不符: %q", profile)
	}
}

// 启发式提取：区服/类型/史低/预算四类词面。
func TestExtractPreferencesFromText(t *testing.T) {
	ctx, db := memoryTestDB(t)
	text := "我只买阿根廷和土耳其区，喜欢肉鸽和魂类游戏，只在史低入手，预算不超过200元"
	extracted := ExtractPreferencesFromText(ctx, db, text, "compact:t")
	if len(extracted) != 4 {
		t.Fatalf("应提取 4 类偏好: %d", len(extracted))
	}
	cats := map[string]bool{}
	for _, rec := range extracted {
		cats[fmt.Sprintf("%v", rec["category"])] = true
	}
	for _, want := range []string{"region_preference", "genre_preference", "budget_habit"} {
		if !cats[want] {
			t.Fatalf("缺少类别 %s: %v", want, cats)
		}
	}
	list, _ := ListMemories(ctx, db)
	budget := map[string]any{}
	for _, m := range list {
		if m["key"] == "max_budget_cny" {
			budget = m
		}
	}
	if fmtValue(budget["value"]) != `{"val":200}` {
		t.Fatalf("预算提取值不符: %v", budget["value"])
	}
	// 空文本不提取
	if got := ExtractPreferencesFromText(ctx, db, "", "x"); got != nil {
		t.Fatalf("空文本应返回空: %v", got)
	}
}

func fmtValue(v any) string {
	raw, _ := json.Marshal(v)
	return string(raw)
}
