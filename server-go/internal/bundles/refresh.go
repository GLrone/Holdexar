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

type RefreshResult struct {
	OK                bool    `json:"ok"`
	Updated           int     `json:"updated"`
	Total             int     `json:"total"`
	RegionPrices      int     `json:"regionPrices"`
	Failed            []int64 `json:"failed"`
	DroppedSingletons *int    `json:"droppedSingletons"`
}

const timeLayout = "2006-01-02 15:04:05.000000"

type Service struct {
	DB      *sql.DB
	Gate    *store.WriteGate
	Mon     *monitoring.Service
	DataDir string
	Now     func() time.Time
	Logf    func(format string, args ...any)

	Discovery func(ctx context.Context, kind string, ids []int64) (int64, error)

	LoadOverrides func() (map[string][]int64, []string)

	client        *steamhttp.Client
	detailsClient *steamhttp.Client

	ratesMu    sync.Mutex
	ratesCache map[string]float64
	ratesAt    time.Time
}

// NewService 装配直连 HTTP 客户端（按 country_code 取价），限流与熔断使用进程级单例（200 请求/300 秒）。
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

// RefreshFollowed 仅刷新 monitoring active 的关注/导入包（空关注集跳过）。
// 全禁用区时捕获 ErrRegionsDisabled 并跳过（仅 ImportBundle 将该错误上抛为 400）。
func (s *Service) RefreshFollowed(ctx context.Context) (RefreshResult, error) {
	zero := RefreshResult{OK: true, Failed: []int64{}}
	rateMap, err := s.loadRates(ctx)
	if err != nil {
		return RefreshResult{}, fmt.Errorf("汇率读取失败: %w", err)
	}
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
	// 形态兜底：库内形态未命中时翻转 bundleid/packageid 复探。
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
	var touched []int64
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
			dropped++
			continue
		}
		updated++
		prices += len(regions)
		touched = append(touched, w.BundleID)
	}

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

// loadWant 形态选键优先取 item_kind，未回填时回退 must_purchase_as_set。
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

func (s *Service) refreshSortCache(ctx context.Context, ids []int64) error {
	tracked := sortedKeys(trackedRegionCodes(ctx, s.DB))
	return s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, err := RefreshSortCache(ctx, conn, ids, tracked)
		return err
	})
}

// upsertBundleRows 落库单包；allowSingleton=false 时包含 <2 个 appid 的单品直接删行跳过。
func (s *Service) upsertBundleRows(ctx context.Context, bundleID int64, regions []regionRow, rateMap map[string]float64, nowText string, allowSingleton bool) (bool, error) {
	baseline := regions[0]
	for _, r := range regions[1:] {
		if len(r.AppIDs) > len(baseline.AppIDs) {
			baseline = r
		}
	}
	if !allowSingleton && len(dedupInt64(baseline.AppIDs)) < 2 {
		err := s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
			if _, e := conn.ExecContext(ctx, `DELETE FROM bundle_region_prices WHERE bundle_id=?`, bundleID); e != nil {
				return e
			}
			_, e := conn.ExecContext(ctx, `DELETE FROM bundles WHERE bundle_id=?`, bundleID)
			return e
		})
		return false, err
	}
	// item_kind 由各区 item_type 多数决；平票或无票时不改写库内既有值。
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
	// must_purchase_as_set 由有效选项区多数决（锁区行不投票）；全无票且库内未定值时补 0。
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
			var appIDsRow any = "[]"
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

		// 删除本次未写入区及非小写重复 region_code 行，避免展示层选到未归一旧值。
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

func nullString(v sql.NullString) any {
	if v.Valid && v.String != "" {
		return v.String
	}
	return nil
}
