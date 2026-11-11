package store

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func TestBackupMaintenanceKeepsLiveAndRecoveryMarkers(t *testing.T) {
	path := filepath.Join(t.TempDir(), "holdexar.db")
	end, err := BeginBackupMaintenance(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := BeginBackupMaintenance(path); !errors.Is(err, ErrMaintenanceProtected) {
		t.Fatalf("concurrent maintenance error=%v", err)
	}
	if err := end(); err != nil {
		t.Fatal(err)
	}
	child := exec.Command(os.Args[0], "-test.run=^$")
	if err := child.Run(); err != nil {
		t.Fatal(err)
	}
	marker := maintenancePath(path)
	for _, purpose := range []string{`"restore"`, `"update"`, `null`, `"maintenance"`} {
		body := fmt.Sprintf(`{"pid":%d,"purpose":%s}`, child.Process.Pid, purpose)
		if err := os.WriteFile(marker, []byte(body), 0600); err != nil {
			t.Fatal(err)
		}
		if purpose != `"maintenance"` {
			if err := CheckMaintenance(path); !errors.Is(err, ErrMaintenanceProtected) {
				t.Fatalf("dangerous marker %s was not protected: %v", body, err)
			}
			continue
		}
		if err := CheckMaintenance(path); err != nil {
			t.Fatalf("dead ordinary marker not released: %v", err)
		}
	}
	if _, err := os.Stat(marker); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("dead ordinary marker remains: %v", err)
	}
}
