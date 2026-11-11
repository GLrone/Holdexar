//go:build windows

// prefs_handlers.go 偏好工具：用户长期偏好的查询与显式保存（agent_memory 账本）。
package agent

// getUserPreferences 用户已保存的长期偏好清单。
func getUserPreferences(c *CallContext) (*ToolResult, error) {
	memories, err := ListMemories(c.Ctx, c.db)
	if err != nil {
		return nil, err
	}
	items := make([]any, 0, len(memories))
	for _, m := range memories {
		obj := newObj()
		for _, key := range []string{"id", "category", "key", "value", "confidence", "source"} {
			obj.set(key, m[key])
		}
		items = append(items, obj)
	}
	out := newObj()
	out.set("kind", "user_preferences")
	out.set("items", items)
	out.set("count", len(items))
	return &ToolResult{Status: StatusOK, Data: out}, nil
}

// pyStrDefault 非空取值否则默认（category 缺省 preference）。
func pyStrDefault(v any, def string) string {
	if s := pyStrOr(v); s != "" {
		return s
	}
	return def
}

// setUserPreference 显式保存或更新一条长期偏好（来源标记 user_explicit）。
func setUserPreference(c *CallContext) (*ToolResult, error) {
	category := pyStrDefault(c.Args["category"], "preference")
	key := pyStrOr(c.Args["key"])
	value := c.Args["value"]
	if key == "" {
		out := newObj()
		out.set("kind", "empty")
		out.set("note", "key_required")
		return &ToolResult{Status: StatusEmpty, Data: out}, nil
	}
	saved, err := UpsertMemory(c.Ctx, c.db, category, key, value, 1.0, "user_explicit")
	if err != nil {
		return nil, err
	}
	out := newObj()
	out.set("kind", "user_preference_saved")
	item := newObj()
	for _, key := range []string{"id", "category", "key", "value", "confidence", "source"} {
		item.set(key, saved[key])
	}
	out.set("item", item)
	return &ToolResult{Status: StatusOK, Data: out}, nil
}
