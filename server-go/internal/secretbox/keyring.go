package secretbox

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"unsafe"

	"golang.org/x/crypto/scrypt"
	"golang.org/x/sys/windows"
)

// 数据主密钥托管：三模式（落点数据目录 secret.master；文件不存在 = legacy）。
// legacy 主密钥由机器指纹 + 安装盐派生；dpapi 主密钥为随机数经系统凭据封装；
// passphrase 主密钥为随机数经口令 scrypt 派生的 KEK 封装，解锁前凭据不可读写。
// 进程内只保留口令模式解封后的主密钥（不落盘），重启即失效。

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

// dpapiEntropy DPAPI 附加熵（第二因素）：blob 被其他进程拿到也无法在白盒外解封。
var dpapiEntropy = []byte("holdexar:keyring:dpapi:v1")

// ── 进程内解封态 ─────────────────────────────────────────

var (
	lockMu      sync.Mutex
	unlockedKey []byte
)

// ResetState 清进程内主密钥缓存（换钥 / 换数据目录 / 测试隔离后调用）。
func ResetState() {
	lockMu.Lock()
	defer lockMu.Unlock()
	unlockedKey = nil
}

// LoadUnlockedKey 把已解封的主密钥写入进程态（换钥成功后由调用方回填）。
func LoadUnlockedKey(key []byte) {
	lockMu.Lock()
	defer lockMu.Unlock()
	unlockedKey = bytes.Clone(key)
}

// Lock 清空进程内主密钥（口令模式回到锁定态）。
func Lock() { ResetState() }

func currentUnlockedKey() []byte {
	lockMu.Lock()
	defer lockMu.Unlock()
	return bytes.Clone(unlockedKey)
}

// ── 模式判定 ─────────────────────────────────────────────

func masterPath(dataDir string) string { return filepath.Join(dataDir, masterFile) }

// ReadRecord 读取密钥文件（不存在返回 nil；损坏或非对象报 KeyringError）。
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

// IsLocked 口令模式且尚未解锁时为 true（其余模式恒 false）。
func IsLocked(dataDir string) (bool, error) {
	mode, err := ModeOf(dataDir)
	if err != nil {
		return false, err
	}
	return mode == ModePassphrase && currentUnlockedKey() == nil, nil
}

// DpapiAvailable 系统凭据保护是否可用（仅 Windows）。
func DpapiAvailable() bool { return runtime.GOOS == "windows" }

// Status 面向界面的状态投影（不泄露主密钥）。
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

// ── 主密钥解析 ───────────────────────────────────────────

// ResolveDMK 返回当前主密钥；口令模式未解锁抛 KeyLockedError。
// legacy / dpapi 每次重算（可随时重算，无陈旧缓存问题）；口令模式用进程内解封态。
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

// Unlock 口令模式解锁：口令正确则写入进程态主密钥，返回是否成功。
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
		return false, nil // 口令不正确
	}
	LoadUnlockedKey(key)
	return true, nil
}

// ── DPAPI 封装 ───────────────────────────────────────────

// WrapDPAPI 随机主密钥 → DPAPI 封装记录（仅 Windows）。
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

// ── 口令封装（scrypt 派生 KEK + AES-256-GCM）────────────────

// WrapPassphrase 随机主密钥 → 口令封装记录。
func WrapPassphrase(master []byte, passphrase string) (map[string]any, error) {
	if passphrase == "" {
		return nil, keyringErr("口令不能为空")
	}
	salt := randBytes(saltBytes)
	kek, err := scrypt.Key([]byte(passphrase), salt, scryptN, scryptR, scryptP, scryptDKLen)
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

// unwrapPassphrase 口令封装 → 主密钥；口令不正确返回 nil。
func unwrapPassphrase(rec map[string]any, passphrase string) ([]byte, error) {
	kdf, _ := rec["kdf"].(map[string]any)
	salt, e1 := unb64(pyStr(rec["salt"]))
	nonce, e2 := unb64(pyStr(rec["nonce"]))
	sealed, e3 := unb64(pyStr(rec["ct"]))
	if e1 != nil || e2 != nil || e3 != nil {
		return nil, keyringErr("口令密钥记录不完整")
	}
	n, ok1 := pyIntOr(kdf, "n", scryptN)
	r, ok2 := pyIntOr(kdf, "r", scryptR)
	p, ok3 := pyIntOr(kdf, "p", scryptP)
	dk, ok4 := pyIntOr(kdf, "dklen", scryptDKLen)
	if !ok1 || !ok2 || !ok3 || !ok4 {
		return nil, keyringErr("口令密钥记录不完整")
	}
	kek, err := scrypt.Key([]byte(passphrase), salt, n, r, p, dk)
	if err != nil {
		return nil, err
	}
	plain, ok := gcmOpen(kek, nonce, sealed)
	if !ok {
		return nil, nil // 认证标签不符 = 口令错误
	}
	return plain, nil
}

// ── 密钥文件落盘 ─────────────────────────────────────────

// WriteRecord 密钥文件落盘（先写临时文件再替换：断电时要么旧要么新，不留半截）。
// JSON 形状与 Python json.dumps(ensure_ascii=False) 逐字节一致（键序固定、
// 分隔符 ", "/": "）。
func WriteRecord(dataDir string, rec map[string]any) error {
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		return keyringErrf("密钥文件写入失败（数据目录不可写？）：%v", err)
	}
	return writeAtomicText(masterPath(dataDir), dumpRecordJSON(rec), "密钥文件写入失败（数据目录不可写？）")
}

// RemoveRecord 删除密钥文件（回到 legacy 模式）。
func RemoveRecord(dataDir string) error {
	if err := os.Remove(masterPath(dataDir)); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// SnapshotRecord 读取密钥文件原始字节（换钥失败回滚用）；不存在返回 nil。
func SnapshotRecord(dataDir string) []byte {
	data, err := os.ReadFile(masterPath(dataDir))
	if err != nil {
		return nil
	}
	return data
}

// RestoreRecord 按原始字节恢复密钥文件（先临时后替换）；nil = 删除。
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

// writeAtomicText 先写临时文件再替换；POSIX 收紧 0600，Windows 走 ACL 继承。
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

// withTmpSuffix 临时文件名：末段后缀替换为 .tmp（对齐 Path.with_suffix(".tmp")，
// secret.master → secret.tmp、secret.master.staging → secret.master.tmp）。
func withTmpSuffix(path string) string {
	if i := strings.LastIndexByte(path, '.'); i >= 0 {
		return path[:i] + ".tmp"
	}
	return path + ".tmp"
}

// ── 记录序列化（Python json.dumps 形状）────────────────────

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
	sort.Strings(rest)
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

// ── Python 值语义窄面复刻 ─────────────────────────────────

// pyStr Python str() 窄面复刻（记录字段诊断与 base64 取值用）。
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

// pyStrOrEmpty Python str(x or "")：假值（None/空串/0/False）回空串。
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

// pyIntOr Python kdf.get(key, default) 后 int() 的窄面复刻；不可解析返回 false。
func pyIntOr(m map[string]any, key string, def int) (int, bool) {
	v, present := m[key]
	if !present || v == nil {
		return def, true
	}
	switch t := v.(type) {
	case json.Number:
		if n, err := t.Int64(); err == nil {
			return int(n), true
		}
		if f, err := t.Float64(); err == nil {
			return int(f), true // int() 向零截断
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

// ── DPAPI 系统调用 ───────────────────────────────────────

func dpapiProtect(data []byte) ([]byte, error) { return dpapiCall(data, true) }

func dpapiUnprotect(blob []byte) ([]byte, error) { return dpapiCall(blob, false) }

func dpapiCall(data []byte, protect bool) ([]byte, error) {
	if len(data) == 0 {
		return nil, keyringErr("系统凭据数据为空")
	}
	in := &windows.DataBlob{Size: uint32(len(data)), Data: &data[0]}
	ent := &windows.DataBlob{Size: uint32(len(dpapiEntropy)), Data: &dpapiEntropy[0]}
	var out windows.DataBlob
	var err error
	if protect {
		err = windows.CryptProtectData(in, nil, ent, 0, nil, windows.CRYPTPROTECT_UI_FORBIDDEN, &out)
	} else {
		err = windows.CryptUnprotectData(in, nil, ent, 0, nil, windows.CRYPTPROTECT_UI_FORBIDDEN, &out)
	}
	if err != nil {
		return nil, err
	}
	result := bytes.Clone(unsafe.Slice(out.Data, out.Size))
	windows.LocalFree(windows.Handle(unsafe.Pointer(out.Data)))
	return result, nil
}
