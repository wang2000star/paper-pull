import contextlib
import gzip
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree.ElementTree import ParseError

from tools import build_dblp_index as dblp


class DblpIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dump = self.root / "dblp.xml.gz"
        self.out = self.root / "index.json"

    def write_dump(self, records):
        xml = '<?xml version="1.0" encoding="ISO-8859-1"?><!DOCTYPE dblp SYSTEM "dblp.dtd"><dblp>' + records + '</dblp>'
        self.dump.write_bytes(gzip.compress(xml.encode("iso-8859-1")))

    def test_stream_extracts_inline_titles_entities_links_and_venues(self):
        self.write_dump('''
        <inproceedings key="conf/crypto/Example24"><title>Secure <i>MPC</i> by Garc&iacute;a.</title><year>2024</year>
          <ee>https://example.org/paper.pdf</ee><ee>https://link.springer.com/chapter/10.1000/springer</ee>
          <ee>https://doi.org/10.1000/paper</ee></inproceedings>
        <article key="journals/tches/Example24"><title>Hardware result.</title><year>2024</year>
          <ee>https://tches.iacr.org/index.php/TCHES/article/view/1</ee></article>
        <inproceedings key="conf/ches/Old05"><title>Old result.</title><year>2005</year><ee>https://iacr.org/old.PDF</ee></inproceedings>
        <inproceedings key="conf/cryptography/Other24"><title>Not CRYPTO.</title><year>2024</year><ee>https://doi.org/10.1000/other</ee></inproceedings>
        <www key="homepages/Example"><title>Home page</title></www>''')
        with contextlib.redirect_stdout(io.StringIO()):
            report = dblp.build_index(self.dump, self.out)
        index = json.loads(self.out.read_text())
        self.assertEqual(index["secure mpc by garcía|2024"],
                         {"doi":"10.1000/paper", "ee":"https://doi.org/10.1000/paper", "v":"CRYPTO", "y":2024})
        self.assertEqual(index["hardware result|2024"]["v"], "TCHES")
        self.assertEqual(index["old result|2005"]["ee"], "")
        self.assertNotIn(".pdf", self.out.read_text().lower())
        self.assertEqual((report["recordsScanned"], report["iacrRecordsKept"], report["withDoi"]), (5, 3, 1))
        self.assertEqual(report["perVenue"]["CRYPTO"], 1)
        self.assertEqual(report["perVenue"]["TCHES"], 1)
        self.assertEqual(report["perVenue"]["RWC"], 0)

    def test_every_requested_venue_prefix_and_explicit_year_window(self):
        self.write_dump(''.join(f'<article key="{prefix}Record24"><title>{venue} result</title><year>2024</year></article>'
                                for prefix,venue in dblp.VENUES.items()) +
                        '<article key="conf/crypto/Old"><title>Old</title><year>1990</year></article>')
        index, stats = dblp.parse_dump(self.dump, start_year=2017, end_year=2026)
        self.assertEqual(len(index), 12)
        self.assertEqual(set(stats["perVenue"].values()), {1})
        self.assertEqual(stats["outsideYearRange"], 1)
        self.assertEqual(stats["recordsScanned"], 13)

    def test_link_ranking_normalizes_doi_and_rejects_downloads(self):
        self.assertEqual(dblp.best_landing(["https://example.org/page", "http://dx.doi.org/10.1000/paper"]),
                         {"doi":"10.1000/paper", "ee":"https://doi.org/10.1000/paper"})
        self.assertEqual(dblp.best_landing(["https://example.org/page", "https://link.springer.com/chapter/10.1000/chapter"]),
                         {"doi":"10.1000/chapter", "ee":"https://doi.org/10.1000/chapter"})
        for url in ["https://example.org/paper.PDF?x=1", "https://example.org/download/1", "https://example.org/file?format=pdf",
                    "https://link.springer.com/content/pdf/10.1000/paper.pdf", "javascript:bad()", "../paper.pdf"]:
            self.assertEqual(dblp.best_landing([url]), {"doi":"", "ee":""}, url)

    def test_normalization_duplicate_preference_is_deterministic(self):
        self.assertEqual(dblp.title_key(r"Secure $\unknown{MPC}$: Result!", 2024), "secure mpc result|2024")
        a = '<article key="conf/crypto/A"><title>Same title!</title><year>2024</year><ee>https://example.org/page</ee></article>'
        b = '<article key="conf/crypto/B"><title>SAME TITLE</title><year>2024</year><ee>https://doi.org/10.1000/doi</ee></article>'
        self.write_dump(a+b)
        first, stats = dblp.parse_dump(self.dump)
        self.write_dump(b+a)
        second, _ = dblp.parse_dump(self.dump)
        self.assertEqual(first, second)
        self.assertEqual(first["same title|2024"]["doi"], "10.1000/doi")
        self.assertEqual(stats["duplicateKeys"], 1)

    def test_incomplete_gzip_is_rejected_before_parsing_and_preserves_output(self):
        self.write_dump('<article key="conf/crypto/A"><title>Paper</title><year>2024</year></article>')
        self.dump.write_bytes(self.dump.read_bytes()[:-8])
        self.out.write_text("previous artifact")
        with patch.object(dblp, "parse_dump") as parse:
            with self.assertRaises((EOFError, gzip.BadGzipFile)):
                dblp.build_index(self.dump, self.out)
            parse.assert_not_called()
        self.assertEqual(self.out.read_text(), "previous artifact")

    def test_bad_xml_or_budget_does_not_replace_previous_artifact(self):
        self.dump.write_bytes(gzip.compress(b'<dblp><article>'))
        self.out.write_text("previous artifact")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ParseError):
            dblp.build_index(self.dump, self.out)
        self.assertEqual(self.out.read_text(), "previous artifact")
        with self.assertRaisesRegex(ValueError, "exceeds"):
            dblp.write_index(self.out, {"long title|2024":{"doi":"", "ee":"", "v":"CRYPTO", "y":2024}}, budget=10)
        self.assertEqual(self.out.read_text(), "previous artifact")
        self.assertEqual(list(self.root.glob(".dblp-index-*")), [])

    def response(self, status, content=b"", headers=None):
        response = Mock(status_code=status, headers=headers or {})
        response.iter_content.return_value = [content]
        return response

    def test_download_resumes_and_validates_before_promoting(self):
        content = gzip.compress(b'<dblp/>')
        self.dump.with_name(self.dump.name + ".part").write_bytes(content[:10])
        response = self.response(206, content[10:], {"Content-Range":f"bytes 10-{len(content)-1}/{len(content)}"})
        session = Mock(get=Mock(return_value=response))
        with contextlib.redirect_stdout(io.StringIO()):
            dblp.download_dump(self.dump, session=session)
        self.assertEqual(self.dump.read_bytes(), content)
        self.assertEqual(session.get.call_args.kwargs["headers"]["Range"], "bytes=10-")
        self.assertFalse(self.dump.with_name(self.dump.name + ".part").exists())
        self.assertEqual(dblp.verify_gzip(self.dump)["xmlBytes"], 7)

    def test_range_ignored_restarts_and_wrong_range_or_bad_gzip_cannot_promote(self):
        content = gzip.compress(b'<dblp/>')
        partial = self.dump.with_name(self.dump.name + ".part")
        partial.write_bytes(b"partial")
        response = self.response(200, content, {"Content-Length":str(len(content))})
        dblp.download_dump(self.dump, session=Mock(get=Mock(return_value=response)))
        self.assertEqual(self.dump.read_bytes(), content)
        self.dump.unlink()
        partial.write_bytes(b"partial")
        response = self.response(206, content, {"Content-Range":f"bytes 0-{len(content)-1}/{len(content)}"})
        with self.assertRaisesRegex(ValueError, "byte range"):
            dblp.download_dump(self.dump, session=Mock(get=Mock(return_value=response)))
        self.assertEqual(partial.read_bytes(), b"partial")
        self.assertFalse(self.dump.exists())
        response = self.response(200, b"not gzip", {"Content-Length":"8"})
        with self.assertRaises(gzip.BadGzipFile):
            dblp.download_dump(self.dump, session=Mock(get=Mock(return_value=response)))
        self.assertFalse(self.dump.exists())

    def test_complete_partial_file_with_416_is_verified_and_promoted(self):
        content = gzip.compress(b'<dblp/>')
        self.dump.with_name(self.dump.name + ".part").write_bytes(content)
        response = self.response(416, headers={"Content-Range":f"bytes */{len(content)}"})
        dblp.download_dump(self.dump, session=Mock(get=Mock(return_value=response)))
        self.assertEqual(self.dump.read_bytes(), content)
        response.iter_content.assert_not_called()


if __name__ == "__main__":
    unittest.main()
