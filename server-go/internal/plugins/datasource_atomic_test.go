package plugins

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func registryRaw(t *testing.T, m *Manager) string {
	t.Helper()
	var raw string
	if err := m.db.QueryRow(`SELECT value_json FROM app_settings WHERE key=?`, registryKey).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestDataSourceSharesRegistryTransactionAndSurvivesRestart(t *testing.T) {
	ctx := context.Background()
	m, db, dir := datasourceTestManager(t)
	if err := os.WriteFile(filepath.Join(dir, "plugins"), []byte("directory unavailable"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := m.InstallLocal(ctx, datasourcePackage(t, "example.history", 10, `{"date":"2026-06-01","priceMinor":100}`)); err != nil {
		t.Fatalf("installation depends on a second data store: %v", err)
	}
	on := true
	if _, err := m.Update(ctx, "example.history", &on, nil); err != nil {
		t.Fatal(err)
	}
	restarted := New(db, m.gate, dir, Options{})
	entry, data, err := restarted.ActiveDataSource(ctx)
	if err != nil || entry == nil || data == nil || data.AppID != 10 {
		t.Fatalf("data did not survive registry reload: %v %v %v", entry, data, err)
	}
	var registered []map[string]json.RawMessage
	if err := json.Unmarshal([]byte(registryRaw(t, m)), &registered); err != nil || len(registered[0]["data"]) == 0 {
		t.Fatalf("registry does not own its data: %s %v", registryRaw(t, m), err)
	}
	list, err := m.List(ctx)
	if err != nil {
		t.Fatal(err)
	}
	public, _ := json.Marshal(list)
	var publicEntries []map[string]json.RawMessage
	if err := json.Unmarshal(public, &publicEntries); err != nil {
		t.Fatal(err)
	}
	if len(publicEntries[0]["data"]) != 0 {
		t.Fatal("listing includes the full data payload")
	}
}

func TestDataSourceFailedMutationRetainsActiveData(t *testing.T) {
	for _, action := range []string{"update", "remove"} {
		t.Run(action, func(t *testing.T) {
			ctx := context.Background()
			m, db, _ := datasourceTestManager(t)
			if _, err := m.InstallLocal(ctx, datasourcePackage(t, "example.history", 10, `{"date":"2026-06-01","priceMinor":100}`)); err != nil {
				t.Fatal(err)
			}
			on := true
			if _, err := m.Update(ctx, "example.history", &on, nil); err != nil {
				t.Fatal(err)
			}
			before := registryRaw(t, m)
			if _, err := db.Exec(`CREATE TRIGGER reject_registry BEFORE UPDATE ON app_settings WHEN NEW.key='plugins.registry' BEGIN SELECT RAISE(ABORT,'test persistence failure'); END`); err != nil {
				t.Fatal(err)
			}
			var err error
			if action == "remove" {
				err = m.Remove(ctx, "example.history")
			} else {
				_, err = m.InstallLocal(ctx, datasourcePackage(t, "example.history", 10, `{"date":"2026-06-01","priceMinor":200}`))
			}
			if err == nil {
				t.Fatal("mutation unexpectedly succeeded")
			}
			if after := registryRaw(t, m); after != before {
				t.Fatal("failed mutation changed the registry")
			}
			entry, data, err := m.ActiveDataSource(ctx)
			if err != nil || entry == nil || data == nil || *data.Observations[0].PriceMinor != 100 {
				t.Fatalf("failed mutation destroyed the active data: %+v %+v %v", entry, data, err)
			}
		})
	}
}

func TestDataSourceRejectsDamagedRegistryPayload(t *testing.T) {
	ctx := context.Background()
	m, db, _ := datasourceTestManager(t)
	if _, err := m.InstallLocal(ctx, datasourcePackage(t, "example.history", 10, `{"date":"2026-06-01","priceMinor":100}`)); err != nil {
		t.Fatal(err)
	}
	on := true
	if _, err := m.Update(ctx, "example.history", &on, nil); err != nil {
		t.Fatal(err)
	}
	var entries []map[string]any
	if err := json.Unmarshal([]byte(registryRaw(t, m)), &entries); err != nil {
		t.Fatal(err)
	}
	entries[0]["data"] = map[string]any{"appid": 10, "region": "cn", "observations": []any{}}
	raw, _ := json.Marshal(entries)
	if _, err := db.Exec(`UPDATE app_settings SET value_json=? WHERE key=?`, string(raw), registryKey); err != nil {
		t.Fatal(err)
	}
	if entry, data, err := m.ActiveDataSource(ctx); err == nil || entry != nil || data != nil {
		t.Fatal("damaged registry data was ignored")
	}
	list, err := m.List(ctx)
	if err != nil || len(list) != 1 || list[0].Configured || list[0].Error == "" {
		t.Fatalf("damaged data remained usable in list: %+v %v", list, err)
	}
}

func TestDataSourceRejectsTomorrowAndConvertedPriceOnly(t *testing.T) {
	tomorrow := time.Now().In(time.FixedZone("UTC+8", 8*3600)).AddDate(0, 0, 1).Format("2006-01-02")
	for _, point := range []string{
		`{"date":"` + tomorrow + `","priceMinor":100}`,
		`{"date":"2026-06-01","cnyFen":100}`,
	} {
		raw := []byte(`{"appid":10,"region":"cn","observations":[` + point + `]}`)
		if _, err := ValidateDataSourceData(raw); err == nil {
			t.Errorf("invalid observation accepted: %s", point)
		}
	}
}
