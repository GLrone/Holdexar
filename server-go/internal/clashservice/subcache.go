package clashservice

import (
	"context"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"holdexar/server-go/internal/clashcfg"
	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/proxypool"
)

const runtimeSuffix = ".runtime.yaml"

func SubscriptionConfigPath(dataDir string, subID int64) string {
	return filepath.Join(clashkernel.KernelDir(dataDir), fmt.Sprintf("clash-sub-%d.yaml", subID))
}

// SubscriptionIDOf 从 clash-sub-{id}.yaml 反解订阅 ID；其他命名返回 false。
func SubscriptionIDOf(configPath string) (int64, bool) {
	stem := strings.TrimSuffix(filepath.Base(configPath), filepath.Ext(configPath))
	if !strings.HasPrefix(stem, "clash-sub-") {
		return 0, false
	}
	id, err := strconv.ParseInt(stem[len("clash-sub-"):], 10, 64)
	if err != nil {
		return 0, false
	}
	return id, true
}

// MaterializeSubscriptionConfig 从原文缓存渲染运行配置（原文保持不动）；渲染失败返回 400。
func MaterializeSubscriptionConfig(rawPath string) (string, error) {
	out := rawPath
	source := rawPath
	if strings.HasSuffix(filepath.Base(rawPath), runtimeSuffix) {
		base := strings.TrimSuffix(filepath.Base(rawPath), runtimeSuffix) + ".yaml"
		paired := filepath.Join(filepath.Dir(rawPath), base)
		if fileExists(paired) {
			source = paired
		}
	} else {
		out = strings.TrimSuffix(rawPath, filepath.Ext(rawPath)) + runtimeSuffix
	}
	raw, err := os.ReadFile(source)
	if err != nil {
		return "", fmt.Errorf("订阅缓存不可读：%w", err)
	}
	rendered, err := clashcfg.RenderSubscriptionConfig(toValidUTF8(raw))
	if err != nil {
		return "", statusErr(400, "%s", err.Error())
	}
	if err := atomicWriteFile(out, []byte(rendered)); err != nil {
		return "", err
	}
	return out, nil
}

// FetchSubscriptionToCache 从最近成功快照恢复缓存，含订阅 URL 归属校验。
func (s *Service) FetchSubscriptionToCache(ctx context.Context, subID int64, subURL string) (*SubscriptionFetch, error) {
	var stored string
	err := s.DB.QueryRowContext(ctx, `SELECT url FROM proxy_subscriptions WHERE id=?`, subID).Scan(&stored)
	if err != nil {
		return nil, statusErr(400, "订阅地址与缓存归属不一致")
	}
	if proxypool.OpenURL(s.DataDir, stored) != subURL {
		return nil, statusErr(400, "订阅地址与缓存归属不一致")
	}
	conn, err := s.DB.Conn(ctx)
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	snapshot, err := proxypool.RestoreLatestSnapshot(ctx, conn, s.DataDir, subID, subURL)
	if err != nil {
		return nil, err
	}
	if snapshot == nil {
		return nil, fmt.Errorf("没有可恢复的成功快照或有效订阅缓存")
	}
	return s.writeCacheFromSnapshot(subID, subURL, snapshot.RawContent, snapshot.ProfileTitle, snapshot.Userinfo, snapshot.NodeCount)
}

func (s *Service) writeCacheFromSnapshot(subID int64, subURL string, raw []byte, title, userinfo string, nodes int) (*SubscriptionFetch, error) {
	cachePath := SubscriptionConfigPath(s.DataDir, subID)
	if err := atomicWriteFile(cachePath, raw); err != nil {
		return nil, err
	}
	if title == "" {
		title = subscriptionAutoNameFromURL(subURL)
	}
	return &SubscriptionFetch{Raw: raw, Title: title, Userinfo: userinfo, Nodes: nodes}, nil
}

func subscriptionAutoNameFromURL(rawURL string) string {
	path := rawURL
	if idx := strings.Index(path, "?"); idx >= 0 {
		path = path[:idx]
	}
	if strings.Count(path, "/") > 2 {
		rest := ""
		if _, tail, found := strings.Cut(path, "//"); found {
			rest = tail
		}
		if idx := strings.Index(rest, "/"); idx >= 0 {
			path = "/" + rest[idx+1:]
		} else {
			path = "/"
		}
	} else {
		path = "/"
	}
	last := strings.TrimRight(path, "/")
	if idx := strings.LastIndex(last, "/"); idx >= 0 {
		last = last[idx+1:]
	}
	if last == "" {
		return ""
	}
	if decoded, err := url.PathUnescape(last); err == nil {
		last = decoded
	}
	if looksLikeOpaqueToken(last) {
		return ""
	}
	return last
}

var opaqueTokenRe = regexp.MustCompile(`^[A-Za-z0-9_\-=]+$`)

func looksLikeOpaqueToken(text string) bool {
	if len([]rune(text)) < 16 {
		return false
	}
	for _, ch := range text {
		if ch <= ' ' {
			return false
		}
		if ch >= 0x4E00 && ch <= 0x9FFF {
			return false
		}
	}
	return opaqueTokenRe.MatchString(text)
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular()
}

func toValidUTF8(raw []byte) string {
	return strings.ToValidUTF8(string(raw), "")
}

func atomicWriteFile(path string, data []byte) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return fmt.Errorf("缓存写入失败：%w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("缓存落盘失败：%w", err)
	}
	return nil
}
