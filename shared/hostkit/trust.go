package hostkit

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
)

const (
	AppName             = "Holdexar"
	AppSlug             = "holdexar"
	EnvPrefix           = "HOLDEXAR_"
	DBFileName          = "holdexar.db"
	ManifestSchema      = 2
	SignatureField      = "signature"
	StagingDirName      = "update-staging"
	StagingManifestName = "manifest.json"
	PayloadDirName      = "Holdexar"
	SwapFailedFlag      = ".swap-failed"
	HandoffLedgerName   = "update-handoff.json"
)

var (
	trustMu        sync.RWMutex
	TrustedPubkeys = map[string]string{
		"k1": "a2a39fde4347156b2353746a144f2dcdd1591fc77c43104206ab44641d898f8d",
	}
)

// SetTrustedPubkey registers or overrides a keyId in TrustedPubkeys and returns a restore callback.
func SetTrustedPubkey(keyID, pubkeyHex string) func() {
	trustMu.Lock()
	prev, existed := TrustedPubkeys[keyID]
	TrustedPubkeys[keyID] = pubkeyHex
	trustMu.Unlock()
	return func() {
		trustMu.Lock()
		if existed {
			TrustedPubkeys[keyID] = prev
		} else {
			delete(TrustedPubkeys, keyID)
		}
		trustMu.Unlock()
	}
}

func lookupTrustedPubkey(keyID string) (string, bool) {
	trustMu.RLock()
	val, ok := TrustedPubkeys[keyID]
	trustMu.RUnlock()
	return val, ok
}

// CanonicalManifestBytes serializes manifest without its signature field using compact sorted JSON keys and unescaped HTML.
func CanonicalManifestBytes(manifest map[string]any) ([]byte, error) {
	payload := make(map[string]any, len(manifest))
	for key, value := range manifest {
		if key == SignatureField {
			continue
		}
		payload[key] = value
	}
	var buf bytes.Buffer
	encoder := json.NewEncoder(&buf)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(payload); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// VerifyManifest verifies the Ed25519 signature block on a release manifest against TrustedPubkeys.
func VerifyManifest(manifest map[string]any) (bool, string) {
	if manifest == nil {
		return false, "清单缺少签名"
	}
	sig, ok := manifest[SignatureField].(map[string]any)
	if !ok {
		return false, "清单缺少签名"
	}
	alg, _ := sig["alg"].(string)
	if alg != "ed25519" {
		return false, fmt.Sprintf("签名算法不支持：%q", alg)
	}
	keyID, _ := sig["keyId"].(string)
	pubkeyHex, trusted := lookupTrustedPubkey(keyID)
	if !trusted {
		return false, fmt.Sprintf("签名公钥不在信任表内：%q", keyID)
	}
	value, ok := sig["sig"].(string)
	if !ok || value == "" {
		return false, "签名为空"
	}
	signature, err := base64.StdEncoding.DecodeString(value)
	if err != nil || len(signature) != ed25519.SignatureSize {
		return false, "签名不是有效 base64"
	}
	pubkey, err := hex.DecodeString(pubkeyHex)
	if err != nil || len(pubkey) != ed25519.PublicKeySize {
		return false, "信任表公钥损坏"
	}
	payload, err := CanonicalManifestBytes(manifest)
	if err != nil {
		return false, "清单无法规范化"
	}
	if !ed25519.Verify(ed25519.PublicKey(pubkey), payload, signature) {
		return false, "签名校验失败"
	}
	return true, ""
}

// IsManifestSchema checks whether value represents strict integer schema version ManifestSchema.
func IsManifestSchema(value any) bool {
	switch v := value.(type) {
	case json.Number:
		n, err := strconv.ParseInt(v.String(), 10, 64)
		return err == nil && n == ManifestSchema
	case int:
		return v == ManifestSchema
	case int64:
		return v == ManifestSchema
	case int32:
		return v == ManifestSchema
	default:
		return false
	}
}

// CheckManifestTrusted verifies Ed25519 signature plus app identity and schema version.
func CheckManifestTrusted(manifest map[string]any) (bool, string) {
	ok, reason := VerifyManifest(manifest)
	if !ok {
		return false, reason
	}
	app, _ := manifest["app"].(string)
	if app != AppName || !IsManifestSchema(manifest["schema"]) {
		return false, "清单应用标识或架构版本不受支持"
	}
	return true, ""
}

// ValidPayloadRelPath checks that rel is a safe relative slash-separated payload file path without traversal or drive prefixes.
func ValidPayloadRelPath(rel string) bool {
	if rel == "" || strings.Contains(rel, "\\") || strings.HasPrefix(rel, "/") || strings.Contains(rel, ":") {
		return false
	}
	for _, part := range strings.Split(rel, "/") {
		if part == "" || part == "." || part == ".." {
			return false
		}
	}
	return true
}

func isHex64(s string) bool {
	if len(s) != 64 {
		return false
	}
	for i := 0; i < 64; i++ {
		c := s[i]
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')) {
			return false
		}
	}
	return true
}

// ValidPayloadFiles validates and normalizes a manifest files map (relPath -> lowercase sha256), rejecting case-folded duplicates.
func ValidPayloadFiles(raw any) (map[string]string, bool) {
	out := map[string]string{}
	folded := map[string]struct{}{}
	addEntry := func(name string, digest any) bool {
		digestStr, ok := digest.(string)
		if !ok || !isHex64(digestStr) || !ValidPayloadRelPath(name) {
			return false
		}
		key := strings.ToLower(name)
		if _, dup := folded[key]; dup {
			return false
		}
		folded[key] = struct{}{}
		out[name] = strings.ToLower(digestStr)
		return true
	}

	switch typed := raw.(type) {
	case map[string]any:
		if len(typed) == 0 {
			return nil, false
		}
		for name, digest := range typed {
			if !addEntry(name, digest) {
				return nil, false
			}
		}
	case map[string]string:
		if len(typed) == 0 {
			return nil, false
		}
		for name, digest := range typed {
			if !addEntry(name, digest) {
				return nil, false
			}
		}
	default:
		return nil, false
	}
	return out, len(out) > 0
}

func sha256OfFile(target string) (string, error) {
	file, err := os.Open(target)
	if err != nil {
		return "", err
	}
	defer file.Close()
	hasher := sha256.New()
	if _, err := io.Copy(hasher, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(hasher.Sum(nil)), nil
}

// ReadStagingManifest reads stagingDir/manifest.json preserving JSON number lexemes.
func ReadStagingManifest(stagingDir string) (map[string]any, error) {
	raw, err := os.ReadFile(filepath.Join(stagingDir, StagingManifestName))
	if err != nil {
		return nil, err
	}
	return decodeSingleJSONObject(raw)
}

// VerifyFilesExactSet checks bidirectional file set equality, SHA-256 hashes, and absence of symlinks/reparse points in payloadDir.
func VerifyFilesExactSet(payloadDir string, expected map[string]string) string {
	if len(expected) == 0 {
		return "发布清单缺少已签名的载荷校验表"
	}
	payloadInfo, err := os.Lstat(payloadDir)
	if err != nil || !payloadInfo.IsDir() || IsReparseOrSymlink(payloadDir, payloadInfo) {
		return "暂存包载荷缺失"
	}
	actual := make(map[string]string, len(expected))
	walkErr := filepath.WalkDir(payloadDir, func(entryPath string, _ os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		info, lerr := os.Lstat(entryPath)
		if lerr != nil {
			return lerr
		}
		if IsReparseOrSymlink(entryPath, info) {
			return errors.New("暂存包载荷包含重解析路径")
		}
		if info.IsDir() {
			return nil
		}
		if !info.Mode().IsRegular() {
			return errors.New("暂存包载荷包含非普通文件")
		}
		rel, rerr := filepath.Rel(payloadDir, entryPath)
		if rerr != nil {
			return rerr
		}
		relSlash := filepath.ToSlash(rel)
		if !ValidPayloadRelPath(relSlash) {
			return errors.New("暂存包载荷路径非法")
		}
		digest, derr := sha256OfFile(entryPath)
		if derr != nil {
			return derr
		}
		actual[relSlash] = digest
		return nil
	})
	if walkErr != nil {
		return walkErr.Error()
	}

	keys := make([]string, 0, len(expected))
	for name := range expected {
		keys = append(keys, name)
	}
	slices.Sort(keys)
	for _, name := range keys {
		got, exists := actual[name]
		if !exists {
			return fmt.Sprintf("暂存包载荷缺失：%s", name)
		}
		if !strings.EqualFold(got, expected[name]) {
			return fmt.Sprintf("暂存包载荷校验不符：%s", name)
		}
	}
	if len(actual) != len(expected) {
		return "暂存包载荷包含发布清单之外的文件"
	}
	return ""
}

func verifyStagedCore(stagingDir string, requireReady bool, expectedTargetVersion string, currentVersion string) (string, map[string]any, string) {
	stagingInfo, err := os.Lstat(stagingDir)
	if err != nil {
		return "", nil, "暂存目录缺失"
	}
	if !stagingInfo.IsDir() || IsReparseOrSymlink(stagingDir, stagingInfo) {
		return "", nil, "暂存目录路径非法"
	}
	manifest, err := ReadStagingManifest(stagingDir)
	if err != nil || manifest == nil {
		return "", nil, "暂存清单缺失或不可读"
	}
	if requireReady {
		ready, ok := manifest["ready"].(bool)
		if !ok || !ready {
			return "", nil, "暂存包尚未就绪"
		}
	}
	release, ok := manifest["release_manifest"].(map[string]any)
	if !ok || len(release) == 0 {
		return "", nil, "发布清单缺失，拒绝执行暂存载荷；请重新下载更新"
	}
	if trusted, reason := CheckManifestTrusted(release); !trusted {
		if reason == "" {
			reason = "清单内容不匹配"
		}
		return "", nil, fmt.Sprintf("发布清单签名无效或版本不受支持：%s", reason)
	}

	tag, _ := release["tag"].(string)
	relVerStr, _ := release["version"].(string)
	if tag == "" && VersionFromTag(relVerStr) != "" {
		tag = "v" + VersionFromTag(relVerStr)
	}
	version := VersionFromTag(tag)
	if version == "" || version != VersionFromTag(relVerStr) || !IsManagedVersionName(version) {
		return "", nil, "发布清单版本信息不一致"
	}
	manifestTag, _ := manifest["tag"].(string)
	if VersionFromTag(manifestTag) != version ||
		(expectedTargetVersion != "" && version != VersionFromTag(expectedTargetVersion)) {
		shownTarget := expectedTargetVersion
		if shownTarget == "" {
			shownTarget = manifestTag
		}
		return "", nil, fmt.Sprintf("发布清单版本不符：签名清单 %s / 暂存 %s", fallbackEmptyText(tag), fallbackEmptyText(shownTarget))
	}
	if currentVersion != "" && !VersionGT(version, currentVersion) {
		return "", nil, fmt.Sprintf("暂存版本 %s 不新于当前版本 %s", version, VersionFromTag(currentVersion))
	}

	assets, _ := release["assets"].([]any)
	var asset map[string]any
	for _, item := range assets {
		if m, ok := item.(map[string]any); ok {
			if role, _ := m["role"].(string); role == "app" {
				asset = m
				break
			}
		}
	}
	if asset == nil {
		return "", nil, "发布清单缺少应用安装包"
	}
	assetName, _ := asset["name"].(string)
	if assetName == "" || path.Base(assetName) != assetName || strings.Contains(assetName, "\\") ||
		strings.Contains(assetName, "/") ||
		!strings.HasPrefix(strings.ToLower(assetName), strings.ToLower(AppName)+"-win64-v") ||
		!strings.HasSuffix(strings.ToLower(assetName), ".zip") {
		return "", nil, "发布清单应用包名称非法"
	}
	rawFiles, hasFiles := asset["files"]
	if !hasFiles || rawFiles == nil {
		return "", nil, "发布清单缺少已签名的载荷校验表"
	}
	if m, ok := rawFiles.(map[string]any); !ok || len(m) == 0 {
		if ms, ok2 := rawFiles.(map[string]string); !ok2 || len(ms) == 0 {
			return "", nil, "发布清单缺少已签名的载荷校验表"
		}
	}
	expected, ok := ValidPayloadFiles(rawFiles)
	if !ok {
		return "", nil, "发布清单载荷校验表非法"
	}
	if reason := VerifyFilesExactSet(filepath.Join(stagingDir, PayloadDirName), expected); reason != "" {
		return "", nil, reason
	}
	return version, release, ""
}

// VerifyStagedPayload validates staging/manifest.json, ready==true, Ed25519 release_manifest, version upgrade over currentVersion, and exact payload files.
func VerifyStagedPayload(stagingDir, currentVersion string) (targetVersion string, releaseManifest map[string]any, errReason string) {
	return verifyStagedCore(stagingDir, true, "", currentVersion)
}

// VerifyStagedTrust validates staging/manifest.json, Ed25519 release_manifest, optional expectedTargetVersion match, and exact payload files.
func VerifyStagedTrust(stagingDir, expectedTargetVersion string) error {
	_, _, reason := verifyStagedCore(stagingDir, false, expectedTargetVersion, "")
	if reason != "" {
		return errors.New(reason)
	}
	return nil
}

func fallbackEmptyText(text string) string {
	if text == "" {
		return "(空)"
	}
	return text
}
