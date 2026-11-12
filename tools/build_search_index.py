#!/usr/bin/env python3
"""Build compact, year-sharded search metadata without changing the corpus."""
import argparse
import json
import re
import tempfile
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlsplit

if __package__:
    from .shards import iter_dict_rows, read_header
else:
    from shards import iter_dict_rows, read_header

ROOT = Path(__file__).resolve().parents[1]
TOTAL_BUDGET = 7_000_000
FILE_BUDGET = 1_500_000
FIELDS = {"标题", "作者", "关键词", "PDF链接", "分类"}
PUBLICATION_FIELDS = {"title", "authors", "year", "venue", "publication_type", "url", "doi"}


def normalized_title(title):
    text = unicodedata.normalize("NFKC", title).casefold().replace("$", "")
    text = re.sub(r"\\(?:mathsf|mathrm|mathbf|mathbb|mathcal|mathit|textsf|textrm|textbf|text|emph|operatorname)\*?", "", text)
    text = text.replace("\\", "")
    return " ".join("".join(char if char.isalnum() else " " for char in text).split())


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean_text(value):
    parser = PlainText()
    parser.feed(value or "")
    parser.close()
    return " ".join("".join(parser.parts).split())


def landing_url(value):
    parsed = urlsplit(value.strip())
    match = re.fullmatch(r"/([0-9]{4})/([0-9]+)(?:\.pdf)?/?", parsed.path)
    if parsed.scheme not in ("http", "https") or parsed.netloc != "eprint.iacr.org" or not match:
        raise ValueError(f"Invalid ePrint URL: {value!r}")
    year, number = match.groups()
    # ePrint uses at least three digits; normalize equivalent padded IDs.
    return f"https://eprint.iacr.org/{year}/{int(number):03d}", int(year)


def short_authors(value):
    authors = [clean_text(author) for author in re.split(r"[;；]", value or "")]
    authors = [author for author in authors if author]
    return "; ".join(authors[:4] + (["et al."] if len(authors) > 4 else []))


def encode(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def safe_url(value):
    value = (value or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        return ""
    if parsed.hostname == "eprint.iacr.org":
        try:
            return landing_url(value)[0]
        except ValueError:
            return ""
    return value if ".pdf" not in value.casefold() else ""


def publication_url(row):
    url = safe_url(row.get("url", ""))
    if url:
        return url
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", row.get("doi", "").strip(), flags=re.I)
    if re.fullmatch(r"10\.[0-9]{4,9}/\S+", doi):
        return safe_url("https://doi.org/" + quote(doi, safe="/():-._~"))
    return ""


def publication_source(row):
    return "journal" if row["publication_type"].casefold() == "journal" else "conference"


def venue_label(row):
    venue = clean_text(row["venue"])
    return venue if publication_source(row) == "journal" else f"{venue} {int(row['year'])}"


def payloads(records, input_counts, dedupe):
    years = sorted({record["y"] for record in records})
    grouped = {year: [] for year in years}
    for record in records:
        grouped[record["y"]].append(record)
    output = {}
    for year, items in grouped.items():
        items.sort(key=lambda item: (-item["y"], item["t"].casefold(), item["u"]))
        output[f"{year}.json"] = encode(items)
    output["manifest.json"] = encode({
        "version": 2, "count": len(records), "sourceCount": sum(input_counts.values()),
        "inputCounts": input_counts, "counts": {source: sum(p['s'] == source for p in records)
                                                 for source in ("eprint", "conference", "journal")},
        "dedupe": dedupe, "duplicatesRemoved": sum(input_counts.values()) - len(records),
        "years": years, "snippetLength": 0,
        "files": [{"y": year, "file": f"{year}.json", "n": len(grouped[year])} for year in years],
    })
    return output


def build_index(source, outdir, *, publications=None, total_budget=TOTAL_BUDGET, file_budget=FILE_BUDGET):
    if not FIELDS <= set(read_header(source)):
        raise ValueError("Source is missing required ePrint CSV columns")
    unique, urls = {}, set()
    input_counts = {"eprint": 0, "conference": 0, "journal": 0}
    dedupe = {"eprint": 0, "publicationVenueYear": 0, "eprintOverlap": 0, "publicationTitle": 0}
    for row in sorted(iter_dict_rows(source), key=lambda r: -landing_url(r["PDF链接"])[1]):
        input_counts["eprint"] += 1
        url, year = landing_url(row["PDF链接"])
        title = clean_text(row["标题"])
        key = normalized_title(title)
        if not key:
            raise ValueError(f"Missing normalized title for {url}")
        if url in urls or key in unique:
            dedupe["eprint"] += 1
            continue
        urls.add(url)
        unique[key] = {"t": title, "a": short_authors(row["作者"]), "y": year,
                       "c": clean_text(row["分类"]), "k": clean_text(row["关键词"]),
                       "u": url, "s": "eprint", "v": "ePrint"}
    publication_keys = set()
    if publications is not None:
        if not PUBLICATION_FIELDS <= set(read_header(publications)):
            raise ValueError("Source is missing required publication CSV columns")
        rows = sorted(iter_dict_rows(publications), key=lambda r: (
            -int(r["year"]), 0 if publication_source(r) == "journal" else 1,
            r["venue"].casefold(), not bool(publication_url(r)), r["url"]))
        for row in rows:
            kind = publication_source(row)
            input_counts[kind] += 1
            title = clean_text(row["title"])
            key = normalized_title(title)
            if not key:
                raise ValueError("Publication has no normalized title")
            year = int(row["year"])
            label, url = venue_label(row), publication_url(row)
            publication_key = (key, clean_text(row["venue"]).casefold(), year)
            if publication_key in publication_keys:
                dedupe["publicationVenueYear"] += 1
                continue
            publication_keys.add(publication_key)
            if key in unique:
                existing = unique[key]
                dedupe["eprintOverlap" if existing["s"] == "eprint" else "publicationTitle"] += 1
                if "al" not in existing and (existing["s"] == "eprint" or existing["v"] != label):
                    if existing["s"] == "eprint":
                        existing["v"] = label
                    existing["al"] = f"also published at {label}"
                    if url:
                        existing["au"] = url
                continue
            unique[key] = {"t": title, "a": short_authors(row["authors"]), "y": year,
                           "c": "", "k": "", "u": url, "s": kind, "v": label}
    if not unique:
        raise ValueError("Source contains no papers")
    output = payloads(list(unique.values()), input_counts, dedupe)
    total_size = sum(map(len, output.values()))
    if total_size > total_budget or any(len(data) > file_budget for data in output.values()):
        raise ValueError(f"Payload exceeds budget without snippets: {total_size:,} bytes total; "
                         f"largest file {max(map(len, output.values())):,} bytes")
    if any(b".pdf" in data.lower() for data in output.values()):
        raise ValueError("Search payload must not contain a PDF reference")
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".search-", dir=outdir) as staging:
        for name, data in output.items():
            path = Path(staging) / name
            path.write_bytes(data)
        for name, data in output.items():
            target = outdir / name
            if not target.exists() or target.read_bytes() != data:
                (Path(staging) / name).replace(target)
        for target in outdir.glob("*.json"):
            if re.fullmatch(r"[0-9]{4}\.json", target.name) and target.name not in output:
                target.unlink()
    manifest = json.loads(output["manifest.json"])
    for shard in manifest["files"]:
        print(f"  {shard['file']}: {shard['n']:,} records; {len(output[shard['file']]):,} bytes")
    print(f"  manifest.json: {len(output['manifest.json']):,} bytes")
    print(f"Input sources: {input_counts}; indexed sources: {manifest['counts']}")
    print(f"{input_counts['eprint']:,} eprint + {input_counts['conference'] + input_counts['journal']:,} publications "
          f"− {manifest['duplicatesRemoved']:,} duplicates = {len(unique):,} unique titles; dedupe: {dedupe}")
    print(f"No abstract snippets; total payload: {total_size:,} bytes "
          f"(budget {total_budget:,}); largest file: {max(map(len, output.values())):,} bytes "
          f"(budget {file_budget:,})")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/papers_merged", help="source CSV shard directory")
    parser.add_argument("--publications", type=Path, default=ROOT / "data/iacr_publications", help="conference/journal CSV shard directory")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs/data", help="generated search JSON directory")
    args = parser.parse_args()
    build_index(args.source, args.out_dir, publications=args.publications)


if __name__ == "__main__":
    main()
