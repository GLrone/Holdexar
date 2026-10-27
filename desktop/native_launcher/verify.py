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
        self.assertEqual(self.run_entry().returncode, 1)

    def test_directory_helper(self):
        (self.root / ".launcher" / "SafetyHost.exe").mkdir(parents=True)
        self.assertEqual(self.run_entry().returncode, 1)

    def test_junction_escape(self):
        outside = Path(self.sandbox.name) / "outside"
        outside.mkdir()
        shutil.copy2(FIXTURE, outside / "SafetyHost.exe")
        link = self.root / ".launcher"
        subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
        self.assertEqual(self.run_entry().returncode, 1)

    def test_handoff(self):
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    LAUNCHER, FIXTURE, OUTPUT = options.launcher.resolve(), options.fixture.resolve(), options.output.resolve()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    unittest.main(argv=[__file__], verbosity=2)
