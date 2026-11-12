import contextlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from html.parser import HTMLParser
from pathlib import Path

from tools import build_search_index as search
from tools import resolve_links as resolver
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
            self.assertEqual(set(row), {"t", "a", "y", "c", "k", "u", "s", "v", "lk"})
            self.assertEqual(row["lk"], "eprint")
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
        self.assertEqual(merged["alk"], "doi")
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
        self.assertEqual(paper["lk"], "none")
        self.assertNotIn(".pdf", json.dumps(self.output_rows()).lower())

    def test_link_priority_and_proceedings_fallback(self):
        row = {"title":"Result", "year":"2024", "url":"./chapter.pdf",
               "doi":"", "source":"https://www.iacr.org/archive/crypto2024/crypto2024-index.html"}
        cache = {search.link_key(row): {"u":"https://doi.org/10.1000/resolved", "k":"doi"}}
        self.assertEqual(search.publication_link(row, cache), {"u":"https://doi.org/10.1000/resolved", "k":"doi"})
        self.assertEqual(search.publication_link({**row, "doi":"10.1000/original"}, cache)["u"], "https://doi.org/10.1000/original")
        self.assertEqual(search.publication_link(row), {"u":row["source"], "k":"index"})
        self.assertEqual(search.publication_link({**row, "source":""}), {"u":"", "k":"none"})
        ojs = {**row, "url":"http://tches.iacr.org/index.php/TCHES/article/download/123/456"}
        self.assertEqual(search.publication_link(ojs), {"u":"https://tches.iacr.org/index.php/TCHES/article/view/123", "k":"ojs"})
        self.assertEqual(search.publication_link(ojs, cache)["k"], "doi")
        self.assertEqual(search.publication_link({**row, "url":"https://eprint.iacr.org/2024/1.pdf", "doi":"10.1000/other"}, cache)["k"], "eprint")

    def test_index_links_survive_canonical_merge_and_cache_loading(self):
        self.source_rows([["Preprint", "", "", "", "https://eprint.iacr.org/2024/001.pdf", ""]])
        fields = ["title", "authors", "year", "venue", "publication_type", "url", "doi", "source"]
        index = "https://www.iacr.org/archive/crypto2024/crypto2024-index.html"
        write_shards(self.publications, fields, [["Preprint", "", "2024", "CRYPTO", "conference", "./first.pdf", "", index],
            ["New result", "", "2024", "CRYPTO", "conference", "./second.pdf", "", index]])
        cache = self.root / "links.json"
        cache.write_text(json.dumps({"new result|2024":{"u":"https://doi.org/10.1000/new", "k":"doi"}}))
        manifest = self.build(publications=self.publications, link_cache=cache)
        rows = {r["t"]:r for r in self.output_rows()}
        self.assertEqual((rows["Preprint"]["au"], rows["Preprint"]["alk"]), (index, "index"))
        self.assertEqual(rows["New result"]["lk"], "doi")
        self.assertEqual(manifest["alsoLinkKinds"]["index"], 1)

    def test_duplicate_prefers_article_page_over_index_fallback(self):
        self.source_rows([["Preprint", "", "", "", "https://eprint.iacr.org/2024/001.pdf", ""]])
        fields = ["title", "authors", "year", "venue", "publication_type", "url", "doi", "source"]
        write_shards(self.publications, fields, [
            ["Article title", "Official author", "2024", "CRYPTO", "conference", "https://doi.org/10.1000/paper", "10.1000/paper", "https://iacr.org/index.html"],
            ["ARTICLE TITLE", "Archive author", "2024", "CRYPTO", "conference", "./chapter.pdf", "", "https://iacr.org/index.html"]])
        self.build(publications=self.publications)
        article = next(r for r in self.output_rows() if r["s"] == "conference")
        self.assertEqual((article["t"], article["a"], article["lk"]), ("Article title", "Official author", "doi"))

    def test_direct_downloads_are_rejected_and_ojs_galleys_normalized(self):
        for url in ["https://iacr.org/a%2Epdf", "https://iacr.org/download/1", "https://iacr.org/a?download=1",
                    "https://iacr.org/a?format=PDF", "https://iacr.org/bitstream/1", "http://iacr.org/page"]:
            self.assertEqual(search.safe_url(url), "", url)
        self.assertEqual(search.ojs_url("https://tosc.iacr.org/index.php/ToSC/article/view/12/34"),
                         "https://tosc.iacr.org/index.php/ToSC/article/view/12")

    def test_shipped_link_contract_and_budgets(self):
        paths = list((ROOT / "docs/data").glob("*.json"))
        self.assertLessEqual(sum(p.stat().st_size for p in paths), search.TOTAL_BUDGET)
        rows = [r for p in paths if p.stem.isdigit() for r in json.loads(p.read_text())]
        manifest = json.loads((ROOT / "docs/data/manifest.json").read_text())
        self.assertEqual(len(rows), manifest["count"])
        self.assertEqual(manifest["count"], 19070)
        self.assertEqual(manifest["counts"], {"eprint":17556, "conference":582, "journal":932})
        for path in paths:
            self.assertNotIn(".pdf", path.read_text().casefold(), path.name)
        for row in rows:
            self.assertIn(row["lk"], search.LINK_KINDS)
            self.assertEqual(row["lk"] == "none", not bool(row["u"]))
            for field, kind in [("u", "lk"), ("au", "alk")]:
                if row.get(field):
                    self.assertIn(row[kind], search.LINK_KINDS - {"none"})
                    self.assertTrue(row[field].startswith("https://"))
                    self.assertEqual(search.safe_url(row[field]), row[field])
        cache = ROOT / "data/link_landing.json"
        self.assertLess(cache.stat().st_size, resolver.CACHE_BUDGET)
        self.assertTrue(all(resolver.valid_entry(entry) for entry in json.loads(cache.read_text()).values()))

    def test_impossible_budget_does_not_change_previous_payload(self):
        self.source_rows([["Title", "", "", "", "https://eprint.iacr.org/2026/001.pdf", ""]])
        self.build()
        before = {p.name: p.read_bytes() for p in self.output.glob("*.json")}
        with self.assertRaisesRegex(ValueError, "exceeds budget"):
            self.build(total_budget=1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.glob("*.json")})
        with self.assertRaisesRegex(ValueError, "exceeds budget"):
            self.build(file_budget=1)

    def test_pdf_postcondition_preserves_previous_payload(self):
        self.source_rows([["Title", "", "", "", "https://eprint.iacr.org/2026/001.pdf", ""]])
        self.build()
        before = {p.name:p.read_bytes() for p in self.output.glob("*.json")}
        self.source_rows([["bad.pdf title", "", "", "", "https://eprint.iacr.org/2026/001.pdf", ""]])
        with self.assertRaisesRegex(ValueError, "PDF reference"):
            self.build()
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.output.glob("*.json")})

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


class LinkResolverTests(unittest.TestCase):
    def test_confident_titles_year_tolerance_and_conflicts(self):
        row = {"title":r"Secure $\mathsf{MPC}$: Protocols", "year":"2024"}
        hit = {"title":"Secure MPC Protocols", "publication_year":2025, "doi":"https://doi.org/10.1000/match"}
        self.assertEqual(resolver.choose_doi(row, [hit]), hit["doi"])
        for bad in [{**hit, "publication_year":2026}, {**hit, "doi":None}, {**hit, "title":"Unrelated MPC construction"}]:
            self.assertEqual(resolver.choose_doi(row, [bad]), "")
        self.assertEqual(resolver.choose_doi(row, [hit, {**hit, "doi":"10.1000/conflict"}]), "")
        near = {**hit, "title":"Secure MPC Protocol"}
        self.assertEqual(resolver.choose_doi(row, [near]), hit["doi"])

    def test_targets_exclude_convertible_eprint_and_ojs_links(self):
        for url in ["./chapter.pdf", "/archive/paper.PDF", "https://iacr.org/chapter.pdf"]:
            self.assertTrue(resolver.needs_resolution({"url":url}))
        for url in ["https://eprint.iacr.org/2024/001.pdf", "https://tches.iacr.org/article/download/1/2.pdf", "https://doi.org/10.1000/paper"]:
            self.assertFalse(resolver.needs_resolution({"url":url}))

    def test_lookup_parameters_and_doi_acceptance(self):
        session = Mock()
        response = session.get.return_value
        response.status_code = 200
        response.json.return_value = {"results":[{"title":"My paper", "publication_year":2024, "doi":"10.1000/paper"}]}
        with patch.object(resolver.time, "sleep"):
            self.assertEqual(resolver.lookup(session, {"title":"My paper", "year":"2024"}, .5, [0]), "https://doi.org/10.1000/paper")
        self.assertEqual(session.get.call_args.kwargs["params"],
                         {"filter":"title.search:My paper", "per-page":5, "select":"doi,title,publication_year"})

    def test_quota_fallback_resume_and_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "links.json"
            rows = [{"title":"First", "year":"2024", "url":"./one.pdf", "source":"https://iacr.org/index.html"},
                    {"title":"Second", "year":"2024", "url":"./two.pdf", "source":""}]
            session = Mock(headers={})
            response = session.get.return_value
            response.status_code = 429
            response.json.return_value = {"dailyRemainingUsd":0}
            with patch.object(resolver, "iter_dict_rows", return_value=iter(rows)), patch.object(resolver.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                report = resolver.resolve_links("unused", cache, session=session)
            self.assertEqual((report["rows"], report["doi"], report["index"], report["unmatched"]), (2, 0, 1, 1))
            self.assertTrue(report["quotaExhausted"])
            self.assertEqual(session.get.call_count, 1)
            self.assertIn("mailto=wangfangzhen23@mails.ucas.ac.cn", session.headers["User-Agent"])
            with patch.object(resolver, "iter_dict_rows", return_value=iter(rows)), patch.object(resolver, "lookup") as lookup, contextlib.redirect_stdout(io.StringIO()):
                resolver.resolve_links("unused", cache, session=session)
                lookup.assert_not_called()
            with patch.object(resolver, "iter_dict_rows", return_value=iter(rows)), patch.object(resolver, "lookup", return_value="https://doi.org/10.1000/paper") as lookup, contextlib.redirect_stdout(io.StringIO()):
                report = resolver.resolve_links("unused", cache, session=session, retry_fallbacks=True, delay=0)
                self.assertEqual(lookup.call_args.args[2], .5)
            self.assertEqual(report["doi"], 2)
            self.assertTrue(all(entry["k"] == "doi" for entry in json.loads(cache.read_text()).values()))


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
        self.assertNotIn(".pdf", html.casefold())
        self.assertIn("opens the proceedings index page", html)
        self.assertIn("OpenAlex", html)
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
        assert(!SearchCore.safeLink('https://iacr.org/file%2Epdf'));
        assert(!SearchCore.safeLink('https://iacr.org/download/1'));
        assert(!SearchCore.safeLink('https://iacr.org/file?format=PDF'));
        assert(!SearchCore.safeLink('https://iacr.org/file?download=1'));
        assert(!SearchCore.safeLink('http://iacr.org/page'));
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
