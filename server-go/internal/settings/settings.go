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

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/config"
	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/httpx"
	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/store"
)

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
	keyLANEnabled     = "access.lan_enabled"
	keyLANPassword    = "access.lan_password"
)

const (
	KeyLANEnabled      = keyLANEnabled
	KeyLANPassword     = keyLANPassword
	KeyLANPasswordHash = keyLANPassword + "_hash"
)

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
	// SessionGate 改密时吊销已签发解锁会话（nil 不轮换）。
	SessionGate *access.Gate
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
	registerSteamLocalRoutes(mux, h)
}

// OnMCPEnabledWrite mcp.enabled 落库后回调（包级变量注入以解耦 agent；nil 跳过）。
var OnMCPEnabledWrite func()

// MCPStatus MCP 运行态快照钩子（nil 返回未在役占位）。
var MCPStatus func() (running bool, errText string, forcedByEnv bool)

type handler struct {
	db      *sql.DB
	gate    *store.WriteGate
	dataDir string
	options Options
}

type accountPayload struct {
	SteamID     any    `json:"steam_id"`
	SteamAPIKey string `json:"steam_api_key"`
	HasAPIKey   bool   `json:"has_api_key"`
}

// lanAccessPayload 口令原文永不回显，仅暴露 has_password 与 password_source。
type lanAccessPayload struct {
	Enabled        bool   `json:"enabled"`
	EnvForced      bool   `json:"env_forced"`
	HasPassword    bool   `json:"has_password"`
	PasswordSource string `json:"password_source"`
}

type settingsResponse struct {
	Account          accountPayload   `json:"account"`
	OnboardingDone   bool             `json:"onboarding_done"`
	AutoPrice        bool             `json:"auto_price"`
	BackupAuto       bool             `json:"backup_auto"`
	UpdateNotified   string           `json:"update_notified"`
	UpdateSkipped    string           `json:"update_skipped"`
	UpdateNotify     bool             `json:"update_notify"`
	UpdateAuto       bool             `json:"update_auto"`
	DirectNoticeSeen bool             `json:"direct_notice_seen"`
	Theme            string           `json:"theme"`
	MCPEnabled       bool             `json:"mcp_enabled"`
	LanAccess        lanAccessPayload `json:"lan_access"`
	MCPRunning       bool             `json:"mcp_running"`
	MCPError         string           `json:"mcp_error"`
	MCPForcedByEnv   bool             `json:"mcp_forced_by_env"`
}

func (h *handler) handleGet(w http.ResponseWriter, r *http.Request) {
	resp, err := h.readSettings(r.Context())
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

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
	hasUserHash := false
	if v, err := h.kvValue(ctx, KeyLANPasswordHash); err != nil {
		return nil, err
	} else if v.present && pyBool(v.val) {
		hasUserHash = true
	}
	hasLegacySecret := false
	if !hasUserHash {
		stored, err := ReadSecret(ctx, h.db, h.dataDir, keyLANPassword)
		if err != nil {
			return nil, err
		}
		hasLegacySecret = stored != ""
	}
	passwordSource := "none"
	if hasUserHash || hasLegacySecret {
		passwordSource = "user"
	} else if access.EnvPassword() != "" {
		passwordSource = "env"
	}
	resp.LanAccess = lanAccessPayload{
		Enabled:        access.LANEnabled(),
		EnvForced:      access.EnvForced(),
		HasPassword:    passwordSource != "none",
		PasswordSource: passwordSource,
	}
	return resp, nil
}

type kvValue struct {
	present bool
	val     any
}

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

func ReadBool(ctx context.Context, db *sql.DB, key string, def bool) (bool, error) {
	var raw sql.NullString
	err := db.QueryRowContext(ctx,
		"SELECT value_json FROM app_settings WHERE key = ?", key).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return def, nil
	}
	if err != nil {
		return false, err
	}
	if !raw.Valid {
		return def, nil
	}
	var val any
	if err := json.Unmarshal([]byte(raw.String), &val); err != nil {
		return false, err
	}
	return pyBool(val), nil
}

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

func ReadValue(ctx context.Context, db *sql.DB, key string) (string, error) {
	var raw sql.NullString
	err := db.QueryRowContext(ctx,
		"SELECT value_json FROM app_settings WHERE key = ?", key).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) || err == nil && !raw.Valid {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	var val any
	if err := json.Unmarshal([]byte(raw.String), &val); err != nil {
		return "", err
	}
	s, _ := val.(string)
	return s, nil
}

func DeleteValue(ctx context.Context, conn *sql.Conn, key string) error {
	_, err := conn.ExecContext(ctx, `DELETE FROM app_settings WHERE key = ?`, key)
	return err
}

func (h *handler) lanPasswordExists(ctx context.Context) (bool, error) {
	if v, err := h.kvValue(ctx, KeyLANPasswordHash); err != nil {
		return false, err
	} else if v.present && pyBool(v.val) {
		return true, nil
	}
	stored, err := ReadSecret(ctx, h.db, h.dataDir, keyLANPassword)
	if err != nil {
		return false, err
	}
	if stored != "" {
		return true, nil
	}
	return access.EnvPassword() != "", nil
}

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

func orEmpty(v kvValue) any {
	if !v.present || !pyBool(v.val) {
		return ""
	}
	return v.val
}

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
		httpx.WriteValidationError(w, *fatal)
		return
	}
	if len(vErrs) > 0 {
		httpx.WriteValidationError(w, vErrs...)
		return
	}
	// 局域网访问密码至少 8 位（422）；空串清密或已有口令（含 env）改密时
	// 触发会话轮换，吊销已签发解锁会话。
	rotateSession := false
	if req.lanAccess != nil && req.lanAccess.password != nil {
		pw := strings.TrimSpace(*req.lanAccess.password)
		if pw == "" {
			rotateSession = true
		} else if !strings.HasPrefix(pw, "****") {
			if len([]rune(pw)) < 8 {
				writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": "访问密码至少 8 位"})
				return
			}
			prev, perr := h.lanPasswordExists(r.Context())
			if perr != nil {
				writeJSON(w, http.StatusInternalServerError, detailNull())
				return
			}
			rotateSession = prev
		}
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
		if req.mcpEnabled != nil && OnMCPEnabledWrite != nil {
			OnMCPEnabledWrite()
		}
		if req.lanAccess != nil {
			if req.lanAccess.enabled != nil {
				access.SetLANEnabled(*req.lanAccess.enabled)
			}
			access.RefreshExtraHosts()
		}
		h.notify(r.Context(), writes, nil)
	}
	resp, err := h.readSettings(r.Context())
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	if rotateSession && h.options.SessionGate != nil {
		// 改密/清密后重置会话令牌，仅为当前响应重签发 Cookie。
		h.options.SessionGate.Reset()
		h.options.SessionGate.Issue(w)
	}
	writeJSON(w, http.StatusOK, resp)
}

type kvWrite struct {
	key   string
	value string
	drop  bool
}

func WriteValue(ctx context.Context, conn *sql.Conn, key string, value any) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return applyWrites(ctx, conn, []kvWrite{{key: key, value: string(raw)}})
}

func applyWrites(ctx context.Context, conn *sql.Conn, writes []kvWrite) error {
	if len(writes) == 0 {
		return nil
	}
	rows := make([][2]string, 0, len(writes))
	for _, write := range writes {
		if write.drop {
			if _, err := conn.ExecContext(ctx, `DELETE FROM app_settings WHERE key = ?`, write.key); err != nil {
				return err
			}
			continue
		}
		rows = append(rows, [2]string{write.key, write.value})
	}
	if len(rows) == 0 {
		return nil
	}
	raw, err := json.Marshal(rows)
	if err != nil {
		return err
	}
	_, err = conn.ExecContext(ctx, `INSERT INTO app_settings(key,value_json,updated_at) SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),? FROM json_each(?) WHERE 1
 ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at`, beijingNow(), string(raw))
	return err
}

func beijingNow() string {
	return time.Now().UTC().Add(8 * time.Hour).Format("2006-01-02 15:04:05.000000")
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	httpx.WriteJSON(w, status, payload)
}

func detailNull() map[string]any {
	return httpx.DetailNull()
}

type valErr = httpx.ValErr

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

type lanAccessUpdate struct {
	enabled  *bool
	password *string
}

type updateReq struct {
	steamID          *string
	steamAPIKey      *string
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
	lanAccess        *lanAccessUpdate
}

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
	if raw, ok := fields["lan_access"]; ok && raw != nil {
		obj, ok := raw.(map[string]any)
		if !ok {
			vErrs = append(vErrs, valErr{Type: errModelType, Loc: []any{"body", "lan_access"}, Msg: errModelTypeMsg, Input: raw})
		} else {
			req.lanAccess = &lanAccessUpdate{}
			if v, present := obj["enabled"]; present && v != nil {
				if b, e := wantBool(v, "lan_access", "enabled"); e != nil {
					vErrs = append(vErrs, *e)
				} else {
					req.lanAccess.enabled = &b
				}
			}
			if v, present := obj["password"]; present && v != nil {
				if s, e := wantStr(v, "lan_access", "password"); e != nil {
					vErrs = append(vErrs, *e)
				} else {
					req.lanAccess.password = &s
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

// buildWrites 将写入意图转为 KV 写操作（纯加密计算在写闸外完成）；
// **** 掩码不回写，steam_api_key 空串跳过，lan_access.password 空串清双键。
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
	if req.theme != nil && (*req.theme == "dark" || *req.theme == "light") {
		push(keyTheme, *req.theme)
	}
	if req.mcpEnabled != nil {
		push(keyMCPEnabled, *req.mcpEnabled)
	}
	if req.lanAccess != nil {
		if req.lanAccess.enabled != nil {
			push(keyLANEnabled, *req.lanAccess.enabled)
		}
		if req.lanAccess.password != nil {
			password := strings.TrimSpace(*req.lanAccess.password)
			if password == "" {
				writes = append(writes,
					kvWrite{key: KeyLANPasswordHash, drop: true},
					kvWrite{key: keyLANPassword, drop: true},
				)
			} else if !strings.HasPrefix(password, "****") {
				push(KeyLANPasswordHash, access.HashPassword(password))
				writes = append(writes, kvWrite{key: keyLANPassword, drop: true})
			}
		}
	}
	return writes, nil
}

func wantStr(raw any, loc ...string) (string, *valErr) {
	s, ok := raw.(string)
	if !ok {
		return "", &valErr{Type: errStringType, Loc: locAny("body", loc), Msg: errStringTypeMsg, Input: raw}
	}
	return s, nil
}

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

func locAny(prefix string, parts []string) []any {
	out := make([]any, 0, len(parts)+1)
	out = append(out, prefix)
	for _, p := range parts {
		out = append(out, p)
	}
	return out
}
