// Package secretbox 凭据静态加密（AES-GCM + HKDF-SHA256）。
// 密文格式 enc1:<b64url(nonce)>:<b64url(ct+tag)>，AAD 恒空；跨运行时逐字节固定。
// 用途子密钥 = HKDF-SHA256(DMK, salt=purposeSalt, info="holdexar:secretbox:<用途>")。
package secretbox

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hkdf"
	crand "crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strings"
	"unicode/utf8"

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

type SecretBoxError struct{ msg string }

func (e *SecretBoxError) Error() string { return e.msg }

type KeyringError struct{ msg string }

func (e *KeyringError) Error() string { return e.msg }

// KeyLockedError 口令保护模式处于锁定态：凭据不可读写。
type KeyLockedError struct{ msg string }

func (e *KeyLockedError) Error() string { return e.msg }

func secretBoxErr(msg string) error        { return &SecretBoxError{msg: msg} }
func keyringErr(msg string) error          { return &KeyringError{msg: msg} }
func keyringErrf(f string, a ...any) error { return &KeyringError{msg: fmt.Sprintf(f, a...)} }
func keyLockedErr(msg string) error        { return &KeyLockedError{msg: msg} }

func IsEncrypted(value string) bool {
	return value != "" && strings.HasPrefix(value, Prefix)
}

func DerivePurposeKey(master []byte, purpose string) []byte {
	return hkdfBytes(master, purposeSalt, []byte("holdexar:secretbox:"+purpose))
}

// LegacyMaster 由机器指纹或目录兜底密钥与安装盐经 HKDF-SHA256 派生主密钥。
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
	out, err := hkdf.Key(sha256.New, ikm, salt, string(info), keyBytes)
	if err != nil {
		panic(err)
	}
	return out
}

func EncryptWith(plaintext, purpose string, master []byte) (string, error) {
	if plaintext == "" {
		return "", nil
	}
	nonce := randBytes(nonceBytes)
	sealed := gcmSeal(DerivePurposeKey(master, purpose), nonce, []byte(plaintext))
	return Prefix + b64Encode(nonce) + ":" + b64Encode(sealed), nil
}

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

func EncryptSecret(plaintext, purpose string) (string, error) {
	dataDir, err := resolveDataDir()
	if err != nil {
		return "", err
	}
	return EncryptSecretIn(dataDir, plaintext, purpose)
}

func DecryptSecret(token, purpose string) (string, error) {
	dataDir, err := resolveDataDir()
	if err != nil {
		return "", err
	}
	return DecryptSecretIn(dataDir, token, purpose)
}

func EncryptSecretIn(dataDir, plaintext, purpose string) (string, error) {
	master, err := ResolveDMK(dataDir)
	if err != nil {
		return "", secretBoxErr(err.Error())
	}
	return EncryptWith(plaintext, purpose, master)
}

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

// candidateKeys 按「当前 DMK → 过渡旧密钥 → legacy」顺序生成候选密钥；当前 DMK 不可用时直接报错。
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

func parseToken(token string) (nonce, sealed []byte, err error) {
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

func gcmSeal(key, nonce, plain []byte) []byte {
	g, err := gcmCipher(key)
	if err != nil {
		panic(err)
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
		panic(err)
	}
	return b
}

func hexEncode(b []byte) string { return fmt.Sprintf("%x", b) }

func resolveDataDir() (string, error) { return config.ResolveDataDir() }

func mustJSONString(s string) string {
	b, err := json.Marshal(s)
	if err != nil {
		panic(err)
	}
	return string(b)
}

func NewSecretBoxError(msg string) error { return secretBoxErr(msg) }
