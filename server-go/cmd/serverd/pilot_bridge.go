package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/agent"
	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/pilot"
	"holdexar/server-go/internal/store"
)

const (
	pilotBulkMaxItems      = 50
	pilotListToolsDescLen  = 56
	pilotEmptyAttachReason = "本会话没有这个附件：id 可能写错，或该附件属于另一段对话。"
)

var (
	pilotDelKeyRe = regexp.MustCompile(`^(alert|bill_import|follow):\d+$`)

	pilotNavTargets = map[string]string{
		"dashboard":    "/dashboard",
		"library":      "/library",
		"gamelib":      "/gamelib",
		"follows":      "/pool",
		"bundles":      "/bundles",
		"alerts":       "/alerts",
		"events":       "/events",
		"achievements": "/achievements",
		"family":       "/family",
		"bills":        "/bills",
		"rates":        "/rates",
		"toolbox":      "/toolbox",
		"crawl":        "/crawl",
		"proxies":      "/proxies",
		"fetch":        "/fetch",
		"logs":         "/logs",
		"settings":     "/settings",
	}

	pilotNavSections = map[string]map[string]bool{
		"gamelib": {"owned": true, "insights": true, "family": true, "play": true},
		"bills":   {"ledger": true, "topup": true, "cdk": true},
		"family":  {"contrib": true, "heat": true, "buy": true, "wish": true},
	}

	pilotUIComponents = map[string]string{
		"metric-grid":     "指标网格",
		"data-table":      "数据表格",
		"time-series":     "时间序列走势图",
		"coverage-matrix": "覆盖率矩阵",
		"status-board":    "状态看板",
	}
)

func pilotToolBridge(db *sql.DB, gate *store.WriteGate, mux *http.ServeMux, monitor *monitoring.Service) pilot.ToolBridge {
	runner, runnerErr := agent.NewPilotRunner(mux, db, &agent.Deps{WriteGate: gate})
	return func(ctx *pilot.CallCtx, name string, args map[string]any, guarded bool) map[string]any {
		if guarded && agent.IsWriteTool(name) {
			return map[string]any{
				"kind": "denied",
				"note": "bulk_guard",
				"via":  "propose_bulk",
			}
		}
		if args == nil {
			args = map[string]any{}
		}
		switch name {
		case "list_tools":
			return pilotMergedToolCatalog(ctx, runner, guarded)
		case "navigate":
			return pilotNavigate(stringOf(args["target"]), stringOf(args["section"]), jsonInt(args["appid"]))
		case "present_ui":
			return pilotPresentUI(args)
		case "read_attachment":
			return pilotReadAttachment(ctx, stringOf(args["id"]), jsonInt(args["start_line"]), jsonInt(args["end_line"]))
		case "find_deletables":
			return pilotFindDeletables(ctx, db)
		case "propose_delete":
			return pilotProposeDelete(ctx, args)
		case "propose_bulk":
			return pilotProposeBulk(ctx, db, args)
		case "start_task":
			return pilotStartTask(ctx, db, mux, args)
		case "add_follow":
			return pilotAddFollow(ctx, db, mux, monitor, jsonInt(args["appid"]))
		case "create_price_alert":
			return pilotCreateAlert(ctx, db, mux, args)
		case "update_price_alert":
			return pilotUpdatePriceAlert(ctx, db, mux, args)
		case "bundle_follow":
			return pilotBundleFollow(ctx, db, mux, args)
		case "region_toggle":
			return pilotRegionToggle(ctx, mux, args)
		case "retry_removed_game":
			return pilotRetryRemovedGame(ctx, db, mux, jsonInt(args["appid"]))
		case "monitor_include":
			return pilotMonitorInclude(ctx, db, mux, monitor, jsonInt(args["appid"]))
		case "acknowledge_update":
			return pilotAcknowledgeUpdate(ctx, mux)
		}
		if runnerErr == nil && runner != nil {
			if res, ok := runner.Execute(ctx.Ctx, name, args, guarded); ok {
				return res
			}
		}
		return map[string]any{"kind": "unknown_tool", "tool": name}
	}
}

func pilotMergedToolCatalog(ctx *pilot.CallCtx, runner *agent.PilotRunner, guarded bool) map[string]any {
	var out map[string]any
	if runner != nil {
		if res, ok := runner.Execute(ctx.Ctx, "list_tools", map[string]any{}, guarded); ok {
			out = res
		}
	}
	if out == nil {
		out = map[string]any{
			"kind":  "tool_catalog",
			"note":  "以下工具已解锁，本会话后续可直接调用，不必再次 list_tools。",
			"tools": []any{},
		}
	}
	existing, _ := out["tools"].([]any)
	seen := map[string]bool{}
	for _, raw := range existing {
		if m, ok := raw.(map[string]any); ok {
			seen[stringOf(m["name"])] = true
		}
	}
	core := agent.CoreToolNames()
	for _, def := range pilotExtraToolDefs {
		if core[def.name] || def.name == "list_tools" || seen[def.name] {
			continue
		}
		group := "read"
		if agent.IsWriteTool(def.name) {
			group = "write"
		}
		desc := strings.Join(strings.Fields(def.description), " ")
		if r := []rune(desc); len(r) > pilotListToolsDescLen {
			desc = string(r[:pilotListToolsDescLen])
		}
		existing = append(existing, map[string]any{
			"name":  def.name,
			"group": group,
			"desc":  desc,
		})
	}
	out["tools"] = existing
	out["count"] = len(existing)
	return out
}

func pilotNavigate(target, section string, appid int) map[string]any {
	if target == "game" {
		if appid <= 0 {
			return map[string]any{"kind": "navigate", "target": "", "path": ""}
		}
		return map[string]any{
			"kind":   "navigate",
			"target": "game",
			"path":   fmt.Sprintf("/game/%d", appid),
			"appid":  appid,
		}
	}
	path, ok := pilotNavTargets[target]
	if !ok {
		return map[string]any{"kind": "navigate", "target": "", "path": ""}
	}
	out := map[string]any{"kind": "navigate", "target": target, "path": path}
	if section != "" && pilotNavSections[target][section] {
		out["section"] = section
	}
	return out
}

func pilotPresentUI(args map[string]any) map[string]any {
	componentID := strings.TrimSpace(stringOf(args["component_id"]))
	componentName, ok := pilotUIComponents[componentID]
	if !ok {
		return map[string]any{
			"kind":   "denied",
			"reason": fmt.Sprintf("未知的 UI 组件类型 '%s'，只允许使用注册中心白名单内的组件。", componentID),
		}
	}
	density := stringOf(args["density"])
	if density != "compact" && density != "standard" && density != "expanded" {
		density = "standard"
	}
	title := strings.TrimSpace(stringOf(args["title"]))
	if title == "" {
		title = componentName
	}
	data, _ := args["data"].(map[string]any)
	if data == nil {
		data = map[string]any{}
	}
	buf := make([]byte, 4)
	_, _ = rand.Read(buf)
	return map[string]any{
		"id":          fmt.Sprintf("ui_%s_%s", componentID, hex.EncodeToString(buf)),
		"componentId": componentID,
		"version":     "1.0",
		"status":      "ready",
		"density":     density,
		"title":       title,
		"data":        data,
		"kind":        componentID,
	}
}

func pilotReadAttachment(ctx *pilot.CallCtx, aid string, startLine, endLine int) map[string]any {
	if ctx.Sid == "" || ctx.DataDir == "" || !pilot.ValidAID(aid) {
		return map[string]any{"kind": "empty", "note": "bad_request"}
	}
	got := pilot.AttachmentReadForTool(ctx.DataDir, ctx.Sid, aid, startLine, endLine)
	if got == nil {
		return map[string]any{"kind": "empty", "note": pilotEmptyAttachReason}
	}
	return got
}

func pilotFindDeletables(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	if ctx.Sid == "" || ctx.Store == nil {
		return map[string]any{"kind": "empty", "note": "no_session"}
	}
	items := []map[string]any{}
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT a.id, a.appid, g.appid IS NOT NULL, g.name, g.removed_at,
		       EXISTS(SELECT 1 FROM game_current_prices p WHERE p.appid = a.appid)
		FROM price_alerts a
		LEFT JOIN games g ON g.appid = a.appid
		ORDER BY a.id`)
	if err == nil {
		for rows.Next() {
			var alertID, appid int
			var hasGame, hasPrice bool
			var name, removedAt sql.NullString
			if rows.Scan(&alertID, &appid, &hasGame, &name, &removedAt, &hasPrice) != nil {
				continue
			}
			displayName := fmt.Sprintf("AppID %d", appid)
			if name.Valid && name.String != "" {
				displayName = name.String
			}
			key := fmt.Sprintf("alert:%d", alertID)
			switch {
			case !hasGame:
				items = append(items, map[string]any{"key": key, "appid": appid, "name": fmt.Sprintf("AppID %d", appid), "reason": "noGame"})
			case removedAt.Valid && removedAt.String != "":
				items = append(items, map[string]any{"key": key, "appid": appid, "name": displayName, "reason": "removed"})
			case !hasPrice:
				items = append(items, map[string]any{"key": key, "appid": appid, "name": displayName, "reason": "neverCrawled"})
			}
			if len(items) >= pilotBulkMaxItems {
				break
			}
		}
		rows.Close()
	}
	if len(items) < pilotBulkMaxItems {
		bRows, bErr := db.QueryContext(ctx.Ctx, `
			SELECT id, COALESCE(source_file,''), COALESCE(nickname,''), COALESCE(CAST(imported_at AS TEXT),'')
			FROM bill_imports ORDER BY id DESC`)
		if bErr == nil {
			seenFiles := map[string]bool{}
			for bRows.Next() {
				var id int
				var sourceFile, nickname, importedAt string
				if bRows.Scan(&id, &sourceFile, &nickname, &importedAt) != nil {
					continue
				}
				if sourceFile == "" {
					continue
				}
				if seenFiles[sourceFile] {
					label := strings.TrimSpace(nickname + " " + importedAt)
					if label == "" {
						label = fmt.Sprintf("#%d", id)
					}
					items = append(items, map[string]any{
						"key":    fmt.Sprintf("bill_import:%d", id),
						"name":   label,
						"reason": "dupImport",
					})
					if len(items) >= pilotBulkMaxItems {
						break
					}
				} else {
					seenFiles[sourceFile] = true
				}
			}
			bRows.Close()
		}
	}
	if len(items) == 0 {
		return map[string]any{"kind": "empty", "note": "nothing_to_delete"}
	}
	pid := proposalID("d")
	record := map[string]any{
		"pid": pid, "action": "delete", "items": items, "args": map[string]any{}, "state": "pending",
	}
	if !ctx.Store.AppendProposal(ctx.Sid, record) {
		return map[string]any{"kind": "empty", "note": "no_session"}
	}
	return map[string]any{
		"kind": "proposal", "pid": pid, "action": "delete",
		"items": items, "args": map[string]any{}, "state": "pending",
	}
}

func pilotProposeDelete(ctx *pilot.CallCtx, args map[string]any) map[string]any {
	if ctx.Sid == "" || ctx.Store == nil {
		return map[string]any{"kind": "empty", "note": "no_session"}
	}
	rawItems, _ := args["items"].([]any)
	clean := []map[string]any{}
	for _, raw := range rawItems {
		item, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		key := stringOf(item["key"])
		if !pilotDelKeyRe.MatchString(key) {
			continue
		}
		name := stringOf(item["name"])
		if name == "" {
			name = key
		}
		row := map[string]any{
			"key":    key,
			"name":   clipRunes(name, 80),
			"reason": clipRunes(stringOf(item["reason"]), 24),
		}
		if appid := jsonInt(item["appid"]); appid > 0 {
			row["appid"] = appid
		}
		clean = append(clean, row)
		if len(clean) >= pilotBulkMaxItems {
			break
		}
	}
	if len(clean) == 0 {
		return map[string]any{"kind": "empty", "note": "no_target"}
	}
	pid := proposalID("d")
	if !ctx.Store.AppendProposal(ctx.Sid, map[string]any{
		"pid": pid, "action": "delete", "items": clean, "args": map[string]any{}, "state": "pending",
	}) {
		return map[string]any{"kind": "empty", "note": "no_session"}
	}
	return map[string]any{
		"kind": "proposal", "pid": pid, "action": "delete",
		"items": clean, "args": map[string]any{}, "state": "pending",
	}
}

func pilotProposeBulk(ctx *pilot.CallCtx, db *sql.DB, args map[string]any) map[string]any {
	if ctx.Sid == "" || ctx.Store == nil {
		return map[string]any{"kind": "empty", "note": "no_session"}
	}
	action := stringOf(args["action"])
	if action != "add_follow" && action != "create_price_alert" {
		return map[string]any{"kind": "empty", "note": "bad_action"}
	}
	ids := extractBulkAppIDs(args)
	if len(ids) == 0 {
		return map[string]any{"kind": "empty", "note": "no_target"}
	}
	if len(ids) > pilotBulkMaxItems {
		return map[string]any{"kind": "empty", "note": "too_many", "data": map[string]any{"max": pilotBulkMaxItems}}
	}
	names := lookupGameNames(ctx.Ctx, db, ids)
	items := make([]map[string]any, 0, len(ids))
	for _, id := range ids {
		label := names[id]
		if label == "" {
			label = fmt.Sprintf("AppID %d", id)
		}
		items = append(items, map[string]any{"appid": id, "name": label})
	}
	propArgs := map[string]any{"target_type": stringOr(args["target_type"], "historic_low")}
	if yuan, ok := jsonFloat(args["target_value_yuan"]); ok {
		propArgs["target_value_yuan"] = yuan
	} else if fen := jsonInt(args["target_value_fen"]); fen > 0 {
		propArgs["target_value_yuan"] = float64(fen) / 100.0
	}
	pid := proposalID("p")
	if !ctx.Store.AppendProposal(ctx.Sid, map[string]any{
		"pid": pid, "action": action, "items": items, "args": propArgs, "state": "pending",
	}) {
		return map[string]any{"kind": "empty", "note": "no_session"}
	}
	return map[string]any{
		"kind": "proposal", "pid": pid, "action": action,
		"items": items, "args": propArgs, "state": "pending",
	}
}

func extractBulkAppIDs(args map[string]any) []int {
	ids := []int{}
	seen := map[int]bool{}
	add := func(id int) {
		if id > 0 && !seen[id] {
			seen[id] = true
			ids = append(ids, id)
		}
	}
	for _, raw := range anySliceOf(args["appids"]) {
		add(jsonInt(raw))
	}
	if len(ids) == 0 {
		for _, raw := range anySliceOf(args["items"]) {
			switch item := raw.(type) {
			case map[string]any:
				add(jsonInt(item["appid"]))
			default:
				add(jsonInt(item))
			}
		}
	}
	return ids
}

func pilotStartTask(ctx *pilot.CallCtx, db *sql.DB, mux *http.ServeMux, args map[string]any) map[string]any {
	kind := stringOf(args["kind"])
	if kind == "" {
		kind = stringOf(args["task"])
	}
	labels := map[string]string{
		"price_refresh": "priceRefresh",
		"price_repair":  "priceRepair",
		"price_missing": "priceMissing",
	}
	label, ok := labels[kind]
	if !ok {
		return map[string]any{
			"kind":     "rows",
			"titleKey": "tasks",
			"rows":     []map[string]any{{"k": "", "vKey": "taskUnknown", "tone": "bad", "v": ""}},
		}
	}
	if mux == nil {
		return map[string]any{"kind": "failed", "tool": "start_task", "note": "mux_unwired"}
	}
	params, _ := args["params"].(map[string]any)
	body := map[string]any{}
	switch kind {
	case "price_refresh":
		body["scope"] = "all"
		body["kind"] = "manual"
	case "price_missing", "price_repair":
		body["scope"] = "appids"
		if kind == "price_missing" {
			body["kind"] = "missing"
		} else {
			body["kind"] = "repair"
			var regions []string
			for _, raw := range anySliceOf(params["regions"]) {
				if s := strings.TrimSpace(stringOf(raw)); s != "" {
					regions = append(regions, s)
					if len(regions) >= 20 {
						break
					}
				}
			}
			if len(regions) > 0 {
				body["regions"] = regions
			}
			var appids []int
			for _, raw := range anySliceOf(params["appids"]) {
				if id := jsonInt(raw); id > 0 {
					appids = append(appids, id)
					if len(appids) >= 300 {
						break
					}
				}
			}
			if len(appids) > 0 {
				body["appids"] = appids
			}
		}
		if cooldown, ok := params["cooldown_minutes"]; ok && cooldown != nil {
			body["cooldown"] = jsonInt(cooldown)
		} else if kind == "price_repair" {
			body["cooldown"] = 0
		}
	}
	status, respBytes, err := muxJSON(ctx.Ctx, mux, http.MethodPost, "/api/v1/crawl/run", body)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "start_task", "note": err.Error()}
	}
	var payload map[string]any
	_ = json.Unmarshal(respBytes, &payload)
	if status != http.StatusOK {
		detail := clipRunes(stringOf(payload["detail"]), 60)
		return map[string]any{
			"kind":     "rows",
			"titleKey": "tasks",
			"rows": []map[string]any{{
				"k":       "",
				"kindKey": "pilot.task." + label,
				"vKey":    "taskBusy",
				"tone":    "warn",
				"v":       detail,
			}},
		}
	}
	var runID string
	if kind == "price_refresh" {
		if cid := jsonInt(payload["cycleId"]); cid > 0 {
			runID = fmt.Sprintf("cycle:%d", cid)
		}
	} else {
		if jid := jsonInt(payload["id"]); jid > 0 {
			runID = fmt.Sprintf("crawl:%d", jid)
		}
	}
	_ = db
	return map[string]any{
		"kind":     "rows",
		"titleKey": "tasks",
		"rows": []map[string]any{{
			"k":        "",
			"kindKey":  "pilot.task." + label,
			"vKey":     "taskRunning",
			"tone":     "ok",
			"v":        "",
			"taskKind": kind,
			"run_id":   runID,
		}},
		"total": 1,
	}
}

func pilotAddFollow(ctx *pilot.CallCtx, db *sql.DB, mux *http.ServeMux, monitor *monitoring.Service, appid int) map[string]any {
	if appid <= 0 {
		return map[string]any{"kind": "failed", "tool": "add_follow", "note": "appid 缺失"}
	}
	var state any
	if monitor != nil {
		off := false
		snap, err := monitor.Apply(ctx.Ctx, "game", []int64{int64(appid)},
			monitoring.Change{Excluded: &off, Sources: map[int64][]string{int64(appid): {"manual"}}})
		if err != nil {
			return map[string]any{"kind": "failed", "tool": "add_follow", "note": err.Error()}
		}
		state = snap
	} else if mux != nil {
		status, body, err := muxJSON(ctx.Ctx, mux, http.MethodPost, "/api/v1/monitoring/track",
			map[string]any{"target_type": "game", "target_id": appid, "source": "manual"})
		if err != nil || status >= 400 {
			return map[string]any{"kind": "failed", "tool": "add_follow", "note": muxErrDetail(status, body, err)}
		}
		var snap map[string]any
		_ = json.Unmarshal(body, &snap)
		state = snap
	} else {
		return map[string]any{"kind": "failed", "tool": "add_follow", "note": "monitor_unwired"}
	}
	var nameVal any
	if n := lookupSingleGameName(ctx.Ctx, db, appid); n != "" {
		nameVal = n
	}
	return map[string]any{
		"action": "monitor_add",
		"appid":  appid,
		"name":   nameVal,
		"state":  state,
	}
}

func pilotCreateAlert(ctx *pilot.CallCtx, db *sql.DB, mux *http.ServeMux, args map[string]any) map[string]any {
	appid := jsonInt(args["appid"])
	if appid <= 0 {
		return map[string]any{"kind": "failed", "tool": "create_price_alert", "note": "appid 缺失"}
	}
	if mux == nil {
		return map[string]any{"kind": "failed", "tool": "create_price_alert", "note": "mux_unwired"}
	}
	ttype := stringOr(args["target_type"], "price")
	var fenVal any
	reqBody := map[string]any{"appid": appid, "region": "CN", "targetType": ttype}
	if yuan, ok := jsonFloat(args["target_value_yuan"]); ok {
		fen := int(yuan*100 + 0.5)
		reqBody["targetValue"] = fen
		fenVal = fen
	} else if fen := jsonInt(args["target_value_fen"]); fen > 0 || (args["target_value_fen"] != nil && ttype == "price") {
		reqBody["targetValue"] = fen
		fenVal = fen
	}
	status, body, err := muxJSON(ctx.Ctx, mux, http.MethodPost, "/api/v1/alerts", reqBody)
	if err != nil || status >= 400 {
		return map[string]any{"kind": "failed", "tool": "create_price_alert", "note": muxErrDetail(status, body, err)}
	}
	var alert map[string]any
	_ = json.Unmarshal(body, &alert)
	var nameVal any
	if n := lookupSingleGameName(ctx.Ctx, db, appid); n != "" {
		nameVal = n
	}
	return map[string]any{
		"action":         "alert_add",
		"appid":          appid,
		"name":           nameVal,
		"targetType":     ttype,
		"targetValueFen": fenVal,
		"alertId":        alert["id"],
	}
}

func pilotUpdatePriceAlert(ctx *pilot.CallCtx, db *sql.DB, mux *http.ServeMux, args map[string]any) map[string]any {
	alertID := jsonInt(args["alert_id"])
	if alertID <= 0 {
		return map[string]any{"kind": "empty", "note": "alert_not_found"}
	}
	if mux == nil {
		return map[string]any{"kind": "failed", "tool": "update_price_alert", "note": "mux_unwired"}
	}
	patch := map[string]any{}
	if rawYuan, present := args["target_value_yuan"]; present && rawYuan != nil {
		yuan, ok := jsonFloat(rawYuan)
		if !ok {
			return map[string]any{"kind": "empty", "note": "bad_value"}
		}
		patch["targetValue"] = int(yuan*100 + 0.5)
	} else if fen, present := args["target_value_fen"]; present && fen != nil {
		patch["targetValue"] = jsonInt(fen)
	}
	var activeVal *bool
	if rawActive, present := args["active"]; present && rawActive != nil {
		if b, ok := rawActive.(bool); ok {
			patch["active"] = b
			activeVal = &b
		}
	}
	status, body, err := muxJSON(ctx.Ctx, mux, http.MethodPut, fmt.Sprintf("/api/v1/alerts/%d", alertID), patch)
	if status == http.StatusNotFound {
		return map[string]any{"kind": "empty", "note": "alert_not_found"}
	}
	if err != nil || status >= 400 {
		return map[string]any{"kind": "failed", "tool": "update_price_alert", "note": muxErrDetail(status, body, err)}
	}
	var updated map[string]any
	_ = json.Unmarshal(body, &updated)
	appid := jsonInt(updated["appid"])
	name := lookupSingleGameName(ctx.Ctx, db, appid)
	if name == "" {
		name = fmt.Sprintf("AppID %d", appid)
	}
	vKey, tone := "alertUpdated", "ok"
	if activeVal != nil {
		if !*activeVal {
			vKey, tone = "alertOff", "warn"
		} else {
			vKey, tone = "alertOn", "ok"
		}
	}
	return map[string]any{
		"kind":     "rows",
		"titleKey": "alerts",
		"rows": []map[string]any{{
			"k":    name,
			"vKey": vKey,
			"data": map[string]any{"priceFen": updated["targetValue"], "alertId": updated["id"]},
			"tone": tone,
		}},
		"total": 1,
	}
}

func pilotBundleFollow(ctx *pilot.CallCtx, db *sql.DB, mux *http.ServeMux, args map[string]any) map[string]any {
	bundleID := jsonInt(args["bundle_id"])
	if bundleID <= 0 && db != nil {
		if bname := strings.TrimSpace(stringOf(args["bundle_name"])); bname != "" {
			_ = db.QueryRowContext(ctx.Ctx, `SELECT bundle_id FROM bundles WHERE name = ? LIMIT 1`, bname).Scan(&bundleID)
		}
	}
	if bundleID <= 0 {
		return map[string]any{"kind": "empty", "note": "bundle_not_found"}
	}
	if mux == nil {
		return map[string]any{"kind": "failed", "tool": "bundle_follow", "note": "mux_unwired"}
	}
	follow := true
	if b, ok := args["follow"].(bool); ok {
		follow = b
	}
	method := http.MethodPut
	if !follow {
		method = http.MethodDelete
	}
	status, body, err := muxJSON(ctx.Ctx, mux, method, fmt.Sprintf("/api/v1/bundles/%d/follow", bundleID), nil)
	if status == http.StatusNotFound {
		return map[string]any{"kind": "empty", "note": "bundle_not_found"}
	}
	if err != nil || status >= 400 {
		return map[string]any{"kind": "failed", "tool": "bundle_follow", "note": muxErrDetail(status, body, err)}
	}
	var name string
	if db != nil {
		_ = db.QueryRowContext(ctx.Ctx, `SELECT COALESCE(name,'') FROM bundles WHERE bundle_id = ?`, bundleID).Scan(&name)
	}
	if name == "" {
		name = fmt.Sprintf("Bundle %d", bundleID)
	}
	vKey, tone := "bundleFollowed", "ok"
	if !follow {
		vKey, tone = "bundleUnfollowed", "warn"
	}
	return map[string]any{
		"kind":     "rows",
		"titleKey": "bundles",
		"rows": []map[string]any{{
			"k":    name,
			"vKey": vKey,
			"tone": tone,
		}},
		"total": 1,
	}
}

func pilotRegionToggle(ctx *pilot.CallCtx, mux *http.ServeMux, args map[string]any) map[string]any {
	code := strings.ToLower(strings.TrimSpace(stringOf(args["region_code"])))
	if code == "" {
		code = strings.ToLower(strings.TrimSpace(stringOf(args["region"])))
	}
	enable := true
	if b, ok := args["enable"].(bool); ok {
		enable = b
	}
	if mux == nil {
		return map[string]any{"kind": "failed", "tool": "region_toggle", "note": "mux_unwired"}
	}
	status, body, err := muxJSON(ctx.Ctx, mux, http.MethodGet, "/api/v1/regions", nil)
	if err != nil || status >= 400 {
		return map[string]any{"kind": "failed", "tool": "region_toggle", "note": muxErrDetail(status, body, err)}
	}
	var payload struct {
		Regions []struct {
			Code    string `json:"code"`
			Enabled bool   `json:"enabled"`
		} `json:"regions"`
	}
	if json.Unmarshal(body, &payload) != nil {
		return map[string]any{"kind": "failed", "tool": "region_toggle", "note": "decode_error"}
	}
	valid := map[string]bool{}
	current := map[string]bool{}
	for _, r := range payload.Regions {
		rc := strings.ToLower(strings.TrimSpace(r.Code))
		valid[rc] = true
		if r.Enabled {
			current[rc] = true
		}
	}
	if !valid[code] {
		return map[string]any{
			"kind":     "rows",
			"titleKey": "regions",
			"rows":     []map[string]any{{"k": strings.ToUpper(code), "vKey": "regionUnknown", "tone": "warn"}},
		}
	}
	if enable {
		current[code] = true
	} else {
		delete(current, code)
	}
	if len(current) == 0 {
		return map[string]any{
			"kind":     "rows",
			"titleKey": "regions",
			"rows":     []map[string]any{{"k": strings.ToUpper(code), "vKey": "regionMinOne", "tone": "warn"}},
		}
	}
	enabled := make([]string, 0, len(current))
	for rc := range current {
		enabled = append(enabled, rc)
	}
	slices.Sort(enabled)
	putStatus, putBody, putErr := muxJSON(ctx.Ctx, mux, http.MethodPut, "/api/v1/regions/enabled", map[string]any{"enabled": enabled})
	if putErr != nil || putStatus >= 400 {
		return map[string]any{"kind": "failed", "tool": "region_toggle", "note": muxErrDetail(putStatus, putBody, putErr)}
	}
	vKey, tone := "regionOn", "ok"
	if !enable {
		vKey, tone = "regionOff", "warn"
	}
	return map[string]any{
		"kind":     "rows",
		"titleKey": "regions",
		"rows":     []map[string]any{{"k": strings.ToUpper(code), "vKey": vKey, "tone": tone}},
	}
}

func pilotRetryRemovedGame(ctx *pilot.CallCtx, db *sql.DB, mux *http.ServeMux, appid int) map[string]any {
	if appid <= 0 {
		return map[string]any{"kind": "empty", "note": "not_removed"}
	}
	if mux == nil {
		return map[string]any{"kind": "failed", "tool": "retry_removed_game", "note": "mux_unwired"}
	}
	status, body, err := muxJSON(ctx.Ctx, mux, http.MethodPost, fmt.Sprintf("/api/v1/games/%d/retry-removed", appid), nil)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "retry_removed_game", "note": err.Error()}
	}
	if status == http.StatusNotFound || status == http.StatusBadRequest || status == http.StatusConflict {
		return map[string]any{"kind": "empty", "note": "not_removed"}
	}
	if status >= 400 {
		return map[string]any{"kind": "failed", "tool": "retry_removed_game", "note": muxErrDetail(status, body, nil)}
	}
	name := lookupSingleGameName(ctx.Ctx, db, appid)
	if name == "" {
		name = fmt.Sprintf("AppID %d", appid)
	}
	return map[string]any{
		"kind":     "rows",
		"titleKey": "retry",
		"rows":     []map[string]any{{"k": name, "vKey": "retryStarted", "tone": "ok"}},
		"total":    1,
	}
}

func pilotMonitorInclude(ctx *pilot.CallCtx, db *sql.DB, mux *http.ServeMux, monitor *monitoring.Service, appid int) map[string]any {
	if appid <= 0 {
		return map[string]any{"kind": "failed", "tool": "monitor_include", "note": "appid 缺失"}
	}
	var stateVal string
	if monitor != nil {
		excluded := false
		snap, err := monitor.Apply(ctx.Ctx, "game", []int64{int64(appid)},
			monitoring.Change{Excluded: &excluded})
		if err != nil {
			return map[string]any{"kind": "failed", "tool": "monitor_include", "note": err.Error()}
		}
		raw, _ := json.Marshal(snap)
		stateVal = string(raw)
	} else if mux != nil {
		status, body, err := muxJSON(ctx.Ctx, mux, http.MethodPost, "/api/v1/monitoring/include",
			map[string]any{"target_type": "game", "target_id": appid})
		if err != nil || status >= 400 {
			return map[string]any{"kind": "failed", "tool": "monitor_include", "note": muxErrDetail(status, body, err)}
		}
		stateVal = strings.TrimSpace(string(body))
	} else {
		return map[string]any{"kind": "failed", "tool": "monitor_include", "note": "monitor_unwired"}
	}
	name := lookupSingleGameName(ctx.Ctx, db, appid)
	if name == "" {
		name = fmt.Sprintf("AppID %d", appid)
	}
	return map[string]any{
		"kind":     "rows",
		"titleKey": "include",
		"rows":     []map[string]any{{"k": name, "vKey": "includeDone", "v": stateVal, "tone": "ok"}},
		"total":    1,
	}
}

func pilotAcknowledgeUpdate(ctx *pilot.CallCtx, mux *http.ServeMux) map[string]any {
	if mux == nil {
		return map[string]any{"kind": "failed", "tool": "acknowledge_update", "note": "mux_unwired"}
	}
	status, body, err := muxJSON(ctx.Ctx, mux, http.MethodPost, "/api/v1/system/update-ack", nil)
	if err != nil || status >= 400 {
		return map[string]any{"kind": "failed", "tool": "acknowledge_update", "note": muxErrDetail(status, body, err)}
	}
	var out map[string]any
	if json.Unmarshal(body, &out) != nil || out == nil {
		return map[string]any{"acknowledged": false, "updated": false}
	}
	return out
}

type muxResponse struct {
	header http.Header
	body   bytes.Buffer
	status int
}

func (r *muxResponse) Header() http.Header { return r.header }

func (r *muxResponse) Write(p []byte) (int, error) {
	if r.status == 0 {
		r.status = http.StatusOK
	}
	return r.body.Write(p)
}

func (r *muxResponse) WriteHeader(code int) {
	if r.status == 0 {
		r.status = code
	}
}

func muxJSON(ctx context.Context, mux *http.ServeMux, method, path string, body any) (int, []byte, error) {
	var reader *bytes.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return 0, nil, err
		}
		reader = bytes.NewReader(raw)
	} else {
		reader = bytes.NewReader(nil)
	}
	if ctx == nil {
		ctx = context.Background()
	}
	req, err := http.NewRequestWithContext(ctx, method, "http://127.0.0.1"+path, reader)
	if err != nil {
		return 0, nil, err
	}
	req.RemoteAddr = "127.0.0.1:0"
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	rec := &muxResponse{header: http.Header{}}
	mux.ServeHTTP(rec, req)
	if rec.status == 0 {
		rec.status = http.StatusOK
	}
	return rec.status, rec.body.Bytes(), nil
}

func muxErrDetail(status int, body []byte, err error) string {
	if err != nil {
		return err.Error()
	}
	var obj map[string]any
	if json.Unmarshal(body, &obj) == nil {
		if d := stringOf(obj["detail"]); d != "" {
			return d
		}
	}
	return fmt.Sprintf("http_%d", status)
}

func lookupSingleGameName(ctx context.Context, db *sql.DB, appid int) string {
	if db == nil || appid <= 0 {
		return ""
	}
	var name string
	_ = db.QueryRowContext(ctx, `SELECT COALESCE(name,'') FROM games WHERE appid = ?`, appid).Scan(&name)
	return name
}

func lookupGameNames(ctx context.Context, db *sql.DB, appids []int) map[int]string {
	out := map[int]string{}
	if db == nil || len(appids) == 0 {
		return out
	}
	raw, _ := json.Marshal(appids)
	rows, err := db.QueryContext(ctx,
		`SELECT appid, COALESCE(name,'') FROM games WHERE appid IN (SELECT value FROM json_each(?))`, string(raw))
	if err != nil {
		return out
	}
	defer rows.Close()
	for rows.Next() {
		var id int
		var name string
		if rows.Scan(&id, &name) == nil && name != "" {
			out[id] = name
		}
	}
	return out
}

func proposalID(prefix string) string {
	now := time.Now().UTC().Add(8 * time.Hour)
	return fmt.Sprintf("%s%s%03d", prefix, now.Format("0102150405"), now.Nanosecond()/1e6)
}

func clipRunes(s string, max int) string {
	r := []rune(s)
	if len(r) <= max {
		return s
	}
	return string(r[:max])
}

func anySliceOf(v any) []any {
	if s, ok := v.([]any); ok {
		return s
	}
	return nil
}

func stringOf(value any) string {
	if text, ok := value.(string); ok {
		return text
	}
	return ""
}

func jsonInt(value any) int {
	switch v := value.(type) {
	case float64:
		return int(v)
	case int:
		return v
	case int64:
		return int(v)
	case json.Number:
		n, _ := v.Int64()
		return int(n)
	}
	return 0
}

func jsonFloat(value any) (float64, bool) {
	switch v := value.(type) {
	case float64:
		return v, true
	case int:
		return float64(v), true
	case int64:
		return float64(v), true
	case json.Number:
		f, err := v.Float64()
		return f, err == nil
	case string:
		f, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
		return f, err == nil
	}
	return 0, false
}

func stringOr(value any, fallback string) string {
	if text, ok := value.(string); ok && text != "" {
		return text
	}
	return fallback
}
