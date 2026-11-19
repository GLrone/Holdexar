package updater

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"

	"holdexar/hostkit"
)

// trustedPubkeys 指向 hostkit 共享 Ed25519 公钥信任表。
var trustedPubkeys = hostkit.TrustedPubkeys

const (
	signatureField = hostkit.SignatureField
	AppName        = hostkit.AppName
	ManifestSchema = hostkit.ManifestSchema
)

// UpdateURLFromEnv 显式更新源仅认回环地址；仅选择运输通道，不免除验签。
func UpdateURLFromEnv() string {
	raw := strings.TrimRight(strings.TrimSpace(os.Getenv("HOLDEXAR_UPDATE_URL")), "/")
	if raw == "" {
		return ""
	}
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	switch strings.ToLower(u.Hostname()) {
	case "127.0.0.1", "localhost", "::1":
		return raw
	}
	return ""
}

func manifestURL(mirror string) string {
	if base := UpdateURLFromEnv(); base != "" {
		return base + "/" + ManifestAsset
	}
	return mirror + "https://github.com/" + GitHubRepo + "/releases/download/" + ManifestTag + "/" + ManifestAsset
}

func canonicalManifestBytes(manifest map[string]any) ([]byte, error) {
	return hostkit.CanonicalManifestBytes(manifest)
}

func verifyManifest(manifest map[string]any) (bool, string) {
	return hostkit.VerifyManifest(manifest)
}

func CheckManifestTrusted(manifest map[string]any) (bool, string) {
	return hostkit.CheckManifestTrusted(manifest)
}

func checkManifestTrusted(manifest map[string]any) (bool, string) {
	return hostkit.CheckManifestTrusted(manifest)
}

// fetchManifest 并发请求镜像链并按镜像优先级取首个验签通过的清单；显式本地源仍须验签。
func fetchManifest(client *http.Client) map[string]any {
	if base := UpdateURLFromEnv(); base != "" {
		data, err := getJSON(client, base+"/"+ManifestAsset)
		if err == nil && stringOf(data["version"]) != "" {
			if ok, _ := checkManifestTrusted(data); ok {
				return data
			}
		}
		return nil
	}
	list := MirrorList()
	results := make([]map[string]any, len(list))
	var wg sync.WaitGroup
	for i, mirror := range list {
		wg.Go(func() {
			data, err := getJSON(client, manifestURL(mirror))
			if err != nil {
				return
			}
			if stringOf(data["version"]) == "" {
				return
			}
			if ok, _ := checkManifestTrusted(data); !ok {
				return
			}
			results[i] = data
		})
	}
	wg.Wait()
	for _, data := range results {
		if data != nil {
			return data
		}
	}
	return nil
}

func getJSON(client *http.Client, url string) (map[string]any, error) {
	resp, err := client.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var data map[string]any
	if err := decoder.Decode(&data); err != nil || data == nil {
		return nil, fmt.Errorf("响应不是 JSON")
	}
	return data, nil
}

func checkViaAPI(client *http.Client, currentVersion string) map[string]any {
	resp, err := client.Get("https://api.github.com/repos/" + GitHubRepo + "/releases/latest")
	if err != nil {
		return map[string]any{"available": false, "reason": "network", "error": clip(err.Error(), 200)}
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return map[string]any{"available": false, "reason": "no_releases"}
	}
	if resp.StatusCode != http.StatusOK {
		return map[string]any{"available": false, "reason": "network", "error": fmt.Sprintf("HTTP %d", resp.StatusCode)}
	}
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var release map[string]any
	if err := decoder.Decode(&release); err != nil || release == nil {
		return map[string]any{"available": false, "reason": "network", "error": "响应不是 JSON"}
	}
	tag := stringOf(release["tag_name"])
	latest := versionFromTag(tag)
	assets, _ := release["assets"].([]any)
	asset := pickAsset(assets, "")
	if tag == "" || latest == "" || asset == nil {
		return map[string]any{"available": false, "reason": "no_asset"}
	}
	// API 兜底必须要求资产含 sha256 digest，缺失即拒报可用更新（fail-closed）。
	digest := stringOf(asset["digest"])
	if !strings.HasPrefix(digest, "sha256:") {
		return map[string]any{"available": false, "reason": "bad_manifest", "error": "发布资产缺少校验值"}
	}
	body, _ := release["body"].(string)
	published, _ := release["published_at"].(string)
	size := 0
	if number, ok := asset["size"].(json.Number); ok {
		fmt.Sscanf(number.String(), "%d", &size)
	}
	return map[string]any{
		"available":   versionGT(latest, versionFromTag(currentVersion)),
		"current":     currentVersion,
		"latest":      latest,
		"tag":         tag,
		"notes":       plainNotes(body),
		"sizeBytes":   size,
		"sha256":      strings.SplitN(digest, ":", 2)[1],
		"asset":       stringOf(asset["name"]),
		"publishedAt": published,
		"source":      "api",
	}
}

func clip(text string, limit int) string {
	if limit <= 0 {
		return ""
	}
	runes := []rune(text)
	if len(runes) > limit {
		return string(runes[:limit])
	}
	return text
}
