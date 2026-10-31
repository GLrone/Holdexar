package store

import (
	"context"
	"testing"
)

// Open 后逐连接核对生产同款 PRAGMA 参数。
func TestOpenPragmas(t *testing.T) {
	db, _ := openTestDB(t)
	checks := []struct {
		pragma string
		want   any
	}{
		{"journal_mode", "wal"},
		{"synchronous", int64(1)}, // NORMAL
		{"journal_size_limit", int64(67108864)},
		{"foreign_keys", int64(1)},
		{"cache_size", int64(-262144)},
		{"mmap_size", int64(0)},
		{"temp_store", int64(2)}, // MEMORY
		{"busy_timeout", int64(60000)},
	}
	for _, c := range checks {
		var got any
		if err := db.QueryRow("PRAGMA " + c.pragma).Scan(&got); err != nil {
			t.Fatalf("PRAGMA %s 读取失败: %v", c.pragma, err)
		}
		if got != c.want {
			t.Fatalf("PRAGMA %s = %v，期望 %v", c.pragma, got, c.want)
		}
	}
	// PRAGMA 参数随 DSN 应用到每个新连接
	for i := 0; i < 3; i++ {
		conn, err := db.Conn(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		var jm string
		if err := conn.QueryRowContext(context.Background(), "PRAGMA journal_mode").Scan(&jm); err != nil {
			t.Fatal(err)
		}
		if jm != "wal" {
			t.Fatalf("第 %d 个连接 journal_mode = %q，期望 wal", i+1, jm)
		}
		var bt int64
		if err := conn.QueryRowContext(context.Background(), "PRAGMA busy_timeout").Scan(&bt); err != nil {
			t.Fatal(err)
		}
		if bt != 60000 {
			t.Fatalf("第 %d 个连接 busy_timeout = %d，期望 60000", i+1, bt)
		}
		conn.Close()
	}
}

// name_bigram UDF 输出与手算值断言。
func TestNameBigramUDF(t *testing.T) {
	db, _ := openTestDB(t)
	cases := []struct {
		name   string
		nameEn string
		want   string
	}{
		{"只狼：影逝二度", "Sekiro", "只狼 狼： ：影 影逝 逝二 二度 Sekiro"},
		{"黑神话悟空", "Black Myth", "黑神 神话 话悟 悟空 Black Myth"},
		{"只狼 只狼", "", "只狼"}, // 去重保序
		{"狼", "", ""},       // 单字 CJK 产出为空
		{"Elden Ring 法环", "", "Elden Ring 法环"},
		{"", "", ""},
	}
	for _, c := range cases {
		var got string
		if err := db.QueryRow("SELECT name_bigram(?, ?)", c.name, c.nameEn).Scan(&got); err != nil {
			t.Fatalf("name_bigram(%q, %q) 执行失败: %v", c.name, c.nameEn, err)
		}
		if got != c.want {
			t.Fatalf("name_bigram(%q, %q) = %q，期望 %q", c.name, c.nameEn, got, c.want)
		}
	}
	nullCases := []struct {
		sql  string
		want string
	}{
		{"SELECT name_bigram(NULL, 'X')", "X"},
		{"SELECT name_bigram('A', NULL)", "A"},
		{"SELECT name_bigram(NULL, NULL)", ""},
	}
	for _, c := range nullCases {
		var got string
		if err := db.QueryRow(c.sql).Scan(&got); err != nil {
			t.Fatalf("%s 执行失败: %v", c.sql, err)
		}
		if got != c.want {
			t.Fatalf("%s = %q，期望 %q", c.sql, got, c.want)
		}
	}
}
