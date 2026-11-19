package secretbox

import (
	"strings"
	"testing"
)

func validPassphraseRecord(t *testing.T) map[string]any {
	t.Helper()
	master := make([]byte, keyBytes)
	for i := range master {
		master[i] = byte(i)
	}
	rec, err := WrapPassphrase(master, "attack-test-passphrase")
	if err != nil {
		t.Fatalf("生成合法记录失败：%v", err)
	}
	return rec
}

func TestValidatePassphraseRecordMalicious(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(rec map[string]any)
	}{
		{"未知 KDF 算法", func(r map[string]any) { r["kdf"].(map[string]any)["name"] = "argon2id" }},
		{"N 极大值（内存耗尽向量）", func(r map[string]any) { r["kdf"].(map[string]any)["n"] = 1 << 40 }},
		{"N 超出硬顶", func(r map[string]any) { r["kdf"].(map[string]any)["n"] = 1 << 23 }},
		{"N 过小", func(r map[string]any) { r["kdf"].(map[string]any)["n"] = 2 }},
		{"N 非偶数", func(r map[string]any) { r["kdf"].(map[string]any)["n"] = 1<<15 + 1 }},
		{"N 非法值", func(r map[string]any) { r["kdf"].(map[string]any)["n"] = -1 << 15 }},
		{"r 零", func(r map[string]any) { r["kdf"].(map[string]any)["r"] = 0 }},
		{"r 极端值", func(r map[string]any) { r["kdf"].(map[string]any)["r"] = 1 << 20 }},
		{"p 零", func(r map[string]any) { r["kdf"].(map[string]any)["p"] = 0 }},
		{"r*p 整数溢出组合", func(r map[string]any) {
			r["kdf"].(map[string]any)["r"] = 1 << 29
			r["kdf"].(map[string]any)["p"] = 4
		}},
		{"盐长度异常", func(r map[string]any) { r["salt"] = "AAAA" }},
		{"Nonce 截断", func(r map[string]any) { r["nonce"] = "AAAA" }},
		{"密文截断", func(r map[string]any) { r["ct"] = "AAAA" }},
		{"未知版本", func(r map[string]any) { r["v"] = 2 }},
		{"缺少 KDF 参数", func(r map[string]any) { delete(r, "kdf") }},
		{"KDF 参数缺失字段", func(r map[string]any) { delete(r["kdf"].(map[string]any), "n") }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := validPassphraseRecord(t)
			tc.mutate(rec)
			_, err := unwrapPassphrase(rec, "attack-test-passphrase")
			if err == nil {
				t.Fatalf("恶意记录未被拒绝（类型 %s）", tc.name)
			}
			if strings.Contains(err.Error(), "KDF 派生失败") {
				t.Fatalf("记录应在派生前被拒绝，实际进入派生层：%v", err)
			}
		})
	}
}

func TestValidatePassphraseRecordAcceptsCurrentFormat(t *testing.T) {
	rec := validPassphraseRecord(t)
	if _, err := validatePassphraseRecord(rec); err != nil {
		t.Fatalf("现役格式被校验拒绝：%v", err)
	}
	master := make([]byte, keyBytes)
	for i := range master {
		master[i] = byte(i)
	}
	plain, err := unwrapPassphrase(rec, "attack-test-passphrase")
	if err != nil {
		t.Fatalf("合法记录解锁失败：%v", err)
	}
	for i := range master {
		if plain[i] != master[i] {
			t.Fatal("解锁主密钥与封装时不一致")
		}
	}
}

func TestUnlockConcurrentStress(t *testing.T) {
	rec := validPassphraseRecord(t)
	done := make(chan error, 8)
	for i := 0; i < 8; i++ {
		go func() {
			_, err := unwrapPassphrase(rec, "attack-test-passphrase")
			done <- err
		}()
	}
	for i := 0; i < 8; i++ {
		if err := <-done; err != nil {
			t.Fatalf("并发解锁失败：%v", err)
		}
	}
}
