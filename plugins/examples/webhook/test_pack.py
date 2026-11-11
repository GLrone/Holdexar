import importlib.util
import pathlib
import tempfile
import unittest
import zipfile


spec = importlib.util.spec_from_file_location("pack", pathlib.Path(__file__).with_name("pack.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PackageTest(unittest.TestCase):
    def test_manifest_only_reproducible_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            first = pathlib.Path(directory) / "first.hlx"
            second = pathlib.Path(directory) / "second.hlx"
            self.assertEqual(module.pack(first), module.pack(second))
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(archive.namelist(), ["manifest.json"])
            with self.assertRaises(FileExistsError):
                module.pack(first)


if __name__ == "__main__":
    unittest.main()
