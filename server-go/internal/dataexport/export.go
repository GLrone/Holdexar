// Package dataexport 敏感数据加密导出：本机用户数据打包为口令加密的单文件。
// 只导出用户侧数据（凭据/关注/提醒/设置/订阅/账单等）；随包种子与公共目录、
// 价格数据由发布包与爬虫重建。正文以导出口令经 scrypt 派生密钥做 AES-256-GCM
// 加密；导出需当前主密钥可用（库内密文先解密再重新封装）。
package dataexport

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"golang.org/x/crypto/scrypt"

	"holdexar/server-go/internal/secretbox"
)

const (
	Format         = "holdexar-secure-export"
	FormatVersion  = 1
	exportDirName  = "exports"
	exportSuffix   = ".hxexport"
	minPasswordLen = 12
	scryptN        = 1 << 15
	scryptR        = 8
	scryptP        = 1
	scryptKeyLen   = 32
	saltBytes      = 16
	nonceBytes     = 12
	seedKeyPrefix  = "seed."
)

// 用户侧数据表（导出白名单）；凭据类列解密后落盘。
var userTables = []string{
	"steam_accounts", "tracked_accounts", "wishlist_items",
	"monitor_targets", "monitor_sources", "monitor_exclusions",
	"price_alerts", "alert_events", "fact_notices",
	"proxies", "proxy_subscriptions", "steam_events",
	"achievement_states", "achievement_games",
	"family_groups", "family_library_snapshots",
	"bill_imports", "bill_game_txs", "bill_topup_txs", "bill_cdk_games",
}

var containsLabels = []string{
	"Steam 账号与登录凭据（Cookie / 续期令牌）",
	"Steam Web API Key 与自建 LLM 供应商密钥",
	"代理订阅链接与代理配置",
	"游戏关注（愿望单 / 监控池 / 排除项）",
	"价格提醒规则与提醒记录",
	"账单记录、家庭组、成就进度、在线事件",
	"界面偏好与其他用户设置",
}

var excludesLabels = []string{
	"游戏目录与名称等公共元数据",
	"价格现价快照与历史价格",
	"捆绑包目录与价格、汇率档案",
	"区服配置、成就定义、玩家标签",
	"插件凭据（恢复后需重新填写并启用插件）",
}

// ExportDir 导出目录（内含凭据副本）。
func ExportDir(dataDir string) string {
	path := filepath.Join(dataDir, exportDirName)
	_ = os.MkdirAll(path, 0o700)
	return path
}

// SafeExportPath 路径穿越防护：文件名仅限合法字符且必须落在导出目录内。
func SafeExportPath(dataDir, name string) (string, error) {
	if name == "" || strings.ContainsAny(name, `/\`) || strings.Contains(name, "..") {
		return "", fmt.Errorf("非法导出文件名: %q", name)
	}
	ok := name != "." && name != ".."
	for _, r := range name {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' ||
			r == '-' || r == '_' || r == '.') {
			ok = false
			break
		}
	}
	if !ok || !strings.HasSuffix(name, exportSuffix) {
		return "", fmt.Errorf("非法导出文件名: %q", name)
	}
	base := ExportDir(dataDir)
	path := filepath.Join(base, name)
	if filepath.Clean(path) != filepath.Join(filepath.Clean(base), name) {
		return "", fmt.Errorf("非法导出文件名: %q", name)
	}
	return path, nil
}

type Result struct {
	Path      string         `json:"path"`
	Name      string         `json:"name"`
	SizeBytes int64          `json:"sizeBytes"`
	CreatedAt string         `json:"createdAt"`
	Counts    map[string]int `json:"counts"`
	Excludes  []string       `json:"excludes"`
}

// Create 组装 → 加密 → 落盘。口令过短 400，凭据锁定 409（由 HTTP 层映射）。
func Create(ctx context.Context, db *sql.DB, dataDir, password string) (Result, error) {
	var result Result
	if len(password) < minPasswordLen {
		return result, fmt.Errorf("导出口令至少 %d 位", minPasswordLen)
	}
	payload, err := buildPayload(ctx, db, dataDir)
	if err != nil {
		return result, err
	}
	envelope, err := encryptPayload(payload, password)
	if err != nil {
		return result, err
	}
	name := "holdexar-secure-" + time.Now().Format("20060102-150405") + exportSuffix
	path := filepath.Join(ExportDir(dataDir), name)
	encoded, err := marshalJSON(envelope)
	if err != nil {
		return result, err
	}
	if err := os.WriteFile(path, encoded, 0o600); err != nil {
		return result, err
	}
	info, err := os.Stat(path)
	if err != nil {
		return result, err
	}
	counts := map[string]int{}
	for table, rows := range payload.Tables {
		if len(rows) > 0 {
			counts[table] = len(rows)
		}
	}
	return Result{
		Path: path, Name: name, SizeBytes: info.Size(),
		CreatedAt: time.Now().Format("2006-01-02T15:04:05.999999"),
		Counts:    counts, Excludes: excludesLabels,
	}, nil
}

type payload struct {
	Format        string                      `json:"format"`
	FormatVersion int                         `json:"formatVersion"`
	ExportedAt    string                      `json:"exportedAt"`
	App           map[string]string           `json:"app"`
	Contains      []string                    `json:"contains"`
	Excludes      []string                    `json:"excludes"`
	Counts        map[string]int              `json:"counts"`
	Tables        map[string][]map[string]any `json:"tables"`
}

// buildPayload 组装导出正文：app_settings 剔除种子 marker 并解密凭据键，
// 用户表整表导出，steam_accounts.cookies / proxy_subscriptions.url 解密。
// 凭据处于锁定态（口令模式未解锁）报 SecretBoxError 语义错误。
func buildPayload(ctx context.Context, db *sql.DB, dataDir string) (payload, error) {
	out := payload{
		Format: Format, FormatVersion: FormatVersion,
		ExportedAt: time.Now().Format("2006-01-02T15:04:05.999999"),
		App:        map[string]string{},
		Contains:   containsLabels, Excludes: excludesLabels,
		Tables: map[string][]map[string]any{},
	}
	if locked, err := secretbox.IsLocked(dataDir); err == nil && locked {
		return out, secretbox.NewSecretBoxError("凭据处于锁定态，请先解锁后再导出")
	}
	settingsRows, err := dumpTable(ctx, db, "app_settings")
	if err != nil {
		return out, err
	}
	kept := make([]map[string]any, 0, len(settingsRows))
	for _, row := range settingsRows {
		key, _ := row["key"].(string)
		if strings.HasPrefix(key, seedKeyPrefix) {
			continue
		}
		raw, _ := row["value_json"].(string)
		var value any
		if err := json.Unmarshal([]byte(raw), &value); err != nil {
			value = raw
		}
		if key == "plugins.registry" {
			if err := portablePluginRegistry(value); err != nil {
				return out, err
			}
		}
		if text, ok := value.(string); ok && secretbox.IsEncrypted(text) {
			if plain, err := secretbox.DecryptSecretIn(dataDir, text, secretbox.SecretPurpose(key)); err == nil {
				value = plain
			}
		}
		kept = append(kept, map[string]any{"key": key, "value": value, "updated_at": row["updated_at"]})
	}
	out.Tables["app_settings"] = kept

	for _, table := range userTables {
		rows, err := dumpTable(ctx, db, table)
		if err != nil {
			return out, err
		}
		if table == "steam_accounts" {
			for _, row := range rows {
				row["cookies"] = decryptOrRaw(dataDir, row["cookies"], secretbox.PurposeSteamCookies)
			}
		} else if table == "proxy_subscriptions" {
			for _, row := range rows {
				row["url"] = decryptOrRaw(dataDir, row["url"], secretbox.PurposeProxySubscription)
			}
		}
		out.Tables[table] = rows
	}
	return out, nil
}

func portablePluginRegistry(value any) error {
	entries, ok := value.([]any)
	if value == nil {
		return nil
	}
	if !ok {
		return fmt.Errorf("插件配置无法导出：记录格式无效")
	}
	for _, item := range entries {
		entry, ok := item.(map[string]any)
		if !ok {
			return fmt.Errorf("插件配置无法导出：记录格式无效")
		}
		manifest, ok := entry["manifest"].(map[string]any)
		if !ok {
			return fmt.Errorf("插件配置无法导出：声明格式无效")
		}
		id, ok := manifest["id"].(string)
		if !ok || id == "" {
			return fmt.Errorf("插件配置无法导出：标识无效")
		}
		config, ok := entry["config"].(map[string]any)
		if entry["config"] != nil && !ok {
			return fmt.Errorf("插件配置无法导出：设置格式无效")
		}
		schema, ok := manifest["configSchema"].(map[string]any)
		if !ok {
			return fmt.Errorf("插件配置无法导出：配置声明无效")
		}
		properties, ok := schema["properties"].(map[string]any)
		if schema["properties"] != nil && !ok {
			return fmt.Errorf("插件配置无法导出：配置声明无效")
		}
		for name, property := range properties {
			field, ok := property.(map[string]any)
			if !ok {
				return fmt.Errorf("插件配置无法导出：配置字段无效")
			}
			if field["format"] == "password" {
				delete(config, name)
			}
		}
		for name, value := range config {
			if text, ok := value.(string); ok && secretbox.IsEncrypted(text) {
				delete(config, name)
			}
		}
		entry["enabled"], entry["configured"], entry["targetDigest"] = false, false, ""
		delete(entry, "configuredSecrets")
	}
	return nil
}

// decryptOrRaw 凭据列导出值：密文解密为明文；解不开/非密文原样带出（不丢数据）。
func decryptOrRaw(dataDir string, value any, purpose string) any {
	text, _ := value.(string)
	if text == "" || !secretbox.IsEncrypted(text) {
		return value
	}
	if plain, err := secretbox.DecryptSecretIn(dataDir, text, purpose); err == nil {
		return plain
	}
	return text
}

// dumpTable 整表导出；表缺失（版本差异）按空表处理。
func dumpTable(ctx context.Context, db *sql.DB, table string) ([]map[string]any, error) {
	rows, err := db.QueryContext(ctx, `SELECT * FROM "`+table+`"`)
	if err != nil {
		return nil, nil // 表缺失：跳过
	}
	defer rows.Close()
	cols, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	var out []map[string]any
	for rows.Next() {
		values := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range values {
			ptrs[i] = &values[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			return nil, err
		}
		row := make(map[string]any, len(cols))
		for i, name := range cols {
			row[name] = jsonValue(values[i])
		}
		out = append(out, row)
	}
	return out, rows.Err()
}

// jsonValue 驱动原生类型 → JSON 安全类型（BLOB 走 base64 文本）。
func jsonValue(value any) any {
	switch typed := value.(type) {
	case []byte:
		return base64.StdEncoding.EncodeToString(typed)
	case time.Time:
		return typed.Format("2006-01-02T15:04:05.999999")
	default:
		return value
	}
}

type envelope struct {
	Format        string    `json:"format"`
	FormatVersion int       `json:"formatVersion"`
	Cipher        string    `json:"cipher"`
	KDF           kdfParams `json:"kdf"`
	Nonce         string    `json:"nonce"`
	Ciphertext    string    `json:"ciphertext"`
}

type kdfParams struct {
	Name  string `json:"name"`
	N     int    `json:"n"`
	R     int    `json:"r"`
	P     int    `json:"p"`
	DKLen int    `json:"dklen"`
	Salt  string `json:"salt"`
}

func encryptPayload(body payload, password string) (envelope, error) {
	var out envelope
	plain, err := marshalJSON(body)
	if err != nil {
		return out, err
	}
	salt := make([]byte, saltBytes)
	if _, err := rand.Read(salt); err != nil {
		return out, err
	}
	kek, err := scrypt.Key([]byte(password), salt, scryptN, scryptR, scryptP, scryptKeyLen)
	if err != nil {
		return out, err
	}
	nonce := make([]byte, nonceBytes)
	if _, err := rand.Read(nonce); err != nil {
		return out, err
	}
	block, err := aes.NewCipher(kek)
	if err != nil {
		return out, err
	}
	sealed, err := cipher.NewGCM(block)
	if err != nil {
		return out, err
	}
	encrypted := sealed.Seal(nil, nonce, plain, nil)
	return envelope{
		Format: Format, FormatVersion: FormatVersion, Cipher: "AES-256-GCM",
		KDF: kdfParams{Name: "scrypt", N: scryptN, R: scryptR, P: scryptP, DKLen: scryptKeyLen,
			Salt: base64.RawURLEncoding.EncodeToString(salt)},
		Nonce:      base64.RawURLEncoding.EncodeToString(nonce),
		Ciphertext: base64.RawURLEncoding.EncodeToString(encrypted),
	}, nil
}

// List 已有导出文件（新→旧）。
func List(dataDir string) []map[string]any {
	entries, err := os.ReadDir(ExportDir(dataDir))
	if err != nil {
		return nil
	}
	type entry struct {
		name     string
		size     int64
		modified time.Time
	}
	var found []entry
	for _, item := range entries {
		if item.IsDir() || !strings.HasSuffix(item.Name(), exportSuffix) {
			continue
		}
		if info, err := item.Info(); err == nil {
			found = append(found, entry{item.Name(), info.Size(), info.ModTime()})
		}
	}
	sort.Slice(found, func(i, j int) bool { return found[i].modified.After(found[j].modified) })
	items := make([]map[string]any, 0, len(found))
	for _, item := range found {
		items = append(items, map[string]any{
			"name": item.name, "sizeBytes": item.size,
			"createdAt": item.modified.Format("2006-01-02T15:04:05.999999"),
		})
	}
	return items
}

// marshalJSON 紧凑序列化（不转义 UTF-8 之外的 HTML 字符）。
func marshalJSON(value any) ([]byte, error) {
	buffer := &strings.Builder{}
	encoder := json.NewEncoder(buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return []byte(strings.TrimRight(buffer.String(), "\n")), nil
}
