package secretbox

import (
	"os"
	"testing"
)

func TestStagingWrapUnwrap(t *testing.T) {
	old, newM := randBytes(keyBytes), randBytes(keyBytes)
	rec := WrapStaging(old, newM)
	if got := UnwrapStaging(rec, newM); hexOf(got) != hexOf(old) {
		t.Fatal("过渡记录往返不一致")
	}
	if got := UnwrapStaging(rec, randBytes(keyBytes)); got != nil {
		t.Fatal("异新钥应解不出")
	}
	if got := UnwrapStaging(map[string]any{"kind": "other"}, newM); got != nil {
		t.Fatal("异 kind 应解不出")
	}
	if got := UnwrapStaging(map[string]any{"kind": stagingKind, "nonce": "!!", "ct": "!!"}, newM); got != nil {
		t.Fatal("坏 base64 应解不出")
	}
}

func TestStagingFileLifecycle(t *testing.T) {
	dir := t.TempDir()
	rec, err := ReadStaging(dir)
	if err != nil || rec != nil {
		t.Fatalf("无过渡记录应返回 nil: %v %v", rec, err)
	}
	if MigrationPending(dir) {
		t.Fatal("无记录不应判待收尾")
	}

	old, newM := randBytes(keyBytes), randBytes(keyBytes)
	if err := WriteStaging(dir, WrapStaging(old, newM)); err != nil {
		t.Fatal(err)
	}
	if !MigrationPending(dir) {
		t.Fatal("落记录后应判待收尾")
	}
	back, err := ReadStaging(dir)
	if err != nil || back == nil {
		t.Fatalf("过渡记录读回失败: %v", err)
	}
	if got := UnwrapStaging(back, newM); hexOf(got) != hexOf(old) {
		t.Fatal("读回后解封不一致")
	}
	// 写后无残留临时文件
	entries, _ := os.ReadDir(dir)
	if len(entries) != 1 || entries[0].Name() != stagingFile {
		t.Fatalf("目录残留异常: %v", entries)
	}

	if err := RemoveStaging(dir); err != nil {
		t.Fatal(err)
	}
	if MigrationPending(dir) {
		t.Fatal("删除后不应判待收尾")
	}

	// 损坏记录：读报错、判待收尾、StagingMaster 上浮
	if err := os.WriteFile(stagingPath(dir), []byte("{bad"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := ReadStaging(dir); err == nil || err.Error() != "换钥过渡记录损坏："+stagingPath(dir) {
		t.Fatalf("损坏文案漂移: %v", err)
	}
	if !MigrationPending(dir) {
		t.Fatal("损坏记录应判待收尾")
	}
	if _, err := StagingMaster(dir, ""); err == nil {
		t.Fatal("损坏记录应使 StagingMaster 上浮报错")
	}
}

func TestStagingMasterCurrentKeyPath(t *testing.T) {
	dir := t.TempDir()
	ResetState()
	t.Cleanup(ResetState)
	// 口令模式锁定：过渡记录解不出（保留记录等解锁，不报错）
	m := randBytes(keyBytes)
	rec, _ := WrapPassphrase(m, "pw")
	if err := WriteRecord(dir, rec); err != nil {
		t.Fatal(err)
	}
	if err := WriteStaging(dir, WrapStaging(randBytes(keyBytes), m)); err != nil {
		t.Fatal(err)
	}
	got, err := StagingMaster(dir, "")
	if err != nil || got != nil {
		t.Fatalf("锁定态应返回 nil: %v %v", got, err)
	}
	// 解锁后用当前主密钥解开
	LoadUnlockedKey(m)
	old := randBytes(keyBytes)
	if err := WriteStaging(dir, WrapStaging(old, m)); err != nil {
		t.Fatal(err)
	}
	got, err = StagingMaster(dir, "")
	if err != nil || hexOf(got) != hexOf(old) {
		t.Fatalf("解锁后过渡解封失败: %v", err)
	}
}

func TestStagingMasterLegacyFormatCompat(t *testing.T) {
	// 旧格式：记录自带 mode，按记录自解（dpapi 封装旧主密钥）
	if !DpapiAvailable() {
		t.Skip("DPAPI 仅 Windows")
	}
	old := randBytes(keyBytes)
	blob, err := dpapiProtect(old)
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	if err := WriteStaging(dir, map[string]any{
		"v": fileVersion, "mode": ModeDPAPI, "blob": b64Encode(blob),
	}); err != nil {
		t.Fatal(err)
	}
	got, err := StagingMaster(dir, "")
	if err != nil || hexOf(got) != hexOf(old) {
		t.Fatalf("旧格式过渡记录解封失败: %v", err)
	}
}
