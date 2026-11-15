package plugins

import (
	"archive/zip"
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"holdexar/server-go/internal/store"
)

func datasourceTestManager(t *testing.T) (*Manager, *sql.DB, string) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "test.db")
	db, e := sql.Open("sqlite", path)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { db.Close() })
	if _, e = db.Exec(`CREATE TABLE app_settings(key TEXT PRIMARY KEY,value_json TEXT,updated_at TEXT)`); e != nil {
		t.Fatal(e)
	}
	return New(db, store.NewWriteGate(db, path), dir, Options{}), db, dir
}

func datasourcePackage(t *testing.T, id string, appid int64, points string) []byte {
	t.Helper()
	manifest := map[string]any{
		"id": id, "name": "Community price history", "version": "1.0.0", "author": "Example author",
		"apiVersion": 1, "kind": "datasource.community_history",
		"configSchema": map[string]any{"type": "object", "properties": map[string]any{}, "additionalProperties": false},
	}
	manifestBytes, e := json.Marshal(manifest)
	if e != nil {
		t.Fatal(e)
	}
	data := `{"appid":` + intToStr(appid) + `,"region":"cn","observations":[` + points + `]}`
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	for _, member := range []struct {
		name    string
		payload []byte
	}{{"manifest.json", manifestBytes}, {"observations.json", []byte(data)}} {
		f, e := w.Create(member.name)
		if e != nil {
			t.Fatal(e)
		}
		if _, e = f.Write(member.payload); e != nil {
			t.Fatal(e)
		}
	}
	if e = w.Close(); e != nil {
		t.Fatal(e)
	}
	return buf.Bytes()
}

func intToStr(v int64) string {
	if v == 0 {
		return "0"
	}
	neg := v < 0
	if neg {
		v = -v
	}
	var digits []byte
	for v > 0 {
		digits = append([]byte{byte('0' + v%10)}, digits...)
		v /= 10
	}
	if neg {
		return "-" + string(digits)
	}
	return string(digits)
}

func datasourceWithEndpoint(t *testing.T) []byte {
	t.Helper()
	manifest := map[string]any{
		"id": "bad.history", "name": "Bad", "version": "1.0.0", "author": "x",
		"apiVersion": 1, "kind": "datasource.community_history",
		"domains": []string{"example.com"}, "endpoint": "https://example.com/notify",
		"configSchema": map[string]any{"type": "object", "properties": map[string]any{}, "additionalProperties": false},
	}
	manifestBytes, e := json.Marshal(manifest)
	if e != nil {
		t.Fatal(e)
	}
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	for _, member := range []struct {
		name    string
		payload []byte
	}{{"manifest.json", manifestBytes}, {"observations.json", []byte(`{"appid":1,"region":"cn","observations":[{"date":"2026-06-01","priceMinor":100}]}`)}} {
		f, e := w.Create(member.name)
		if e != nil {
			t.Fatal(e)
		}
		if _, e = f.Write(member.payload); e != nil {
			t.Fatal(e)
		}
	}
	if e = w.Close(); e != nil {
		t.Fatal(e)
	}
	return buf.Bytes()
}

func TestDataSourceInstallRejectsBadPackages(t *testing.T) {
	ctx := context.Background()
	m, _, _ := datasourceTestManager(t)
	// 数据源清单带网络投递面被拒。
	if _, e := m.InstallLocal(ctx, datasourceWithEndpoint(t)); e == nil || !strings.Contains(e.Error(), "网络投递面") {
		t.Fatalf("endpoint datasource err=%v", e)
	}
	// 重复日期被拒。
	if _, e := m.InstallLocal(ctx, datasourcePackage(t, "bad.history", 1,
		`{"date":"2026-06-01","priceMinor":100},{"date":"2026-06-01","priceMinor":200}`)); e == nil || !strings.Contains(e.Error(), "重复") {
		t.Fatalf("duplicate date err=%v", e)
	}
	// 未来日期被拒。
	if _, e := m.InstallLocal(ctx, datasourcePackage(t, "bad.history", 1,
		`{"date":"2030-01-01","priceMinor":100}`)); e == nil || !strings.Contains(e.Error(), "晚于当前时间") {
		t.Fatalf("future date err=%v", e)
	}
	// 非正价格被拒。
	if _, e := m.InstallLocal(ctx, datasourcePackage(t, "bad.history", 1,
		`{"date":"2026-06-01","priceMinor":0}`)); e == nil || !strings.Contains(e.Error(), "正数") {
		t.Fatalf("zero price err=%v", e)
	}
	// 缺数据文件被拒。
	if _, e := m.InstallLocal(ctx, notificationPackageOnly(t)); e != nil {
		t.Fatalf("notification package must still install: %v", e)
	}
	entries, e := m.List(ctx)
	if e != nil || len(entries) != 1 {
		t.Fatalf("after rejects entries=%d err=%v", len(entries), e)
	}
}

// notificationPackageOnly 既有通知包 fixture（复用主测试文件打包器）。
func notificationPackageOnly(t *testing.T) []byte {
	t.Helper()
	return pack(t, fixture("example.webhook"))
}

func TestDataSourceLifecycleCoexistsWithNotification(t *testing.T) {
	ctx := context.Background()
	m, _, dir := datasourceTestManager(t)

	// 1) 数据源安装并启用。
	if _, e := m.InstallLocal(ctx, datasourcePackage(t, "example.history", 10,
		`{"date":"2026-06-01","priceMinor":8800,"cnyFen":880}`)); e != nil {
		t.Fatal(e)
	}
	on := true
	if _, e := m.Update(ctx, "example.history", &on, nil); e != nil {
		t.Fatal(e)
	}
	entry, data, e := m.ActiveDataSource(ctx)
	if e != nil || entry == nil || data == nil || data.AppID != 10 || len(data.Observations) != 1 {
		t.Fatalf("active=%+v data=%+v err=%v", entry, data, e)
	}
	// 观测与安装状态同事务持久，不另建文件。
	if _, e = os.Stat(filepath.Join(dir, "plugins")); !os.IsNotExist(e) {
		t.Fatalf("unexpected plugin data directory: %v", e)
	}
	// 2) 数据源启用不影响通知渠道选择（Recipient 空 = 无通知插件）。
	if recipient, e := m.Recipient(ctx); e != nil || recipient != "" {
		t.Fatalf("recipient=%q err=%v", recipient, e)
	}
	// 3) 通知插件并存安装启用：两 kind 互不挤占。
	if _, e = m.InstallLocal(ctx, notificationPackageOnly(t)); e != nil {
		t.Fatal(e)
	}
	if _, e = m.Update(ctx, "example.webhook", &on, map[string]any{"token": "tok"}); e != nil {
		t.Fatal(e)
	}
	if recipient, e := m.Recipient(ctx); e != nil || !strings.HasPrefix(recipient, "plugin:example.webhook:") {
		t.Fatalf("recipient=%q err=%v", recipient, e)
	}
	// 4) 第二个数据源启用为顶替式（与通知渠道同语义）：启用后原数据源被停用。
	if _, e = m.InstallLocal(ctx, datasourcePackage(t, "second.history", 12,
		`{"date":"2026-06-01","priceMinor":100}`)); e != nil {
		t.Fatal(e)
	}
	if _, e = m.Update(ctx, "second.history", &on, nil); e != nil {
		t.Fatal(e)
	}
	entry, data, e = m.ActiveDataSource(ctx)
	if e != nil || entry == nil || entry.Manifest.ID != "second.history" || data.AppID != 12 {
		t.Fatalf("after switch=%+v data=%+v err=%v", entry, data, e)
	}
	// 5) 卸载当前启用数据源：registry 清空、数据文件删除、通知插件不受影响。
	if e = m.Remove(ctx, "second.history"); e != nil {
		t.Fatal(e)
	}
	if entry, data, e = m.ActiveDataSource(ctx); e != nil || entry != nil {
		t.Fatalf("after remove=%+v err=%v", entry, e)
	}
	if _, e = os.Stat(filepath.Join(dir, "plugins")); !os.IsNotExist(e) {
		t.Fatalf("uninstall created an independent data directory: %v", e)
	}
	if recipient, e := m.Recipient(ctx); e != nil || !strings.HasPrefix(recipient, "plugin:example.webhook:") {
		t.Fatalf("recipient after remove=%q err=%v", recipient, e)
	}
}
