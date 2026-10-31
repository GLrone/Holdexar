package secretbox

import (
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// 跨语言兼容矩阵：Python 侧（真实 app.core.secretbox/keyring）与 Go 侧互为
// 编解码方。夹具脚本与全部数据目录都在 t.TempDir() 内，测试结束自动清理；
// 密钥材料只在结果文件内传递，断言与日志只输出条数与布尔结论。
// server/.venv 不可用的环境整组跳过。

const fixturePass = "fixture-pass-123"

const pyFixture = `import json
import os
import sys
import traceback

SERVER = sys.argv[1]
PHASE = sys.argv[2]
DATA = sys.argv[3]
OUT = sys.argv[4]
EXTRA = sys.argv[5]
PASS = "fixture-pass-123"
PURPOSES = ["steam-cookies", "kv:account.steam_api_key", "proxy-subscription", "proxy-credential"]


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def phase_encrypt():
    from app.core import keyring, secretbox
    plains = json.load(open(EXTRA, encoding="utf-8"))
    master = None
    if PHASE == "dpapi":
        master = os.urandom(32)
        keyring.write_record(DATA, keyring.wrap_dpapi(master, DATA))
    elif PHASE == "passphrase":
        master = os.urandom(32)
        keyring.write_record(DATA, keyring.wrap_passphrase(master, PASS))
        keyring.load_unlocked_key(master)
    samples = []
    for purpose in PURPOSES:
        for plain in plains:
            token = secretbox.encrypt_secret(plain, purpose)
            if plain == "":
                if token != "":
                    raise AssertionError("空串必须原样返回")
                continue
            if not token.startswith("enc1:"):
                raise AssertionError("密文缺前缀")
            if secretbox.decrypt_secret(token, purpose) != plain:
                raise AssertionError("Python 自解密不一致: " + purpose)
            samples.append({"purpose": purpose, "plain": plain, "token": token})
    if master is None:
        master = secretbox.legacy_master(DATA)
    res = {"samples": samples, "master_hex": master.hex(), "record_raw": None}
    p = os.path.join(DATA, "secret.master")
    if os.path.exists(p):
        res["record_raw"] = open(p, encoding="utf-8").read()
    if PHASE == "passphrase":
        newm = os.urandom(32)
        res["staging"] = keyring.wrap_staging(secretbox.legacy_master(DATA), newm)
        res["staging_new_hex"] = newm.hex()
        res["staging_old_hex"] = secretbox.legacy_master(DATA).hex()
    write_json(OUT, res)


def phase_decrypt():
    from app.core import keyring, secretbox
    req = json.load(open(EXTRA, encoding="utf-8"))
    mode = req["mode"]
    if mode == "passphrase":
        if not keyring.unlock(DATA, PASS):
            raise AssertionError("口令解锁失败")
    master = keyring.resolve_dmk(DATA)
    ok = 0
    for s in req["samples"]:
        if secretbox.decrypt_with(s["token"], s["purpose"], master) != s["plain"]:
            raise AssertionError("显式主密钥解密不一致: " + s["purpose"])
        if secretbox.decrypt_secret(s["token"], s["purpose"]) != s["plain"]:
            raise AssertionError("候选链解密不一致: " + s["purpose"])
        ok += 1
    res = {"ok": ok, "master_hex": master.hex()}
    if mode == "legacy":
        legacy = secretbox.legacy_master(DATA)
        if master.hex() != legacy.hex():
            raise AssertionError("legacy 主密钥与解析结果不一致")
        if req.get("expect_legacy_hex") and legacy.hex() != req["expect_legacy_hex"]:
            raise AssertionError("legacy 主密钥双端派生不一致")
        res["legacy_match"] = True
    if req.get("staging"):
        old = keyring.unwrap_staging(req["staging"], bytes.fromhex(req["staging_new_hex"]))
        if old is None or old.hex() != req["staging_old_hex"]:
            raise AssertionError("过渡记录解封不一致")
        res["staging_ok"] = True
    write_json(OUT, res)


try:
    sys.path.insert(0, SERVER)
    if PHASE == "decrypt":
        phase_decrypt()
    else:
        phase_encrypt()
except Exception:
    traceback.print_exc()
    sys.exit(1)
`

func locateVenv() (pyExe, serverDir string) {
	wd, err := os.Getwd()
	if err != nil {
		return "", ""
	}
	root := filepath.Join(wd, "..", "..", "..")
	pyExe = filepath.Join(root, "server", ".venv", "Scripts", "python.exe")
	if _, err := os.Stat(pyExe); err != nil {
		return "", ""
	}
	return pyExe, filepath.Join(root, "server")
}

func runPyFixture(t *testing.T, pyExe, script, envDir, outPath string, args ...string) map[string]any {
	t.Helper()
	var env []string
	for _, kv := range os.Environ() {
		if !strings.HasPrefix(kv, "HOLDEXAR_DATA_DIR=") {
			env = append(env, kv)
		}
	}
	cmd := exec.Command(pyExe, append([]string{"-I", script}, args...)...)
	cmd.Env = append(env, "HOLDEXAR_DATA_DIR="+envDir)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("python 夹具失败: %v\n%s", err, out)
	}
	return readJSONFile(t, outPath)
}

func writeJSONFile(t *testing.T, path string, v any) {
	t.Helper()
	data, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
}

func readJSONFile(t *testing.T, path string) map[string]any {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读夹具结果失败: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(data, &m); err != nil {
		t.Fatalf("夹具结果非 JSON: %v", err)
	}
	return m
}

func fixtureSamples(t *testing.T, res map[string]any) []map[string]any {
	t.Helper()
	raw, ok := res["samples"].([]any)
	if !ok || len(raw) == 0 {
		t.Fatal("夹具无样值")
	}
	out := make([]map[string]any, 0, len(raw))
	for _, item := range raw {
		out = append(out, item.(map[string]any))
	}
	return out
}

func checkPySamples(t *testing.T, dataDir string, samples []map[string]any, master []byte, purposes []string) {
	t.Helper()
	for _, s := range samples {
		purpose, plain, token := s["purpose"].(string), s["plain"].(string), s["token"].(string)
		if got, err := DecryptWith(token, purpose, master); err != nil || got != plain {
			t.Fatalf("显式主密钥解密失败 (%s): %v", purpose, err)
		}
		if got, err := DecryptSecretIn(dataDir, token, purpose); err != nil || got != plain {
			t.Fatalf("候选链解密失败 (%s): %v", purpose, err)
		}
		for _, other := range purposes {
			if other == purpose {
				continue
			}
			if _, err := DecryptSecretIn(dataDir, token, other); err == nil {
				t.Fatalf("跨用途解密未被拒绝 (%s→%s)", purpose, other)
			}
		}
	}
	t.Logf("%d 样值双路径解密一致，跨用途全部拒绝", len(samples))
}

func TestCrossLanguageCredentialMatrix(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("DPAPI 仅 Windows")
	}
	pyExe, serverDir := locateVenv()
	if pyExe == "" {
		t.Skip("server/.venv 不可用，跳过跨语言夹具")
	}
	root := t.TempDir()
	script := filepath.Join(root, "fixture.py")
	if err := os.WriteFile(script, []byte(pyFixture), 0o600); err != nil {
		t.Fatal(err)
	}
	plains := []string{
		"",
		"sk-0123456789abcdef",
		"包含中文与符号：///?#&=+ 的凭据值",
		strings.Repeat("x", 500),
	}
	plainsPath := filepath.Join(root, "plains.json")
	writeJSONFile(t, plainsPath, plains)
	purposes := []string{"steam-cookies", "kv:account.steam_api_key", "proxy-subscription", "proxy-credential"}
	ResetState()
	t.Cleanup(ResetState)

	// ── 方向一：Python 加密 → Go 解密 ──
	for _, mode := range []string{"legacy", "dpapi", "passphrase"} {
		t.Run("py2go-"+mode, func(t *testing.T) {
			dataDir := filepath.Join(root, "py-"+mode)
			if err := os.MkdirAll(dataDir, 0o755); err != nil {
				t.Fatal(err)
			}
			outPath := filepath.Join(root, "py-"+mode+".json")
			runPyFixture(t, pyExe, script, dataDir, outPath, serverDir, mode, dataDir, outPath, plainsPath)
			res := readJSONFile(t, outPath)
			samples := fixtureSamples(t, res)

			switch mode {
			case "legacy":
				goMaster, err := LegacyMaster(dataDir)
				if err != nil {
					t.Fatal(err)
				}
				if hex.EncodeToString(goMaster) != res["master_hex"].(string) {
					t.Fatal("legacy 主密钥双端派生不一致（指纹或安装盐读取漂移）")
				}
				checkPySamples(t, dataDir, samples, goMaster, purposes)
			case "dpapi":
				rec, err := ReadRecord(dataDir)
				if err != nil || rec == nil {
					t.Fatal("读不到 Python 写的密钥文件")
				}
				master, err := unwrapDPAPI(rec)
				if err != nil {
					t.Fatalf("Go 侧 DPAPI 解封 Python blob 失败: %v", err)
				}
				if hex.EncodeToString(master) != res["master_hex"].(string) {
					t.Fatal("DPAPI 解封主密钥与 Python 导出不一致")
				}
				checkPySamples(t, dataDir, samples, master, purposes)
			case "passphrase":
				rec, err := ReadRecord(dataDir)
				if err != nil || rec == nil {
					t.Fatal("读不到 Python 写的密钥文件")
				}
				unlocked, err := unwrapPassphrase(rec, fixturePass)
				if err != nil {
					t.Fatal(err)
				}
				if unlocked == nil || hex.EncodeToString(unlocked) != res["master_hex"].(string) {
					t.Fatal("scrypt 口令解封主密钥与 Python 导出不一致")
				}
				// 锁定态必须拒绝，不得绕过
				ResetState()
				s0 := samples[0]
				if _, err := DecryptSecretIn(dataDir, s0["token"].(string), s0["purpose"].(string)); err == nil || !strings.Contains(err.Error(), "锁定") {
					t.Fatalf("锁定态未拒绝: %v", err)
				}
				LoadUnlockedKey(unlocked)
				checkPySamples(t, dataDir, samples, unlocked, purposes)
			}
			if raw, ok := res["staging"]; ok && raw != nil {
				staging := raw.(map[string]any)
				newM, _ := hex.DecodeString(res["staging_new_hex"].(string))
				old := UnwrapStaging(staging, newM)
				if old == nil || hex.EncodeToString(old) != res["staging_old_hex"].(string) {
					t.Fatal("Python 封装的过渡记录 Go 解封失败")
				}
			}
			if raw, ok := res["record_raw"]; ok && raw != nil && mode != "legacy" {
				data, _ := os.ReadFile(filepath.Join(dataDir, "secret.master"))
				if string(data) != raw.(string) {
					t.Fatal("Go 读到的密钥文件与 Python 写入字节不一致")
				}
			}
		})
	}

	// ── 方向二：Go 加密 → Python 解密 ──
	for _, mode := range []string{"legacy", "dpapi", "passphrase"} {
		t.Run("go2py-"+mode, func(t *testing.T) {
			dataDir := filepath.Join(root, "go-"+mode)
			if err := os.MkdirAll(dataDir, 0o755); err != nil {
				t.Fatal(err)
			}
			var master []byte
			var err error
			switch mode {
			case "legacy":
				master, err = LegacyMaster(dataDir)
			case "dpapi":
				master = randBytes(keyBytes)
				var rec map[string]any
				rec, err = WrapDPAPI(master, dataDir)
				if err == nil {
					err = WriteRecord(dataDir, rec)
				}
			case "passphrase":
				master = randBytes(keyBytes)
				var rec map[string]any
				rec, err = WrapPassphrase(master, fixturePass)
				if err == nil {
					err = WriteRecord(dataDir, rec)
				}
			}
			if err != nil {
				t.Fatal(err)
			}

			samples := make([]map[string]any, 0, len(purposes)*len(plains))
			LoadUnlockedKey(master) // passphrase 全链封装需解锁态；其余模式无副作用
			for i, purpose := range purposes {
				for j, plain := range plains {
					var token string
					if (i+j)%2 == 0 {
						token, err = EncryptWith(plain, purpose, master)
					} else {
						token, err = EncryptSecretIn(dataDir, plain, purpose)
					}
					if err != nil {
						t.Fatal(err)
					}
					if plain == "" {
						if token != "" {
							t.Fatal("空串必须原样返回")
						}
						continue
					}
					samples = append(samples, map[string]any{"purpose": purpose, "plain": plain, "token": token})
				}
			}
			req := map[string]any{"mode": mode, "samples": samples}
			if mode == "legacy" {
				legacy, lerr := LegacyMaster(dataDir)
				if lerr != nil {
					t.Fatal(lerr)
				}
				req["expect_legacy_hex"] = hex.EncodeToString(legacy)
			}
			// 过渡记录交叉解封（Go 封 → Python 解）
			legacyMaster, lerr := LegacyMaster(dataDir)
			if lerr != nil {
				t.Fatal(lerr)
			}
			stagingNew := randBytes(keyBytes)
			req["staging"] = WrapStaging(legacyMaster, stagingNew)
			req["staging_new_hex"] = hex.EncodeToString(stagingNew)
			req["staging_old_hex"] = hex.EncodeToString(legacyMaster)

			reqPath := filepath.Join(root, "go-"+mode+"-req.json")
			outPath := filepath.Join(root, "go-"+mode+"-res.json")
			writeJSONFile(t, reqPath, req)
			// 脚本参数序：argv[4]=输出、argv[5]=请求
			runPyFixture(t, pyExe, script, dataDir, outPath, serverDir, "decrypt", dataDir, outPath, reqPath)
			res := readJSONFile(t, outPath)
			if int(res["ok"].(float64)) != len(samples) {
				t.Fatalf("Python 解密通过数 %v != %d", res["ok"], len(samples))
			}
			if mode == "legacy" && res["legacy_match"] != true {
				t.Fatal("legacy 主密钥双端派生不一致")
			}
			if res["staging_ok"] != true {
				t.Fatal("Go 封装的过渡记录 Python 解封失败")
			}
			t.Logf("%d 样值双路径解密一致（含全链候选）", len(samples))
		})
	}
}
