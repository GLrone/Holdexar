// 捆绑包刷新落库链：监控在册整表逐区抓取 → 单事务落库（单品甄别/多数决/
// 脏行清理）→ 排序快照增量重建（对齐 Python refresh.py refresh_bundles）。
package bundles

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/steamhttp"
	"holdexar/server-go/internal/store"
)

// RefreshResult 刷新返回（对齐 refresh_bundles 返回值）。整轮跳过时
// DroppedSingletons 为 nil（Python 跳过分支无该键）。
type RefreshResult struct {
	OK                bool    `json:"ok"`
	Updated           int     `json:"updated"`
	Total             int     `json:"total"`
	RegionPrices      int     `json:"regionPrices"`
	Failed            []int64 `json:"failed"`
	DroppedSingletons *int    `json:"droppedSingletons"`
}

// timeLayout DATETIME 列存储文本：naive 挂钟 6 位微秒（对齐 SQLAlchemy 存储）。
const timeLayout = "2006-01-02 15:04:05.000000"

// Service 捆绑包抓取落库链。生产装配一份（cmd/serverd），测试每例一份
// （汇率缓存/出网客户端随实例隔离）。
type Service struct {
	DB   *sql.DB
	Gate *store.WriteGate
	Mon  *monitoring.Service
	Now  func() time.Time
	Logf func(format string, args ...any)

	// Discovery 新 AppID 小批量爬取入口（serverd 注入 crawl 运行时；nil = 不爬）。
	Discovery func(ctx context.Context, kind string, ids []int64) (int64, error)

	// LoadOverrides 系列覆盖表装填（闸外文件读取；nil = 无覆盖）。
	LoadOverrides func() (map[string][]int64, []string)

	client        *steamhttp.Client
	detailsClient *steamhttp.Client

	ratesMu    sync.Mutex
	ratesCache map[string]float64
	ratesAt    time.Time
}

// NewService 装配刷新服务：出网直连档（browse 按 country_code 返回各区
// 数据，出口 IP 不参与判定），限流/熔断回落进程级单例（与 app 爬取链共享
// 200 发/300 秒窗口，对齐 Python 共享 steam_rate_limiter）。
func NewService(db *sql.DB, gate *store.WriteGate, mon *monitoring.Service) *Service {
	return &Service{
		DB:            db,
		Gate:          gate,
		Mon:           mon,
		Now:           func() time.Time { return time.Now().UTC() },
		Logf:          func(string, ...any) {},
		client:        steamhttp.New(steamhttp.Config{Timeout: browseTimeout}),
		detailsClient: steamhttp.New(steamhttp.Config{Timeout: detailsTimeout}),
	}
}

// RefreshFollowed 刷新关注的捆绑包：监控层在册的包监控区整表每区一发
// （≤300 条）→ upsert → 排序快照增量重建。
//
// 候选集 = monitoring 层 target_type=bundle 的 active 对象——favorite
// （星标关注）与 import（手动导入）都是用户显式意图，excluded / released
// 被状态门挡在 active 之外。库内未关注的包（含游戏爬取落下的发现桩）不随
// 轮刷新，运行负担只随关注的集合走。空关注集整轮跳过且零出网；全禁用区按
// 跳过语义处理（自动链尾不炸；手动通道经 fetchCCs 的哨兵错误转 400）。
func (s *Service) RefreshFollowed(ctx context.Context) (RefreshResult, error) {
	zero := RefreshResult{OK: true, Failed: []int64{}}
	rateMap, err := s.loadRates(ctx)
	if err != nil {
		return RefreshResult{}, fmt.Errorf("汇率读取失败: %w", err)
	}
	// 先看关注集再解析抓取区：无关注包时整轮跳过，不碰区域配置
	watched, err := s.Mon.ActiveBundleIDs(ctx)
	if err != nil {
		return RefreshResult{}, err
	}
	want, err := s.loadWant(ctx, watched)
	if err != nil {
		return RefreshResult{}, err
	}
	if len(want) == 0 {
		s.Logf("没有关注的捆绑包，本轮跳过刷新")
		return zero, nil
	}
	ccs, err := s.fetchCCs(ctx)
	if err != nil {
		if errors.Is(err, ErrRegionsDisabled) {
			s.Logf("捆绑包刷新跳过：%v", err)
			return zero, nil
		}
		return RefreshResult{}, err
	}

	nowText := s.Now().Format(timeLayout)
	s.Logf("开始刷新关注的捆绑包（监控区整表逐区一发） │ 包数=%d，抓取区=%s", len(want), strings.Join(ccs, "/"))

	rowsByID, err := s.fetchRegionsBatched(ctx, want, ccs)
	if err != nil {
		return RefreshResult{}, err
	}
	// 形态兜底：按库内形态问不到真实条目的包，换另一形态再问一次
	// （历史形态缺失/曾按错形态导入的包自愈；单区一发，代价可忽略）
	var missing []wantEntry
	for _, w := range want {
		if len(rowsByID[w.BundleID]) == 0 {
			missing = append(missing, w)
		}
	}
	if len(missing) > 0 {
		flipped := make([]wantEntry, len(missing))
		for i, w := range missing {
			alt := int64(0)
			if browseKind(w.Kind) == "bundleid" {
				alt = 1
			}
			flipped[i] = wantEntry{BundleID: w.BundleID, Kind: &alt}
		}
		s.Logf("部分包按库内形态未命中，换形态兜底探测 │ 包数=%d", len(flipped))
		altRows, err := s.fetchRegionsBatched(ctx, flipped, ccs)
		if err != nil {
			return RefreshResult{}, err
		}
		for _, w := range missing {
			if len(altRows[w.BundleID]) > 0 {
				rowsByID[w.BundleID] = altRows[w.BundleID]
			}
		}
	}

	failed := []int64{}
	for _, w := range want {
		if len(rowsByID[w.BundleID]) == 0 {
			failed = append(failed, w.BundleID)
		}
	}
	updated, prices, dropped := 0, 0, 0
	var touched []int64 // 本轮成功落库的包：链尾统一重建排序快照
	for _, w := range want {
		regions := rowsByID[w.BundleID]
		if len(regions) == 0 {
			continue
		}
		stored, err := s.upsertBundleRows(ctx, w.BundleID, regions, rateMap, nowText, false)
		if err != nil {
			return RefreshResult{}, err
		}
		if !stored {
			dropped++ // 包内 appid<2：非捆绑，甄别删除
			continue
		}
		updated++
		prices += len(regions)
		touched = append(touched, w.BundleID)
	}

	// 排序快照增量重建（bundles.min_cny_fen/diff_fen/is_lowest）：写时算好、
	// GET 只读；失败仅日志（列表沿用上一版快照，短窗口自愈）
	if len(touched) > 0 {
		if err := s.refreshSortCache(ctx, touched); err != nil {
			s.Logf("捆绑包排序快照重建失败，列表沿用上一版快照 │ 原因=%v", err)
		}
	}

	s.Logf("捆绑包刷新完成 │ 成功包数=%d，总包数=%d，区域价行=%d，失败包数=%d，单品剔除=%d，抓取区=%s",
		updated, len(want), prices, len(failed), dropped, strings.Join(ccs, "/"))
	droppedValue := dropped
	return RefreshResult{
		OK:                len(failed) == 0,
		Updated:           updated,
		Total:             len(want),
		RegionPrices:      prices,
		Failed:            failed,
		DroppedSingletons: &droppedValue,
	}, nil
}

// loadWant 关注集 → (bundle_id, COALESCE(item_kind, must_purchase_as_set))
// 按 bundle_id 升序（形态选键：item_kind 权威，v3 之前的行兜底沿用 mps 旧值）。
func (s *Service) loadWant(ctx context.Context, watched []int64) ([]wantEntry, error) {
	if len(watched) == 0 {
		return nil, nil
	}
	raw, err := json.Marshal(watched)
	if err != nil {
		return nil, err
	}
	rows, err := s.DB.QueryContext(ctx, `SELECT bundle_id, COALESCE(item_kind, must_purchase_as_set)
 FROM bundles WHERE bundle_id IN (SELECT value FROM json_each(?)) ORDER BY bundle_id`, string(raw))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var want []wantEntry
	for rows.Next() {
		var id int64
		var kind sql.NullInt64
		if err := rows.Scan(&id, &kind); err != nil {
			return nil, err
		}
		want = append(want, wantEntry{BundleID: id, Kind: bundleNullKind(kind)})
	}
	return want, rows.Err()
}

// refreshSortCache 排序快照增量重建：tracked 区集由本层解析（对齐 Python
// refresh_bundle_sort_cache 内取 _tracked_region_codes），BACKGROUND 写闸内
// 在单连接上执行。
func (s *Service) refreshSortCache(ctx context.Context, ids []int64) error {
	tracked := sortedKeys(trackedRegionCodes(ctx, s.DB))
	return s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, err := RefreshSortCache(ctx, conn, ids, tracked)
		return err
	})
}

// upsertBundleRows 整包落库：主档 upsert + 区域价逐区 upsert + 双轨混写脏行
// 清理。refresh_bundles 与 ImportBundle 共用（导入即单包刷新，同一套写库
// 语义：主档取 appids 最多行为基准，非本次写入的区行整行清理）。
//
// 返回是否落库。单品甄别（allowSingleton=false 时）：包内 appid < 2 的不是
// 捆绑包——游戏页 purchase_options 发现链会把本体 sub / 单 DLC sub / 单游戏
// 升级 sub 一起暴露出来（included_appids 是权威鉴别），这类整包删除既有行
// 并跳过写入，防止发现链把单品灌进捆绑包表。手动导入（用户明确意图）传
// allowSingleton=true 例外。
func (s *Service) upsertBundleRows(ctx context.Context, bundleID int64, regions []regionRow, rateMap map[string]float64, nowText string, allowSingleton bool) (bool, error) {
	// 主档取 appids 最多的行为基准（baseline 语义；锁区/包内游戏依据）；
	// 并列取首行（对齐 Python max 的严格大于语义）
	baseline := regions[0]
	for _, r := range regions[1:] {
		if len(r.AppIDs) > len(baseline.AppIDs) {
			baseline = r
		}
	}
	if !allowSingleton && len(dedupInt64(baseline.AppIDs)) < 2 {
		// 单品：删净既有主档/区域价（发现桩经链尾刷新抓价后到此甄别）
		err := s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
			if _, e := conn.ExecContext(ctx, `DELETE FROM bundle_region_prices WHERE bundle_id=?`, bundleID); e != nil {
				return e
			}
			_, e := conn.ExecContext(ctx, `DELETE FROM bundles WHERE bundle_id=?`, bundleID)
			return e
		})
		return false, err
	}
	// 形态（链接/CDN 用）：item_type 1=Sub / 2=Bundle 是 Steam 权威答案。
	// 逐区 item_type 理论恒定，但取多数决防单区脏值；无票（历史行/合成行）
	// 时不改写库内既有形态
	var kindVotes, mpsVotes []int64
	for _, r := range regions {
		if r.ItemKind != nil && (*r.ItemKind == 0 || *r.ItemKind == 1) {
			kindVotes = append(kindVotes, *r.ItemKind)
		}
		if r.MPS != nil && (*r.MPS == 0 || *r.MPS == 1) {
			mpsVotes = append(mpsVotes, *r.MPS)
		}
	}
	kindValue := majority(kindVotes)
	// 购买语义：Sub 形态恒不可拆；bundle 形态以选项级 must_purchase_as_set
	// 为准（形态与语义解耦）。锁区行不投票（browseRow 已滤），有选项的区间
	// 偶发不一致由多数决归位。全无票且库内 NULL/-1 时补 0（判据兜底）
	mpsValue := majority(mpsVotes)

	err := s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		var curName, curHeader, curAppIDs sql.NullString
		var curMPS, curKind sql.NullInt64
		e := conn.QueryRowContext(ctx, `SELECT name, header_image, app_ids, must_purchase_as_set, item_kind
 FROM bundles WHERE bundle_id=?`, bundleID).Scan(&curName, &curHeader, &curAppIDs, &curMPS, &curKind)
		exists := e == nil
		if e != nil && !errors.Is(e, sql.ErrNoRows) {
			return e
		}
		if !exists {
			// 新行缺省对齐 Python 模型：mps=-1、kind=-1、名称空串（Go 建表
			// name NOT NULL）、其余可空列 NULL
			curName = sql.NullString{String: "", Valid: true}
			curMPS = sql.NullInt64{Int64: -1, Valid: true}
			curKind = sql.NullInt64{Int64: -1, Valid: true}
		}
		name := curName
		if baseline.Name != "" {
			name = sql.NullString{String: baseline.Name, Valid: true}
		}
		header := curHeader
		if baseline.HeaderImage != "" {
			header = sql.NullString{String: baseline.HeaderImage, Valid: true}
		}
		appIDs := curAppIDs
		if len(baseline.AppIDs) > 0 {
			appIDs = sql.NullString{String: pyJSONInts(baseline.AppIDs), Valid: true}
		}
		mps := curMPS
		if mpsValue != nil {
			mps = sql.NullInt64{Int64: *mpsValue, Valid: true}
		} else if !curMPS.Valid || curMPS.Int64 == -1 {
			mps = sql.NullInt64{Int64: 0, Valid: true}
		}
		kind := curKind
		if kindValue != nil {
			kind = sql.NullInt64{Int64: *kindValue, Valid: true}
		} else if !curKind.Valid || curKind.Int64 == -1 {
			if mps.Valid && (mps.Int64 == 0 || mps.Int64 == 1) {
				kind = sql.NullInt64{Int64: 0, Valid: true}
			} else {
				kind = sql.NullInt64{Int64: -1, Valid: true}
			}
		}
		var appIDsValue any
		if appIDs.Valid {
			appIDsValue = appIDs.String
		}
		if !exists {
			if _, e = conn.ExecContext(ctx, `INSERT INTO bundles
 (bundle_id,name,must_purchase_as_set,item_kind,header_image,is_lowest,min_cny_fen,diff_fen,smart_score,url,app_ids,view_count,updated_at)
 VALUES(?,?,?,?,?,0,NULL,0,NULL,NULL,?,0,?)`,
				bundleID, name.String, mps.Int64, kind.Int64, nullString(header), appIDsValue, nowText); e != nil {
				return e
			}
		} else {
			if _, e = conn.ExecContext(ctx, `UPDATE bundles SET name=?, must_purchase_as_set=?, item_kind=?,
 header_image=?, app_ids=?, updated_at=? WHERE bundle_id=?`,
				name.String, mps.Int64, kind.Int64, nullString(header), appIDsValue, nowText, bundleID); e != nil {
				return e
			}
		}

		for _, r := range regions {
			cnyFen := rowCnyFen(r, rateMap)
			var priceValue, originalValue, cnyValue, endTSValue any
			if r.Price != nil {
				priceValue = *r.Price
			}
			if r.OriginalPrice != nil {
				originalValue = *r.OriginalPrice
			}
			if cnyFen != nil {
				cnyValue = *cnyFen
			}
			if r.DiscountEndTS != nil {
				endTSValue = *r.DiscountEndTS
			}
			var appIDsRow any = "[]" // Python r.get("app_ids") or []
			if r.AppIDs != nil {
				appIDsRow = pyJSONInts(r.AppIDs)
			}
			var currencyValue any
			if r.Currency != "" {
				currencyValue = r.Currency
			}
			var existingID int64
			e := conn.QueryRowContext(ctx, `SELECT id FROM bundle_region_prices
 WHERE bundle_id=? AND region_code=?`, bundleID, r.RegionCode).Scan(&existingID)
			if errors.Is(e, sql.ErrNoRows) {
				if _, e = conn.ExecContext(ctx, `INSERT INTO bundle_region_prices
 (bundle_id,region_code,currency,price,original_price,discount_percent,bundle_base_discount,price_status,cny_fen,discount_end_ts,app_ids,crawled_at)
 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)`,
					bundleID, r.RegionCode, currencyValue, priceValue, originalValue,
					r.DiscountPercent, r.BaseDiscount, r.PriceStatus, cnyValue, endTSValue, appIDsRow, nowText); e != nil {
					return e
				}
				continue
			}
			if e != nil {
				return e
			}
			if _, e = conn.ExecContext(ctx, `UPDATE bundle_region_prices SET currency=?, price=?, original_price=?,
 discount_percent=?, bundle_base_discount=?, price_status=?, cny_fen=?, discount_end_ts=?, app_ids=?, crawled_at=?
 WHERE id=?`,
				currencyValue, priceValue, originalValue, r.DiscountPercent, r.BaseDiscount,
				r.PriceStatus, cnyValue, endTSValue, appIDsRow, nowText, existingID); e != nil {
				return e
			}
		}

		// 历史脏行清理：非本次写入的区（双轨混写/异种产品/下架区）整行删；
		// 大小写重复行（'JP' 与 'jp' 同区）以本次写入的小写行为权威，旧行删——
		// 否则展示层「有价者优先」会随机挑中未归一的旧值
		fresh := map[string]bool{}
		for _, r := range regions {
			fresh[strings.ToLower(r.RegionCode)] = true
		}
		rows, e := conn.QueryContext(ctx, `SELECT id, region_code FROM bundle_region_prices WHERE bundle_id=?`, bundleID)
		if e != nil {
			return e
		}
		var stale []int64
		for rows.Next() {
			var id int64
			var code string
			if err := rows.Scan(&id, &code); err != nil {
				rows.Close()
				return err
			}
			if lower := strings.ToLower(code); !fresh[lower] || code != lower {
				stale = append(stale, id)
			}
		}
		if e := rows.Err(); e != nil {
			rows.Close()
			return e
		}
		rows.Close()
		for _, id := range stale {
			if _, e := conn.ExecContext(ctx, `DELETE FROM bundle_region_prices WHERE id=?`, id); e != nil {
				return e
			}
		}
		return nil
	})
	if err != nil {
		return false, err
	}
	return true, nil
}

// nullString 空串 → SQL NULL（可空文本列的 None 语义）。
func nullString(v sql.NullString) any {
	if v.Valid && v.String != "" {
		return v.String
	}
	return nil
}
