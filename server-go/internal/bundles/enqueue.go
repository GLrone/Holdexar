// enqueue.go 新 AppID 预检入队：捆绑包内游戏的自动入库爬取联动
// （对齐 Python refresh.py:_enqueue_new_bundle_apps + ensure_game_exists +
// mark_non_game_type）。候选 = 全部 bundles.app_ids 中 games.type IS NULL 的
// 行；逐个 appdetails 轻量预检（每轮限量 30，余量顺延下轮），game/dlc 落
// 占位行后小批量爬取，非游戏打标结案，预检失败不打标（宁重试勿漏）。
package bundles

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"time"

	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/store"
)

const (
	appdetailsURL     = "https://store.steampowered.com/api/appdetails"
	precheckPerRun    = 30
	precheckInterval  = 300 * time.Millisecond
	detailsTimeout    = 15 * time.Second
	enqueueCrawlKind  = "bundle_apps"
)

// Discovery 新 AppID 小批量爬取入口（serverd 注入 priceRuntime.RunDiscovery；
// nil = 只预检落行不启动爬取）。Python 侧直调 run_crawl 且不建 crawl_jobs 行，
// Go 经 RunDiscovery 走同一执行链（任务账本多一行 kind=bundle_apps，登记差异）。
func (s *Service) EnqueueNewBundleApps(ctx context.Context) (int, int, error) {
	rows, err := s.DB.QueryContext(ctx, "SELECT app_ids FROM bundles")
	if err != nil {
		return 0, 0, err
	}
	all := map[int64]bool{}
	for rows.Next() {
		var raw sql.NullString
		if err := rows.Scan(&raw); err != nil {
			rows.Close()
			return 0, 0, err
		}
		for _, appid := range parseAppIDs(raw) {
			all[appid] = true
		}
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, 0, err
	}
	typedRows, err := s.DB.QueryContext(ctx, "SELECT appid FROM games WHERE type IS NOT NULL")
	if err != nil {
		return 0, 0, err
	}
	for typedRows.Next() {
		var appid int64
		if err := typedRows.Scan(&appid); err != nil {
			typedRows.Close()
			return 0, 0, err
		}
		delete(all, appid)
	}
	typedRows.Close()
	if err := typedRows.Err(); err != nil {
		return 0, 0, err
	}
	if len(all) == 0 {
		return 0, 0, nil
	}
	candidates := make([]int64, 0, len(all))
	for appid := range all {
		candidates = append(candidates, appid)
	}
	sort.Slice(candidates, func(i, j int) bool { return candidates[i] < candidates[j] })
	if len(candidates) > precheckPerRun {
		s.Logf("候选 AppID 超过本轮预检上限，只处理前一批，余量顺延下轮 │ 候选数=%d，本轮上限=%d",
			len(candidates), precheckPerRun)
		candidates = candidates[:precheckPerRun]
	}

	crawlPairs := []int64{}
	skipped := 0
	probeFailed := 0
	for _, appid := range candidates {
		appType, name, err := s.fetchAppType(ctx, appid)
		if err != nil {
			probeFailed++ // 预检失败：不标不记，下次刷新重试
			continue
		}
		if appType == "game" || appType == "dlc" {
			if err := s.ensureGameExists(ctx, appid, name); err != nil {
				s.Logf("补建游戏占位行失败 │ AppID=%d，原因=%v", appid, err)
				probeFailed++
				continue
			}
			crawlPairs = append(crawlPairs, appid)
			label := name
			if len([]rune(label)) > 30 {
				label = string([]rune(label)[:30])
			}
			s.Logf("捆绑包内游戏进入 App 爬取队列 │ AppID=%d，名称=%s，类型=%s", appid, label, appType)
		} else if appType != "" {
			if err := s.markNonGameType(ctx, appid, appType); err != nil {
				s.Logf("标记非游戏类型失败 │ AppID=%d，原因=%v", appid, err)
				probeFailed++
				continue
			}
			skipped++
			label := name
			if len([]rune(label)) > 30 {
				label = string([]rune(label)[:30])
			}
			s.Logf("捆绑包内非游戏项打标跳过 │ AppID=%d，名称=%s，类型=%s", appid, label, appType)
		} else {
			probeFailed++
		}
		if err := sleepPrecheck(ctx); err != nil {
			return len(crawlPairs), skipped, nil
		}
	}
	if probeFailed > 0 {
		s.Logf("部分 AppID 类型预检失败（限速/无可用代理/下架），未打标，下轮刷新重试 │ 失败数=%d，候选数=%d",
			probeFailed, len(candidates))
	}
	if len(crawlPairs) > 0 && s.Discovery != nil {
		if _, err := s.Discovery(ctx, enqueueCrawlKind, crawlPairs); err != nil {
			// 占位行已落库，本批未爬取——失败不外溢，未预检余量与占位行下轮自愈
			s.Logf("新 AppID 爬取任务失败，占位行已落库（本批未爬取）│ 原因=%v", err)
		} else {
			s.Logf("新 AppID 爬取完成 │ 总数=%d", len(crawlPairs))
		}
	}
	return len(crawlPairs), skipped, nil
}

// RefreshWithEnqueue 刷新 + 新 AppID 入队联动（对齐 refresh_bundles 尾段）：
// 联动失败时结果不含 appsEnqueued/appsSkippedNonGame（对齐 Python 条件键）。
func (s *Service) RefreshWithEnqueue(ctx context.Context) (map[string]any, error) {
	res, err := s.RefreshFollowed(ctx)
	if err != nil {
		return nil, err
	}
	out := refreshResultMap(res)
	if res.Total > 0 { // 空关注集整轮跳过：零出网也不做预检
		enqueued, skipped, err := s.EnqueueNewBundleApps(ctx)
		if err == nil {
			out["appsEnqueued"] = enqueued
			out["appsSkippedNonGame"] = skipped
		}
	}
	return out, nil
}

// refreshResultMap 刷新结果转响应形状：failed 恒为数组，droppedSingletons
// 仅在刷新真实执行时出现（跳过分支无该键，对齐 Python）。
func refreshResultMap(res RefreshResult) map[string]any {
	failed := res.Failed
	if failed == nil {
		failed = []int64{}
	}
	out := map[string]any{"ok": res.OK, "updated": res.Updated, "total": res.Total,
		"regionPrices": res.RegionPrices, "failed": failed}
	if res.DroppedSingletons != nil {
		out["droppedSingletons"] = *res.DroppedSingletons
	}
	return out
}

// fetchAppType 轻量 appdetails type 预检：只取 filters=basic（单 appid 一次
// 小请求）；cc=us（type/name 不随区变）。失败 (None, "")。
func (s *Service) fetchAppType(ctx context.Context, appid int64) (string, string, error) {
	url := fmt.Sprintf("%s?appids=%d&cc=us&filters=basic", appdetailsURL, appid)
	status, body, err := s.detailsClient.GetJSON(ctx, url, "")
	if err != nil || status != 200 {
		return "", "", fmt.Errorf("HTTP 状态 %d", status)
	}
	var data map[string]any
	if json.Unmarshal(body, &data) != nil {
		return "", "", fmt.Errorf("响应解析失败")
	}
	entry, _ := data[strconv.FormatInt(appid, 10)].(map[string]any)
	if entry == nil || entry["success"] != true {
		return "", "", fmt.Errorf("预检未成功（下架或限速）")
	}
	info, _ := entry["data"].(map[string]any)
	if info == nil {
		return "", "", fmt.Errorf("响应缺 data 节")
	}
	appType, _ := info["type"].(string)
	name, _ := info["name"].(string)
	return lowerASCII(appType), name, nil
}

// ensureGameExists 确保 games 表有占位行（满足外键约束），同事务派生重算
// （目录快照先取后插：新行对系列联动视为变化，对齐 Python ensure 链）。
func (s *Service) ensureGameExists(ctx context.Context, appid int64, name string) error {
	return s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		before, err := derived.CatalogInputs(ctx, conn, []int64{appid})
		if err != nil {
			return err
		}
		now := s.Now().Format(timeLayout)
		if _, err := conn.ExecContext(ctx,
			"INSERT INTO games(appid,name,created_at,updated_at) VALUES(?,?,?,?) ON CONFLICT(appid) DO NOTHING",
			appid, name, now, now); err != nil {
			return err
		}
		overrides, order := s.seriesOverrides()
		_, err = derived.UpdatePriceDerived(ctx, conn, []int64{appid}, derived.Options{
			Now: s.Now(), CatalogBefore: before, Overrides: overrides, OverrideOrder: order,
		})
		return err
	})
}

// markNonGameType 非游戏类型落 type + updated_at（脱离首爬候选）：只写
// type/时间戳两列，不碰其他元数据（名字保持挂名值）。
func (s *Service) markNonGameType(ctx context.Context, appid int64, appType string) error {
	return s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		before, err := derived.CatalogInputs(ctx, conn, []int64{appid})
		if err != nil {
			return err
		}
		now := s.Now().Format(timeLayout)
		if _, err := conn.ExecContext(ctx,
			"INSERT INTO games(appid,name,type,created_at,updated_at) VALUES(?,?,?,?,?)"+
				" ON CONFLICT(appid) DO UPDATE SET type=excluded.type, updated_at=excluded.updated_at",
			appid, fmt.Sprintf("AppID_%d", appid), upperASCII(appType), now, now); err != nil {
			return err
		}
		overrides, order := s.seriesOverrides()
		_, err = derived.UpdatePriceDerived(ctx, conn, []int64{appid}, derived.Options{
			Now: s.Now(), CatalogBefore: before, Overrides: overrides, OverrideOrder: order,
		})
		return err
	})
}

// seriesOverrides 系列覆盖表（闸外装填；nil 装填器 = 无覆盖）。
func (s *Service) seriesOverrides() (map[string][]int64, []string) {
	if s.LoadOverrides == nil {
		return nil, nil
	}
	return s.LoadOverrides()
}

func sleepPrecheck(ctx context.Context) error {
	timer := time.NewTimer(precheckInterval)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func lowerASCII(s string) string {
	out := []rune(s)
	for i, r := range out {
		if r >= 'A' && r <= 'Z' {
			out[i] = r + 32
		}
	}
	return string(out)
}

func upperASCII(s string) string {
	out := []rune(s)
	for i, r := range out {
		if r >= 'a' && r <= 'z' {
			out[i] = r - 32
		}
	}
	return string(out)
}
