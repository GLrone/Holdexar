// bills 只读域对拍：Python 侧 bills_summary 输出与 Go ListImports 逐字段
// 哈希等价（真实账单为用户数据——只比哈希与计数，内容不落日志）。
package bills

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"

	_ "modernc.org/sqlite"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestListImportsShape 库态自检：金额口径（净额=支出-退款）、笔数口径、isoformat。
func TestListImportsShape(t *testing.T) {
	dir := t.TempDir()
	db, err := sql.Open("sqlite", filepath.Join(dir, "t.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	for _, ddl := range []string{
		`CREATE TABLE bill_imports (id INTEGER PRIMARY KEY, steam_id TEXT, nickname TEXT,
		 source_file TEXT, imported_at TIMESTAMP)`,
		`CREATE TABLE bill_game_txs (id INTEGER PRIMARY KEY, import_id INTEGER,
		 is_refund BOOLEAN, cny_fen INTEGER)`,
		`INSERT INTO bill_imports VALUES (5, '765', 'nA', 'a.json', '2026-10-01 10:00:00')`,
		`INSERT INTO bill_game_txs VALUES (1, 5, 0, 10000)`,
		`INSERT INTO bill_game_txs VALUES (2, 5, 0, 2500)`,
		`INSERT INTO bill_game_txs VALUES (3, 5, 1, 3000)`,
		`INSERT INTO bill_game_txs VALUES (4, 5, 0, NULL)`,
	} {
		if _, err := db.Exec(ddl); err != nil {
			t.Fatal(err)
		}
	}
	recs, err := ListImports(context.Background(), db)
	if err != nil {
		t.Fatal(err)
	}
	if len(recs) != 1 {
		t.Fatalf("行数 %d", len(recs))
	}
	r := recs[0]
	// spend=12500（两条非退款有价），refund=3000，net=9500，orders=3
	//（CASE..THEN id 对 NULL 行不计数？cny_fen IS NULL 的行 is_refund=0 → orders 含它），
	// fxMissing=1
	if r.GameSpendFen != 12500 || r.GameRefundFen != 3000 || r.GameNetFen != 9500 {
		t.Fatalf("金额口径: %+v", r)
	}
	if r.Orders != 3 || r.FxMissing != 1 {
		t.Fatalf("笔数口径: orders=%d fxMissing=%d", r.Orders, r.FxMissing)
	}
	if r.ImportedAt.String != "2026-10-01T10:00:00" {
		t.Fatalf("isoformat: %q", r.ImportedAt.String)
	}
}

// g6Data 隔离副本数据文件（G6 副本目录，绝对路径不受测试工作目录影响）。
func g6Data(name string) string {
	return filepath.Join("E:/Holdexar-g6/python-test-data", name)
}

// TestPyParityHash 与 Python harness 输出哈希对拍（副本缺席自动跳过）。
func TestPyParityHash(t *testing.T) {
	db, err := sql.Open("sqlite", g6Data("holdexar.db"))
	if err != nil {
		t.Skip("副本缺席: " + err.Error())
	}
	defer db.Close()
	recs, err := ListImports(context.Background(), db)
	if err != nil {
		t.Fatal(err)
	}
	rows := make([]map[string]any, 0, len(recs))
	for _, r := range recs {
		rows = append(rows, map[string]any{
			"id": r.ID, "steamId": nullStr(r.SteamID), "nickname": nullStr(r.Nickname),
			"sourceFile": nullStr(r.SourceFile), "importedAt": nullStr(r.ImportedAt),
			"gameNetFen": r.GameNetFen, "gameSpendFen": r.GameSpendFen,
			"gameRefundFen": r.GameRefundFen, "orders": r.Orders, "fxMissing": r.FxMissing,
		})
	}
	b, _ := json.Marshal(rows)
	sum := sha256.Sum256(b)
	goHash := hex.EncodeToString(sum[:])

	pyHashB, err := os.ReadFile(g6Data("py-bills-hash.txt"))
	if err != nil {
		t.Skip("py 侧哈希未生成（harness 未跑）")
	}
	pyHash := strings.TrimSpace(string(pyHashB))
	if goHash != pyHash {
		t.Fatalf("对拍哈希不一致: go=%s py=%s", goHash[:16], pyHash[:16])
	}
}

func nullStr(v sql.NullString) any {
	if !v.Valid {
		return nil
	}
	return v.String
}
