package hostkit

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func writeTestFile(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestPathsJunctionAndTrustedPath(t *testing.T) {
	base := t.TempDir()
	realDir := filepath.Join(base, "real")
	outsideDir := t.TempDir()
	writeTestFile(t, filepath.Join(realDir, "item.txt"), []byte("ok"))
	writeTestFile(t, filepath.Join(outsideDir, "secret.txt"), []byte("secret"))

	if got, err := TrustedPath(filepath.Join(realDir, "item.txt")); err != nil || !SamePath(got, filepath.Join(realDir, "item.txt")) {
		t.Fatalf("TrustedPath on normal file failed: (%q, %v)", got, err)
	}
	if got, err := TrustedPath(filepath.Join(realDir, "not-yet-created.json")); err != nil || got == "" {
		t.Fatalf("TrustedPath on missing child in clean parent failed: (%q, %v)", got, err)
	}

	linkPath := filepath.Join(base, "linked")
	makeDirLink(t, linkPath, outsideDir)

	if _, err := TrustedPath(linkPath); err == nil {
		t.Fatal("TrustedPath accepted junction/symlink directory")
	}
	if _, err := TrustedPath(filepath.Join(linkPath, "secret.txt")); err == nil {
		t.Fatal("TrustedPath accepted file through junction/symlink")
	}
	if _, err := TrustedPath(filepath.Join(linkPath, "missing.txt")); err == nil {
		t.Fatal("TrustedPath accepted missing child through junction/symlink")
	}

	if !SamePath(linkPath, outsideDir) {
		t.Fatalf("SamePath did not resolve link %q to %q", linkPath, outsideDir)
	}
	if IsUnderPath(filepath.Join(linkPath, "secret.txt"), base) {
		t.Fatal("IsUnderPath failed to reject junction/symlink escape out of base")
	}
	if !IsUnderPath(filepath.Join(realDir, "item.txt"), base) {
		t.Fatal("IsUnderPath rejected legitimate child")
	}
}

func TestProcessWriteLockPathExactParityAndJunction(t *testing.T) {
	base := t.TempDir()
	realDir := filepath.Join(base, "real")
	if err := os.MkdirAll(realDir, 0o755); err != nil {
		t.Fatal(err)
	}
	linkDir := filepath.Join(base, "junc")
	makeDirLink(t, linkDir, realDir)

	realDB := filepath.Join(realDir, "holdexar.db")
	linkDB := filepath.Join(linkDir, "holdexar.db")

	lockReal := ProcessWriteLockPath(realDB)
	lockLink := ProcessWriteLockPath(linkDB)
	if lockReal != lockLink {
		t.Fatalf("lock path mismatch before DB exists:\n  real=%s\n  link=%s", lockReal, lockLink)
	}

	canonicalReal := CanonicalPath(realDB)
	identity := canonicalReal
	if runtime.GOOS == "windows" {
		identity = strings.ToLower(filepath.FromSlash(filepath.ToSlash(identity)))
	}
	sum := sha256.Sum256([]byte(identity))
	want := filepath.Join(os.TempDir(), fmt.Sprintf("holdexar-write-%x.lock", sum[:12]))
	if lockReal != want {
		t.Fatalf("ProcessWriteLockPath = %q, want exact store/writegate match %q", lockReal, want)
	}
	baseName := filepath.Base(lockReal)
	hexPart := strings.TrimSuffix(strings.TrimPrefix(baseName, "holdexar-write-"), ".lock")
	if len(hexPart) != 24 {
		t.Fatalf("expected 24 hex chars (12 bytes), got %d in %q", len(hexPart), baseName)
	}
}

func TestInstallRootFromExeAllLayouts(t *testing.T) {
	t.Run("root launcher with current.json", func(t *testing.T) {
		root := CanonicalPath(t.TempDir())
		exe := filepath.Join(root, "Holdexar.exe")
		writeTestFile(t, exe, []byte("MZ"))
		writeTestFile(t, filepath.Join(root, "state", "current.json"), []byte(`{"version":"0.4.0"}`))
		gotRoot, gotVerDir, managed := InstallRootFromExe(exe)
		if !SamePath(gotRoot, root) || gotVerDir != "" || !managed {
			t.Fatalf("got (%q, %q, %v), want (%q, \"\", true)", gotRoot, gotVerDir, managed, root)
		}
	})

	t.Run("root launcher with updater/UpdateHost.exe", func(t *testing.T) {
		root := CanonicalPath(t.TempDir())
		exe := filepath.Join(root, "Holdexar.exe")
		writeTestFile(t, exe, []byte("MZ"))
		writeTestFile(t, filepath.Join(root, "updater", "UpdateHost.exe"), []byte("MZ"))
		gotRoot, gotVerDir, managed := InstallRootFromExe(exe)
		if !SamePath(gotRoot, root) || gotVerDir != "" || !managed {
			t.Fatalf("got (%q, %q, %v), want (%q, \"\", true)", gotRoot, gotVerDir, managed, root)
		}
	})

	t.Run("unmanaged dev binary", func(t *testing.T) {
		root := CanonicalPath(t.TempDir())
		exe := filepath.Join(root, "Holdexar.exe")
		writeTestFile(t, exe, []byte("MZ"))
		gotRoot, gotVerDir, managed := InstallRootFromExe(exe)
		if !SamePath(gotRoot, root) || gotVerDir != "" || managed {
			t.Fatalf("got (%q, %q, %v), want (%q, \"\", false)", gotRoot, gotVerDir, managed, root)
		}
	})

	t.Run("versions/<v>/Holdexar.exe and serverd.exe and _internal/serverd.exe and updater/UpdateHost.exe", func(t *testing.T) {
		root := CanonicalPath(t.TempDir())
		vDir := filepath.Join(root, "versions", "0.4.0")
		for _, tc := range []struct {
			exe        string
			wantVerDir string
		}{
			{filepath.Join(vDir, "Holdexar.exe"), vDir},
			{filepath.Join(vDir, "serverd.exe"), vDir},
			{filepath.Join(vDir, "_internal", "serverd.exe"), vDir},
			{filepath.Join(root, "updater", "UpdateHost.exe"), ""},
		} {
			writeTestFile(t, tc.exe, []byte("MZ"))
			gotRoot, gotVerDir, managed := InstallRootFromExe(tc.exe)
			if !SamePath(gotRoot, root) || !managed {
				t.Fatalf("exe %s -> (%q, %q, %v)", tc.exe, gotRoot, gotVerDir, managed)
			}
			if tc.wantVerDir == "" && gotVerDir != "" {
				t.Fatalf("exe %s expected empty versionDir, got %q", tc.exe, gotVerDir)
			}
			if tc.wantVerDir != "" && !SamePath(gotVerDir, tc.wantVerDir) {
				t.Fatalf("exe %s versionDir = %q, want %q", tc.exe, gotVerDir, tc.wantVerDir)
			}
		}
	})
}

func TestResolveDataDirZeroAppDataFallback(t *testing.T) {
	trapLocal := filepath.Join(t.TempDir(), "trap-localappdata")
	trapRoaming := filepath.Join(t.TempDir(), "trap-appdata")
	trapHome := filepath.Join(t.TempDir(), "trap-home")
	t.Setenv("LOCALAPPDATA", trapLocal)
	t.Setenv("APPDATA", trapRoaming)
	t.Setenv("USERPROFILE", trapHome)

	t.Run("explicit HOLDEXAR_DATA_DIR wins", func(t *testing.T) {
		custom := filepath.Join(t.TempDir(), "explicit-data")
		t.Setenv("HOLDEXAR_DATA_DIR", custom)
		got, err := ResolveDataDir(DataDirOptions{InstallRoot: t.TempDir()})
		if err != nil || !SamePath(got, custom) {
			t.Fatalf("got (%q, %v), want %q", got, err, custom)
		}
	})

	t.Run("portable.flag uses <root>/data", func(t *testing.T) {
		t.Setenv("HOLDEXAR_DATA_DIR", "")
		root := CanonicalPath(t.TempDir())
		writeTestFile(t, filepath.Join(root, PortableFlag), nil)
		got, err := ResolveDataDir(DataDirOptions{InstallRoot: root})
		if err != nil || !SamePath(got, filepath.Join(root, "data")) {
			t.Fatalf("got (%q, %v), want %q", got, err, filepath.Join(root, "data"))
		}
	})

	t.Run("state/current.json dataCompatibility.dataDir wins in managed layout", func(t *testing.T) {
		t.Setenv("HOLDEXAR_DATA_DIR", "")
		root := CanonicalPath(t.TempDir())
		compatDir := filepath.Join(CanonicalPath(t.TempDir()), "configured-data")
		raw, _ := json.Marshal(map[string]any{
			"version": "0.4.0",
			"dataCompatibility": map[string]any{
				"dataDir":       compatDir,
				"schemaVersion": 32,
				"versions":      []string{"0.4.0"},
			},
		})
		writeTestFile(t, filepath.Join(root, "state", "current.json"), raw)
		got, err := ResolveDataDir(DataDirOptions{InstallRoot: root})
		if err != nil || !SamePath(got, compatDir) {
			t.Fatalf("got (%q, %v), want %q", got, err, compatDir)
		}
	})

	t.Run("existing <root>/data/holdexar.db uses <root>/data", func(t *testing.T) {
		t.Setenv("HOLDEXAR_DATA_DIR", "")
		root := CanonicalPath(t.TempDir())
		writeTestFile(t, filepath.Join(root, "data", DBFileName), []byte("db"))
		got, err := ResolveDataDir(DataDirOptions{InstallRoot: root})
		if err != nil || !SamePath(got, filepath.Join(root, "data")) {
			t.Fatalf("got (%q, %v), want %q", got, err, filepath.Join(root, "data"))
		}
	})

	t.Run("managed mode uses <root>/data", func(t *testing.T) {
		t.Setenv("HOLDEXAR_DATA_DIR", "")
		root := CanonicalPath(t.TempDir())
		got, err := ResolveDataDir(DataDirOptions{InstallRoot: root, IsManaged: true})
		if err != nil || !SamePath(got, filepath.Join(root, "data")) {
			t.Fatalf("got (%q, %v), want %q", got, err, filepath.Join(root, "data"))
		}
	})

	t.Run("dev pointer file with BOM and comments", func(t *testing.T) {
		t.Setenv("HOLDEXAR_DATA_DIR", "")
		root := CanonicalPath(t.TempDir())
		pointed := filepath.Join(CanonicalPath(t.TempDir()), "dev-custom")
		content := "\uFEFF# comment\n\n" + pointed + "\n"
		writeTestFile(t, filepath.Join(root, DevPointerFile), []byte(content))
		got, err := ResolveDataDir(DataDirOptions{InstallRoot: root})
		if err != nil || !SamePath(got, pointed) {
			t.Fatalf("got (%q, %v), want %q", got, err, pointed)
		}
	})

	t.Run("dev fallback uses <root>/holdexar-dev and NEVER touches LOCALAPPDATA/APPDATA", func(t *testing.T) {
		t.Setenv("HOLDEXAR_DATA_DIR", "   ")
		root := CanonicalPath(t.TempDir())
		writeTestFile(t, filepath.Join(root, DevPointerFile), []byte("relative/not-abs\n"))
		got, err := ResolveDataDir(DataDirOptions{InstallRoot: root})
		want := filepath.Join(root, DevDataDirName)
		if err != nil || !SamePath(got, want) {
			t.Fatalf("got (%q, %v), want %q", got, err, want)
		}
		for _, forbidden := range []string{trapLocal, trapRoaming, trapHome} {
			if _, statErr := os.Stat(forbidden); !os.IsNotExist(statErr) {
				t.Fatalf("forbidden profile directory touched: %s", forbidden)
			}
		}
	})
}
