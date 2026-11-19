package secretbox

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"sync"

	"golang.org/x/crypto/scrypt"
)

// 数据主密钥三模式（secret.master 缺失=legacy）：legacy 机器派生、dpapi 系统凭据、passphrase 口令 scrypt。
// 仅 passphrase 模式在进程内缓存已解封主密钥，重启失效。

const (
	ModeLegacy     = "legacy"
	ModeDPAPI      = "dpapi"
	ModePassphrase = "passphrase"

	masterFile = "secret.master"

	fileVersion = 1

	// scrypt 成本参数与 Python 侧逐字节一致（跨语言 KEK 不变量）。
	scryptN     = 1 << 15
	scryptR     = 8
	scryptP     = 1
	scryptDKLen = keyBytes
)

var (
	lockMu      sync.Mutex
	unlockedKey []byte
)

// ResetState 清零并置空进程内主密钥缓存。
func ResetState() {
	lockMu.Lock()
	defer lockMu.Unlock()
	for i := range unlockedKey {
		unlockedKey[i] = 0
	}
	unlockedKey = nil
}

func LoadUnlockedKey(key []byte) {
	lockMu.Lock()
	defer lockMu.Unlock()
	unlockedKey = bytes.Clone(key)
}

func Lock() { ResetState() }

func currentUnlockedKey() []byte {
	lockMu.Lock()
	defer lockMu.Unlock()
	return bytes.Clone(unlockedKey)
}

func masterPath(dataDir string) string { return filepath.Join(dataDir, masterFile) }

func ReadRecord(dataDir string) (map[string]any, error) {
	data, err := os.ReadFile(masterPath(dataDir))
	if err != nil {
		return nil, nil // 任何读取失败（含权限）按不存在处理
	}
	var parsed any
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	if err := dec.Decode(&parsed); err != nil {
		return nil, keyringErrf("密钥文件损坏，无法解析：%s", masterPath(dataDir))
	}
	m, ok := parsed.(map[string]any)
	if !ok {
		return nil, keyringErrf("密钥文件格式非法：%s", masterPath(dataDir))
	}
	return m, nil
}

// ModeOf 当前防护模式；无密钥文件 = legacy。记录里 mode=legacy 同样按未知拒绝。
func ModeOf(dataDir string) (string, error) {
	rec, err := ReadRecord(dataDir)
	if err != nil {
		return "", err
	}
	if rec == nil {
		return ModeLegacy, nil
	}
	mode := pyStrOrEmpty(rec["mode"])
	switch mode {
	case ModeDPAPI, ModePassphrase:
		return mode, nil
	}
	return "", keyringErrf("未知的密钥模式：'%s'", mode)
}

func IsLocked(dataDir string) (bool, error) {
	mode, err := ModeOf(dataDir)
	if err != nil {
		return false, err
	}
	return mode == ModePassphrase && currentUnlockedKey() == nil, nil
}

func Status(dataDir string) map[string]any {
	mode, err := ModeOf(dataDir)
	if err != nil {
		return map[string]any{
			"mode": "unknown", "locked": true,
			"dpapi_available": DpapiAvailable(), "error": err.Error(),
		}
	}
	locked, _ := IsLocked(dataDir)
	return map[string]any{
		"mode": mode, "locked": locked,
		"dpapi_available": DpapiAvailable(), "error": "",
	}
}

// ResolveDMK 返回当前主密钥（legacy/dpapi 每次重算，passphrase 未解锁返回 KeyLockedError）。
func ResolveDMK(dataDir string) ([]byte, error) {
	mode, err := ModeOf(dataDir)
	if err != nil {
		return nil, err
	}
	switch mode {
	case ModePassphrase:
		key := currentUnlockedKey()
		if key == nil {
			return nil, keyLockedErr("凭据处于口令保护锁定态，需先解锁")
		}
		return key, nil
	case ModeLegacy:
		return LegacyMaster(dataDir)
	}
	rec, err := ReadRecord(dataDir)
	if err != nil {
		return nil, err
	}
	if rec == nil {
		rec = map[string]any{}
	}
	return unwrapDPAPI(rec)
}

func Unlock(dataDir string, passphrase string) (bool, error) {
	rec, err := ReadRecord(dataDir)
	if err != nil {
		return false, err
	}
	if rec == nil || rec["mode"] != any(ModePassphrase) {
		return false, keyringErr("当前不是口令保护模式")
	}
	key, err := unwrapPassphrase(rec, passphrase)
	if err != nil {
		return false, err
	}
	if key == nil {
		return false, nil
	}
	LoadUnlockedKey(key)
	return true, nil
}

func WrapDPAPI(master []byte, dataDir string) (map[string]any, error) {
	if !DpapiAvailable() {
		return nil, keyringErr("系统凭据保护在当前平台不可用")
	}
	blob, err := dpapiProtect(master)
	if err != nil {
		return nil, err
	}
	return map[string]any{"v": fileVersion, "mode": ModeDPAPI, "blob": b64Encode(blob)}, nil
}

func unwrapDPAPI(rec map[string]any) ([]byte, error) {
	if !DpapiAvailable() {
		return nil, keyringErr("系统凭据保护在当前平台不可用")
	}
	raw, ok := rec["blob"]
	if !ok {
		return nil, keyringErr("DPAPI 密钥记录不完整")
	}
	blob, err := unb64(pyStr(raw))
	if err != nil {
		return nil, keyringErr("DPAPI 密钥记录不完整")
	}
	master, err := dpapiUnprotect(blob)
	if err != nil {
		return nil, keyringErr("系统凭据无法解封（换了 Windows 用户或机器？）")
	}
	return master, nil
}

// KDF 参数硬顶（log2(N)≤22、峰值内存 ≤256MiB），防止不可信文件触发内存耗尽。
const (
	kdfNMin     = 1 << 12
	kdfNMax     = 1 << 22
	kdfRMax     = 64
	kdfPMax     = 8
	kdfMemLimit = 256 << 20 // 128·N·r 峰值内存上限
	gcmTagBytes = 16
)

// scryptMu 串行化口令派生，避免并发叠加 KDF 内存。
var scryptMu sync.Mutex

type passphraseRecord struct {
	n, r, p, dk int
	salt, nonce []byte
	sealed      []byte
}

// validatePassphraseRecord 在 scrypt 派生前校验算法、参数边界与密文字段长度（禁止缺省降级）。
func validatePassphraseRecord(rec map[string]any) (*passphraseRecord, error) {
	if v, ok := pyIntStrict(rec, "v"); !ok || v != fileVersion {
		return nil, keyringErrf("密钥记录版本非法：%v", rec["v"])
	}
	if mode := pyStrOrEmpty(rec["mode"]); mode != ModePassphrase {
		return nil, keyringErrf("密钥记录模式非法：%q", mode)
	}
	kdf, ok := rec["kdf"].(map[string]any)
	if !ok {
		return nil, keyringErr("密钥记录缺少 KDF 参数")
	}
	if name := pyStrOrEmpty(kdf["name"]); name != "scrypt" {
		return nil, keyringErrf("KDF 算法非法：%q", name)
	}
	n, okN := pyIntStrict(kdf, "n")
	r, okR := pyIntStrict(kdf, "r")
	p, okP := pyIntStrict(kdf, "p")
	dk, okDK := pyIntStrict(kdf, "dklen")
	if !okN || !okR || !okP || !okDK {
		return nil, keyringErr("密钥记录 KDF 参数不完整")
	}
	if n%2 != 0 || n < kdfNMin || n > kdfNMax {
		return nil, keyringErrf("KDF 参数 N 越界：%d", n)
	}
	if r < 1 || r > kdfRMax || p < 1 || p > kdfPMax {
		return nil, keyringErrf("KDF 参数 r/p 越界：%d/%d", r, p)
	}
	if uint64(r)*uint64(p) >= 1<<30 || uint64(n)*uint64(r)*128 > kdfMemLimit {
		return nil, keyringErrf("KDF 参数组合超出内存帽：%d/%d/%d", n, r, p)
	}
	if dk != keyBytes {
		return nil, keyringErrf("KDF 派生长度非法：%d", dk)
	}
	salt, err1 := unb64(pyStrOrEmpty(rec["salt"]))
	nonce, err2 := unb64(pyStrOrEmpty(rec["nonce"]))
	sealed, err3 := unb64(pyStrOrEmpty(rec["ct"]))
	if err1 != nil || err2 != nil || err3 != nil {
		return nil, keyringErr("密钥记录字段编码非法")
	}
	if len(salt) != saltBytes {
		return nil, keyringErrf("盐长度非法：%d", len(salt))
	}
	if len(nonce) != nonceBytes {
		return nil, keyringErrf("Nonce 长度非法：%d", len(nonce))
	}
	if len(sealed) < nonceBytes+gcmTagBytes+1 {
		return nil, keyringErr("密文长度非法")
	}
	return &passphraseRecord{n: n, r: r, p: p, dk: dk, salt: salt, nonce: nonce, sealed: sealed}, nil
}

// pyIntStrict 字段缺失或为 null 返回 false，不做默认降级。
func pyIntStrict(m map[string]any, key string) (int, bool) {
	if v, present := m[key]; !present || v == nil {
		return 0, false
	}
	return pyIntOr(m, key, 0)
}

func WrapPassphrase(master []byte, passphrase string) (map[string]any, error) {
	if passphrase == "" {
		return nil, keyringErr("口令不能为空")
	}
	salt := randBytes(saltBytes)
	scryptMu.Lock()
	kek, err := scrypt.Key([]byte(passphrase), salt, scryptN, scryptR, scryptP, scryptDKLen)
	scryptMu.Unlock()
	if err != nil {
		return nil, err
	}
	nonce := randBytes(nonceBytes)
	sealed := gcmSeal(kek, nonce, master)
	return map[string]any{
		"v": fileVersion, "mode": ModePassphrase,
		"kdf":   map[string]any{"name": "scrypt", "n": scryptN, "r": scryptR, "p": scryptP, "dklen": scryptDKLen},
		"salt":  b64Encode(salt),
		"nonce": b64Encode(nonce),
		"ct":    b64Encode(sealed),
	}, nil
}

func unwrapPassphrase(rec map[string]any, passphrase string) ([]byte, error) {
	parsed, err := validatePassphraseRecord(rec)
	if err != nil {
		return nil, err
	}
	scryptMu.Lock()
	defer scryptMu.Unlock()
	kek, err := scrypt.Key([]byte(passphrase), parsed.salt, parsed.n, parsed.r, parsed.p, parsed.dk)
	if err != nil {
		return nil, keyringErrf("KDF 派生失败：%v", err)
	}
	plain, ok := gcmOpen(kek, parsed.nonce, parsed.sealed)
	if !ok {
		return nil, nil
	}
	return plain, nil
}

// WriteRecord 以固定键序与 ", "/": " 分隔符原子写入 secret.master。
func WriteRecord(dataDir string, rec map[string]any) error {
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		return keyringErrf("密钥文件写入失败（数据目录不可写？）：%v", err)
	}
	return writeAtomicText(masterPath(dataDir), dumpRecordJSON(rec), "密钥文件写入失败（数据目录不可写？）")
}

func RemoveRecord(dataDir string) error {
	if err := os.Remove(masterPath(dataDir)); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

func SnapshotRecord(dataDir string) []byte {
	data, err := os.ReadFile(masterPath(dataDir))
	if err != nil {
		return nil
	}
	return data
}

func RestoreRecord(dataDir string, raw []byte) error {
	if raw == nil {
		return RemoveRecord(dataDir)
	}
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		return keyringErrf("密钥文件恢复失败（数据目录不可写？）：%v", err)
	}
	tmp := withTmpSuffix(masterPath(dataDir))
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return keyringErrf("密钥文件恢复失败（数据目录不可写？）：%v", err)
	}
	if err := os.Rename(tmp, masterPath(dataDir)); err != nil {
		return keyringErrf("密钥文件恢复失败（数据目录不可写？）：%v", err)
	}
	return nil
}

func writeAtomicText(path, text, errPrefix string) error {
	tmp := withTmpSuffix(path)
	if err := os.WriteFile(tmp, []byte(text), 0o600); err != nil {
		return keyringErrf("%s：%v", errPrefix, err)
	}
	if runtime.GOOS != "windows" {
		_ = os.Chmod(tmp, 0o600)
	}
	if err := os.Rename(tmp, path); err != nil {
		return keyringErrf("%s：%v", errPrefix, err)
	}
	return nil
}

// withTmpSuffix 将末段扩展名替换为 .tmp（对齐 Path.with_suffix(".tmp")）。
func withTmpSuffix(path string) string {
	if i := strings.LastIndexByte(path, '.'); i >= 0 {
		return path[:i] + ".tmp"
	}
	return path + ".tmp"
}

// 记录键序 = Python 字典插入序；未知键按字典序追加在末尾保确定性。
var recordKeyOrder = map[string][]string{
	ModeDPAPI:       {"v", "mode", "blob"},
	ModePassphrase:  {"v", "mode", "kdf", "salt", "nonce", "ct"},
	"rekey-staging": {"v", "kind", "nonce", "ct"},
	"kdf":           {"name", "n", "r", "p", "dklen"},
}

func dumpRecordJSON(rec map[string]any) string {
	kind, _ := rec["kind"].(string)
	order, ok := recordKeyOrder[kind]
	if !ok {
		order = recordKeyOrder[pyStr(rec["mode"])]
	}
	var b strings.Builder
	dumpOrdered(&b, rec, order)
	return b.String()
}

func dumpOrdered(b *strings.Builder, m map[string]any, order []string) {
	b.WriteByte('{')
	first := true
	writeKV := func(k string, v any) {
		if !first {
			b.WriteString(", ")
		}
		first = false
		kb, _ := json.Marshal(k)
		b.Write(kb)
		b.WriteString(": ")
		dumpValue(b, k, v)
	}
	seen := make(map[string]bool, len(m))
	for _, k := range order {
		if v, ok := m[k]; ok {
			writeKV(k, v)
			seen[k] = true
		}
	}
	var rest []string
	for k := range m {
		if !seen[k] {
			rest = append(rest, k)
		}
	}
	slices.Sort(rest)
	for _, k := range rest {
		writeKV(k, m[k])
	}
	b.WriteByte('}')
}

func dumpValue(b *strings.Builder, key string, v any) {
	switch t := v.(type) {
	case string:
		e, _ := json.Marshal(t)
		b.Write(e)
	case bool:
		if t {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case int:
		b.WriteString(strconv.Itoa(t))
	case int64:
		b.WriteString(strconv.FormatInt(t, 10))
	case json.Number:
		b.WriteString(t.String())
	case nil:
		b.WriteString("null")
	case map[string]any:
		dumpOrdered(b, t, recordKeyOrder[key])
	case []any:
		b.WriteByte('[')
		for i, item := range t {
			if i > 0 {
				b.WriteString(", ")
			}
			dumpValue(b, "", item)
		}
		b.WriteByte(']')
	default:
		e, _ := json.Marshal(t)
		b.Write(e)
	}
}

func pyStr(v any) string {
	switch t := v.(type) {
	case nil:
		return "None"
	case string:
		return t
	case bool:
		if t {
			return "True"
		}
		return "False"
	case json.Number:
		return t.String()
	case float64:
		return strconv.FormatFloat(t, 'g', -1, 64)
	case []any, map[string]any:
		e, _ := json.Marshal(t)
		return string(e)
	}
	return ""
}

func pyStrOrEmpty(v any) string {
	if !pyTruthy(v) {
		return ""
	}
	return pyStr(v)
}

func pyTruthy(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case string:
		return t != ""
	case bool:
		return t
	case json.Number:
		s := t.String()
		return s != "" && s != "0" && s != "0.0"
	case float64:
		return t != 0
	}
	return true
}

func pyIntOr(m map[string]any, key string, def int) (int, bool) {
	v, present := m[key]
	if !present || v == nil {
		return def, true
	}
	switch t := v.(type) {
	case int:
		return t, true
	case json.Number:
		if n, err := t.Int64(); err == nil {
			return int(n), true
		}
		if f, err := t.Float64(); err == nil {
			return int(f), true
		}
		return 0, false
	case float64:
		return int(t), true
	case string:
		if n, err := strconv.Atoi(strings.TrimSpace(t)); err == nil {
			return n, true
		}
		return 0, false
	case bool:
		if t {
			return 1, true
		}
		return 0, true
	}
	return 0, false
}
