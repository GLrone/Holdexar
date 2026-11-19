package secretbox

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
)

// 换钥过渡记录：用新主密钥派生子密钥封装旧主密钥，供崩溃续跑恢复。

const (
	stagingFile = "secret.master.staging"
	stagingKind = "rekey-staging"
)

// stagingInfo 过渡记录 HKDF 用途串（salt=nil 等价全零盐）。
var stagingInfo = []byte("holdexar:keyring:rekey-staging:v1")

func stagingPath(dataDir string) string { return filepath.Join(dataDir, stagingFile) }

func stagingKey(newMaster []byte) []byte {
	return hkdfBytes(newMaster, nil, stagingInfo)
}

func WrapStaging(oldMaster, newMaster []byte) map[string]any {
	nonce := randBytes(nonceBytes)
	sealed := gcmSeal(stagingKey(newMaster), nonce, oldMaster)
	return map[string]any{
		"v": fileVersion, "kind": stagingKind,
		"nonce": b64Encode(nonce), "ct": b64Encode(sealed),
	}
}

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

func WriteStaging(dataDir string, rec map[string]any) error {
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		return keyringErrf("换钥过渡记录写入失败：%v", err)
	}
	return writeAtomicText(stagingPath(dataDir), dumpRecordJSON(rec), "换钥过渡记录写入失败")
}

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

func RemoveStaging(dataDir string) error {
	if err := os.Remove(stagingPath(dataDir)); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// StagingMaster 从过渡记录解出旧主密钥；未解锁或不可解返回 nil，JSON 损坏返回 KeyringError。
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
