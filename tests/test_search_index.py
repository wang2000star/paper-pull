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
        self.publications = self.root / "publications"

    def build(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return search.build_index(self.source, self.output, **kwargs)

    def source_rows(self, rows):
        write_shards(self.source, ["标题", "作者", "摘要", "关键词", "PDF链接", "分类"], rows)

    def publication_rows(self, rows):
        write_shards(self.publications, ["title", "authors", "year", "venue", "publication_type", "url", "doi"], rows)

    def output_rows(self):
        return [row for path in sorted(self.output.glob("[0-9]*.json")) for row in json.loads(path.read_text())]

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
        self.assertEqual(rows[1]["s"], "eprint")
        self.assertEqual(rows[1]["v"], "ePrint")
        for row in rows:
            self.assertEqual(set(row), {"t", "a", "y", "c", "k", "u", "s", "v"})
            self.assertRegex(row["u"], r"^https://eprint\.iacr\.org/2026/\d+$")
            self.assertNotIn("abstract", json.dumps(row).lower())
        self.assertNotIn(".pdf", (self.output / "2026.json").read_text())
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.output.glob("*.json")}
        self.build()
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.output.glob("*.json")})

    def test_abstracts_are_never_indexed_even_with_room(self):
        self.source_rows([["Title", "Author", "hidden-abstract-marker" * 100, "", "https://eprint.iacr.org/2026/001.pdf", ""]])
        self.assertEqual(self.build()["snippetLength"], 0)
        row = json.loads((self.output / "2026.json").read_text())[0]
        self.assertEqual(row["s"], "eprint")
        self.assertNotIn("hidden-abstract-marker", json.dumps(row))

    def test_normalized_titles_merge_sources_and_keep_publication_metadata(self):
        self.source_rows([
            [r"Secure $\mathsf{MPC}$", "A", "", "protocol", "https://eprint.iacr.org/2024/001.pdf", "Protocols"],
            ["secure MPC", "B", "", "", "https://eprint.iacr.org/2023/002.pdf", ""],
        ])
        self.publication_rows([
            ["SECURE MPC", "C", "2024", "CRYPTO", "conference proceedings", "https://doi.org/10.1000/secure", ""],
            ["secure-mpc", "D", "2024", "CRYPTO", "conference proceedings", "https://doi.org/10.1000/secure", ""],
            ["A journal result", "E", "2025", "TCHES", "journal", "https://tches.iacr.org/article/view/1", ""],
            ["A JOURNAL result", "E", "2025", "TCHES", "journal", "https://tches.iacr.org/article/view/1", ""],
            ["Conference only", "F", "2022", "RWC", "accepted conference talk (no proceedings)", "https://realworldcrypto.iacr.org/2022/", ""],
        ])
        manifest = self.build(publications=self.publications)
        rows = {search.normalized_title(row["t"]): row for row in self.output_rows()}
        merged = rows["secure mpc"]
        self.assertEqual((merged["s"], merged["u"], merged["v"]), ("eprint", "https://eprint.iacr.org/2024/001", "CRYPTO 2024"))
        self.assertEqual(merged["al"], "also published at CRYPTO 2024")
        self.assertEqual(merged["au"], "https://doi.org/10.1000/secure")
        self.assertEqual(rows["a journal result"]["s"], "journal")
        self.assertEqual(rows["a journal result"]["v"], "TCHES")
        self.assertEqual(rows["conference only"]["v"], "RWC 2022")
        self.assertEqual(manifest["inputCounts"], {"eprint":2, "conference":3, "journal":2})
        self.assertEqual(manifest["counts"], {"eprint":1, "conference":1, "journal":1})
        self.assertEqual(manifest["sourceCount"] - manifest["duplicatesRemoved"], manifest["count"])
        self.assertEqual(len(rows), 3)

    def test_duplicate_publication_titles_across_venues_keep_one_entry(self):
        self.source_rows([["Preprint", "", "", "", "https://eprint.iacr.org/2026/001.pdf", ""]])
        self.publication_rows([
            ["Same title", "", "2024", "CRYPTO", "conference proceedings", "https://doi.org/10.1000/conf", ""],
            ["Same TITLE!", "", "2025", "JoC", "journal", "https://doi.org/10.1000/journal", ""],
        ])
        self.build(publications=self.publications)
        rows = self.output_rows()
        self.assertEqual(len(rows), 2)
        paper = next(row for row in rows if row["s"] != "eprint")
        self.assertEqual((paper["s"], paper["v"], paper["y"]), ("journal", "JoC", 2025))
        self.assertEqual(paper["al"], "also published at CRYPTO 2024")

    def test_publication_links_use_doi_fallback_or_stay_unlinked(self):
        self.assertEqual(search.publication_url({"url":"https://iacr.org/paper.pdf", "doi":"10.1000/paper"}), "https://doi.org/10.1000/paper")
        self.assertEqual(search.publication_url({"url":"https://iacr.org/paper.PDF", "doi":""}), "")
        self.assertEqual(search.publication_url({"url":"../relative.pdf", "doi":""}), "")
        self.assertEqual(search.publication_url({"url":"javascript:alert(1)", "doi":""}), "")
        self.assertEqual(search.publication_url({"url":"https://eprint.iacr.org/2025/1.pdf", "doi":""}), "https://eprint.iacr.org/2025/001")
        self.source_rows([["Preprint", "", "", "", "https://eprint.iacr.org/2026/001.pdf", ""]])
        self.publication_rows([["Unlinked", "", "2025", "PKC", "conference proceedings", "https://iacr.org/paper.pdf", ""]])
        self.build(publications=self.publications)
        paper = next(row for row in self.output_rows() if row["s"] == "conference")
        self.assertEqual(paper["u"], "")
        self.assertNotIn(".pdf", json.dumps(self.output_rows()).lower())

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
        self.assertTrue({"query", "year-from", "year-to", "category", "sort", "source", "venue"} <= parser.labels)
        self.assertNotIn('class="snippet"', html)
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
        const p = SearchCore.prepare({t:'Fast TFHE', a:'Alice; Bob', y:2026, c:'Protocols', k:'key switching', s:'eprint', v:'CRYPTO 2026', u:'https://eprint.iacr.org/2026/001'});
        assert(SearchCore.matches(p, SearchCore.terms('"key switching" alice'), 2025, 2026, 'Protocols'));
        assert(!SearchCore.matches(p, ['tfhe', 'missing'], 2025, 2026, ''));
        assert(!SearchCore.matches(p, ['tfhe'], 2017, 2025, ''));
        assert(!SearchCore.matches(p, ['tfhe'], 2017, 2026, 'Other'));
        assert(SearchCore.matches(p, ['crypto'], 2017, 2026, '', 'eprint', 'CRYPTO'));
        assert(!SearchCore.matches(p, [], 2017, 2026, '', 'journal', 'CRYPTO'));
        assert(!SearchCore.matches(p, [], 2017, 2026, '', 'all', 'TCC'));
        const j = SearchCore.prepare({...p,s:'journal',v:'TCHES',c:''});
        assert(SearchCore.matches(j, [], 2017, 2026, '', 'journal', 'TCHES'));
        assert(!SearchCore.matches(j, [], 2017, 2026, '__uncategorized__', 'journal', ''));
        assert(!SearchCore.safeLink('https://iacr.org/file.pdf'));
        assert(!SearchCore.safeLink('javascript:alert(1)'));
        assert(SearchCore.safeLink('https://doi.org/10.1000/paper'));
        assert.deepEqual(SearchCore.highlightRanges('A [test] and a+b', ['[test]', 'a+b']), [[2,8],[13,16]]);
        const older = {...p, y:2025, t:'Alpha'};
        assert.equal([older,p].sort(SearchCore.compare('year'))[0], p);
        assert.equal([p,older].sort(SearchCore.compare('title'))[0], older);
        '''
        subprocess.run([shutil.which("node"), "-e", core + checks], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
