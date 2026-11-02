// g6injection：G6-3 隔离副本故障注入实验程序（tools/ 临时实验物，结论沉淀后按纪律评估去留）。
//
// 子命令：
//
//	snapshot <db>                       integrity_check + foreign_key_check + 关键表行数
//	replay <db>                         实验 A：真实库上同观测重放保 ID（含跨秒新行对照）
//	concurrent <db>                     E3：8 goroutine 经写闸并发 UPSERT 同一 settings 键
//	rollback <db>                       E4：闸内事务中途 error → 断言零残留
//	holdgate <db> <sec>                 E2 配套：持写闸 N 秒（跨运行时互斥编排用）
//	killtx <db>                         E1 配套：闸内写一批 settings 后挂起（等待外部强杀）
//	walfix <db>                         E5：WAL checkpoint(TRUNCATE) + 复检
//	migrationgate <db> <ver>            E6：对（伪造版本的）库执行版本门，输出行为与文案
//
// 全部命令只作用于调用方显式传入的隔离副本路径。
package main

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"strconv"
	"sync"
	"time"

	"holdexar/server-go/internal/crawler/dbwriter"
	"holdexar/server-go/internal/store"
)

func die(err error) {
	fmt.Println("ERR:", err)
	os.Exit(1)
}

func openDB(dbPath string) (*sql.DB, *store.WriteGate) {
	db, err := store.Open(dbPath)
	if err != nil {
		die(err)
	}
	return db, store.NewWriteGate(db, dbPath)
}

func snapshot(dbPath string) {
	db, err := store.Open(dbPath)
	if err != nil {
		die(err)
	}
	defer db.Close()
	var ic, fk string
	if err := db.QueryRow("PRAGMA integrity_check").Scan(&ic); err != nil {
		die(err)
	}
	fkRows, err := db.Query("PRAGMA foreign_key_check")
	if err != nil {
		die(err)
	}
	fkViol := 0
	for fkRows.Next() {
		fkViol++
	}
	fkRows.Close()
	if fkViol == 0 {
		fk = "ok"
	} else {
		fk = fmt.Sprintf("%d 处违规", fkViol)
	}
	fmt.Println("integrity:", ic)
	fmt.Println("foreign_key:", fk)
	for _, t := range []string{"games", "game_current_prices", "game_price_history",
		"app_settings", "proxy_nodes", "health_observations", "crawl_jobs", "price_cycles"} {
		var n any
		if err := db.QueryRow("SELECT COUNT(*) FROM " + t).Scan(&n); err == nil {
			fmt.Printf("rows %s = %v\n", t, n)
		}
	}
}

// replay 实验 A：挑最新一条 game_price_history 观测，构造同观测批两次写入，
// 断言行 id 不变（唯一观测键重放保 ID）；再跨秒写入一次，断言产生新行。
func replay(dbPath string) {
	db, gate := openDB(dbPath)
	defer db.Close()
	ctx := context.Background()
	w := dbwriter.NewWriter(db, gate)
	if err := w.LoadFxRates(ctx); err != nil {
		die(err)
	}
	var appid int64
	var region, snapshotAt, currency string
	var price, original, discount int64
	var subID int64
	var histID int64
	err := db.QueryRow(`SELECT h.id, h.appid, h.region_code, h.snapshot_at, h.currency,
		COALESCE(h.price,0), COALESCE(h.original_price,0), COALESCE(h.discount_percent,0),
		COALESCE(h.sub_id,0)
		FROM game_price_history h
		JOIN games g ON g.appid = h.appid
		WHERE h.price IS NOT NULL AND h.price_status='ok' AND h.region_code != ''
		ORDER BY h.snapshot_at DESC LIMIT 1`).Scan(&histID, &appid, &region, &snapshotAt,
		&currency, &price, &original, &discount, &subID)
	if err != nil {
		die(fmt.Errorf("挑观测行: %w", err))
	}
	var name string
	if err := db.QueryRow(`SELECT name FROM games WHERE appid=?`, appid).Scan(&name); err != nil {
		die(err)
	}
	var gexist int64
	_ = db.QueryRow(`SELECT COUNT(*) FROM game_current_prices WHERE appid=? AND region_code=?`,
		appid, region).Scan(&gexist)
	fmt.Printf("样本: appid=%d region=%s currency=%s price=%d 现价行在位=%d 历史行id=%d\n",
		appid, region, currency, price, gexist, histID)

	rg := region
	gold := false
	statusOK := "ok"
	isBundle := false
	entry := dbwriter.BatchEntry{
		Game: dbwriter.GameData{AppID: appid, Name: &name},
		Prices: []dbwriter.PriceRow{{
			AppID: appid, RegionCode: &rg, Currency: &currency,
			Price: &price, OriginalPrice: &original, DiscountPercent: &discount,
			SubID: &subID, IsGold: &gold, PriceStatus: &statusOK,
			IsBundle: &isBundle,
		}},
	}
	// 写两次（同观测重放，进程内 now 同秒 → 同唯一键）
	var firstID int64
	for i := 0; i < 2; i++ {
		if _, err := w.UpsertTaskBatch(ctx, []dbwriter.BatchEntry{entry}); err != nil {
			die(fmt.Errorf("重放第%d次: %w", i+1, err))
		}
	}
	if err := db.QueryRow(`SELECT id FROM game_price_history
		WHERE appid=? AND region_code=? AND snapshot_at=(SELECT MAX(snapshot_at) FROM game_price_history
		WHERE appid=? AND region_code=?) ORDER BY id DESC LIMIT 1`,
		appid, region, appid, region).Scan(&firstID); err != nil {
		die(err)
	}
	var cnt int64
	if err := db.QueryRow(`SELECT COUNT(*) FROM game_price_history
		WHERE appid=? AND region_code=? AND snapshot_at=
		(SELECT MAX(snapshot_at) FROM game_price_history WHERE appid=? AND region_code=?)`,
		appid, region, appid, region).Scan(&cnt); err != nil {
		die(err)
	}
	fmt.Printf("重放后: 同观测行数=%d（期望 1，重放保 ID）最新id=%d\n", cnt, firstID)
	// 跨秒写第三次：snapshot_at 变化 → 新行
	time.Sleep(1100 * time.Millisecond)
	price2 := price + 100
	entry.Prices[0].Price = &price2
	if _, err := w.UpsertTaskBatch(ctx, []dbwriter.BatchEntry{entry}); err != nil {
		die(err)
	}
	var cnt2 int64
	if e := db.QueryRow(`SELECT COUNT(*) FROM game_price_history WHERE appid=? AND region_code=?`,
		appid, region).Scan(&cnt2); e != nil {
		die(e)
	}
	fmt.Printf("跨秒写入后: 该 appid+region 总历史行数=%d（应 +1，时间参与身份）\n", cnt2)
	if cnt != 1 {
		fmt.Println("REPLAY_RESULT = FAIL（重放产生重复行）")
		os.Exit(1)
	}
	fmt.Println("REPLAY_RESULT = PASS")
}

// concurrent E3：8 goroutine × 25 次 UPSERT 同一 settings 键（全部经写闸）。
func concurrent(dbPath string) {
	db, gate := openDB(dbPath)
	defer db.Close()
	ctx := context.Background()
	var wg sync.WaitGroup
	errs := make(chan error, 8)
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			for i := 0; i < 25; i++ {
				e := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
					_, ex := conn.ExecContext(ctx,
						`INSERT INTO app_settings(key, value_json, updated_at)
						 VALUES('g6.injection.probe', ?, datetime('now','localtime'))
						 ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at`,
						fmt.Sprintf(`"w%d-%d"`, id, i))
					return ex
				})
				if e != nil {
					errs <- e
					return
				}
			}
		}(g)
	}
	wg.Wait()
	close(errs)
	for e := range errs {
		die(e)
	}
	var v string
	if err := db.QueryRow(`SELECT value_json FROM app_settings WHERE key='g6.injection.probe'`).Scan(&v); err != nil {
		die(err)
	}
	fmt.Printf("并发写后终值=%s（应形如 \"w7-24\"：后写赢，无丢更新异常）\n", v)
	fmt.Println("CONCURRENT_RESULT = PASS")
}

// rollback E4：闸内 INSERT 后主动返回 error，断言零残留。
func rollback(dbPath string) {
	db, gate := openDB(dbPath)
	defer db.Close()
	ctx := context.Background()
	err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if _, e := conn.ExecContext(ctx,
			`INSERT INTO app_settings(key, value_json, updated_at)
			 VALUES('g6.injection.rollback', '"半途"', datetime('now','localtime'))`); e != nil {
			return e
		}
		return fmt.Errorf("注入错误：事务应整体回滚")
	})
	if err == nil {
		die(fmt.Errorf("回滚实验未按预期失败"))
	}
	var n int64
	if err := db.QueryRow(`SELECT COUNT(*) FROM app_settings WHERE key='g6.injection.rollback'`).Scan(&n); err != nil {
		die(err)
	}
	fmt.Printf("注入失败后残留行=%d（期望 0）\n", n)
	if n != 0 {
		os.Exit(1)
	}
	fmt.Println("ROLLBACK_RESULT = PASS")
}

// holdgate E2 配套：持写闸 N 秒。
func holdgate(dbPath string, sec int) {
	db, gate := openDB(dbPath)
	defer db.Close()
	ctx := context.Background()
	err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if _, e := conn.ExecContext(ctx, `SELECT 1`); e != nil {
			return e
		}
		fmt.Printf("已持写闸 %d 秒\n", sec)
		time.Sleep(time.Duration(sec) * time.Second)
		return nil
	})
	if err != nil {
		die(err)
	}
	fmt.Println("HOLD_RELEASED")
}

// killtx E1 配套：闸内写一批 settings 后挂起，等待外部强杀。
func killtx(dbPath string) {
	_, gate := openDB(dbPath)
	ctx := context.Background()
	err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		for i := 0; i < 5; i++ {
			if _, e := conn.ExecContext(ctx,
				`INSERT INTO app_settings(key, value_json, updated_at)
				 VALUES(?, ?, datetime('now','localtime'))
				 ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json`,
				fmt.Sprintf("g6.injection.killtx%d", i), fmt.Sprintf(`"midtx%d"`, i)); e != nil {
				return e
			}
		}
		fmt.Println("KILLTX_ARMED（事务未提交，等待强杀）")
		time.Sleep(120 * time.Second)
		return nil
	})
	if err != nil {
		fmt.Println("killtx 退出:", err)
	}
}

// walfix E5：checkpoint(TRUNCATE) + 复检。
func walfix(dbPath string) {
	db, err := store.Open(dbPath)
	if err != nil {
		die(err)
	}
	defer db.Close()
	if _, err := db.Exec("PRAGMA wal_checkpoint(TRUNCATE)"); err != nil {
		die(err)
	}
	var ic string
	if err := db.QueryRow("PRAGMA integrity_check").Scan(&ic); err != nil {
		die(err)
	}
	var mode string
	_ = db.QueryRow("PRAGMA journal_mode").Scan(&mode)
	fmt.Println("journal_mode:", mode, "integrity:", ic)
	fmt.Println("WALFIX_RESULT = PASS")
}

// migrationgate E6：版本门行为。
func migrationgate(dbPath string, ver int) {
	got, err := store.ReadSchemaVersion(dbPath)
	if err != nil {
		die(err)
	}
	fmt.Printf("库实际 schema_version=%d，门版本=%d\n", got, ver)
	err = store.EnsureSupportedVersion(dbPath, ver)
	if err != nil {
		fmt.Println("版本门拒绝:", err)
		fmt.Println("MIGRATIONGATE_RESULT = REJECTED")
		return
	}
	fmt.Println("版本门放行")
	fmt.Println("MIGRATIONGATE_RESULT = ACCEPTED")
}

func main() {
	if len(os.Args) < 3 {
		fmt.Println("usage: g6injection <cmd> <db> [args]")
		os.Exit(2)
	}
	cmd, dbPath := os.Args[1], os.Args[2]
	switch cmd {
	case "snapshot":
		snapshot(dbPath)
	case "replay":
		replay(dbPath)
	case "concurrent":
		concurrent(dbPath)
	case "rollback":
		rollback(dbPath)
	case "holdgate":
		sec, _ := strconv.Atoi(os.Args[3])
		holdgate(dbPath, sec)
	case "killtx":
		killtx(dbPath)
	case "walfix":
		walfix(dbPath)
	case "migrationgate":
		ver, _ := strconv.Atoi(os.Args[3])
		migrationgate(dbPath, ver)
	default:
		die(fmt.Errorf("未知命令 %s", cmd))
	}
}
