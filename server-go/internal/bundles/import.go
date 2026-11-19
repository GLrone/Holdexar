package bundles

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"

	"holdexar/server-go/internal/monitoring"
)

var ErrUnrecognizedRef = errors.New("无法识别的 Steam 链接或捆绑包 ID")

var ErrImportFetchFailed = errors.New("捆绑包抓取失败")

type importFetchError struct{ message string }

func (e *importFetchError) Error() string { return e.message }

func (e *importFetchError) Is(target error) bool { return target == ErrImportFetchFailed }

// bundleLinkRE 匹配 /bundle/ 或 /sub/ 链接；裸数字默认按 bundle 探测。
var bundleLinkRE = regexp.MustCompile(`(?i)(?:store\.steampowered\.com|steamdb\.info|steamdb\.in)/(?:bundle|sub)/(\d+)`)

var bareIDRE = regexp.MustCompile(`^[0-9]+$`)

func parseBundleRef(text string) (int64, string, bool) {
	s := strings.TrimSpace(text)
	if s == "" {
		return 0, "", false
	}
	if m := bundleLinkRE.FindStringSubmatch(s); m != nil {
		id, err := strconv.ParseInt(m[1], 10, 64)
		if err != nil {
			return 0, "", false
		}
		kind := "bundle"
		if strings.Contains(strings.ToLower(m[0]), "/sub/") {
			kind = "sub"
		}
		return id, kind, true
	}
	if bareIDRE.MatchString(s) {
		id, err := strconv.ParseInt(s, 10, 64)
		if err != nil {
			return 0, "", false
		}
		return id, "bundle", true
	}
	return 0, "", false
}

type ImportOutcome struct {
	OK           bool
	BundleID     int64
	Kind         string
	Existed      bool
	RegionPrices int
	Name         string
	Detail       string
}

// ImportBundle 导入并覆盖刷新单包；手动导入允许保留单 appid 的 sub。
func (s *Service) ImportBundle(ctx context.Context, text string) (ImportOutcome, error) {
	bundleID, kind, ok := parseBundleRef(text)
	if !ok {
		return ImportOutcome{}, ErrUnrecognizedRef
	}
	rateMap, err := s.loadRates(ctx)
	if err != nil {
		return ImportOutcome{}, fmt.Errorf("汇率读取失败: %w", err)
	}
	regions, err := s.fetchBundleRegions(ctx, bundleID, kind == "sub")
	if err != nil {
		return ImportOutcome{}, err
	}
	if len(regions) == 0 {
		label := "捆绑包"
		if kind == "sub" {
			label = "Sub"
		}
		return ImportOutcome{}, &importFetchError{message: fmt.Sprintf(
			"%s %d 抓取失败（不存在/限速/网络）——稍后重试或检查链接", label, bundleID)}
	}
	existed, err := s.bundleExists(ctx, bundleID)
	if err != nil {
		return ImportOutcome{}, err
	}
	nowText := s.Now().Format(timeLayout)
	stored, err := s.upsertBundleRows(ctx, bundleID, regions, rateMap, nowText, true)
	if err != nil {
		return ImportOutcome{}, err
	}
	if !stored {
		return ImportOutcome{OK: false, Detail: "落库失败"}, nil
	}
	baseline := regions[0]
	for _, r := range regions[1:] {
		if len(r.AppIDs) > len(baseline.AppIDs) {
			baseline = r
		}
	}
	name := baseline.Name

	var clearExclusion = false
	if _, err := s.Mon.Apply(ctx, "bundle", []int64{bundleID}, monitoring.Change{
		Sources:  map[int64][]string{bundleID: {"import"}},
		Excluded: &clearExclusion,
	}); err != nil {
		s.Logf("捆绑包监控来源登记失败，不影响导入结果 │ 原因=%v", err)
	}
	formLabel := "捆绑包（Bundle）"
	if kind == "sub" {
		formLabel = "订阅包（Sub）"
	}
	s.Logf("捆绑包导入完成 │ 形态=%s，捆绑包号=%d，区域价数=%d", formLabel, bundleID, len(regions))

	if err := s.refreshSortCache(ctx, []int64{bundleID}); err != nil {
		s.Logf("导入后捆绑包排序快照重建失败，列表沿用上一版快照 │ 原因=%v", err)
	}
	return ImportOutcome{
		OK:           true,
		BundleID:     bundleID,
		Kind:         kind,
		Existed:      existed,
		RegionPrices: len(regions),
		Name:         name,
	}, nil
}

func (s *Service) bundleExists(ctx context.Context, bundleID int64) (bool, error) {
	var exists bool
	if err := s.DB.QueryRowContext(ctx,
		`SELECT EXISTS(SELECT 1 FROM bundles WHERE bundle_id=?)`, bundleID).Scan(&exists); err != nil {
		return false, err
	}
	return exists, nil
}
