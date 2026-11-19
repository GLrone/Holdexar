package updater

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"holdexar/hostkit"
)

const (
	// GitHubRepo 须与 systemx.githubRepo 保持一致。
	GitHubRepo = "GLrone/Holdexar"
	// 镜像前缀链（末尾空串表示直连 GitHub）。
	mirrors       = "https://ghfast.top/,https://gh-proxy.com/,https://gh-proxy.net/,"
	ManifestTag   = "updater"
	ManifestAsset = "latest.json"

	StagingDirName     = hostkit.StagingDirName
	StagingManifest    = hostkit.StagingManifestName
	SwapFailedFlag     = hostkit.SwapFailedFlag
	HandoffLedgerName  = hostkit.HandoffLedgerName
	downloadMetaName   = "download_meta.json"
	partName           = "update.zip.part"
	zipName            = "update.zip"
	appPayloadDir      = hostkit.PayloadDirName
	probeTimeoutSec    = 8
	stallTimeout       = 15
	trialSeconds       = 15
	minChannelRate     = 64 * 1024
	notesLimit         = 8000
	downloadTimeoutSec = 300
	maxExtractBytes    = int64(1 << 30)
)

var (
	minPackageBytes int64 = 1 << 20
	stallDuration         = time.Duration(stallTimeout) * time.Second
	assetPattern          = regexp.MustCompile(`(?i)^Holdexar-win64-v[\w.-]+\.zip$`)
	hex64Re               = regexp.MustCompile(`^[0-9a-fA-F]{64}$`)
)

var (
	codeBlockRe = regexp.MustCompile("(?s)```.*?```")
	imageRe     = regexp.MustCompile(`!\[[^\]]*\]\([^)]*\)`)
	linkRe      = regexp.MustCompile(`\[([^\]]*)\]\([^)]*\)`)
	headingRe   = regexp.MustCompile(`(?m)^#{1,6}\s*`)
	boldRe      = regexp.MustCompile(`\*\*?([^*]+)\*\*?`)
	blankRe     = regexp.MustCompile("\n{3,}")
)

var _ = isReparseOrSymlink

func MirrorList() []string { return strings.Split(mirrors, ",") }

type Channel struct {
	Proxy string
	Label string
}

type probeChannel struct {
	Channel
	URL    string
	direct bool
}

func VersionGT(first, second string) bool {
	return hostkit.VersionGT(first, second)
}

func versionGT(first, second string) bool {
	if first == "" || first[0] < '0' || first[0] > '9' {
		return false
	}
	return hostkit.VersionGT(first, second)
}

func VersionFromTag(tag string) string {
	return hostkit.VersionFromTag(tag)
}

func versionFromTag(tag string) string {
	return hostkit.VersionFromTag(tag)
}

func pickAsset(assets []any, name string) map[string]any {
	if name != "" {
		for _, item := range assets {
			if asset, ok := item.(map[string]any); ok && stringOf(asset["name"]) == name {
				return asset
			}
		}
	}
	for _, item := range assets {
		if asset, ok := item.(map[string]any); ok && stringOf(asset["role"]) == "app" {
			return asset
		}
	}
	for _, item := range assets {
		if asset, ok := item.(map[string]any); ok && assetPattern.MatchString(stringOf(asset["name"])) {
			return asset
		}
	}
	return nil
}

func stringOf(value any) string {
	text, _ := value.(string)
	return text
}

func plainNotes(body string) string {
	text := codeBlockRe.ReplaceAllString(body, "（代码块略）")
	text = imageRe.ReplaceAllString(text, "")
	text = linkRe.ReplaceAllString(text, "$1")
	text = headingRe.ReplaceAllString(text, "")
	text = boldRe.ReplaceAllString(text, "$1")
	text = blankRe.ReplaceAllString(text, "\n\n")
	text = strings.TrimSpace(text)
	return clip(text, notesLimit)
}

func sha256OfFile(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	h := sha256.New()
	if _, err := io.Copy(h, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func payloadHashes(payloadRoot string) (map[string]string, error) {
	out := map[string]string{}
	err := filepath.WalkDir(payloadRoot, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		rel, rerr := filepath.Rel(payloadRoot, path)
		if rerr != nil {
			return rerr
		}
		digest, derr := sha256OfFile(path)
		if derr != nil {
			return derr
		}
		out[filepath.ToSlash(rel)] = digest
		return nil
	})
	if err != nil {
		return nil, err
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("暂存载荷为空")
	}
	return out, nil
}

func int64Of(value any) (int64, bool) {
	switch v := value.(type) {
	case json.Number:
		n, err := strconv.ParseInt(v.String(), 10, 64)
		return n, err == nil
	case int:
		return int64(v), true
	case int64:
		return v, true
	case float64:
		return int64(v), float64(int64(v)) == v
	default:
		return 0, false
	}
}

func validPayloadFiles(raw any) (map[string]string, bool) {
	return hostkit.ValidPayloadFiles(raw)
}

// VerifyStagedTrust 复核暂存包 release_manifest 的 Ed25519 签名、版本、防重解析路径与逐文件 SHA-256（fail-closed）。
func VerifyStagedTrust(stagingDir, dataDir, targetVersion string) error {
	_ = dataDir
	return hostkit.VerifyStagedTrust(stagingDir, targetVersion)
}
