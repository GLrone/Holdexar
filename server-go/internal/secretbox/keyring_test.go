package secretbox

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/crypto/scrypt"
)

func TestModeOfDefaults(t *testing.T) {
	dir := t.TempDir()
	mode, err := ModeOf(dir)
	if err != nil || mode != ModeLegacy {
		t.Fatalf("无密钥文件应为 legacy: %q %v", mode, err)
	}
	locked, err := IsLocked(dir)
	if err != nil || locked {
		t.Fatalf("legacy 恒非锁定: %v %v", locked, err)
	}

	m := randBytes(keyBytes)
	rec, err := WrapPassphrase(m, "pw")
	if err != nil {
		t.Fatal(err)
	}
	if err := WriteRecord(dir, rec); err != nil {
		t.Fatal(err)
	}
	mode, err = ModeOf(dir)
	if err != nil || mode != ModePassphrase {
		t.Fatalf("口令记录应判为 passphrase: %q %v", mode, err)
	}
	ResetState()
	t.Cleanup(ResetState)
	locked, _ = IsLocked(dir)
	if !locked {
		t.Fatal("口令模式未解锁应为锁定")
	}

	if err := WriteRecord(dir, map[string]any{"v": fileVersion, "mode": "bogus"}); err != nil {
		t.Fatal(err)
	}
	_, err = ModeOf(dir)
	if err == nil || err.Error() != "未知的密钥模式：'bogus'" {
		t.Fatalf("未知模式文案漂移: %v", err)
	}
	if err := WriteRecord(dir, map[string]any{"v": fileVersion, "mode": ModeLegacy}); err != nil {
		t.Fatal(err)
	}
	_, err = ModeOf(dir)
	if err == nil || err.Error() != "未知的密钥模式：'legacy'" {
		t.Fatalf("记录内 mode=legacy 应同样拒绝: %v", err)
	}
}

func TestRecordJSONShapePinned(t *testing.T) {
	// 键序与分隔符逐字节对齐 Python json.dumps(record, ensure_ascii=False)
	dpapi := dumpRecordJSON(map[string]any{"v": fileVersion, "mode": ModeDPAPI, "blob": "AAA"})
	if dpapi != `{"v": 1, "mode": "dpapi", "blob": "AAA"}` {
		t.Fatalf("dpapi 记录形状漂移: %s", dpapi)
	}
	pass := dumpRecordJSON(map[string]any{
		"v": fileVersion, "mode": ModePassphrase,
		"kdf":   map[string]any{"name": "scrypt", "n": scryptN, "r": scryptR, "p": scryptP, "dklen": scryptDKLen},
		"salt":  "S",
		"nonce": "N",
		"ct":    "C",
	})
	if pass != `{"v": 1, "mode": "passphrase", "kdf": {"name": "scrypt", "n": 32768, "r": 8, "p": 1, "dklen": 32}, "salt": "S", "nonce": "N", "ct": "C"}` {
		t.Fatalf("passphrase 记录形状漂移: %s", pass)
	}
	staging := dumpRecordJSON(map[string]any{"v": fileVersion, "kind": stagingKind, "nonce": "N", "ct": "C"})
	if staging != `{"v": 1, "kind": "rekey-staging", "nonce": "N", "ct": "C"}` {
		t.Fatalf("staging 记录形状漂移: %s", staging)
	}
}

func TestWriteRecordTmpFileLifecycle(t *testing.T) {
	dir := t.TempDir()
	m := randBytes(keyBytes)
	rec, _ := WrapPassphrase(m, "pw")
	if err := WriteRecord(dir, rec); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].Name() != masterFile {
		t.Fatalf("目录残留异常: %v", entries)
	}
	back, err := ReadRecord(dir)
	if err != nil || back == nil {
		t.Fatalf("写后读失败: %v", err)
	}
	if back["mode"] != any(ModePassphrase) {
		t.Fatalf("记录 mode 读回漂移: %v", back["mode"])
	}
	if withTmpSuffix("/x/secret.master") != "/x/secret.tmp" {
		t.Fatal("主密钥临时文件名漂移")
	}
	if withTmpSuffix("/x/secret.master.staging") != "/x/secret.master.tmp" {
		t.Fatal("过渡记录临时文件名漂移")
	}
}

func TestUnlockLifecycle(t *testing.T) {
	dir := t.TempDir()
	ResetState()
	t.Cleanup(ResetState)
	m := randBytes(keyBytes)
	rec, _ := WrapPassphrase(m, "right-pass")
	if err := WriteRecord(dir, rec); err != nil {
		t.Fatal(err)
	}

	if _, err := ResolveDMK(dir); err == nil || err.Error() != "凭据处于口令保护锁定态，需先解锁" {
		t.Fatalf("锁定态文案漂移: %v", err)
	} else {
		var locked *KeyLockedError
		if !errors.As(err, &locked) {
			t.Fatalf("锁定错误类型漂移: %T", err)
		}
	}

	okUnlock, err := Unlock(t.TempDir(), "x")
	if okUnlock || err == nil || err.Error() != "当前不是口令保护模式" {
		t.Fatalf("非口令模式解锁拒绝漂移: %v %v", okUnlock, err)
	}
	if okUnlock, err := Unlock(dir, "wrong"); okUnlock || err != nil {
		t.Fatalf("错口令应返回 false 无错: %v %v", okUnlock, err)
	}
	if okUnlock, err := Unlock(dir, "right-pass"); !okUnlock || err != nil {
		t.Fatalf("正确口令解锁失败: %v %v", okUnlock, err)
	}
	got, err := ResolveDMK(dir)
	if err != nil || hexOf(got) != hexOf(m) {
		t.Fatalf("解锁后主密钥不符: %v", err)
	}
	Lock()
	if _, err := ResolveDMK(dir); err == nil {
		t.Fatal("Lock 后应回到锁定态")
	}
}

func TestResolveDMKLegacyAndRecordShape(t *testing.T) {
	dir := t.TempDir()
	m, err := ResolveDMK(dir)
	if err != nil {
		t.Fatal(err)
	}
	want, err := LegacyMaster(dir)
	if err != nil {
		t.Fatal(err)
	}
	if hexOf(m) != hexOf(want) {
		t.Fatal("legacy 解析与 LegacyMaster 不一致")
	}
	if _, err := os.Stat(filepath.Join(dir, saltFile)); err != nil {
		t.Fatal("解析时未生成安装盐")
	}
}

func TestDPAPIRoundTrip(t *testing.T) {
	if !DpapiAvailable() {
		t.Skip("DPAPI 仅 Windows")
	}
	m := randBytes(keyBytes)
	rec, err := WrapDPAPI(m, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	got, err := unwrapDPAPI(rec)
	if err != nil || hexOf(got) != hexOf(m) {
		t.Fatalf("DPAPI 封解往返失败: %v", err)
	}
	if _, err := unwrapDPAPI(map[string]any{}); err == nil || err.Error() != "DPAPI 密钥记录不完整" {
		t.Fatalf("缺 blob 文案漂移: %v", err)
	}
	if _, err := unwrapDPAPI(map[string]any{"blob": "abcde"}); err == nil || err.Error() != "DPAPI 密钥记录不完整" {
		t.Fatalf("坏 padding blob 文案漂移: %v", err)
	}
	// 字母表外字符丢弃后成空串：与 Python 一致落到系统解封失败文案
	if _, err := unwrapDPAPI(map[string]any{"blob": "!!"}); err == nil || err.Error() != "系统凭据无法解封（换了 Windows 用户或机器？）" {
		t.Fatalf("空 blob 文案漂移: %v", err)
	}
}

func TestScryptRFC7914Vector(t *testing.T) {
	out, err := scrypt.Key([]byte(""), []byte(""), 16, 1, 1, 64)
	if err != nil {
		t.Fatal(err)
	}
	want := "77d6576238657b203b19ca42c18a0497f16b4844e3074ae8dfdffa3fede21442" +
		"fcd0069ded0948f8326a753a0fc81f17e8d3e0fb2e0d3628cf35e20c38d18906"
	if hexOf(out) != want {
		t.Fatalf("scrypt 与 RFC 7914 向量不符: %s", hexOf(out))
	}
}

func TestStatusProjection(t *testing.T) {
	dir := t.TempDir()
	st := Status(dir)
	if st["mode"] != any(ModeLegacy) || st["locked"] != any(false) || st["error"] != any("") {
		t.Fatalf("legacy 状态投影漂移: %v", st)
	}
	if st["dpapi_available"] != any(DpapiAvailable()) {
		t.Fatalf("dpapi_available 漂移: %v", st["dpapi_available"])
	}
	if err := WriteRecord(dir, map[string]any{"v": fileVersion, "mode": "bogus"}); err != nil {
		t.Fatal(err)
	}
	st = Status(dir)
	if st["mode"] != any("unknown") || st["locked"] != any(true) {
		t.Fatalf("损坏态投影漂移: %v", st)
	}
}

func hexOf(b []byte) string { return hexEncode(b) }
