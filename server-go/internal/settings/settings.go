// Package settings：settings 域读写（KV 配置，首个走写闸的写路径）。
// 读路径直查 app_settings；写路径必须经 store.WriteGate 串行落库，
// 凭据加密等纯计算在闸外完成，闸内只做库写。
// MCP 运行态经函数字段注入（serverd 装配 agent 实现），未注入时按未在役形状输出。
package settings

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/config"
	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/store"
)

// app_settings 键（与 Python 侧 KV 键一致）。
const (
	keySteamID        = "account.steam_id"
	keySteamAPIKey    = "account.steam_api_key"
	keyOnboardingDone = "ui.onboarding_done"
	keyAutoPrice      = "crawl.auto_price"
	keyBackupAuto     = "backup.auto"
	keyUpdateNotified = "ui.update_notified"
	keyUpdateSkipped  = "ui.update_skipped_version"
	keyUpdateNotify   = "ui.update_notify"
	keyUpdateAuto     = "ui.update_auto"
	keyDirectNotice   = "ui.direct_notice_seen"
	keyTheme          = "ui.theme"
	keyMCPEnabled     = "mcp.enabled"
)

// resolveDataDir 数据目录判定（凭据主密钥托管挂在其下），Register 时调用
// 一次；测试可替换。与 systemx 同款装配。
var resolveDataDir = func() string {
	dir, err := config.ResolveDataDir()
	if err != nil {
		return ""
	}
	return dir
}

type Options struct {
	Bus     *events.Bus
	Changed func(context.Context, []string, []string)
}

func Register(mux *http.ServeMux, db *sql.DB, gate *store.WriteGate, options ...Options) {
	h := &handler{db: db, gate: gate, dataDir: resolveDataDir()}
	if len(options) > 0 {
		h.options = options[0]
	}
	mux.HandleFunc("GET /api/v1/settings", h.handleGet)
	mux.HandleFunc("PUT /api/v1/settings", h.handlePut)
	mux.HandleFunc("GET /api/v1/settings/fetch", h.handleFetchGet)
	mux.HandleFunc("PUT /api/v1/settings/fetch", h.handleFetchPut)
}

// OnMCPEnabledWrite mcp.enabled 落库成功后的运行时重载回调；cmd/serverd 注入
// agent 侧实现（函数字段注入，避免 settings → agent 依赖）。nil 时跳过。
var OnMCPEnabledWrite func()

// MCPStatus MCP 运行态快照（GET 的 mcp_running/mcp_error/mcp_forced_by_env）；
// cmd/serverd 注入 agent 侧实现，nil 保持未在役占位形状。
var MCPStatus func() (running bool, errText string, forcedByEnv bool)

type handler struct {
	db      *sql.DB
	gate    *store.WriteGate
	dataDir string
	options Options
}

type accountPayload struct {
	// SteamID Python dict 原值透传（真相为字符串；异常类型不收口）。
	SteamID     any    `json:"steam_id"`
	SteamAPIKey string `json:"steam_api_key"`
	HasAPIKey   bool   `json:"has_api_key"`
}

type settingsResponse struct {
	Account          accountPayload `json:"account"`
	OnboardingDone   bool           `json:"onboarding_done"`
	AutoPrice        bool           `json:"auto_price"`
	BackupAuto       bool           `json:"backup_auto"`
	UpdateNotified   string         `json:"update_notified"`
	UpdateSkipped    string         `json:"update_skipped"`
	UpdateNotify     bool           `json:"update_notify"`
	UpdateAuto       bool           `json:"update_auto"`
	DirectNoticeSeen bool           `json:"direct_notice_seen"`
	Theme            string         `json:"theme"`
	MCPEnabled       bool           `json:"mcp_enabled"`
	// MCP 运行态：cmd/serverd 注入 agent 侧快照；未注入时恒为未在役形状。
	MCPRunning     bool   `json:"mcp_running"`
	MCPError       string `json:"mcp_error"`
	MCPForcedByEnv bool   `json:"mcp_forced_by_env"`
}

func (h *handler) handleGet(w http.ResponseWriter, r *http.Request) {
	resp, err := h.readSettings(r.Context())
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// readSettings 聚合 GET 全量形状：KV 各键 + 缺省（Python get_settings 同源）。
func (h *handler) readSettings(ctx context.Context) (*settingsResponse, error) {
	resp := &settingsResponse{}

	if v, err := h.kvValue(ctx, keySteamID); err != nil {
		return nil, err
	} else {
		resp.Account.SteamID = orEmpty(v)
	}

	if apiKey, err := ReadSecret(ctx, h.db, h.dataDir, keySteamAPIKey); err != nil {
		return nil, err
	} else {
		resp.Account.SteamAPIKey = maskKey(apiKey)
		resp.Account.HasAPIKey = apiKey != ""
	}

	boolKeys := []struct {
		key    string
		target *bool
		def    bool
	}{
		{keyOnboardingDone, &resp.OnboardingDone, false},
		{keyAutoPrice, &resp.AutoPrice, true},
		{keyBackupAuto, &resp.BackupAuto, true},
		{keyUpdateNotify, &resp.UpdateNotify, true},
		{keyUpdateAuto, &resp.UpdateAuto, false},
		{keyDirectNotice, &resp.DirectNoticeSeen, false},
		{keyMCPEnabled, &resp.MCPEnabled, false},
	}
	for _, b := range boolKeys {
		v, err := h.kvValue(ctx, b.key)
		if err != nil {
			return nil, err
		}
		if !v.present {
			v.val = b.def
		}
		*b.target = pyBool(v.val)
	}

	strKeys := []struct {
		key    string
		target *string
		def    string
	}{
		{keyUpdateNotified, &resp.UpdateNotified, ""},
		{keyUpdateSkipped, &resp.UpdateSkipped, ""},
		{keyTheme, &resp.Theme, "dark"},
	}
	for _, s := range strKeys {
		v, err := h.kvValue(ctx, s.key)
		if err != nil {
			return nil, err
		}
		if !v.present {
			v.val = s.def
		}
		*s.target = pyStrOr(v.val, s.def)
	}
	if MCPStatus != nil {
		resp.MCPRunning, resp.MCPError, resp.MCPForcedByEnv = MCPStatus()
	}
	return resp, nil
}

type kvValue struct {
	present bool
	val     any
}

// kvValue 读单键并反序列化 value_json；缺行/NULL 视为不存在（Python 返回 default 同径）。
func (h *handler) kvValue(ctx context.Context, key string) (kvValue, error) {
	var raw sql.NullString
	err := h.db.QueryRowContext(ctx,
		"SELECT value_json FROM app_settings WHERE key = ?", key).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return kvValue{}, nil
	}
	if err != nil {
		return kvValue{}, err
	}
	if !raw.Valid {
		return kvValue{}, nil
	}
	var val any
	dec := json.NewDecoder(strings.NewReader(raw.String))
	dec.UseNumber()
	if err := dec.Decode(&val); err != nil {
		return kvValue{}, err
	}
	return kvValue{present: true, val: val}, nil
}

// ReadSecret 兼容明文凭据，密文不可解时返回空值。
func ReadSecret(ctx context.Context, db *sql.DB, dataDir, key string) (string, error) {
	var raw sql.NullString
	err := db.QueryRowContext(ctx, `SELECT value_json FROM app_settings WHERE key=?`, key).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) || err == nil && !raw.Valid {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	var value any
	if err = json.Unmarshal([]byte(raw.String), &value); err != nil {
		return "", err
	}
	s, ok := value.(string)
	if !ok || s == "" {
		return "", nil
	}
	if !secretbox.IsEncrypted(s) {
		return s, nil
	}
	plain, derr := secretbox.DecryptSecretIn(dataDir, s, secretbox.SecretPurpose(key))
	if derr != nil {
		return "", nil
	}
	return plain, nil
}

// maskKey 掩码回显：保留末 4 字符（Steam Key 恒 ASCII，字符/字节切片同形）。
func maskKey(key string) string {
	r := []rune(key)
	if len(r) == 0 {
		return ""
	}
	if len(r) > 4 {
		return "****" + string(r[len(r)-4:])
	}
	return "****"
}

// orEmpty Python `值 or ""`：假值回空串，真值原样（字符串外的异常类型透传）。
func orEmpty(v kvValue) any {
	if !v.present || !pyBool(v.val) {
		return ""
	}
	return v.val
}

// pyBool Python bool() 语义：None/空串/零值/空容器为假，其余为真。
func pyBool(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case json.Number:
		f, err := x.Float64()
		return err == nil && f != 0
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	}
	return true
}

// pyStrOr Python `值 or 默认` 后 str()：标量直转，容器退化为 JSON 文本。
func pyStrOr(v any, def string) string {
	if !pyBool(v) {
		return def
	}
	switch x := v.(type) {
	case string:
		return x
	case bool:
		if x {
			return "True"
		}
		return "False"
	case json.Number:
		if f, err := x.Float64(); err == nil {
			return pyFloatStr(f, x.String())
		}
	}
	b, err := json.Marshal(v)
	if err != nil {
		return ""
	}
	return string(b)
}

// pyFloatStr Python float str() 近似：整值补 .0，科学计数走 g 格式。
func pyFloatStr(f float64, raw string) string {
	if !strings.ContainsAny(raw, ".eE") {
		return raw
	}
	if math.Trunc(f) == f && math.Abs(f) < 1e16 {
		return strconv.FormatFloat(f, 'f', -1, 64) + ".0"
	}
	return strconv.FormatFloat(f, 'g', -1, 64)
}

func (h *handler) handlePut(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	req, vErrs, fatal := parseUpdate(body)
	if fatal != nil {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []any{*fatal}})
		return
	}
	if len(vErrs) > 0 {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": vErrs})
		return
	}
	writes, err := h.buildWrites(req)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	if len(writes) > 0 {
		err := h.gate.WithWrite(r.Context(), store.PriorityInteractive, func(conn *sql.Conn) error {
			return applyWrites(r.Context(), conn, writes)
		})
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, detailNull())
			return
		}
		// mcp.enabled 变更后运行时重应用（Python 写入口 reapply 同径）
		if req.mcpEnabled != nil && OnMCPEnabledWrite != nil {
			OnMCPEnabledWrite()
		}
		h.notify(r.Context(), writes, nil)
	}
	resp, err := h.readSettings(r.Context())
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

type kvWrite struct {
	key   string
	value string // JSON 序列化文本（value_json 存 JSON 文本）
}

func WriteValue(ctx context.Context, conn *sql.Conn, key string, value any) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return applyWrites(ctx, conn, []kvWrite{{key: key, value: string(raw)}})
}

// applyWrites 单事务批量落库（Python set_values 批量同形；一行一 updated_at 快照）。
func applyWrites(ctx context.Context, conn *sql.Conn, writes []kvWrite) error {
	if len(writes) == 0 {
		return nil
	}
	rows := make([][2]string, 0, len(writes))
	for _, write := range writes {
		rows = append(rows, [2]string{write.key, write.value})
	}
	raw, err := json.Marshal(rows)
	if err != nil {
		return err
	}
	_, err = conn.ExecContext(ctx, `INSERT INTO app_settings(key,value_json,updated_at) SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),? FROM json_each(?) WHERE 1
 ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at`, beijingNow(), string(raw))
	return err
}

// beijingNow 本地挂钟北京时间（UTC+8 固定，无夏令时），裸时刻字符串与
// Python DateTime 列存储格式一致。
func beijingNow() string {
	return time.Now().UTC().Add(8 * time.Hour).Format("2006-01-02 15:04:05.000000")
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	body, err := json.Marshal(payload)
	if err != nil {
		http.Error(w, "响应序列化失败", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

func detailNull() map[string]any {
	return map[string]any{"detail": nil}
}

// ---- 请求体校验（形状对齐 FastAPI/Pydantic 实测锚点） ----

// valErr 校验错误项：type/loc/msg/input 与 Pydantic 输出逐字段一致。
type valErr struct {
	Type  string `json:"type"`
	Loc   []any  `json:"loc"`
	Msg   string `json:"msg"`
	Input any    `json:"input"`
}

const (
	errStringType     = "string_type"
	errStringTypeMsg  = "Input should be a valid string"
	errBoolParsing    = "bool_parsing"
	errBoolParsingMsg = "Input should be a valid boolean, unable to interpret input"
	errBoolType       = "bool_type"
	errBoolTypeMsg    = "Input should be a valid boolean"
	errModelType      = "model_attributes_type"
	errModelTypeMsg   = "Input should be a valid dictionary or object to extract fields from"
	errMissing        = "missing"
	errMissingMsg     = "Field required"
	errJSONInvalid    = "json_invalid"
	errJSONInvalidMsg = "JSON decode error"
)

// updateReq 校验后的写入意图；nil 表示该字段不在请求中（含显式 null）。
type updateReq struct {
	steamID          *string
	steamAPIKey      *string // 已 strip 的明文；加密在 buildWrites 闸外完成
	onboardingDone   *bool
	autoPrice        *bool
	backupAuto       *bool
	updateNotified   *string
	updateSkipped    *string
	updateNotify     *bool
	updateAuto       *bool
	directNoticeSeen *bool
	theme            *string
	mcpEnabled       *bool
}

// parseUpdate 解析并校验 PUT 请求体：全部字段错误合并上报（Pydantic 非短路同径）。
// fatal 为整体形状错误（非法 JSON / 顶层非对象），直接替代字段级结果。
func parseUpdate(body []byte) (*updateReq, []valErr, *valErr) {
	if len(body) == 0 {
		return nil, nil, &valErr{Type: errMissing, Loc: []any{"body"}, Msg: errMissingMsg, Input: nil}
	}
	var top any
	dec := json.NewDecoder(strings.NewReader(string(body)))
	dec.UseNumber()
	if err := dec.Decode(&top); err != nil {
		return nil, nil, &valErr{Type: errJSONInvalid, Loc: []any{"body"}, Msg: errJSONInvalidMsg, Input: map[string]any{}}
	}
	fields, ok := top.(map[string]any)
	if !ok {
		return nil, nil, &valErr{Type: errModelType, Loc: []any{"body"}, Msg: errModelTypeMsg, Input: top}
	}

	req := &updateReq{}
	vErrs := []valErr{}
	if raw, ok := fields["account"]; ok && raw != nil {
		obj, ok := raw.(map[string]any)
		if !ok {
			vErrs = append(vErrs, valErr{Type: errModelType, Loc: []any{"body", "account"}, Msg: errModelTypeMsg, Input: raw})
		} else {
			if v, present := obj["steam_id"]; present && v != nil {
				if s, e := wantStr(v, "account", "steam_id"); e != nil {
					vErrs = append(vErrs, *e)
				} else {
					trimmed := strings.TrimSpace(s)
					req.steamID = &trimmed
				}
			}
			if v, present := obj["steam_api_key"]; present && v != nil {
				if s, e := wantStr(v, "account", "steam_api_key"); e != nil {
					vErrs = append(vErrs, *e)
				} else {
					trimmed := strings.TrimSpace(s)
					req.steamAPIKey = &trimmed
				}
			}
		}
	}
	boolSpecs := []struct {
		name   string
		target **bool
	}{
		{"onboarding_done", &req.onboardingDone},
		{"auto_price", &req.autoPrice},
		{"backup_auto", &req.backupAuto},
		{"update_notify", &req.updateNotify},
		{"update_auto", &req.updateAuto},
		{"direct_notice_seen", &req.directNoticeSeen},
		{"mcp_enabled", &req.mcpEnabled},
	}
	for _, b := range boolSpecs {
		raw, ok := fields[b.name]
		if !ok || raw == nil {
			continue
		}
		v, e := wantBool(raw, b.name)
		if e != nil {
			vErrs = append(vErrs, *e)
			continue
		}
		*b.target = &v
	}
	strSpecs := []struct {
		name   string
		trim   bool
		target **string
	}{
		{"update_notified", true, &req.updateNotified},
		{"update_skipped", true, &req.updateSkipped},
		{"theme", false, &req.theme},
	}
	for _, s := range strSpecs {
		raw, ok := fields[s.name]
		if !ok || raw == nil {
			continue
		}
		v, e := wantStr(raw, s.name)
		if e != nil {
			vErrs = append(vErrs, *e)
			continue
		}
		if s.trim {
			v = strings.TrimSpace(v)
		}
		*s.target = &v
	}
	if len(vErrs) > 0 {
		return nil, vErrs, nil
	}
	return req, nil, nil
}

// buildWrites 校验后的写入意图 → KV 写列表（值已 JSON 序列化）。
// 凭据加密在此完成：纯计算放闸外，落库统一经写闸单事务。
// 掩码回显（**** 前缀）与空串不落库（Python 掩码防回写同径）。
func (h *handler) buildWrites(req *updateReq) ([]kvWrite, error) {
	var writes []kvWrite
	push := func(key string, val any) {
		b, _ := json.Marshal(val)
		writes = append(writes, kvWrite{key: key, value: string(b)})
	}
	if req.steamID != nil {
		push(keySteamID, *req.steamID)
	}
	if req.steamAPIKey != nil {
		key := *req.steamAPIKey
		if key != "" && !strings.HasPrefix(key, "****") {
			token, err := secretbox.EncryptSecretIn(h.dataDir, key, secretbox.SecretPurpose(keySteamAPIKey))
			if err != nil {
				return nil, err
			}
			push(keySteamAPIKey, token)
		}
	}
	if req.onboardingDone != nil {
		push(keyOnboardingDone, *req.onboardingDone)
	}
	if req.autoPrice != nil {
		push(keyAutoPrice, *req.autoPrice)
	}
	if req.backupAuto != nil {
		push(keyBackupAuto, *req.backupAuto)
	}
	if req.updateNotified != nil {
		push(keyUpdateNotified, *req.updateNotified)
	}
	if req.updateSkipped != nil {
		push(keyUpdateSkipped, *req.updateSkipped)
	}
	if req.updateNotify != nil {
		push(keyUpdateNotify, *req.updateNotify)
	}
	if req.updateAuto != nil {
		push(keyUpdateAuto, *req.updateAuto)
	}
	if req.directNoticeSeen != nil {
		push(keyDirectNotice, *req.directNoticeSeen)
	}
	// 主题只认两值，异常值忽略（防脏配置）。
	if req.theme != nil && (*req.theme == "dark" || *req.theme == "light") {
		push(keyTheme, *req.theme)
	}
	if req.mcpEnabled != nil {
		push(keyMCPEnabled, *req.mcpEnabled)
	}
	return writes, nil
}

// wantStr 字符串字段校验（Pydantic string_type 同径）。
func wantStr(raw any, loc ...string) (string, *valErr) {
	s, ok := raw.(string)
	if !ok {
		return "", &valErr{Type: errStringType, Loc: locAny("body", loc), Msg: errStringTypeMsg, Input: raw}
	}
	return s, nil
}

// wantBool 布尔字段宽松强转：真值集 {true,false,t,f,y,n,yes,no,on,off,1,0}
// 大小写不敏感，0/1 数值合法，越界数值/容器拒绝（Pydantic lax 实测同径）。
func wantBool(raw any, loc ...string) (bool, *valErr) {
	switch x := raw.(type) {
	case bool:
		return x, nil
	case string:
		switch strings.ToLower(x) {
		case "true", "t", "y", "yes", "on", "1":
			return true, nil
		case "false", "f", "n", "no", "off", "0":
			return false, nil
		}
		return false, &valErr{Type: errBoolParsing, Loc: locAny("body", loc), Msg: errBoolParsingMsg, Input: raw}
	case json.Number:
		if f, err := x.Float64(); err == nil {
			if f == 0 || f == 1 {
				return f == 1, nil
			}
			if strings.ContainsAny(x.String(), ".eE") {
				return false, &valErr{Type: errBoolType, Loc: locAny("body", loc), Msg: errBoolTypeMsg, Input: raw}
			}
		}
		return false, &valErr{Type: errBoolParsing, Loc: locAny("body", loc), Msg: errBoolParsingMsg, Input: raw}
	}
	return false, &valErr{Type: errBoolType, Loc: locAny("body", loc), Msg: errBoolTypeMsg, Input: raw}
}

// locAny 前缀 + 变长段（["body","theme"] / ["body","account","steam_id"]）。
func locAny(prefix string, parts []string) []any {
	out := make([]any, 0, len(parts)+1)
	out = append(out, prefix)
	for _, p := range parts {
		out = append(out, p)
	}
	return out
}
