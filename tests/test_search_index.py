import contextlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from tools import build_search_index as search
from tools.shards import write_shards


ROOT = Path(__file__).resolve().parents[1]


class SearchIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.output = self.root / "output"

    def build(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return search.build_index(self.source, self.output, **kwargs)

    def source_rows(self, rows):
        write_shards(self.source, ["标题", "作者", "摘要", "关键词", "PDF链接", "分类"], rows)

    def test_records_are_sorted_deduplicated_safe_and_compact(self):
        self.source_rows([
            ["Zeta", "A; B; C; D; E", "<p>Abstract &amp; detail</p>\nnew line", "keyword", "https://eprint.iacr.org/2026/001.pdf", "Foundations"],
            ["Alpha", "One", "<script>bad()</script><b>Safe</b> " + "x" * 250, "", "https://eprint.iacr.org/2026/002.pdf", ""],
            ["Duplicate", "Other", "", "", "https://eprint.iacr.org/2026/0001.pdf", ""],
        ])
        manifest = self.build()
        self.assertEqual((manifest["sourceCount"], manifest["count"]), (3, 2))
        rows = json.loads((self.output / "2026.json").read_text())
        self.assertEqual([r["t"] for r in rows], ["Alpha", "Zeta"])
        self.assertEqual(rows[1]["a"], "A; B; C; D; et al.")
        self.assertEqual(rows[1]["s"], "Abstract & detail new line")
        self.assertNotIn("bad()", rows[0]["s"])
        self.assertLessEqual(len(rows[0]["s"]), 180)
        for row in rows:
            self.assertEqual(set(row), {"t", "a", "y", "c", "k", "u", "s"})
            self.assertRegex(row["u"], r"^https://eprint\.iacr\.org/2026/\d+$")
            self.assertNotIn("<", row["s"])
        self.assertNotIn(".pdf", (self.output / "2026.json").read_text())
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.output.glob("*.json")}
        self.build()
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.output.glob("*.json")})

    def test_budget_falls_back_to_120_then_omits_snippets(self):
        self.source_rows([["Title", "Author", "x" * 250, "", "https://eprint.iacr.org/2026/001.pdf", ""]])
        self.assertEqual(self.build()["snippetLength"], 180)
        total = sum(p.stat().st_size for p in self.output.glob("*.json"))
        self.assertEqual(self.build(total_budget=total - 20)["snippetLength"], 120)
        total = sum(p.stat().st_size for p in self.output.glob("*.json"))
        self.assertEqual(self.build(total_budget=total - 20)["snippetLength"], 0)
        row = json.loads((self.output / "2026.json").read_text())[0]
        self.assertNotIn("s", row)

    def test_impossible_budget_does_not_change_previous_payload(self):
        self.source_rows([["Title", "", "", "", "https://eprint.iacr.org/2026/001.pdf", ""]])
        self.build()
        before = {p.name: p.read_bytes() for p in self.output.glob("*.json")}
        with self.assertRaisesRegex(ValueError, "exceeds budget"):
            self.build(total_budget=1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.glob("*.json")})
        with self.assertRaisesRegex(ValueError, "exceeds budget"):
            self.build(file_budget=1)

    def test_urls_reject_other_hosts_and_normalize_to_landing_page(self):
        self.assertEqual(search.landing_url("http://eprint.iacr.org/2025/1.pdf?download=1"),
                         ("https://eprint.iacr.org/2025/001", 2025))
        for url in ["https://example.com/2026/1.pdf", "javascript:alert(1)",
                    "https://eprint.iacr.org.evil.test/2026/1.pdf"]:
            with self.assertRaises(ValueError):
                search.landing_url(url)

    def test_rebuild_removes_only_obsolete_year_files(self):
        self.source_rows([["Title", "", "", "", "https://eprint.iacr.org/2026/001.pdf", ""]])
        self.build()
        (self.output / "2017.json").write_text("[]")
        (self.output / "notes.txt").write_text("preserve")
        self.build()
        self.assertFalse((self.output / "2017.json").exists())
        self.assertTrue((self.output / "notes.txt").exists())


class PageTests(unittest.TestCase):
    def test_page_is_self_contained_with_accessible_search(self):
        class Assets(HTMLParser):
            def __init__(self):
                super().__init__()
                self.assets = []
                self.labels = set()

            def handle_starttag(self, tag, attrs):
                values = dict(attrs)
                if "src" in values or (tag == "link" and values.get("rel") == "stylesheet"):
                    self.assets.append(values)
                if tag == "label":
                    self.labels.add(values.get("for"))

        html = (ROOT / "docs/index.html").read_text()
        parser = Assets()
        parser.feed(html)
        self.assertEqual(parser.assets, [])
        self.assertTrue({"query", "year-from", "year-to", "category", "sort"} <= parser.labels)
        self.assertIn("data/manifest.json", html)
        self.assertIn("prefers-color-scheme:dark", html)
        self.assertNotIn("http://", html)
        self.assertEqual((ROOT / "docs/.nojekyll").stat().st_size, 0)

    @unittest.skipUnless(shutil.which("node"), "Node is optional for JavaScript unit checks")
    def test_search_phrases_and_filters_literal_highlights_and_sort(self):
        html = (ROOT / "docs/index.html").read_text()
        script = re.search(r"<script>(.*?)</script>", html, re.S)[1]
        core = script.split("\n    (() => {", 1)[0]
        checks = r'''
        const assert = require("node:assert/strict");
        assert.deepEqual(SearchCore.terms('  TFHE "key switching"  '), ['tfhe', 'key switching']);
        const p = SearchCore.prepare({t:'Fast TFHE', a:'Alice; Bob', y:2026, c:'Protocols', k:'key switching', s:'A compact proof', u:'https://eprint.iacr.org/2026/001'});
        assert(SearchCore.matches(p, SearchCore.terms('"key switching" alice'), 2025, 2026, 'Protocols'));
        assert(!SearchCore.matches(p, ['tfhe', 'missing'], 2025, 2026, ''));
        assert(!SearchCore.matches(p, ['tfhe'], 2017, 2025, ''));
        assert(!SearchCore.matches(p, ['tfhe'], 2017, 2026, 'Other'));
        assert(SearchCore.matches(p, ['compact'], 2017, 2026, ''));
        assert.deepEqual(SearchCore.highlightRanges('A [test] and a+b', ['[test]', 'a+b']), [[2,8],[13,16]]);
        const older = {...p, y:2025, t:'Alpha'};
        assert.equal([older,p].sort(SearchCore.compare('year'))[0], p);
        assert.equal([p,older].sort(SearchCore.compare('title'))[0], older);
        '''
        subprocess.run([shutil.which("node"), "-e", core + checks], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
