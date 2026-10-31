// Package secretbox：凭据静态加密与数据主密钥托管。
//
// 密文格式 enc1:<b64url(nonce)>:<b64url(密文+认证标签)>，AAD 恒空，是跨运行时
// 不变量：Go 与 Python 侧写的密文必须互解。HKDF 盐/info 常量与密钥文件 JSON
// 形状逐字节固定，不得改动。
//
// 密钥分层：数据主密钥（DMK）由 keyring 三模式提供（legacy 机器派生 / DPAPI /
// 口令 scrypt，见 keyring.go）；用途子密钥 = HKDF-SHA256(master, salt=固定,
// info="holdexar:secretbox:<用途>")，跨用途解密直接失败。
package secretbox

import (
	"crypto/aes"
	"crypto/cipher"
	crand "crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"strings"
	"unicode/utf8"

	"golang.org/x/crypto/hkdf"

	"holdexar/server-go/internal/config"
)

const (
	// Prefix 密文自描述前缀：读取侧据此区分密文与存量明文。
	Prefix = "enc1:"

	nonceBytes      = 12
	keyBytes        = 32
	saltFile        = "secret.salt"
	saltBytes       = 16
	fallbackKeyFile = "secret.machine.key"
)

// HKDF 盐与 info 常量（跨语言不变量）。
var (
	masterInfo  = []byte("holdexar:secretbox:master:v1")
	purposeSalt = []byte("holdexar:secretbox:purpose")
)

// ── 错误类型（调用方按类型区分降级路径）─────────────────────

// SecretBoxError 凭据解密失败（密文被篡改、派生密钥不符、格式不识别或锁定态）。
type SecretBoxError struct{ msg string }

func (e *SecretBoxError) Error() string { return e.msg }

// KeyringError 密钥托管操作失败（封装不可用 / 口令不正确 / 文件损坏）。
type KeyringError struct{ msg string }

func (e *KeyringError) Error() string { return e.msg }

// KeyLockedError 口令保护模式处于锁定态：凭据不可读写。
type KeyLockedError struct{ msg string }

func (e *KeyLockedError) Error() string { return e.msg }

func secretBoxErr(msg string) error        { return &SecretBoxError{msg: msg} }
func keyringErr(msg string) error          { return &KeyringError{msg: msg} }
func keyringErrf(f string, a ...any) error { return &KeyringError{msg: fmt.Sprintf(f, a...)} }
func keyLockedErr(msg string) error        { return &KeyLockedError{msg: msg} }

// ── 用途派生 ─────────────────────────────────────────────

// IsEncrypted 值是否为 enc1: 密文（存量明文为 false）。
func IsEncrypted(value string) bool {
	return value != "" && strings.HasPrefix(value, Prefix)
}

// DerivePurposeKey 主密钥 → 用途子密钥（各用途 info 不同，彼此计算独立）。
func DerivePurposeKey(master []byte, purpose string) []byte {
	return hkdfBytes(master, purposeSalt, []byte("holdexar:secretbox:"+purpose))
}

// LegacyMaster legacy 数据主密钥：机器指纹 + 安装盐 → HKDF-SHA256。
// 指纹与安装盐的获取顺序对齐 Python：注册表指纹 → 机器文件指纹 → 数据目录
// 随机兜底文件；安装盐缺失时落盘新随机数。
func LegacyMaster(dataDir string) ([]byte, error) {
	fingerprint, err := machineFingerprintOrFallback(dataDir)
	if err != nil {
		return nil, err
	}
	salt, err := installSalt(dataDir)
	if err != nil {
		return nil, err
	}
	return hkdfBytes(fingerprint, salt, masterInfo), nil
}

func hkdfBytes(ikm, salt, info []byte) []byte {
	r := hkdf.New(sha256.New, ikm, salt, info)
	out := make([]byte, keyBytes)
	_, _ = io.ReadFull(r, out)
	return out
}

// ── 封装 / 解封 ──────────────────────────────────────────

// EncryptWith 用指定主密钥封装（换钥重加密用；空串原样返回）。
func EncryptWith(plaintext, purpose string, master []byte) (string, error) {
	if plaintext == "" {
		return "", nil
	}
	nonce := randBytes(nonceBytes)
	sealed := gcmSeal(DerivePurposeKey(master, purpose), nonce, []byte(plaintext))
	return Prefix + b64Encode(nonce) + ":" + b64Encode(sealed), nil
}

// DecryptWith 用指定主密钥解封（换钥重加密用）。失败抛 SecretBoxError。
func DecryptWith(token, purpose string, master []byte) (string, error) {
	nonce, sealed, err := parseToken(token)
	if err != nil {
		return "", err
	}
	plain, ok := gcmOpen(DerivePurposeKey(master, purpose), nonce, sealed)
	if !ok {
		return "", secretBoxErr("凭据解密失败（密文被篡改或派生密钥不符）")
	}
	return decodePlain(plain)
}

// EncryptSecret 明文 → enc1: 密文（当前主密钥）。空串原样返回。
func EncryptSecret(plaintext, purpose string) (string, error) {
	dataDir, err := resolveDataDir()
	if err != nil {
		return "", err
	}
	return EncryptSecretIn(dataDir, plaintext, purpose)
}

// DecryptSecret enc1: 密文 → 明文（候选链）。空串原样返回，非密文格式报错。
func DecryptSecret(token, purpose string) (string, error) {
	dataDir, err := resolveDataDir()
	if err != nil {
		return "", err
	}
	return DecryptSecretIn(dataDir, token, purpose)
}

// EncryptSecretIn 明文 → enc1: 密文，数据目录显式注入（多实例 / 测试用）。
func EncryptSecretIn(dataDir, plaintext, purpose string) (string, error) {
	master, err := ResolveDMK(dataDir)
	if err != nil {
		return "", secretBoxErr(err.Error())
	}
	return EncryptWith(plaintext, purpose, master)
}

// DecryptSecretIn enc1: 密文 → 明文，数据目录显式注入。
// 候选链：当前主密钥 → 换钥过渡记录旧主密钥 → legacy 兜底（非 legacy 模式时）。
func DecryptSecretIn(dataDir, token, purpose string) (string, error) {
	if token == "" {
		return "", nil
	}
	if !strings.HasPrefix(token, Prefix) {
		return "", secretBoxErr("凭据值不是密文格式（缺 enc1: 前缀）")
	}
	nonce, sealed, err := parseToken(token)
	if err != nil {
		return "", err
	}
	keys, err := candidateKeys(purpose, dataDir)
	if err != nil {
		return "", err
	}
	var last error
	for _, key := range keys {
		plain, ok := gcmOpen(key, nonce, sealed)
		if !ok {
			last = secretBoxErr("凭据解密失败（密文被篡改或派生密钥不符）")
			continue
		}
		return decodePlain(plain)
	}
	if last != nil {
		return "", last
	}
	return "", secretBoxErr("凭据解密失败")
}

// candidateKeys 解密候选密钥（顺序即优先级）。
// 当前主密钥解析失败（口令未解锁 / 文件损坏）直接报错：锁定态不得绕过。
func candidateKeys(purpose, dataDir string) ([][]byte, error) {
	master, err := ResolveDMK(dataDir)
	if err != nil {
		return nil, secretBoxErr(err.Error())
	}
	keys := [][]byte{DerivePurposeKey(master, purpose)}
	staged, err := StagingMaster(dataDir, "")
	if err == nil && staged != nil {
		keys = append(keys, DerivePurposeKey(staged, purpose))
	}
	mode, err := ModeOf(dataDir)
	if err != nil {
		return nil, secretBoxErr(err.Error())
	}
	if mode != ModeLegacy {
		legacy, err := LegacyMaster(dataDir)
		if err != nil {
			return nil, err
		}
		keys = append(keys, DerivePurposeKey(legacy, purpose))
	}
	return keys, nil
}

// ── enc1 编解码 ──────────────────────────────────────────

func parseToken(token string) (nonce, sealed []byte, err error) {
	// 对齐 Python 切片语义：短于前缀的输入 rest 为空串，走「格式不完整」
	var rest string
	if len(token) >= len(Prefix) {
		rest = token[len(Prefix):]
	}
	i := strings.Index(rest, ":")
	if i < 0 {
		return nil, nil, secretBoxErr("密文格式不完整")
	}
	nonceB64, sealedB64 := rest[:i], rest[i+1:]
	if nonceB64 == "" || sealedB64 == "" {
		return nil, nil, secretBoxErr("密文格式不完整")
	}
	nonce, err1 := unb64(nonceB64)
	sealed, err2 := unb64(sealedB64)
	if err1 != nil || err2 != nil {
		return nil, nil, secretBoxErr("密文 base64 解析失败")
	}
	return nonce, sealed, nil
}

// unb64 对齐 Python urlsafe_b64decode：先丢弃字母表外字符，再按带 padding 解码。
func unb64(s string) ([]byte, error) {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= 'A' && c <= 'Z', c >= 'a' && c <= 'z', c >= '0' && c <= '9',
			c == '-', c == '_', c == '+', c == '/', c == '=':
			b.WriteByte(c)
		}
	}
	std := strings.NewReplacer("-", "+", "_", "/").Replace(b.String())
	return base64.StdEncoding.DecodeString(std)
}

func b64Encode(data []byte) string { return base64.URLEncoding.EncodeToString(data) }

func decodePlain(plain []byte) (string, error) {
	if !utf8.Valid(plain) {
		return "", secretBoxErr("凭据明文编码异常")
	}
	return string(plain), nil
}

// ── AES-256-GCM / 随机数 ─────────────────────────────────

func gcmSeal(key, nonce, plain []byte) []byte {
	g, err := gcmCipher(key)
	if err != nil {
		panic(err) // 32 字节密钥恒可建 GCM
	}
	return g.Seal(nil, nonce, plain, nil)
}

func gcmOpen(key, nonce, sealed []byte) ([]byte, bool) {
	g, err := gcmCipher(key)
	if err != nil {
		return nil, false
	}
	// 异长 nonce 统一按解密失败拒绝（Go 侧 Open 对异长 nonce 是 panic 不是报错）
	if len(nonce) != g.NonceSize() {
		return nil, false
	}
	plain, err := g.Open(nil, nonce, sealed, nil)
	if err != nil {
		return nil, false
	}
	return plain, true
}

func gcmCipher(key []byte) (cipher.AEAD, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	return cipher.NewGCM(block)
}

func randBytes(n int) []byte {
	b := make([]byte, n)
	if _, err := crand.Read(b); err != nil {
		panic(err) // 系统熵源不可用属致命配置
	}
	return b
}

func hexEncode(b []byte) string { return fmt.Sprintf("%x", b) }

// resolveDataDir 全局数据目录（密钥文件落点）；显式注入场景走 *In 变体。
func resolveDataDir() (string, error) { return config.ResolveDataDir() }

func mustJSONString(s string) string {
	b, err := json.Marshal(s)
	if err != nil {
		panic(err) // string 恒可序列化
	}
	return string(b)
}
