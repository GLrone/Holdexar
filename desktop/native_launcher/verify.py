"""Check native entry points against the same isolated helper process."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class EntryContract(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory(prefix="native-entry-", dir=OUTPUT)
        self.addCleanup(self.sandbox.cleanup)
        self.root = Path(self.sandbox.name) / "中文 安装目录"
        self.root.mkdir()
        self.launcher = self.root / "Holdexar.exe"
        shutil.copy2(LAUNCHER, self.launcher)
        self.env = {**os.environ, "HOLDEXAR_NO_DIALOG": "1", "HOLDEXAR_INSTALL_ROOT": "untrusted"}

    def run_entry(self, *args):
        return subprocess.run([self.launcher, *args], env=self.env, capture_output=True, timeout=15)

    def test_missing_helper(self):
        """薄架构：无 .launcher/SafetyHost.exe 必须拒启。"""
        if DESIGN == "full":
            self.skipTest("full 架构无 SafetyHost，由 versions 契约覆盖")
        self.assertEqual(self.run_entry().returncode, 1)

    def test_directory_helper(self):
        if DESIGN == "full":
            self.skipTest("full 架构无 SafetyHost，由 versions 契约覆盖")
        (self.root / ".launcher" / "SafetyHost.exe").mkdir(parents=True)
        self.assertEqual(self.run_entry().returncode, 1)

    def test_junction_escape(self):
        if DESIGN == "full":
            self.skipTest("full 架构版本目录 junction 由 packaged_contract 覆盖")
        outside = Path(self.sandbox.name) / "outside"
        outside.mkdir()
        shutil.copy2(FIXTURE, outside / "SafetyHost.exe")
        link = self.root / ".launcher"
        subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
        self.assertEqual(self.run_entry().returncode, 1)

    def test_handoff(self):
        if DESIGN == "full":
            self.skipTest("full 入口在 test_handoff_full 验证")
        helper = self.root / ".launcher" / "SafetyHost.exe"
        helper.parent.mkdir()
        shutil.copy2(FIXTURE, helper)
        observed = self.root / "observed.json"
        self.env.update(HOLDEXAR_TEST_HELPER=str(observed), HOLDEXAR_TEST_INHERITED="中文 preserved")
        args = ["", "two words", "中文", 'a\\"b', "C:\\space path\\", "--server", "--update-protocol"]
        result = self.run_entry("-test.run=TestHelperProcess", "--", *args)
        self.assertEqual(result.returncode, 37, result.stderr)
        payload = json.loads(observed.read_text(encoding="utf-8"))
        self.assertEqual(payload["Args"], args)
        self.assertTrue(os.path.samefile(payload["Root"], self.root))
        self.assertEqual(payload["Inherited"], "中文 preserved")

    def test_handoff_full(self):
        """全原生架构：安装根来自 exe 所在目录（不信任 HOLDEXAR_INSTALL_ROOT），
        versions/0.0.0/Holdexar.exe 为目标版本程序，参数逐字传递、--server
        前台等待退出码、安装根与数据目录环境注入。"""
        if DESIGN != "full":
            self.skipTest("full 架构专用契约")
        self.env.pop("HOLDEXAR_INSTALL_ROOT", None)
        version_dir = self.root / "versions" / "0.0.0"
        version_dir.mkdir(parents=True)
        shutil.copy2(FIXTURE, version_dir / "Holdexar.exe")
        state = self.root / "state"
        state.mkdir()
        (state / "current.json").write_text(json.dumps({"version": "0.0.0"}), encoding="utf-8")
        observed = self.root / "observed.json"
        self.env.update(HOLDEXAR_TEST_HELPER=str(observed), HOLDEXAR_TEST_INHERITED="中文 preserved")
        args = ["-test.run=TestHelperProcess", "--", "", "two words", "中文",
                'a\\"b', "C:\\space path\\", "--server"]
        result = self.run_entry(*args)
        self.assertEqual(result.returncode, 37, result.stderr)
        payload = json.loads(observed.read_text(encoding="utf-8"))
        # go test 框架把 "--" 之前的自身参数吃掉，夹具只看到其后的业务参数
        self.assertEqual(payload["Args"], args[2:])
        self.assertTrue(os.path.samefile(payload["Root"], self.root))
        self.assertEqual(payload["Inherited"], "中文 preserved")

    def test_protocol_full(self):
        if DESIGN != "full":
            self.skipTest("thin 入口仅转交，由保护核心报告协议")
        result_path = self.root / "protocol.json"
        self.env["HOLDEXAR_UPDATE_PROTOCOL_RESULT"] = str(result_path)
        result = self.run_entry("--update-protocol")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result_path.read_text(encoding="utf-8")), {"updateDataProtocol": 1})
        self.assertFalse((self.root / "state").exists(), "protocol query executed the startup path")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--design", choices=("thin", "full"), default="thin")
    options = parser.parse_args()
    LAUNCHER, FIXTURE, OUTPUT, DESIGN = (options.launcher.resolve(), options.fixture.resolve(),
                                         options.output.resolve(), options.design)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    unittest.main(argv=[__file__], verbosity=2)
