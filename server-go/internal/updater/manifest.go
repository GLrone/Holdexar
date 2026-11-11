//go:build windows

// manifest.go 清单获取与验签：镜像只是字节运输，清单必须经内嵌公钥验签。
package updater

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

// 信任表：keyId → Ed25519 公钥原始 32 字节 hex。清单里出现的任何公钥字段
// 一律忽略；轮换密钥时新增条目、保留旧条目（对齐 update_signing.TRUSTED_PUBKEYS）。
var trustedPubkeys = map[string]string{
	"k1": "a2a39fde4347156b2353746a144f2dcdd1591fc77c43104206ab44641d898f8d",
}

const signatureField = "signature"

// UpdateURLFromEnv 显式更新源（本机测试源，豁免验签）。
func UpdateURLFromEnv() string {
	return strings.TrimRight(os.Getenv("HOLDEXAR_UPDATE_URL"), "/")
}

// manifestURL 清单地址（mirror 为空=直连）。
func manifestURL(mirror string) string {
	if base := UpdateURLFromEnv(); base != "" {
		return base + "/" + ManifestAsset
	}
	return mirror + "https://github.com/" + GitHubRepo + "/releases/download/" + ManifestTag + "/" + ManifestAsset
}

// canonicalManifestBytes 签名覆盖对象的规范化字节：剔除签名字段后排序紧凑
// 序列化。发布端（build_manifest.py）与客户端共用本函数，字节必须逐位一致；
// json.Decoder 用 UseNumber 保数字字面量原样（整数不冒出 .0 尾巴）。
func canonicalManifestBytes(manifest map[string]any) ([]byte, error) {
	payload := make(map[string]any, len(manifest))
	for key, value := range manifest {
		if key == signatureField {
			continue
		}
		payload[key] = value
	}
	return marshalCanonical(payload)
}

func marshalCanonical(payload map[string]any) ([]byte, error) {
	var buf bytes.Buffer
	encoder := json.NewEncoder(&buf)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(payload); err != nil {
		return nil, err
	}
	// json.Encoder 以换行结尾且 map 序列化本身按键排序，去尾换行即紧凑形态
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// verifyManifest 验签更新清单，返回 (可信, 失败原因)。
func verifyManifest(manifest map[string]any) (bool, string) {
	sig, ok := manifest[signatureField].(map[string]any)
	if !ok {
		return false, "清单缺少签名"
	}
	if stringOf(sig["alg"]) != "ed25519" {
		return false, fmt.Sprintf("签名算法不支持：%q", stringOf(sig["alg"]))
	}
	keyID := stringOf(sig["keyId"])
	pubkeyHex, trusted := trustedPubkeys[keyID]
	if !trusted {
		return false, fmt.Sprintf("签名公钥不在信任表内：%q", keyID)
	}
	value, ok := sig["sig"].(string)
	if !ok || value == "" {
		return false, "签名为空"
	}
	signature, err := base64.StdEncoding.DecodeString(value)
	if err != nil {
		return false, "签名不是有效 base64"
	}
	pubkey, err := hex.DecodeString(pubkeyHex)
	if err != nil {
		return false, "信任表公钥损坏"
	}
	payload, err := canonicalManifestBytes(manifest)
	if err != nil {
		return false, "清单无法规范化"
	}
	if !ed25519.Verify(ed25519.PublicKey(pubkey), payload, signature) {
		return false, "签名校验失败"
	}
	return true, ""
}

// fetchManifest 按镜像链取 latest.json：全通道并发取回，按镜像优先级取
// 第一个验签通过者；显式 UPDATE_URL 指向的本地测试源豁免验签。全失败返回 nil。
func fetchManifest(client *http.Client) map[string]any {
	if base := UpdateURLFromEnv(); base != "" {
		data, err := getJSON(client, base+"/"+ManifestAsset)
		if err == nil {
			if version := stringOf(data["version"]); version != "" {
				return data
			}
		}
		return nil
	}
	list := MirrorList()
	results := make([]map[string]any, len(list))
	var wg sync.WaitGroup
	for i, mirror := range list {
		wg.Add(1)
		go func(slot int, prefix string) {
			defer wg.Done()
			data, err := getJSON(client, manifestURL(prefix))
			if err != nil {
				return
			}
			if stringOf(data["version"]) == "" {
				return
			}
			if ok, _ := verifyManifest(data); !ok {
				return
			}
			results[slot] = data
		}(i, mirror)
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
	if json.Unmarshal(raw, &data) != nil {
		return nil, fmt.Errorf("响应不是 JSON")
	}
	_ = decoder
	return data, nil
}

// checkViaAPI GitHub API 兜底路径（尚未发清单的仓库兼容）。
func checkViaAPI(client *http.Client) map[string]any {
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
	if json.Unmarshal(raw, &release) != nil {
		return map[string]any{"available": false, "reason": "network", "error": "响应不是 JSON"}
	}
	_ = decoder
	tag := stringOf(release["tag_name"])
	assets, _ := release["assets"].([]any)
	asset := pickAsset(assets, "")
	if tag == "" || asset == nil {
		return map[string]any{"available": false, "reason": "no_asset"}
	}
	// GitHub 资产自带 digest（"sha256:..."）——API 路径的完整性锚点，缺它
	// 不宣告可用更新：宁可「暂不可更新」，不造「有更新却在下载端被拒」的悬空态。
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
		"available":   true,
		"latest":      strings.TrimLeft(tag, "vV"),
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
	if len(text) > limit {
		return text[:limit]
	}
	return text
}

// sortedKeys 规范化辅助（测试断言用）。
func sortedKeys(payload map[string]any) []string {
	keys := make([]string, 0, len(payload))
	for key := range payload {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

var _ = time.Second
