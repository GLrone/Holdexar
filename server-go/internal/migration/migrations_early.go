package migration

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"
	"unicode"
)

var earlyMigrationSteps = map[int]migrationStep{
	2: migrateBundlePriceUnits,
	3: func(ctx context.Context, conn *sql.Conn) error {
		return migrationExec(ctx, conn, "UPDATE bundles SET item_kind = must_purchase_as_set WHERE item_kind IS NULL OR item_kind = -1")
	},
	4: migrateHistorySnapshotUnique,
	5: func(ctx context.Context, conn *sql.Conn) error {
		return migrationExec(ctx, conn, "UPDATE wishlist_items SET wishlisted = 1 WHERE owned = 0 AND active = 1 AND manual = 0")
	},
	6: func(ctx context.Context, conn *sql.Conn) error {
		return migrationExec(ctx, conn, "DROP TABLE IF EXISTS game_achievements", "DROP TABLE IF EXISTS player_achievements")
	},
	7:  migrateAlertTargetsToCNY,
	8:  migrateFXHistoryCanonical,
	9:  func(ctx context.Context, conn *sql.Conn) error { return migrateMonitoringSources(ctx, conn, false) },
	10: migratePoolFlagsRetire,
	11: func(ctx context.Context, conn *sql.Conn) error {
		columns, err := migrationColumns(ctx, conn, "price_cycles")
		if err != nil || columns["coverage_json"] {
			return err
		}
		return migrationExec(ctx, conn, "ALTER TABLE price_cycles ADD COLUMN coverage_json JSON")
	},
	12: func(ctx context.Context, conn *sql.Conn) error {
		columns, err := migrationColumns(ctx, conn, "games")
		if err != nil || !columns["genres"] {
			return err
		}
		return migrationExec(ctx, conn, "ALTER TABLE games DROP COLUMN genres")
	},
	13: migrateGamesNameFTS,
}

const migrationDecimalFree = "'IDR', 'JPY', 'KRW', 'VND', 'CLP', 'COP', 'PYG', 'HUF'"

func migrationRates(ctx context.Context, conn *sql.Conn) (map[string]float64, error) {
	rows, err := conn.QueryContext(ctx, "SELECT currency_code, rate_to_cny FROM fx_rates")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	rates := make(map[string]float64)
	for rows.Next() {
		var currency string
		var rate sql.NullFloat64
		if err := rows.Scan(&currency, &rate); err != nil {
			return nil, err
		}
		if rate.Valid && rate.Float64 != 0 {
			rates[currency] = rate.Float64
		}
	}
	return rates, rows.Err()
}

func migrationRoundedAmount(value float64) (int64, error) {
	amount := math.RoundToEven(value)
	if math.IsNaN(amount) || math.IsInf(amount, 0) || amount < math.MinInt64 || amount >= 9223372036854775808.0 {
		return 0, errors.New("migrated monetary amount is outside SQLite integer range")
	}
	return int64(amount), nil
}

func migrationHundredfold(value int64) (int64, error) {
	if value > math.MaxInt64/100 || value < math.MinInt64/100 {
		return 0, errors.New("migrated monetary amount is outside SQLite integer range")
	}
	return value * 100, nil
}

func migrationAmountDistance(a, b int64) uint64 {
	if a >= b {
		return uint64(a) - uint64(b)
	}
	return uint64(b) - uint64(a)
}

func migrateBundlePriceUnits(ctx context.Context, conn *sql.Conn) error {
	rates, err := migrationRates(ctx, conn)
	if err != nil {
		return err
	}
	refs, err := conn.QueryContext(ctx, "SELECT bundle_id, cny_fen FROM bundle_region_prices WHERE currency NOT IN ("+migrationDecimalFree+") AND cny_fen IS NOT NULL AND cny_fen > 0")
	if err != nil {
		return err
	}
	references := make(map[int64][]float64)
	for refs.Next() {
		var id int64
		var fen float64
		if err := refs.Scan(&id, &fen); err != nil {
			refs.Close()
			return err
		}
		references[id] = append(references[id], fen)
	}
	err = refs.Err()
	refs.Close()
	if err != nil {
		return err
	}
	for _, values := range references {
		sort.Float64s(values)
	}
	rows, err := conn.QueryContext(ctx, `SELECT p.id,p.price,p.original_price,p.cny_fen,p.currency,p.bundle_id,b.must_purchase_as_set
		FROM bundle_region_prices p LEFT JOIN bundles b ON b.bundle_id=p.bundle_id
		WHERE p.currency IN (`+migrationDecimalFree+`) AND p.price IS NOT NULL AND p.price>0 AND p.cny_fen IS NOT NULL`)
	if err != nil {
		return err
	}
	type change struct {
		ID       int64  `json:"id"`
		Price    int64  `json:"price"`
		Original *int64 `json:"original"`
		Fen      int64  `json:"fen"`
	}
	var changes []change
	for rows.Next() {
		var id, price, fen, bundle int64
		var original, mps sql.NullInt64
		var currency string
		if err := rows.Scan(&id, &price, &original, &fen, &currency, &bundle, &mps); err != nil {
			rows.Close()
			return err
		}
		rate, ok := rates[currency]
		if !ok {
			continue
		}
		cents, err := migrationRoundedAmount(float64(price) * rate)
		if err != nil {
			rows.Close()
			return err
		}
		units, err := migrationRoundedAmount(float64(price) * rate * 100)
		if err != nil {
			rows.Close()
			return err
		}
		isUnits := false
		if values := references[bundle]; len(values) > 0 && cents > 0 {
			reference := values[len(values)/2]
			isUnits = math.Abs(math.Log(float64(units)/reference)) < math.Abs(math.Log(float64(cents)/reference))
		} else if migrationAmountDistance(fen, cents) <= migrationAmountDistance(fen, units) {
			isUnits = false
		} else if mps.Valid && mps.Int64 == 1 {
			isUnits = false
		} else if mps.Valid && mps.Int64 == 0 {
			isUnits = true
		} else {
			continue
		}
		updated := change{ID: id, Price: price, Fen: fen}
		if original.Valid {
			updated.Original = &original.Int64
		}
		if isUnits {
			updated.Price, err = migrationHundredfold(price)
			if err == nil && original.Valid {
				value, conversionErr := migrationHundredfold(original.Int64)
				err = conversionErr
				updated.Original = &value
			}
			if err != nil {
				rows.Close()
				return err
			}
			if migrationAmountDistance(fen, units) > migrationAmountDistance(fen, cents) {
				updated.Fen = units
			}
		} else if migrationAmountDistance(fen, cents) > migrationAmountDistance(fen, units) {
			updated.Fen = cents
		}
		if updated.Price != price || updated.Fen != fen || original.Valid && *updated.Original != original.Int64 {
			changes = append(changes, updated)
		}
	}
	err = rows.Err()
	rows.Close()
	if err != nil || len(changes) == 0 {
		return err
	}
	data, err := json.Marshal(changes)
	if err != nil {
		return err
	}
	_, err = conn.ExecContext(ctx, `UPDATE bundle_region_prices AS p SET
		price=json_extract(patch.value,'$.price'),original_price=json_extract(patch.value,'$.original'),cny_fen=json_extract(patch.value,'$.fen')
		FROM json_each(?) AS patch WHERE p.id=json_extract(patch.value,'$.id')`, string(data))
	return err
}

func migrateHistorySnapshotUnique(ctx context.Context, conn *sql.Conn) error {
	const expected = "CREATE UNIQUE INDEX ux_gph_snapshot ON game_price_history (appid, region_code, snapshot_at, COALESCE(sub_id, -1), COALESCE(price, 0), COALESCE(is_gold, 0))"
	var existing sql.NullString
	err := conn.QueryRowContext(ctx, "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_gph_snapshot'").Scan(&existing)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	normalize := func(value string) string {
		return strings.ToLower(strings.Map(func(r rune) rune {
			if unicode.IsSpace(r) || r == '"' {
				return -1
			}
			return r
		}, value))
	}
	if existing.Valid && existing.String != "" {
		if normalize(existing.String) == normalize(expected) {
			return nil
		}
		if err := migrationExec(ctx, conn, "DROP INDEX ux_gph_snapshot"); err != nil {
			return err
		}
	}
	var duplicates int
	if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM (
		SELECT 1 FROM game_price_history GROUP BY appid,region_code,snapshot_at,COALESCE(sub_id,-1),COALESCE(price,0),COALESCE(is_gold,0)
		HAVING COUNT(*)>1)`).Scan(&duplicates); err != nil {
		return err
	}
	if duplicates > 0 {
		return fmt.Errorf("%d duplicate price history groups prevent unique snapshot migration", duplicates)
	}
	return migrationExec(ctx, conn, expected)
}

var migrationRegionCurrencies = map[string]string{
	"CN": "CNY", "RU": "RUB", "KZ": "KZT", "UA": "UAH", "PK": "USD",
	"TR": "USD", "AR": "USD", "AZ": "USD", "VN": "VND", "ID": "IDR",
	"IN": "INR", "BR": "BRL", "CL": "CLP", "JP": "JPY", "HK": "HKD",
	"PH": "PHP", "TW": "TWD", "KW": "KWD", "SA": "SAR", "ZA": "ZAR",
	"QA": "QAR", "MY": "MYR", "TH": "THB", "PE": "PEN", "MX": "MXN",
	"SG": "SGD", "AE": "AED", "UY": "UYU", "CO": "COP", "KR": "KRW",
	"NZ": "NZD", "PL": "PLN", "CR": "CRC", "CA": "CAD", "US": "USD",
	"AU": "AUD", "PT": "EUR", "GB": "GBP", "NO": "NOK", "IL": "ILS", "CH": "CHF",
}

var migrationFallbackRates = map[string]float64{
	"CNY": 1.0, "USD": 7.25, "RUB": 0.079, "KZT": 0.015,
	"UAH": 0.175, "TRY": 0.22, "ARS": 0.0083, "VND": 0.00029,
	"IDR": 0.00046, "INR": 0.087, "BRL": 1.42, "CLP": 0.0076,
	"JPY": 0.048, "HKD": 0.928, "PHP": 0.126, "AZN": 4.26,
	"TWD": 0.21, "KWD": 3.31, "SAR": 1.93, "ZAR": 0.38,
	"QAR": 1.97, "MYR": 1.55, "THB": 0.20, "PEN": 2.12,
	"MXN": 0.42, "SGD": 5.40, "AED": 1.97, "UYU": 0.21,
	"COP": 0.0018, "KRW": 0.0052, "NZD": 4.50, "PLN": 2.15,
	"CRC": 0.13, "CAD": 5.20, "AUD": 4.70, "EUR": 7.90,
	"GBP": 9.20, "NOK": 0.66, "ILS": 1.95, "CHF": 8.10,
}

func migrateAlertTargetsToCNY(ctx context.Context, conn *sql.Conn) error {
	rates, err := migrationRates(ctx, conn)
	if err != nil {
		return err
	}
	rows, err := conn.QueryContext(ctx, "SELECT id,region,target_value FROM price_alerts WHERE target_type='price' AND target_value IS NOT NULL")
	if err != nil {
		return err
	}
	type change struct {
		ID    int64 `json:"id"`
		Value int64 `json:"value"`
	}
	var changes []change
	for rows.Next() {
		var id int64
		var region sql.NullString
		var value float64
		if err := rows.Scan(&id, &region, &value); err != nil {
			rows.Close()
			return err
		}
		currency := migrationRegionCurrencies[strings.ToUpper(region.String)]
		rate, ok := rates[currency]
		if !ok {
			rate, ok = migrationFallbackRates[currency]
		}
		if !ok {
			continue
		}
		amount, err := migrationRoundedAmount(value * rate)
		if err != nil {
			rows.Close()
			return err
		}
		changes = append(changes, change{ID: id, Value: amount})
	}
	err = rows.Err()
	rows.Close()
	if err != nil || len(changes) == 0 {
		return err
	}
	data, err := json.Marshal(changes)
	if err != nil {
		return err
	}
	_, err = conn.ExecContext(ctx, `UPDATE price_alerts AS p SET target_value=json_extract(patch.value,'$.value')
		FROM json_each(?) AS patch WHERE p.id=json_extract(patch.value,'$.id')`, string(data))
	return err
}

func migrateFXHistoryCanonical(ctx context.Context, conn *sql.Conn) error {
	return migrationExec(ctx, conn,
		"UPDATE fx_rate_history SET rate_date=substr(fetched_at,1,10) WHERE rate_date IS NULL AND fetched_at IS NOT NULL",
		"UPDATE fx_rate_history SET source_kind=CASE WHEN source='backfill' THEN 'carried' ELSE 'observed' END WHERE source_kind IS NULL",
		`DELETE FROM fx_rate_history WHERE id IN (SELECT id FROM (
			SELECT id,ROW_NUMBER() OVER (PARTITION BY currency_code,rate_date ORDER BY fetched_at DESC,id DESC) AS rn
			FROM fx_rate_history WHERE rate_date IS NOT NULL) WHERE rn>1)`,
		"CREATE UNIQUE INDEX IF NOT EXISTS ux_frh_currency_date ON fx_rate_history(currency_code,rate_date)")
}

func migrateMonitoringSources(ctx context.Context, conn *sql.Conn, projectOnly bool) error {
	now := time.Now().In(time.FixedZone("Asia/Shanghai", 8*60*60)).Format("2006-01-02 15:04:05")
	const projectFlag = "(COALESCE(board_pool,0)=1 OR COALESCE(manual_pool,0)=1)"
	where := "active=1"
	if projectOnly {
		where += " AND " + projectFlag
	}
	const priority = `MAX(CASE WHEN COALESCE(manual,0)=1 THEN 100 WHEN COALESCE(wishlisted,0)=1 THEN 95
		WHEN COALESCE(manual_pool,0)=1 THEN 60 WHEN COALESCE(owned,0)=1 THEN 40 WHEN COALESCE(board_pool,0)=1 THEN 20 ELSE 0 END)`
	if _, err := conn.ExecContext(ctx, `INSERT OR IGNORE INTO monitor_targets(target_type,target_id,state,priority,created_at,updated_at,activated_at)
		SELECT 'game',appid,'active',`+priority+`,?,?,? FROM wishlist_items WHERE `+where+` GROUP BY appid`, now, now, now); err != nil {
		return err
	}
	flags := []struct {
		column, source string
		priority       int
	}{{"manual", "favorite", 100}, {"wishlisted", "family_wishlist", 95}, {"manual_pool", "manual", 60}, {"owned", "owned", 40}, {"board_pool", "board", 20}}
	var projections []string
	for _, flag := range flags {
		if projectOnly && flag.column != "manual_pool" && flag.column != "board_pool" {
			continue
		}
		projections = append(projections, fmt.Sprintf("SELECT appid,'%s' AS source,%d AS priority FROM wishlist_items WHERE active=1 AND COALESCE(%s,0)=1 GROUP BY appid", flag.source, flag.priority, flag.column))
	}
	sources := strings.Join(projections, " UNION ALL ")
	if _, err := conn.ExecContext(ctx, `INSERT OR IGNORE INTO monitor_sources(target_type,target_id,source,priority,active,created_at,updated_at)
		SELECT 'game',appid,source,priority,1,?,? FROM (`+sources+`)`, now, now); err != nil {
		return err
	}
	if _, err := conn.ExecContext(ctx, `UPDATE monitor_sources AS s SET active=1,priority=p.priority,updated_at=?
		FROM (`+sources+`) AS p WHERE s.target_type='game' AND s.source=p.source AND s.target_id=p.appid`, now); err != nil {
		return err
	}
	removed := "active=0 AND COALESCE(excluded,0)=1"
	if projectOnly {
		removed += " AND " + projectFlag
	}
	removed += " AND appid NOT IN (SELECT appid FROM wishlist_items WHERE active=1) AND appid NOT IN (SELECT appid FROM games WHERE free_kind='f2p')"
	if _, err := conn.ExecContext(ctx, `INSERT OR IGNORE INTO monitor_targets(target_type,target_id,state,priority,created_at,updated_at,excluded_at)
		SELECT 'game',appid,'excluded',0,?,?,? FROM wishlist_items WHERE `+removed+` GROUP BY appid`, now, now, now); err != nil {
		return err
	}
	_, err := conn.ExecContext(ctx, `INSERT OR IGNORE INTO monitor_exclusions(target_type,target_id,reason,active,created_at)
		SELECT 'game',appid,'wishlist_removed',1,? FROM wishlist_items WHERE `+removed+` GROUP BY appid`, now)
	return err
}

func migratePoolFlagsRetire(ctx context.Context, conn *sql.Conn) error {
	if err := migrateMonitoringSources(ctx, conn, true); err != nil {
		return err
	}
	const accountFact = "(COALESCE(wishlisted,0)=1 OR COALESCE(owned,0)=1 OR COALESCE(manual,0)=1)"
	return migrationExec(ctx, conn,
		"UPDATE wishlist_items SET board_pool=0 WHERE COALESCE(board_pool,0)=1 AND "+accountFact,
		"UPDATE wishlist_items SET manual_pool=0 WHERE COALESCE(manual_pool,0)=1 AND "+accountFact,
		`DELETE FROM wishlist_items WHERE (COALESCE(board_pool,0)=1 OR COALESCE(manual_pool,0)=1)
			AND COALESCE(wishlisted,0)=0 AND COALESCE(owned,0)=0 AND COALESCE(manual,0)=0`,
		`UPDATE tracked_accounts SET item_count=(SELECT COUNT(*) FROM wishlist_items wi WHERE wi.steamid=tracked_accounts.steamid AND wi.active=1)`)
}

func migrateGamesNameFTS(ctx context.Context, conn *sql.Conn) error {
	return migrationExec(ctx, conn,
		"CREATE VIRTUAL TABLE IF NOT EXISTS games_name_fts USING fts5(appid UNINDEXED,name_bi)",
		`CREATE TRIGGER IF NOT EXISTS games_name_bi_ai AFTER INSERT ON games BEGIN
			INSERT INTO games_name_fts(appid,name_bi) VALUES(new.appid,name_bigram(new.name,new.name_en)); END`,
		`CREATE TRIGGER IF NOT EXISTS games_name_bi_au AFTER UPDATE OF name,name_en ON games BEGIN
			DELETE FROM games_name_fts WHERE appid=old.appid;
			INSERT INTO games_name_fts(appid,name_bi) VALUES(new.appid,name_bigram(new.name,new.name_en)); END`,
		"CREATE TRIGGER IF NOT EXISTS games_name_bi_ad AFTER DELETE ON games BEGIN DELETE FROM games_name_fts WHERE appid=old.appid; END",
		"DELETE FROM games_name_fts",
		"INSERT INTO games_name_fts(appid,name_bi) SELECT appid,name_bigram(name,name_en) FROM games")
}
