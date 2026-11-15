// 捆绑包导入链：链接/裸 ID 识别 → 双轨整包抓取 → 落库（单包刷新语义）
// → 监控登记 → 排序快照（对齐 Python refresh.py import_bundle）。
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

// ErrUnrecognizedRef 链接/ID 识别失败（HTTP 层转 400）。
var ErrUnrecognizedRef = errors.New("无法识别的 Steam 链接或捆绑包 ID")

// ErrImportFetchFailed 抓取空表（不存在/限速/网络；HTTP 层转 400）。
var ErrImportFetchFailed = errors.New("捆绑包抓取失败")

// importFetchError 抓取空表错误：消息逐字对齐 Python 文案，
// errors.Is(err, ErrImportFetchFailed) 判别。
type importFetchError struct{ message string }

func (e *importFetchError) Error() string { return e.message }

func (e *importFetchError) Is(target error) bool { return target == ErrImportFetchFailed }

// bundleLinkRE Steam 链接形态：/bundle/21478、/sub/61597（商店/steamdb 同段式，
// 容忍 query 串），裸数字默认按 bundle 探测（双轨判定会自动落到 sub）。
var bundleLinkRE = regexp.MustCompile(`(?i)(?:store\.steampowered\.com|steamdb\.info|steamdb\.in)/(?:bundle|sub)/(\d+)`)

// bareIDRE 裸数字（Python str.isdigit 的 ASCII 子集）。
var bareIDRE = regexp.MustCompile(`^[0-9]+$`)

// parseBundleRef Steam 链接/裸 ID → (id, kind)。kind: bundle/sub。
// 其余输入返回 ok=false。
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

// ImportOutcome 导入结果。OK=false 时仅 OK/Detail 有值（对齐 Python 失败
// 分支 {"ok":false,"detail":...}）；ErrUnrecognizedRef / ErrImportFetchFailed /
// ErrRegionsDisabled 经 error 返回（HTTP 层转 400）。
type ImportOutcome struct {
	OK           bool
	BundleID     int64
	Kind         string
	Existed      bool
	RegionPrices int
	Name         string
	Detail       string
}

// ImportBundle 导入单个捆绑包/Sub：链接识别 → 双轨整包抓取 → 落库（单包
// 刷新语义）。已在库的包重新导入 = 单包刷新（覆盖更新）。手动导入 = 用户
// 明确意图：单 appid 的 sub（Prime 升级类）例外保留。
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
		return ImportOutcome{}, err // ErrRegionsDisabled 哨兵上抛（HTTP 400 族）
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
	// 主档名 = appids 最多行的 name（与 upsert baseline 同判据，回显给前端）
	baseline := regions[0]
	for _, r := range regions[1:] {
		if len(r.AppIDs) > len(baseline.AppIDs) {
			baseline = r
		}
	}
	name := baseline.Name

	// 导入 = 用户明确意图：挂上 import 来源并解除排除。与 Game 同一套
	// 生命周期——失败不影响导入结果（主档与价格已落库）
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

	// 排序快照重建（单包，写时算好、GET 只读）；失败仅日志
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

// bundleExists existed 判定：bundles 表已有该包（重新导入 = 单包刷新）。
func (s *Service) bundleExists(ctx context.Context, bundleID int64) (bool, error) {
	var exists bool
	if err := s.DB.QueryRowContext(ctx,
		`SELECT EXISTS(SELECT 1 FROM bundles WHERE bundle_id=?)`, bundleID).Scan(&exists); err != nil {
		return false, err
	}
	return exists, nil
}
