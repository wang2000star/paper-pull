import io
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import collect_iacr_publications as publications
import fetch_iacr
import fetch_oai_bulk as bulk
import merge_papers
from tools.shards import append_rows, iter_dict_rows, write_shards


def eprint(year, number, title, keywords="", category=""):
    return dict(zip(merge_papers.HEADER, [title, "author", "abstract", keywords,
                                        f"https://eprint.iacr.org/{year}/{number}.pdf", category]))


def page(rows, token=""):
    root = ET.Element(f"{{{bulk.NS['oai']}}}OAI-PMH")
    records = ET.SubElement(root, f"{{{bulk.NS['oai']}}}ListRecords")
    for row in rows:
        rec = ET.SubElement(records, f"{{{bulk.NS['oai']}}}record")
        hdr = ET.SubElement(rec, f"{{{bulk.NS['oai']}}}header")
        identifier = ET.SubElement(hdr, f"{{{bulk.NS['oai']}}}identifier")
        identifier.text = "oai:eprint.iacr.org:" + merge_papers.pid_of(row)
        metadata = ET.SubElement(rec, "metadata")
        for key, tag in [("标题", "title"), ("作者", "creator"), ("摘要", "description")]:
            ET.SubElement(metadata, f"{{{bulk.NS['dc']}}}{tag}").text = row[key]
    ET.SubElement(records, f"{{{bulk.NS['oai']}}}resumptionToken").text = token
    return root


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def call(self, module, args):
        with mock.patch.object(sys, "argv", [module.__file__, *map(str, args)]), mock.patch("sys.stdout", new=io.StringIO()):
            module.main()

    def test_merge_refreshes_one_corpus_and_preserves_other_years(self):
        raw, crawl, merged = [self.root / p for p in ("bulk", "crawl", "merged")]
        write_shards(raw, bulk.CSV_HEADER, [eprint(2026, 1, "OAI title", category="category")])
        write_shards(crawl, bulk.CSV_HEADER, [eprint(2026, 1, "Crawl title", "new keyword"),
                                             eprint(2026, 2, "Crawl only", "other")])
        write_shards(merged, merge_papers.HEADER, [eprint(2025, 1, "Untouched"),
                                                 eprint(2026, 1, "Old title", "old keyword")])
        old = merged / "2025.csv"
        before = old.read_bytes(), old.stat().st_mtime_ns
        self.call(merge_papers, ["--bulk", raw, "--crawl", crawl, "--out-dir", merged,
                                "--year", "2026"])
        rows = list(iter_dict_rows(merged / "2026.csv"))
        self.assertEqual([r["标题"] for r in rows], ["OAI title", "Crawl only"])
        self.assertEqual(rows[0]["关键词"], "new keyword")
        self.assertEqual(rows[0]["分类"], "category")
        self.assertEqual((old.read_bytes(), old.stat().st_mtime_ns), before)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"bulk", "crawl", "merged"})

    def test_merge_defaults_write_only_merged_corpus(self):
        data = self.root / "data"
        write_shards(data / "papers_bulk", bulk.CSV_HEADER, [eprint(2026, 1, "OAI title")])
        row = eprint(2026, 1, "Crawl title", "keyword")
        row.pop("分类")
        write_shards(data / "papers", fetch_iacr.CSV_HEADER, [row])
        subprocess.run([sys.executable, "-B", str(Path(merge_papers.__file__).resolve())],
                       cwd=self.root, check=True, capture_output=True)
        self.assertEqual({p.name for p in data.iterdir()}, {"papers", "papers_bulk", "papers_merged"})
        rows = list(iter_dict_rows(data / "papers_merged"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["标题"], "OAI title")
        self.assertEqual(rows[0]["关键词"], "keyword")

    def test_landing_crawler_skips_existing_ids_and_appends_to_year(self):
        directory = self.root / "crawl"
        row = eprint(2025, 1, "Already fetched")
        row.pop("分类")
        append_rows(directory, fetch_iacr.CSV_HEADER, [row])
        new = eprint(2026, 1, "Fresh")
        new.pop("分类")
        with mock.patch.object(fetch_iacr, "parse_landing", return_value=new) as parse, mock.patch.object(fetch_iacr.time, "sleep"):
            self.call(fetch_iacr, ["2025/1", "2026/1", "--data-dir", directory, "--fail-log", "", "--delay", "0"])
        parse.assert_called_once()
        self.assertEqual(fetch_iacr.load_done(directory), {"2025/1", "2026/1"})
        self.assertEqual(list(iter_dict_rows(directory / "2026.csv"))[0]["标题"], "Fresh")

    def test_oai_restart_year_preserves_others_and_resume_deduplicates_replay(self):
        directory, state = self.root / "bulk", self.root / "state.txt"
        write_shards(directory, bulk.CSV_HEADER, [eprint(2025, 1, "Keep"), eprint(2026, 9, "Replace")])
        other = directory / "2025.csv"
        before = other.read_bytes(), other.stat().st_mtime_ns
        args = ["--year", "2026", "--until", "2026-11-11", "--out-dir", directory, "--state", state, "--delay", "0"]
        first = eprint(2026, 1, "First")
        with mock.patch.object(bulk, "get", side_effect=[page([eprint(2025, 2, "Filter"), first], "next"), RuntimeError("interrupted")]), mock.patch.object(bulk.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.call(bulk, [*args, "--restart"])
        self.assertTrue(state.exists())
        with mock.patch.object(bulk, "get", return_value=page([first, eprint(2026, 2, "Second")])) as get:
            self.call(bulk, args)
        self.assertEqual(get.call_args.args[1], {"verb": "ListRecords", "resumptionToken": "next"})
        self.assertFalse(state.exists())
        self.assertEqual([r["标题"] for r in iter_dict_rows(directory / "2026.csv")], ["First", "Second"])
        self.assertEqual((other.read_bytes(), other.stat().st_mtime_ns), before)

    def test_oai_rejects_state_for_other_output_before_mutation(self):
        import json
        state = self.root / "state.txt"
        state.write_text(json.dumps({"query": {}, "token": "wrong"}))
        with mock.patch.object(bulk, "get") as get, mock.patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                self.call(bulk, ["--state", state, "--out-dir", self.root / "output"])
        get.assert_not_called()
        self.assertFalse((self.root / "output").exists())

    def test_oai_empty_year_is_a_readable_header_only_shard(self):
        directory = self.root / "bulk"
        root = ET.Element(f"{{{bulk.NS['oai']}}}OAI-PMH")
        ET.SubElement(root, f"{{{bulk.NS['oai']}}}error", code="noRecordsMatch")
        with mock.patch.object(bulk, "get", return_value=root):
            self.call(bulk, ["--year", "2026", "--out-dir", directory, "--restart"])
        self.assertEqual(list(iter_dict_rows(directory)), [])
        self.assertTrue((directory / "2026.csv").is_file())

    def test_publication_collector_writes_only_selected_year(self):
        directory = self.root / "publications"
        old = {key: "" for key in publications.HEADER}
        old.update(title="Old", year="2025", venue="CRYPTO")
        write_shards(directory, publications.HEADER, [old])
        before = (directory / "2025.csv").read_bytes()
        new = {**old, "title": "New", "year": "2026"}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(publications, "START_YEAR", 2017))
            stack.enter_context(mock.patch.object(publications, "END_YEAR", 2026))
            stack.enter_context(mock.patch.object(publications, "YEAR_RE", publications.YEAR_RE))
            for name in ("collect_archive", "collect_crossref_proceedings", "collect_rwc", "collect_ojs", "collect_crossref_joc", "collect_cic"):
                stack.enter_context(mock.patch.object(publications, name, return_value=[old, new] if name == "collect_archive" else []))
            self.call(publications, ["--year", "2026", "--out-dir", directory])
        self.assertEqual((directory / "2025.csv").read_bytes(), before)
        self.assertEqual(list(iter_dict_rows(directory / "2026.csv"))[0]["title"], "New")


if __name__ == "__main__":
    unittest.main()
