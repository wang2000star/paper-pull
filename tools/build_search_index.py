#!/usr/bin/env python3
"""Build compact, year-sharded search metadata without changing the corpus."""
import argparse
import json
import re
import tempfile
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit

if __package__:
    from .shards import iter_dict_rows, read_header
else:
    from shards import iter_dict_rows, read_header

ROOT = Path(__file__).resolve().parents[1]
TOTAL_BUDGET = 7_000_000
FILE_BUDGET = 1_500_000
FIELDS = {"标题", "作者", "关键词", "PDF链接", "分类"}
PUBLICATION_FIELDS = {"title", "authors", "year", "venue", "publication_type", "url", "doi"}
LINK_PRIORITY = {kind: rank for rank, kind in enumerate(("eprint", "doi", "ojs", "index", "none"))}
LINK_KINDS = set(LINK_PRIORITY)


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
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    if parsed.hostname == "eprint.iacr.org":
        try:
            return landing_url(value)[0]
        except ValueError:
            return ""
    decoded = unquote(value).casefold()
    if ".pdf" in decoded or re.search(r"/(?:downloads?|bitstream|content/pdf)(?:/|$|[?#])", decoded):
        return ""
    if any(key.casefold() in {"download", "attachment"} or
           (key.casefold() in {"format", "type", "output"} and val.casefold() in {"pdf", "download"})
           for key, val in parse_qsl(parsed.query, keep_blank_values=True)):
        return ""
    return value


def doi_url(value):
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", (value or "").strip(), flags=re.I)
    if re.fullmatch(r"10\.[0-9]{4,9}/\S+", doi):
        return safe_url("https://doi.org/" + quote(doi, safe="/():-._~"))
    return ""


def ojs_url(value):
    parsed = urlsplit((value or "").strip())
    if parsed.hostname not in {"tches.iacr.org", "tosc.iacr.org"} or parsed.scheme not in {"http", "https"}:
        return ""
    match = re.fullmatch(r"(.*/article)/(?:view|download)/([0-9]+)(?:\.pdf)?(?:/.*)?", parsed.path, re.I)
    return f"https://{parsed.hostname}{match[1]}/view/{match[2]}" if match else ""


def link_key(row):
    return f"{normalized_title(clean_text(row['title']))}|{int(row['year'])}"


def publication_link(row, landings=None):
    raw = row.get("url", "").strip()
    parsed = urlsplit(raw)
    if parsed.hostname == "eprint.iacr.org":
        try:
            return {"u": landing_url(raw)[0], "k": "eprint"}
        except ValueError:
            pass
    cached = (landings or {}).get(link_key(row), {}) if "title" in row and "year" in row else {}
    doi = doi_url(row.get("doi", ""))
    if not doi and parsed.hostname in {"doi.org", "dx.doi.org"}:
        doi = doi_url(raw)
    if not doi and cached.get("k") == "doi":
        doi = doi_url(cached.get("u", ""))
    if doi:
        return {"u": doi, "k": "doi"}
    ojs = ojs_url(raw)
    if ojs:
        return {"u": ojs, "k": "ojs"}
    index = safe_url(cached.get("u", "")) if cached.get("k") == "index" else ""
    index = index or safe_url(row.get("source", ""))
    if not index and parsed.hostname and (parsed.hostname == "iacr.org" or parsed.hostname.endswith(".iacr.org")):
        index = safe_url(raw)
    return {"u": index, "k": "index"} if index else {"u": "", "k": "none"}


def publication_url(row, landings=None):
    return publication_link(row, landings)["u"]


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
        "version": 3, "count": len(records), "sourceCount": sum(input_counts.values()),
        "inputCounts": input_counts, "counts": {source: sum(p['s'] == source for p in records)
                                                 for source in ("eprint", "conference", "journal")},
        "dedupe": dedupe, "duplicatesRemoved": sum(input_counts.values()) - len(records),
        "years": years, "snippetLength": 0,
        "linkKinds": {kind: sum(p["lk"] == kind for p in records) for kind in sorted(LINK_KINDS)},
        "alsoLinkKinds": {kind: sum(p.get("alk") == kind for p in records) for kind in sorted(LINK_KINDS)},
        "files": [{"y": year, "file": f"{year}.json", "n": len(grouped[year])} for year in years],
    })
    return output


def build_index(source, outdir, *, publications=None, link_cache=None, total_budget=TOTAL_BUDGET, file_budget=FILE_BUDGET):
    if not FIELDS <= set(read_header(source)):
        raise ValueError("Source is missing required ePrint CSV columns")
    unique, urls = {}, set()
    landings = json.loads(Path(link_cache).read_text(encoding="utf-8")) if link_cache and Path(link_cache).exists() else {}
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
                       "u": url, "s": "eprint", "v": "ePrint", "lk": "eprint"}
    publication_keys = set()
    if publications is not None:
        if not PUBLICATION_FIELDS <= set(read_header(publications)):
            raise ValueError("Source is missing required publication CSV columns")
        rows = sorted(iter_dict_rows(publications), key=lambda r: (
            -int(r["year"]), 0 if publication_source(r) == "journal" else 1,
            r["venue"].casefold(), LINK_PRIORITY[publication_link(r, landings)["k"]], r["url"]))
        for row in rows:
            kind = publication_source(row)
            input_counts[kind] += 1
            title = clean_text(row["title"])
            key = normalized_title(title)
            if not key:
                raise ValueError("Publication has no normalized title")
            year = int(row["year"])
            label, link = venue_label(row), publication_link(row, landings)
            url = link["u"]
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
                        existing["alk"] = link["k"]
                continue
            unique[key] = {"t": title, "a": short_authors(row["authors"]), "y": year,
                           "c": "", "k": "", "u": url, "s": kind, "v": label, "lk": link["k"]}
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
    print(f"Link kinds: {manifest['linkKinds']}; also-published link kinds: {manifest['alsoLinkKinds']}")
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
    parser.add_argument("--link-cache", type=Path, default=ROOT / "data/link_landing.json", help="OpenAlex DOI / proceedings-index cache")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs/data", help="generated search JSON directory")
    args = parser.parse_args()
    build_index(args.source, args.out_dir, publications=args.publications, link_cache=args.link_cache)


if __name__ == "__main__":
    main()
