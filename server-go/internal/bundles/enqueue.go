package bundles

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"slices"
	"strconv"
	"time"

	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/store"
)

const (
	appdetailsURL    = "https://store.steampowered.com/api/appdetails"
	precheckPerRun   = 30
	precheckInterval = 300 * time.Millisecond
	detailsTimeout   = 15 * time.Second
	enqueueCrawlKind = "bundle_apps"
)

// EnqueueNewBundleApps 预检未知 AppID 并落游戏占位行，经 Discovery 触发小批量爬取（nil 时跳过爬取）。
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
	slices.Sort(candidates)
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
			probeFailed++
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
			s.Logf("新 AppID 爬取任务失败，占位行已落库（本批未爬取）│ 原因=%v", err)
		} else {
			s.Logf("新 AppID 爬取完成 │ 总数=%d", len(crawlPairs))
		}
	}
	return len(crawlPairs), skipped, nil
}

// RefreshWithEnqueue 刷新关注捆绑包并触发新 AppID 入队，联动失败时不写入入队计数字段。
func (s *Service) RefreshWithEnqueue(ctx context.Context) (map[string]any, error) {
	res, err := s.RefreshFollowed(ctx)
	if err != nil {
		return nil, err
	}
	out := refreshResultMap(res)
	if res.Total > 0 {
		enqueued, skipped, err := s.EnqueueNewBundleApps(ctx)
		if err == nil {
			out["appsEnqueued"] = enqueued
			out["appsSkippedNonGame"] = skipped
		}
	}
	return out, nil
}

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

// fetchAppType 轻量 appdetails 预检（filters=basic, cc=us），失败返回空串与错误。
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

// ensureGameExists 写入 games 占位行并在同事务重算派生列（须在插入前采目录快照）。
func (s *Service) ensureGameExists(ctx context.Context, appid int64, name string) error {
	overrides, order := s.seriesOverrides()
	return s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		before, err := derived.CatalogInputs(ctx, conn, []int64{appid})
		if err != nil {
			return err
		}
		now := s.Now().Format(timeLayout)
		if _, err := conn.ExecContext(ctx,
			`INSERT INTO games(appid,name,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count,created_at,updated_at)
			 VALUES(?,?,0,0,0,0,0,0,0,0,0,0,0,0,0,?,?) ON CONFLICT(appid) DO NOTHING`,
			appid, name, now, now); err != nil {
			return err
		}
		_, err = derived.UpdatePriceDerived(ctx, conn, []int64{appid}, derived.Options{
			Now: s.Now(), CatalogBefore: before, Overrides: overrides, OverrideOrder: order,
		})
		return err
	})
}

// markNonGameType 仅更新非游戏条目的 type 与 updated_at，使其脱离首爬候选。
func (s *Service) markNonGameType(ctx context.Context, appid int64, appType string) error {
	overrides, order := s.seriesOverrides()
	return s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		before, err := derived.CatalogInputs(ctx, conn, []int64{appid})
		if err != nil {
			return err
		}
		now := s.Now().Format(timeLayout)
		if _, err := conn.ExecContext(ctx,
			`INSERT INTO games(appid,name,type,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count,created_at,updated_at)
			 VALUES(?,?,?,0,0,0,0,0,0,0,0,0,0,0,0,0,?,?)
			 ON CONFLICT(appid) DO UPDATE SET type=excluded.type, updated_at=excluded.updated_at`,
			appid, fmt.Sprintf("AppID_%d", appid), upperASCII(appType), now, now); err != nil {
			return err
		}
		_, err = derived.UpdatePriceDerived(ctx, conn, []int64{appid}, derived.Options{
			Now: s.Now(), CatalogBefore: before, Overrides: overrides, OverrideOrder: order,
		})
		return err
	})
}

func (s *Service) seriesOverrides() (map[string][]int64, []string) {
	if s.LoadOverrides != nil {
		return s.LoadOverrides()
	}
	if s.DataDir != "" {
		return derived.LoadSeriesOverrides(s.DataDir)
	}
	return nil, nil
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
