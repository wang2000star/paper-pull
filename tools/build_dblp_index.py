#!/usr/bin/env python3
"""Build a compact IACR title/year landing-link index from DBLP's bulk dump."""
import argparse
import gzip
import json
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from html.entities import name2codepoint
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

if __package__:
    from .build_search_index import ROOT, doi_url, normalized_title, safe_url
    from .resolve_links import USER_AGENT, query_title
else:
    from build_search_index import ROOT, doi_url, normalized_title, safe_url
    from resolve_links import USER_AGENT, query_title

DUMP_URL = "https://dblp.org/xml/dblp.xml.gz"
INDEX_BUDGET = 4_000_000
VENUES = {
    "conf/crypto/":"CRYPTO", "conf/eurocrypt/":"EUROCRYPT", "conf/asiacrypt/":"ASIACRYPT",
    "conf/pkc/":"PKC", "conf/tcc/":"TCC", "conf/ches/":"CHES", "conf/fse/":"FSE", "conf/rwc/":"RWC",
    "journals/tches/":"TCHES", "journals/tosc/":"ToSC", "journals/joc/":"JoC", "journals/cic/":"CiC",
}


def title_key(title, year):
    return f"{normalized_title(query_title(title))}|{int(year)}"


def verify_gzip(path):
    """Consume through the footer to check all members' CRC and uncompressed size."""
    started, size = time.monotonic(), 0
    with gzip.open(path, "rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            size += len(block)
    return {"gzipBytes":Path(path).stat().st_size, "xmlBytes":size,
            "gzipVerifySeconds":round(time.monotonic() - started, 3)}


def download_dump(path, *, url=DUMP_URL, session=None):
    """Resume a partial download, validating HTTP ranges and gzip before promotion."""
    path = Path(path)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"User-Agent":USER_AGENT, "Accept-Encoding":"identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    session = session or requests.Session()
    response = session.get(url, headers=headers, stream=True, timeout=(30, 120))
    try:
        if response.status_code == 416:
            match = re.fullmatch(r"bytes \*/([0-9]+)", response.headers.get("Content-Range", ""))
            if not match or int(match[1]) != offset:
                response.raise_for_status()
            expected = offset
        else:
            response.raise_for_status()
            if response.status_code == 206:
                match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", response.headers.get("Content-Range", ""))
                if not match or int(match[1]) != offset:
                    raise ValueError("DBLP response does not match the requested byte range")
                expected, mode = int(match[3]), "ab"
            elif response.status_code == 200:
                expected, offset, mode = int(response.headers.get("Content-Length", 0)), 0, "wb"
            else:
                raise ValueError(f"Unexpected DBLP download status: {response.status_code}")
            received, next_report = offset, offset + 64 * 1024 * 1024
            with partial.open(mode) as stream:
                for block in response.iter_content(chunk_size=1024 * 1024):
                    if block:
                        stream.write(block)
                        received += len(block)
                        if received >= next_report:
                            print(f"Downloaded {received:,}" + (f" / {expected:,}" if expected else "") + " bytes", flush=True)
                            next_report = received + 64 * 1024 * 1024
            if expected and received != expected:
                raise ValueError(f"Incomplete DBLP download: {received:,} / {expected:,} bytes; rerun to resume")
    finally:
        response.close()
    verify_gzip(partial)
    partial.replace(path)
    return path


def best_landing(values):
    candidates = []
    for raw in values:
        raw = (raw or "").strip()
        parsed = urlsplit(raw)
        # DBLP also contains legacy HTTP DOI links. Canonicalize them to HTTPS.
        doi = doi_url(raw) if parsed.hostname in {"doi.org", "dx.doi.org"} else ""
        if not doi and parsed.hostname in {"link.springer.com", "www.link.springer.com"}:
            match = re.match(r"/(?:chapter|article|book)/(.+)", unquote(parsed.path))
            doi = doi_url(match[1]) if match else ""
        if doi:
            candidates.append((0 if parsed.hostname in {"doi.org", "dx.doi.org"} else 1, doi, doi.removeprefix("https://doi.org/")))
            continue
        url = safe_url(raw)
        if url:
            candidates.append((1 if parsed.hostname == "link.springer.com" else 2, url, ""))
    if not candidates:
        return {"doi":"", "ee":""}
    _, url, doi = min(candidates)
    return {"doi":doi, "ee":url}


def parse_dump(path, *, start_year=None, end_year=None):
    """Parse records while retaining only the small IACR result map in memory."""
    started, scanned, kept, skipped_years, duplicates = time.monotonic(), 0, 0, 0, 0
    index, counts = {}, Counter()
    parser = ET.XMLParser()
    # DBLP's external DTD declares Latin-1 named entities. ElementTree does not
    # fetch that DTD; this local HTML entity map covers all its declarations.
    parser.entity.update({name:chr(codepoint) for name, codepoint in name2codepoint.items()})
    with gzip.open(path, "rb") as stream:
        depth, root = 0, None
        for event, element in ET.iterparse(stream, events=("start", "end"), parser=parser):
            if event == "start":
                depth += 1
                if root is None:
                    root = element
                continue
            if depth == 2:
                scanned += 1
                key = element.get("key", "")
                prefix = "/".join(key.split("/")[:2]) + "/"
                venue = VENUES.get(prefix)
                if venue:
                    year_text = element.findtext("year", "")
                    title_element = element.find("title")
                    title = "" if title_element is None else "".join(title_element.itertext()).strip()
                    if title and year_text.isdigit():
                        year = int(year_text)
                        if (start_year is not None and year < start_year) or (end_year is not None and year > end_year):
                            skipped_years += 1
                        else:
                            kept += 1
                            counts[venue] += 1
                            landing = best_landing("".join(ee.itertext()) for ee in element.findall("ee"))
                            value = {**landing, "v":venue, "y":year}
                            normalized = title_key(title, year)
                            previous = index.get(normalized)
                            if previous:
                                duplicates += 1
                            # Prefer DOI > other landing > empty; ties are deterministic.
                            rank = lambda item: (0 if item["doi"] else 1 if item["ee"] else 2, item["ee"], item["v"])
                            if not previous or rank(value) < rank(previous):
                                index[normalized] = value
                element.clear()
                root.clear()  # Clearing only the record would leave millions of root children.
                if scanned % 1_000_000 == 0:
                    print(f"Scanned {scanned:,} records; kept {kept:,} IACR records", flush=True)
            depth -= 1
    stats = {"recordsScanned":scanned, "iacrRecordsKept":kept, "indexEntries":len(index),
             "withDoi":sum(bool(value["doi"]) for value in index.values()),
             "withLanding":sum(bool(value["ee"]) for value in index.values()),
             "perVenue":{venue:counts[venue] for venue in VENUES.values()}, "duplicateKeys":duplicates,
             "outsideYearRange":skipped_years, "startYear":start_year, "endYear":end_year,
             "parseSeconds":round(time.monotonic() - started, 3)}
    return index, stats


def write_index(path, index, *, budget=INDEX_BUDGET):
    data = (json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) >= budget:
        raise ValueError(f"DBLP index exceeds {budget:,} byte budget: {len(data):,} bytes; use an explicit corpus year range")
    if b".pdf" in data.lower():
        raise ValueError("DBLP index must not contain a PDF reference")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".dblp-index-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return len(data)


def build_index(dump, output, *, start_year=None, end_year=None, budget=INDEX_BUDGET):
    validation = verify_gzip(dump)
    print("Gzip verified: " + json.dumps(validation, sort_keys=True), flush=True)
    index, stats = parse_dump(dump, start_year=start_year, end_year=end_year)
    print("DBLP parse summary: " + json.dumps(stats, sort_keys=True), flush=True)
    size = write_index(output, index, budget=budget)
    report = {**validation, **stats, "indexBytes":size}
    print(f"Wrote {size:,} bytes to {output}", flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", type=Path, help="reuse a complete local gzip dump instead of downloading")
    parser.add_argument("--out", type=Path, default=ROOT / "data/dblp_dois.json")
    parser.add_argument("--start-year", type=int, help="explicitly scope the artifact to this first publication year")
    parser.add_argument("--end-year", type=int, help="explicitly scope the artifact to this last publication year")
    args = parser.parse_args()
    if args.start_year is not None and args.end_year is not None and args.start_year > args.end_year:
        parser.error("--start-year must not exceed --end-year")
    dump = args.dump if args.dump else download_dump(ROOT / "var/dblp.xml.gz")
    if not dump.is_file():
        parser.error(f"Dump does not exist: {dump}")
    build_index(dump, args.out, start_year=args.start_year, end_year=args.end_year)


if __name__ == "__main__":
    main()
