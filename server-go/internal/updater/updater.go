// Package updater 应用自更新后端：静态清单检查（Ed25519 验签）→ 多通道
// 下载暂存（断点续传/停滞与速率双闸换道）→ 校验解包 → staging 清单落盘，
// 重启后由更新宿主换装。语义对齐 app/core/updater.py 与 app/core/update_signing.py。
package updater

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

const (
	// 发布仓库（与 systemx.githubRepo 同源；更新检查与资产种子共用）。
	GitHubRepo = "GLrone/Holdexar"
	// 镜像前缀链（空串=直连 GitHub），单一顺序对齐 app_info.GITHUB_MIRRORS。
	mirrors = "https://ghfast.top/,https://gh-proxy.com/,https://gh-proxy.net/,"
	// 固定 tag 下挂 latest.json：地址与版本号解耦，检查更新只读一个小文件。
	ManifestTag   = "updater"
	ManifestAsset = "latest.json"

	StagingDirName     = "update-staging"
	StagingManifest    = "manifest.json"
	SwapFailedFlag     = ".swap-failed"
	HandoffLedgerName  = "update-handoff.json"
	downloadMetaName   = "download_meta.json"
	partName           = "update.zip.part"
	zipName            = "update.zip"
	appPayloadDir      = "Holdexar"
	minPackageBytes    = 1 << 20
	probeTimeoutSec    = 8
	stallTimeout       = 15 // 秒：连续无字节即换道
	trialSeconds       = 15 // 秒：速率考核窗口
	minChannelRate     = 64 * 1024
	notesLimit         = 8000
	downloadTimeoutSec = 300 // 单通道总时长兜底
)

// assetPattern 发行包资产命名（build_release 产物）。
var assetPattern = regexp.MustCompile(`(?i)^Holdexar-win64-v[\w.-]+\.zip$`)

// plainNotes 的 markdown 噪音清理规则（对齐 updater._plain_notes）。
var (
	codeBlockRe = regexp.MustCompile("(?s)```.*?```")
	imageRe     = regexp.MustCompile(`!\[[^\]]*\]\([^)]*\)`)
	linkRe      = regexp.MustCompile(`\[([^\]]*)\]\([^)]*\)`)
	headingRe   = regexp.MustCompile(`(?m)^#{1,6}\s*`)
	boldRe      = regexp.MustCompile(`\*\*?([^*]+)\*\*?`)
	blankRe     = regexp.MustCompile(`
{3,}`)
)

// MirrorList 镜像前缀（空串=直连，保持尾部）。
func MirrorList() []string { return strings.Split(mirrors, ",") }

// Channel 一条下载通道：代理 URL（空=该通道直连）与展示名。
type Channel struct {
	Proxy string // 形如 http://127.0.0.1:port；空串=直连
	Label string
}

// probeChannel 探测/下载共用的通道形态（带目标 URL 与直连标记）。
type probeChannel struct {
	Channel
	URL    string
	direct bool
}

// versionGT 主段逐点分数字比较 + 预发布段判旧（对齐 app_info.version_gt）。
func versionGT(first, second string) bool {
	parse := func(v string) (string, string) {
		main, pre := v, ""
		if idx := strings.IndexAny(v, "-+"); idx >= 0 {
			main, pre = v[:idx], v[idx+1:]
		}
		return main, pre
	}
	leftMain, leftPre := parse(first)
	rightMain, rightPre := parse(second)
	l := splitNums(leftMain)
	r := splitNums(rightMain)
	for i := 0; i < len(l) || i < len(r); i++ {
		a, b := 0, 0
		if i < len(l) {
			a = l[i]
		}
		if i < len(r) {
			b = r[i]
		}
		if a != b {
			return a > b
		}
	}
	if leftPre == "" && rightPre != "" {
		return true
	}
	if leftPre != "" && rightPre != "" {
		return leftPre > rightPre
	}
	return false
}

func splitNums(v string) []int {
	parts := strings.Split(v, ".")
	out := make([]int, 0, len(parts))
	for _, piece := range parts {
		n := 0
		for _, r := range piece {
			if r < '0' || r > '9' {
				break
			}
			n = n*10 + int(r-'0')
		}
		out = append(out, n)
	}
	return out
}

// pickAsset 挑应用包资产：显式 name 精确匹配 → role=app → 命名模式。
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

// plainNotes release body 转可展示纯文本（去 markdown 噪音，截断保形）。
func plainNotes(body string) string {
	text := codeBlockRe.ReplaceAllString(body, "（代码块略）")
	text = imageRe.ReplaceAllString(text, "")
	text = linkRe.ReplaceAllString(text, "$1")
	text = headingRe.ReplaceAllString(text, "")
	text = boldRe.ReplaceAllString(text, "$1")
	text = blankRe.ReplaceAllString(text, "\n\n")
	text = strings.TrimSpace(text)
	if len(text) > notesLimit {
		text = text[:notesLimit]
	}
	return text
}

// sha256OfFile 分片哈希大文件。
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

// payloadHashes 暂存载荷逐文件 sha256（相对载荷根，posix 路径键）。
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
