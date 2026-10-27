package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"testing"
)

func createJunction(link, target string) error {
	return exec.Command("cmd.exe", "/c", "mklink", "/J", link, target).Run()
}

func TestHelperPathRejectsMissingFileAndDirectory(t *testing.T) {
	root := t.TempDir()
	if _, err := helperPath(root); err == nil {
		t.Fatal("missing safety helper accepted")
	}
	if err := os.MkdirAll(filepath.Join(root, ".launcher", "SafetyHost.exe"), 0755); err != nil {
		t.Fatal(err)
	}
	if _, err := helperPath(root); err == nil {
		t.Fatal("directory accepted as safety helper")
	}
}

func TestHelperPathRejectsJunction(t *testing.T) {
	root, outside := t.TempDir(), t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "SafetyHost.exe"), []byte("outside"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := createJunction(filepath.Join(root, ".launcher"), outside); err != nil {
		t.Fatal(err)
	}
	if _, err := helperPath(root); err == nil {
		t.Fatal("junction escape accepted")
	}
}

func TestLauncherPreservesArgumentsEnvironmentAndExitCode(t *testing.T) {
	root := filepath.Join(t.TempDir(), "中文 安装目录")
	if err := os.MkdirAll(filepath.Join(root, ".launcher"), 0755); err != nil {
		t.Fatal(err)
	}
	testExe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(testExe)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, ".launcher", "SafetyHost.exe"), data, 0700); err != nil {
		t.Fatal(err)
	}
	out := filepath.Join(root, "observed.json")
	t.Setenv("HOLDEXAR_TEST_HELPER", out)
	t.Setenv("HOLDEXAR_INSTALL_ROOT", "untrusted override")
	t.Setenv("HOLDEXAR_TEST_INHERITED", "中文 preserved")
	args := []string{"", "two words", "中文", `a\"b`, `C:\space path\`, `--server`, `--update-protocol`}
	code, err := launch(root, append([]string{"-test.run=TestHelperProcess", "--"}, args...))
	if err != nil || code != 37 {
		t.Fatalf("launch returned code=%d error=%v", code, err)
	}
	observed := struct {
		Args            []string
		Root, Inherited string
	}{}
	data, err = os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(data, &observed); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(observed.Args, args) || observed.Root != root || observed.Inherited != "中文 preserved" {
		t.Fatalf("handoff changed values: %#v", observed)
	}
}

func TestHelperProcess(t *testing.T) {
	out := os.Getenv("HOLDEXAR_TEST_HELPER")
	if out == "" {
		return
	}
	for i, arg := range os.Args {
		if arg == "--" {
			payload, _ := json.Marshal(struct {
				Args            []string
				Root, Inherited string
			}{
				os.Args[i+1:], os.Getenv("HOLDEXAR_INSTALL_ROOT"), os.Getenv("HOLDEXAR_TEST_INHERITED"),
			})
			if err := os.WriteFile(out, payload, 0600); err != nil {
				os.Exit(99)
			}
			os.Exit(37)
		}
	}
	os.Exit(98)
}
