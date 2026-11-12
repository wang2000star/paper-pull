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
        # A newly resolved archive duplicate must not replace the original metadata row.
        cache = self.root / "links.json"
        cache.write_text(json.dumps({"article title|2024":{"u":"https://doi.org/10.1000/paper", "k":"doi", "r":"dblp"}}))
        self.build(publications=self.publications, link_cache=cache)
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

    @staticmethod
    def response(status=200, body=None, headers=None):
        return Mock(status_code=status, headers=headers or {}, json=Mock(return_value=body or {}))

    def provider(self, name, responses):
        session = Mock(headers={})
        if isinstance(responses, list):
            session.get.side_effect = responses
        else:
            session.get.return_value = responses
        return resolver.Provider(name, session=session)

    def test_lookup_parameters_and_doi_acceptance(self):
        hit = {"title":"My paper", "publication_year":2024, "doi":"10.1000/paper"}
        p = self.provider("openalex", self.response(body={"results":[hit]}))
        with patch.object(resolver.time, "sleep"):
            self.assertEqual(p.lookup({"title":"My paper", "year":"2024"}), "https://doi.org/10.1000/paper")
        self.assertEqual(p.session.get.call_args.kwargs["params"],
                         {"filter":"title.search:My paper", "per-page":5, "select":"doi,title,publication_year", "mailto":resolver.CONTACT})
        self.assertIn("mailto=" + resolver.CONTACT, p.session.headers["User-Agent"])
        self.assertEqual(p.delay, .35)
        self.assertEqual(p.report()["resolved"], 1)

    def test_semantic_scholar_external_doi_and_isolated_auth(self):
        hit = {"title":"My paper", "year":2024, "externalIds":{"DOI":"10.1000/paper"}}
        p = self.provider("semantic_scholar", self.response(body={"data":[hit]}))
        with patch.object(resolver.time, "sleep"), patch.dict("os.environ", {"OPENALEX_API_KEY":"openalex-only"}):
            self.assertEqual(p.lookup({"title":"My paper", "year":"2024"}), "https://doi.org/10.1000/paper")
        self.assertEqual(p.session.get.call_args.kwargs["params"],
                         {"query":"My paper", "limit":5, "fields":"title,year,externalIds"})
        self.assertNotIn("Authorization", p.session.get.call_args.kwargs["headers"])
        self.assertEqual(p.delay, .6)

    def test_one_429_retries_same_query_and_recovers(self):
        p = self.provider("openalex", [self.response(429, {"dailyRemainingUsd":0}), self.response()])
        row = {"title":"First", "year":"2024"}
        with patch.object(resolver.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(p.lookup(row), "")
        self.assertEqual(p.session.get.call_count, 2)
        self.assertEqual(p.session.get.call_args_list[0], p.session.get.call_args_list[1])
        self.assertIn(2, [call.args[0] for call in sleep.call_args_list])
        self.assertEqual(p.report()["rateLimitedStreak"], 0)
        self.assertFalse(p.disabled)

    def test_backoff_and_eight_distinct_queries_before_cutoff(self):
        p = self.provider("openalex", self.response(429))
        with patch.object(resolver.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            for i in range(8):
                with self.assertRaises(resolver.LookupUnavailable):
                    p.lookup({"title":f"Paper {i}", "year":"2024"})
                self.assertEqual(p.disabled, i == 7)
        self.assertEqual(p.session.get.call_count, 48)
        self.assertEqual([call.args[0] for call in sleep.call_args_list if call.args[0] >= 2], [2,4,8,16,32] * 8)
        self.assertEqual(p.report()["attempted"], 8)
        self.assertEqual(p.report()["rateLimitedStreak"], 8)

    def test_repeated_title_and_non_429_do_not_trip_cutoff(self):
        p = self.provider("openalex", self.response(429))
        row = {"title":"Same title", "year":"2024"}
        with patch.object(resolver.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            for _ in range(9):
                with self.assertRaises(resolver.LookupUnavailable): p.lookup(row)
        self.assertFalse(p.disabled)
        self.assertEqual(p.report()["rateLimitedStreak"], 1)
        p.session.get.return_value = self.response(404)
        with self.assertRaises(resolver.LookupUnavailable): p.lookup(row)
        self.assertEqual(p.report()["rateLimitedStreak"], 0)

    def test_retry_after_seconds_dates_and_long_deferral(self):
        with patch.object(resolver.time, "time", return_value=100):
            self.assertEqual(resolver.retry_after(self.response(headers={"Retry-After":"12"})), 12)
            self.assertEqual(resolver.retry_after(self.response(headers={"Retry-After":"Thu, 01 Jan 1970 00:02:00 GMT"})), 20)
            p = self.provider("openalex", self.response(429, headers={"Retry-After":"3600"}))
            with patch.object(resolver.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(resolver.LookupUnavailable): p.lookup({"title":"First", "year":"2024"})
            self.assertEqual(p.retry_at, 3700)
            self.assertEqual(p.session.get.call_count, 1)
            self.assertFalse(p.available)
            self.assertFalse(p.disabled)
        p = self.provider("openalex", [self.response(429, headers={"Retry-After":"3"}), self.response()])
        with patch.object(resolver.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            p.lookup({"title":"First", "year":"2024"})
        self.assertIn(3, [call.args[0] for call in sleep.call_args_list])

    def test_retry_index_fallback_preserves_cache_and_uses_second_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "links.json"
            report_path = Path(temporary) / "report.json"
            original = {"first|2024":{"u":"https://iacr.org/index.html", "k":"index"},
                        "already|2024":{"u":"https://doi.org/10.1000/already", "k":"doi"},
                        "unmatched|2024":{"u":"", "k":"none"}}
            cache.write_text(json.dumps(original))
            rows = [{"title":title, "year":"2024", "url":"./one.pdf", "source":"https://iacr.org/index.html"}
                    for title in ["First", "Already", "Unmatched"]]
            providers = [self.provider("openalex", self.response()), self.provider("semantic_scholar",
                         self.response(body={"data":[{"title":"First", "year":2024, "externalIds":{"DOI":"10.1000/first"}}]}))]
            with patch.object(resolver, "iter_dict_rows", return_value=iter(rows)), patch.object(resolver.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                report = resolver.resolve_links("unused", cache, providers=providers, retry_index=True, report_path=report_path)
            after = json.loads(cache.read_text())
            self.assertEqual(after["already|2024"], original["already|2024"])
            self.assertEqual(after["unmatched|2024"], original["unmatched|2024"])
            self.assertEqual(after["first|2024"], {"u":"https://doi.org/10.1000/first", "k":"doi", "r":"semantic_scholar"})
            self.assertEqual((report["attempted"],report["resolved"],report["pending"]), (1,1,0))
            self.assertEqual(report["sources"]["openalex"]["resolved"], 0)
            self.assertEqual(report["sources"]["semantic_scholar"]["resolved"], 1)
            self.assertTrue(report["complete"])
            self.assertEqual(json.loads(report_path.read_text()), report)

    def test_both_sources_unavailable_leave_unprocessed_cache_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "links.json"
            report_path = Path(temporary) / "report.json"
            rows = [{"title":str(i), "year":"2024", "url":"./one.pdf", "source":"https://iacr.org/index.html"} for i in range(10)]
            original = {resolver.link_key(r):{"u":r["source"], "k":"index"} for r in rows}
            cache.write_text(json.dumps(original))
            providers = [self.provider(name, self.response(429)) for name in ["openalex","semantic_scholar"]]
            with patch.object(resolver, "iter_dict_rows", return_value=iter(rows)), patch.object(resolver.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                report = resolver.resolve_links("unused", cache, providers=providers, retry_index=True, report_path=report_path, max_api_requests=200)
            self.assertEqual(json.loads(cache.read_text()), original)
            self.assertEqual((report["processed"], report["unprocessed"], report["pending"]), (8,2,10))
            self.assertFalse(report["complete"])
            self.assertEqual(report["sources"]["openalex"]["attempted"], 8)
            # A resumed run must honor persisted long Retry-After deadlines before any new request.
            state = {"sources":{name:{"retryAt":1000} for name in ["openalex","semantic_scholar"]}}
            report_path.write_text(json.dumps(state))
            with patch.object(resolver, "iter_dict_rows", return_value=iter(rows)), patch.object(resolver.time, "time", return_value=100), contextlib.redirect_stdout(io.StringIO()):
                report = resolver.resolve_links("unused", cache, retry_index=True, report_path=report_path)
            self.assertEqual(report["attempted"], 0)
            self.assertEqual(report["pending"], 10)

    def test_token_overlap_and_arbitrary_latex_macros(self):
        row = {"title":r"$\unknown{Secure}$ Multi Party Computation Protocols", "year":"2024"}
        hit = {"title":"Protocols for Secure Multi Party Computation", "year":2024, "externalIds":{"DOI":"10.1000/reordered"}}
        self.assertEqual(resolver.choose_doi(row, [hit]), "")  # Extra token falls below 0.85 Jaccard.
        hit["title"] = "Protocols Secure Multi Party Computation"
        self.assertEqual(resolver.choose_doi(row, [hit]), "https://doi.org/10.1000/reordered")

    def test_local_dblp_join_finishes_before_api_limits_without_fuzzy_venue(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "links.json"
            dblp = Path(temporary) / "dblp.json"
            rows = [{"title":title, "year":"2024", "url":"./one.pdf", "source":"https://iacr.org/index.html"}
                    for title in [r"Secure $\unknown{MPC}$", "Wrong year", "Publisher landing", "Missing"]]
            original = {resolver.link_key(r):{"u":r["source"], "k":"index"} for r in rows}
            cache.write_text(json.dumps(original))
            dblp.write_text(json.dumps({
                "secure mpc|2024":{"doi":"10.1000/local", "ee":"https://doi.org/10.1000/local", "y":2024, "v":"CRYPTO"},
                "wrong year|2023":{"doi":"10.1000/wrong", "ee":"", "y":2023, "v":"CRYPTO"},
                "publisher landing|2024":{"doi":"", "ee":"https://link.springer.com/chapter/safe", "y":2024, "v":"CRYPTO"}}))
            providers = [self.provider(name, self.response()) for name in ["openalex","semantic_scholar"]]
            with patch.object(resolver, "iter_dict_rows", return_value=iter(rows)), contextlib.redirect_stdout(io.StringIO()):
                report = resolver.resolve_links("unused", cache, providers=providers, dblp_path=dblp,
                                                max_api_requests=0, retry_index=True)
            after = json.loads(cache.read_text())
            self.assertEqual(report["dblpMatched"], 2)
            self.assertEqual(report["dblpResolved"], 2)
            self.assertEqual(report["apiRequests"], 0)
            self.assertEqual(report["pending"], 2)
            for p in providers: p.session.get.assert_not_called()
            self.assertEqual(after[resolver.link_key(rows[0])]["u"], "https://doi.org/10.1000/local")
            self.assertEqual(after[resolver.link_key(rows[1])], original[resolver.link_key(rows[1])])
            link = after[resolver.link_key(rows[2])]
            self.assertTrue(resolver.valid_entry(link))
            self.assertEqual(search.publication_link(rows[2], after), {"u":link["u"], "k":"doi"})

    def test_shared_api_budget_counts_retries_across_providers(self):
        budget = resolver.RequestBudget(3)
        a = self.provider("openalex", [self.response(429), self.response()])
        b = self.provider("semantic_scholar", self.response(429))
        a.budget = b.budget = budget
        with patch.object(resolver.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(a.lookup({"title":"First", "year":"2024"}), "")
            with self.assertRaisesRegex(resolver.LookupUnavailable, "budget exhausted"):
                b.lookup({"title":"Second", "year":"2024"})
        self.assertEqual(budget.used, 3)
        self.assertEqual(a.session.get.call_count, 2)
        self.assertEqual(b.session.get.call_count, 1)
        self.assertFalse(a.available)
        self.assertFalse(b.available)

    def test_dblp_venue_aliases_and_unsafe_editions(self):
        row = {"title":"Result", "year":"2024", "venue":"TCHES"}
        index = {"result|2024":{"doi":"10.1000/result", "ee":"", "v":"CHES", "y":2024}}
        self.assertEqual(resolver.dblp_link(row, index), "https://doi.org/10.1000/result")
        self.assertEqual(resolver.dblp_link({**row, "venue":"CRYPTO"}, index), "")
        index["result|2024"].update(doi="", ee="https://iacr.org/paper.pdf")
        self.assertEqual(resolver.dblp_link(row, index), "")

    def test_dblp_fuzzy_sequence_match_handles_math_spacing(self):
        row = {"title":r"Constrained PRFs for $NC^{1}$ in Traditional Groups", "year":"2018", "v":"CRYPTO 2018"}
        index = {"constrained prfs for nc1 in traditional groups|2018":
                 {"doi":"10.1000/nc1", "v":"CRYPTO", "y":2018}}
        match = resolver.dblp_match(row, index)
        self.assertEqual(match["u"], "https://doi.org/10.1000/nc1")
        self.assertEqual(match["method"], "fuzzy")
        title = resolver.dblp_key(row).split("|")[0]
        other = match["title"]
        self.assertLess(len(set(title.split()) & set(other.split())) / len(set(title.split()) | set(other.split())), .85)
        self.assertGreaterEqual(match["similarity"], .93)

    def test_dblp_fuzzy_jaccard_handles_reordering(self):
        row = {"title":"Simplex Consensus A Fast and Simple Protocol", "year":"2024", "venue":"CRYPTO"}
        candidate = "simplex consensus a simple and fast protocol"
        index = {candidate + "|2024":{"doi":"10.1000/reordered", "v":"CRYPTO", "y":2024}}
        self.assertLess(resolver.SequenceMatcher(None, resolver.dblp_key(row).split("|")[0], candidate).ratio(), .93)
        match = resolver.dblp_match(row, index)
        self.assertEqual(match["u"], "https://doi.org/10.1000/reordered")
        self.assertEqual(match["similarity"], 1)

    def test_dblp_fuzzy_requires_venue_year_and_safe_landing(self):
        row = {"title":"A Secure Protocol for Computing Functions", "year":"2024", "venue":"TCHES 2024"}
        title = resolver.dblp_key(row).split("|")[0]
        for venue in ["CHES", "TCHES"]:
            index = {title + "|2023":{"doi":"10.1000/near", "v":venue, "y":2023}}
            self.assertEqual(resolver.dblp_link(row, index), "https://doi.org/10.1000/near")
            self.assertEqual(resolver.dblp_link({**row, "year":"2025"}, index), "")
            self.assertEqual(resolver.dblp_link({**row, "venue":""}, index), "")
            self.assertEqual(resolver.dblp_link({**row, "venue":"CRYPTO"}, index), "")
            index[title + "|2023"].update(doi="", ee="https://iacr.org/unsafe.pdf")
            self.assertEqual(resolver.dblp_link(row, index), "")
        self.assertEqual(resolver.venue_family("ToSC 2024"), resolver.venue_family("FSE"))

    def test_dblp_fuzzy_ranking_and_exact_priority(self):
        row = {"title":"A Secure Protocol for Computing Functions", "year":"2024", "venue":"CRYPTO"}
        title = resolver.dblp_key(row).split("|")[0]
        reordered = "computing functions for a secure protocol"
        index = {
            reordered + "|2025":{"doi":"10.1000/later", "v":"CRYPTO", "y":2025},
            reordered + "|2023":{"doi":"10.1000/earlier", "v":"CRYPTO", "y":2023},
            title + "s|2023":{"doi":"10.1000/lower", "v":"CRYPTO", "y":2023},
            title + "|2024":{"doi":"10.1000/wrong-venue", "v":"EUROCRYPT", "y":2024}}
        self.assertEqual(resolver.dblp_link(row, index), "https://doi.org/10.1000/earlier")
        self.assertEqual(resolver.dblp_link(row, dict(reversed(list(index.items())))), "https://doi.org/10.1000/earlier")
        index[title + "|2024"].update(doi="10.1000/exact", v="CRYPTO")
        self.assertEqual(resolver.dblp_match(row, index)["method"], "exact")
        self.assertEqual(resolver.dblp_link(row, index), "https://doi.org/10.1000/exact")

    def test_dblp_fuzzy_rejects_below_both_thresholds(self):
        row = {"title":"Secure Multi Party Computation Protocols", "year":"2024", "venue":"CRYPTO"}
        index = {"protocols for secure multi party computation|2024":
                 {"doi":"10.1000/weak", "v":"CRYPTO", "y":2024}}
        self.assertEqual(resolver.dblp_link(row, index), "")

    def test_fuzzy_local_join_preserves_existing_dois_and_rwc_talks(self):
        rows = [{"title":title, "year":"2018", "venue":"CRYPTO", "url":"./paper.pdf", "source":"https://iacr.org/index.html"}
                for title in ["Constrained PRFs for NC 1 in Traditional Groups", "Constrained PRFs for NC1 in Traditional Groups"]]
        rows.append({"title":"RWC Talk", "year":"2025", "venue":"RWC", "url":"https://rwc.iacr.org/2025/acceptedtalks.php"})
        with tempfile.TemporaryDirectory() as temporary:
            cache, dblp = Path(temporary) / "cache.json", Path(temporary) / "dblp.json"
            original = {resolver.link_key(rows[0]):{"u":rows[0]["source"], "k":"index"},
                        resolver.link_key(rows[1]):{"u":"https://doi.org/10.1000/existing", "k":"doi", "r":"openalex"}}
            cache.write_text(json.dumps(original))
            dblp.write_text(json.dumps({"constrained prfs for nc1 in traditional groups|2018":
                                       {"doi":"10.1000/nc1", "v":"CRYPTO", "y":2018}}))
            with patch.object(resolver, "iter_dict_rows", return_value=iter(rows)), contextlib.redirect_stdout(io.StringIO()):
                report = resolver.resolve_links("unused", cache, dblp_path=dblp, max_api_requests=0, retry_index=True)
            after = json.loads(cache.read_text())
            self.assertEqual(report["rows"], 2)
            self.assertEqual(report["dblpResolved"], 1)
            self.assertEqual(len(report["dblpFuzzyUpgrades"]), 1)
            self.assertEqual(report["apiRequests"], 0)
            self.assertEqual(after[resolver.link_key(rows[0])]["u"], "https://doi.org/10.1000/nc1")
            self.assertEqual(after[resolver.link_key(rows[1])], original[resolver.link_key(rows[1])])
            self.assertFalse(resolver.needs_resolution(rows[2]))


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
        self.assertIn("Semantic Scholar", html)
        self.assertIn("DBLP (CC0)", html)
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
