package secretbox

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
)

// 换钥过渡记录（崩溃恢复的唯一依据）：把**旧主密钥**用新主密钥派生的独立
// 子密钥封装后落盘，换钥期间与密钥文件并存。崩溃后库内密文无论归哪一边都
// 能解开；迁移完成后删除。记录本身不含明文密钥。

const (
	stagingFile = "secret.master.staging"
	stagingKind = "rekey-staging"
)

// stagingInfo 过渡记录用途串：用新主密钥派生独立子密钥封装旧主密钥。
// salt=nil → HKDF 以全零盐展开，与 Python salt=None 语义一致。
var stagingInfo = []byte("holdexar:keyring:rekey-staging:v1")

func stagingPath(dataDir string) string { return filepath.Join(dataDir, stagingFile) }

func stagingKey(newMaster []byte) []byte {
	return hkdfBytes(newMaster, nil, stagingInfo)
}

// WrapStaging 用新主密钥封装旧主密钥（AES-256-GCM，独立用途子密钥）。
func WrapStaging(oldMaster, newMaster []byte) map[string]any {
	nonce := randBytes(nonceBytes)
	sealed := gcmSeal(stagingKey(newMaster), nonce, oldMaster)
	return map[string]any{
		"v": fileVersion, "kind": stagingKind,
		"nonce": b64Encode(nonce), "ct": b64Encode(sealed),
	}
}

// UnwrapStaging 解出旧主密钥；记录不是本格式或新密钥不符时返回 nil。
func UnwrapStaging(rec map[string]any, newMaster []byte) []byte {
	if rec["kind"] != any(stagingKind) {
		return nil
	}
	nonce, e1 := unb64(pyStr(rec["nonce"]))
	sealed, e2 := unb64(pyStr(rec["ct"]))
	if e1 != nil || e2 != nil {
		return nil
	}
	plain, ok := gcmOpen(stagingKey(newMaster), nonce, sealed)
	if !ok {
		return nil
	}
	return plain
}

// WriteStaging 落过渡记录（先写临时文件再替换：断电时要么旧要么新，不留半截）。
func WriteStaging(dataDir string, rec map[string]any) error {
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		return keyringErrf("换钥过渡记录写入失败：%v", err)
	}
	return writeAtomicText(stagingPath(dataDir), dumpRecordJSON(rec), "换钥过渡记录写入失败")
}

// ReadStaging 读取过渡记录（不存在返回 nil；损坏抛 KeyringError）。
func ReadStaging(dataDir string) (map[string]any, error) {
	data, err := os.ReadFile(stagingPath(dataDir))
	if err != nil {
		return nil, nil
	}
	var parsed any
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	if err := dec.Decode(&parsed); err != nil {
		return nil, keyringErrf("换钥过渡记录损坏：%s", stagingPath(dataDir))
	}
	m, ok := parsed.(map[string]any)
	if !ok {
		return nil, nil
	}
	return m, nil
}

// RemoveStaging 迁移完成后删除过渡记录。
func RemoveStaging(dataDir string) error {
	if err := os.Remove(stagingPath(dataDir)); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// StagingMaster 从过渡记录解出旧主密钥；解不出返回 nil（不报错）。
// 过渡记录用当前主密钥封装，正常无需口令；passphrase 参数只为兼容早期按
// 旧模式封装的记录。记录损坏时抛 KeyringError（调用方按启动失败处理）。
func StagingMaster(dataDir string, passphrase string) ([]byte, error) {
	rec, err := ReadStaging(dataDir)
	if err != nil {
		return nil, err
	}
	if rec == nil {
		return nil, nil
	}
	if rec["kind"] == any(stagingKind) {
		current, err := ResolveDMK(dataDir)
		if err != nil {
			return nil, nil // 当前密钥不可用（含锁定态）：保留记录等解锁，不做破坏性动作
		}
		return UnwrapStaging(rec, current), nil
	}
	// 兼容旧格式：按记录自带的 mode 解
	mode := pyStrOrEmpty(rec["mode"])
	var (
		master []byte
		err2   error
	)
	switch mode {
	case ModeLegacy:
		master, err2 = LegacyMaster(dataDir)
	case ModeDPAPI:
		master, err2 = unwrapDPAPI(rec)
	case ModePassphrase:
		if passphrase != "" {
			master, err2 = unwrapPassphrase(rec, passphrase)
		} else if key := currentUnlockedKey(); key != nil {
			master = key
		} else {
			return nil, nil
		}
	default:
		return nil, nil
	}
	if err2 != nil {
		if isKeyringFamilyErr(err2) {
			return nil, nil
		}
		return nil, err2
	}
	return master, nil
}

func isKeyringFamilyErr(err error) bool {
	switch err.(type) {
	case *KeyringError, *KeyLockedError:
		return true
	}
	return false
}
