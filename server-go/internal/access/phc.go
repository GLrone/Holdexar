package access

// 访问密码 PHC 验证器（RFC 7914 scrypt，格式 $scrypt$<n>,<r>,<p>$<salt>$<hash>）。

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"fmt"
	"strconv"
	"strings"

	"golang.org/x/crypto/scrypt"
)

const (
	phcAlgo      = "scrypt"
	phcN         = 1 << 15
	phcR         = 8
	phcP         = 1
	phcDKLen     = 32
	phcSaltBytes = 16
	phcHashBytes = 32
	phcNMin      = 1 << 12
	phcNMax      = 1 << 22
	phcRMax      = 64
	phcPMax      = 8
	phcMemLimit  = 256 << 20
)

func HashPassword(password string) string {
	salt := make([]byte, phcSaltBytes)
	if _, err := rand.Read(salt); err != nil {
		panic("PHC 盐生成失败：" + err.Error())
	}
	derived, err := scrypt.Key([]byte(password), salt, phcN, phcR, phcP, phcDKLen)
	if err != nil {
		panic("scrypt 派生失败：" + err.Error())
	}
	return fmt.Sprintf("$%s$%d,%d,%d$%s$%s",
		phcAlgo, phcN, phcR, phcP,
		base64.RawURLEncoding.EncodeToString(salt),
		base64.RawURLEncoding.EncodeToString(derived))
}

// parsePHC 解析验证器并在派生前校验 KDF 参数与内存上限。
func parsePHC(phc string) (n, r, p int, salt, expected []byte, err error) {
	parts := strings.Split(phc, "$")
	if len(parts) != 5 || parts[0] != "" || parts[1] != phcAlgo {
		return 0, 0, 0, nil, nil, fmt.Errorf("验证器格式非法")
	}
	seg := strings.Split(parts[2], ",")
	if len(seg) != 3 {
		return 0, 0, 0, nil, nil, fmt.Errorf("验证器参数非法")
	}
	n, errN := strconv.Atoi(seg[0])
	r, errR := strconv.Atoi(seg[1])
	p, errP := strconv.Atoi(seg[2])
	if errN != nil || errR != nil || errP != nil {
		return 0, 0, 0, nil, nil, fmt.Errorf("验证器参数非法")
	}
	if n%2 != 0 || n < phcNMin || n > phcNMax {
		return 0, 0, 0, nil, nil, fmt.Errorf("验证器参数 N 越界")
	}
	if r < 1 || r > phcRMax || p < 1 || p > phcPMax {
		return 0, 0, 0, nil, nil, fmt.Errorf("验证器参数 r/p 越界")
	}
	if r*p >= 1<<30 || 128*n*r > phcMemLimit {
		return 0, 0, 0, nil, nil, fmt.Errorf("验证器参数组合超出内存帽")
	}
	salt, err = base64.RawURLEncoding.DecodeString(parts[3])
	if err != nil || len(salt) != phcSaltBytes {
		return 0, 0, 0, nil, nil, fmt.Errorf("验证器盐非法")
	}
	expected, err = base64.RawURLEncoding.DecodeString(parts[4])
	if err != nil || len(expected) < phcHashBytes {
		return 0, 0, 0, nil, nil, fmt.Errorf("验证器哈希长度非法")
	}
	return n, r, p, salt, expected, nil
}

// VerifyPassword 以恒定时间比对口令与 PHC 验证器。
func VerifyPassword(password, phc string) bool {
	n, r, p, salt, expected, err := parsePHC(phc)
	if err != nil {
		return false
	}
	derived, kerr := scrypt.Key([]byte(password), salt, n, r, p, len(expected))
	if kerr != nil {
		return false
	}
	return subtle.ConstantTimeCompare(derived, expected) == 1
}
