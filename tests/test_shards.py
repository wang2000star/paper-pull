import csv
import io
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from collections import Counter
from pathlib import Path

from tools import shards


ROOT = Path(__file__).resolve().parents[1]


class ShardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def flat(self, name, header, rows):
        path = self.root / name
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)
        return path

    def test_roundtrip_preserves_duplicates_unicode_and_multiline(self):
        header = ["标题", "PDF链接", "摘要"]
        row = ["带逗号,标题", "https://eprint.iacr.org/2026/001.pdf", 'line one\n"line two"']
        rows = [row, ["旧文", "https://eprint.iacr.org/2025/2.pdf", ""], row,
                ["未知", "", ""], header]
        source = self.flat("flat.csv", header, rows)
        directory = self.root / "shards"
        counts = shards.write_shards(directory, header, shards.iter_rows(source))
        self.assertEqual(counts, {"2025": 1, "2026": 2, "_unknown": 1})
        rebuilt = io.StringIO()
        self.assertEqual(shards.concat(directory, rebuilt), 4)
        self.assertEqual(Counter(map(tuple, list(csv.reader(io.StringIO(rebuilt.getvalue())))[1:])),
                         Counter(map(tuple, rows[:-1])))
        with mock.patch("sys.stdout", new=io.StringIO()):
            shards.verify(source, directory)
        for path in directory.glob("*.csv"):
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_explicit_year_and_unknown(self):
        self.assertEqual(shards.year_of({"year": " 2024 ", "PDF链接": ""}), "2024")
        self.assertEqual(shards.year_of({"year": "2024.0", "PDF链接": "https://eprint.iacr.org/2026/1.pdf"}), "_unknown")
        self.assertEqual(shards.year_of({"PDF链接": "https://example.com/2026/1.pdf"}), "_unknown")

    def test_scoped_update_preserves_other_year_and_unchanged_mtime(self):
        directory = self.root / "shards"
        shards.write_shards(directory, ["year", "value"], [["2025", "old"], ["2026", "initial"]])
        old = directory / "2025.csv"
        before = old.read_bytes(), old.stat().st_mtime_ns
        shards.write_shards(directory, ["year", "value"], [["2026", "new"]], years=[2026])
        self.assertEqual((old.read_bytes(), old.stat().st_mtime_ns), before)
        current = directory / "2026.csv"
        stamp = current.stat().st_mtime_ns
        shards.write_shards(directory, ["year", "value"], [["2026", "new"]], years=[2026])
        self.assertEqual(current.stat().st_mtime_ns, stamp)
        shards.write_shards(directory, ["year", "value"], [], years=[2026])
        self.assertEqual(list(shards.iter_rows(current)), [])
        self.assertEqual(old.read_bytes(), before[0])

    def test_bad_scoped_row_does_not_publish_partial_changes(self):
        directory = self.root / "shards"
        shards.write_shards(directory, ["year"], [["2026"]])
        before = (directory / "2026.csv").read_bytes()
        with self.assertRaises(ValueError):
            shards.write_shards(directory, ["year"], [["2026"], ["2025"]], years=[2026])
        self.assertEqual((directory / "2026.csv").read_bytes(), before)

    def test_mismatched_headers_and_malformed_rows_fail(self):
        directory = self.root / "shards"
        directory.mkdir()
        self.flat("shards/2025.csv", ["year"], [["2025"]])
        self.flat("shards/2026.csv", ["year", "extra"], [["2026", "x"]])
        with self.assertRaises(ValueError):
            list(shards.iter_rows(directory))
        path = self.flat("bad.csv", ["year"], [["2026", "extra"]])
        with self.assertRaises(ValueError):
            list(shards.iter_rows(path))

    def test_empty_dataset_and_replacement_remove_stale_shards(self):
        directory = self.root / "shards"
        shards.write_shards(directory, ["year"], [["2025"]])
        shards.write_shards(directory, ["year"], [])
        self.assertEqual([p.name for p in directory.glob("*.csv")], ["_unknown.csv"])
        self.assertEqual(list(shards.iter_rows(directory)), [])

    def test_append_has_one_bom_and_header(self):
        directory = self.root / "shards"
        for value in ("first", "second"):
            shards.append_rows(directory, ["year", "value"], [{"year": "2026", "value": value}])
        raw = (directory / "2026.csv").read_bytes()
        self.assertEqual(raw.count(b"\xef\xbb\xbf"), 1)
        self.assertEqual(list(shards.iter_rows(directory)), [["2026", "first"], ["2026", "second"]])

    def test_verifier_rejects_lost_duplicate_wrong_year_and_missing_bom(self):
        source = self.flat("original.csv", ["year"], [["2026"], ["2026"]])
        directory = self.root / "shards"
        shards.write_shards(directory, ["year"], [["2026"]])
        with self.assertRaises(ValueError):
            shards.verify(source, directory)
        (directory / "2026.csv").rename(directory / "2025.csv")
        with self.assertRaises(ValueError):
            shards.verify(source, directory)
        path = directory / "2025.csv"
        path.write_bytes(path.read_bytes()[3:])
        with self.assertRaises(ValueError):
            shards.verify(source, directory)

    def test_cli_stdout_and_file_exports_and_overwrite_guard(self):
        directory = self.root / "shards"
        shards.write_shards(directory, ["year"], [["2026"]])
        command = [sys.executable, "-B", str(ROOT / "tools/shards.py"), "concat", str(directory)]
        result = subprocess.run(command, check=True, capture_output=True)
        self.assertEqual(result.stdout, b"\xef\xbb\xbfyear\n2026\n")
        output = self.root / "dist" / "flat.csv"
        subprocess.run(command + ["--out", str(output)], check=True, capture_output=True)
        self.assertEqual(output.read_bytes(), result.stdout)
        target = directory / "2026.csv"
        before = target.read_bytes()
        failed = subprocess.run(command + ["--out", str(target)], capture_output=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
