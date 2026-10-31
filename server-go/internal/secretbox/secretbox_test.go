package secretbox

import (
	"encoding/hex"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestEnc1RoundTrip(t *testing.T) {
	master := randBytes(keyBytes)
	plains := []string{"sk-0123456789abcdef", "包含中文与符号：///?#&=+ 的凭据值", strings.Repeat("x", 500)}
	for _, purpose := range []string{"steam-cookies", "kv:account.steam_api_key"} {
		for _, plain := range plains {
			token, err := EncryptWith(plain, purpose, master)
			if err != nil {
				t.Fatal(err)
			}
			if !IsEncrypted(token) {
				t.Fatalf("密文缺前缀: %s", purpose)
			}
			parts := strings.Split(strings.TrimPrefix(token, Prefix), ":")
			if len(parts) != 2 {
				t.Fatalf("密文段数 %d != 2", len(parts))
			}
			nonce, err := unb64(parts[0])
			if err != nil || len(nonce) != nonceBytes {
				t.Fatalf("nonce 长度 %d != %d", len(nonce), nonceBytes)
			}
			got, err := DecryptWith(token, purpose, master)
			if err != nil || got != plain {
				t.Fatalf("往返不一致 (%s): %v", purpose, err)
			}
		}
	}
	if got, _ := EncryptWith("", "steam-cookies", master); got != "" {
		t.Fatal("空串必须原样返回")
	}
	// 全链路径空串原样返回；显式解封路径与 Python 一致报格式不完整
	if got, err := DecryptSecretIn(t.TempDir(), "", "steam-cookies"); got != "" || err != nil {
		t.Fatalf("空串全链解封应原样返回: %q %v", got, err)
	}
	if _, err := DecryptWith("", "steam-cookies", master); err == nil || err.Error() != "密文格式不完整" {
		t.Fatalf("空串显式解封文案漂移: %v", err)
	}
}

func TestEnc1NonceRandomized(t *testing.T) {
	master := randBytes(keyBytes)
	a, _ := EncryptWith("same", "steam-cookies", master)
	b, _ := EncryptWith("same", "steam-cookies", master)
	if a == b {
		t.Fatal("同一明文两次加密得到相同密文")
	}
}

func TestDecryptWrongKeyFails(t *testing.T) {
	token, _ := EncryptWith("v", "steam-cookies", randBytes(keyBytes))
	_, err := DecryptWith(token, "steam-cookies", randBytes(keyBytes))
	if err == nil {
		t.Fatal("异钥解密未被拒绝")
	}
	if err.Error() != "凭据解密失败（密文被篡改或派生密钥不符）" {
		t.Fatalf("错误文案漂移: %q", err.Error())
	}
}

func TestDerivePurposeIsolation(t *testing.T) {
	master := randBytes(keyBytes)
	k1 := DerivePurposeKey(master, "steam-cookies")
	k2 := DerivePurposeKey(master, "proxy-subscription")
	if hex.EncodeToString(k1) == hex.EncodeToString(k2) {
		t.Fatal("不同用途派生出相同子密钥")
	}
	token, _ := EncryptWith("v", "steam-cookies", master)
	if _, err := DecryptWith(token, "proxy-subscription", master); err == nil {
		t.Fatal("跨用途解密未被拒绝")
	}
}

func TestHKDFRFC5869Vector(t *testing.T) {
	ikm := make([]byte, 22)
	for i := range ikm {
		ikm[i] = 0x0b
	}
	salt := []byte{0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c}
	info := []byte{0xf0, 0xf1, 0xf2, 0xf3, 0xf4, 0xf5, 0xf6, 0xf7, 0xf8, 0xf9}
	got := hkdfBytes(ikm, salt, info)
	want := "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
	if hex.EncodeToString(got) != want {
		t.Fatalf("HKDF-SHA256 与 RFC 5869 向量不符: %s", hex.EncodeToString(got))
	}
	// salt=nil 等价全零盐（与 Python HKDF salt=None 一致）
	if hex.EncodeToString(hkdfBytes(ikm, nil, stagingInfo)) != hex.EncodeToString(hkdfBytes(ikm, make([]byte, 32), stagingInfo)) {
		t.Fatal("nil 盐与全零盐派生不一致")
	}
}

func TestUnb64LenientLikePython(t *testing.T) {
	// Python validate=False：字母表外字符丢弃后按带 padding 解码（长度须仍对齐 4）
	a, err := unb64("c3Ry$$$$MTIz")
	if err != nil {
		t.Fatal(err)
	}
	b, err := unb64("c3RyMTIz")
	if err != nil {
		t.Fatal(err)
	}
	if hex.EncodeToString(a) != hex.EncodeToString(b) {
		t.Fatal("字母表外字符未按丢弃语义处理")
	}
	u, err := unb64("--__--__")
	if err != nil {
		t.Fatal(err)
	}
	s, err := unb64("++//++//")
	if err != nil {
		t.Fatal(err)
	}
	if hex.EncodeToString(u) != hex.EncodeToString(s) {
		t.Fatal("urlsafe 与 std 字符表映射不一致")
	}
	if _, err := unb64("ab"); err == nil {
		t.Fatal("错误 padding 应报错")
	}
}

func TestFormatReject(t *testing.T) {
	master := randBytes(keyBytes)
	cases := []struct{ token, want string }{
		{"plain-value", "凭据值不是密文格式（缺 enc1: 前缀）"},
		{"enc1:", "密文格式不完整"},
		{"enc1:onlyone", "密文格式不完整"},
		{"enc1::", "密文格式不完整"},
		{"enc1:abcde:fghij", "密文 base64 解析失败"},
	}
	for _, c := range cases {
		_, err := DecryptSecretIn(t.TempDir(), c.token, "steam-cookies")
		if err == nil {
			t.Fatalf("格式错误未被拒绝: %q", c.token)
		}
		if err.Error() != c.want {
			t.Fatalf("错误文案漂移: %q != %q", err.Error(), c.want)
		}
	}
	// 病态输入（空 nonce 等）统一按解密失败拒绝，不得 panic
	if _, err := DecryptWith("enc1:!!:!!", "steam-cookies", master); err == nil {
		t.Fatal("病态密文未被拒绝")
	}
	// 篡改密文体：认证标签必须拒绝
	token, _ := EncryptWith("v", "steam-cookies", master)
	parts := strings.Split(token, ":")
	tampered := parts[0] + ":" + parts[1] + ":" + "A" + parts[2][1:]
	if _, err := DecryptWith(tampered, "steam-cookies", master); err == nil {
		t.Fatal("篡改密文未被拒绝")
	}
	// 截断密文体：必须报错
	if _, err := DecryptWith(parts[0]+":"+parts[1]+":"+parts[2][:len(parts[2])-2], "steam-cookies", master); err == nil {
		t.Fatal("截断密文未被拒绝")
	}
}

func TestLegacyMasterDeterministicPerDataDir(t *testing.T) {
	dir := t.TempDir()
	a, err := LegacyMaster(dir)
	if err != nil {
		t.Fatal(err)
	}
	b, err := LegacyMaster(dir)
	if err != nil {
		t.Fatal(err)
	}
	if hex.EncodeToString(a) != hex.EncodeToString(b) {
		t.Fatal("同数据目录 legacy 主密钥不稳定")
	}
	if _, err := os.Stat(filepath.Join(dir, saltFile)); err != nil {
		t.Fatal("安装盐未落盘")
	}
	other, err := LegacyMaster(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if hex.EncodeToString(a) == hex.EncodeToString(other) {
		t.Fatal("不同数据目录共钥（安装盐未隔离）")
	}
}
