package agent

import "holdexar/server-go/internal/store"

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

func pyStrDefault(v any, def string) string {
	if s := pyStrOr(v); s != "" {
		return s
	}
	return def
}

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
	var gate *store.WriteGate
	if c.deps != nil {
		gate = c.deps.WriteGate
	}
	saved, err := UpsertMemory(c.Ctx, c.db, gate, category, key, value, 1.0, "user_explicit")
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
