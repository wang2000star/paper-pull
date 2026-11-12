#!/usr/bin/env python3
"""Build compact, year-sharded search metadata without changing the corpus."""
import argparse
import json
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from .shards import iter_dict_rows, read_header
else:
    from shards import iter_dict_rows, read_header

ROOT = Path(__file__).resolve().parents[1]
TOTAL_BUDGET = 6_000_000
FILE_BUDGET = 1_500_000
FIELDS = {"标题", "作者", "摘要", "关键词", "PDF链接", "分类"}


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


def payloads(records, source_count, snippet_length):
    years = sorted({record["y"] for record in records})
    grouped = {year: [] for year in years}
    for record in records:
        item = {key: value for key, value in record.items() if key != "s"}
        if snippet_length:
            snippet = record["s"]
            item["s"] = (snippet if len(snippet) <= snippet_length
                         else snippet[:snippet_length - 1].rstrip() + "…")
        grouped[item["y"]].append(item)
    output = {}
    for year, items in grouped.items():
        items.sort(key=lambda item: (-item["y"], item["t"].casefold(), item["u"]))
        output[f"{year}.json"] = encode(items)
    output["manifest.json"] = encode({
        "version": 1, "count": len(records), "sourceCount": source_count,
        "years": years, "snippetLength": snippet_length,
        "files": [{"y": year, "file": f"{year}.json", "n": len(grouped[year])} for year in years],
    })
    return output


def build_index(source, outdir, *, total_budget=TOTAL_BUDGET, file_budget=FILE_BUDGET):
    if not FIELDS <= set(read_header(source)):
        raise ValueError("Source is missing required ePrint CSV columns")
    unique = {}
    source_count = 0
    for row in iter_dict_rows(source):
        source_count += 1
        url, year = landing_url(row["PDF链接"])
        if url in unique:
            continue
        title = clean_text(row["标题"])
        if not title:
            raise ValueError(f"Missing title for {url}")
        unique[url] = {"t": title, "a": short_authors(row["作者"]), "y": year,
                       "c": clean_text(row["分类"]), "k": clean_text(row["关键词"]),
                       "u": url, "s": clean_text(row["摘要"])}
    if not unique:
        raise ValueError("Source contains no papers")
    for snippet_length in (180, 120, 0):
        output = payloads(list(unique.values()), source_count, snippet_length)
        total_size = sum(map(len, output.values()))
        if total_size <= total_budget and all(len(data) <= file_budget for data in output.values()):
            break
        print(f"Snippet limit {snippet_length}: {total_size:,} bytes total; "
              f"largest file {max(map(len, output.values())):,} bytes; exceeds budget")
    else:
        raise ValueError(f"Payload exceeds budget even without snippets: {total_size:,} bytes total; "
                         f"largest file {max(map(len, output.values())):,} bytes")
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
    print(f"Source: {source_count:,} records; deduplicated: {len(unique):,}; "
          f"duplicates removed: {source_count - len(unique):,}")
    print(f"Snippet limit: {snippet_length} characters; total payload: {total_size:,} bytes "
          f"(budget {total_budget:,}); largest file: {max(map(len, output.values())):,} bytes "
          f"(budget {file_budget:,})")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/papers_merged", help="source CSV shard directory")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs/data", help="generated search JSON directory")
    args = parser.parse_args()
    build_index(args.source, args.out_dir)


if __name__ == "__main__":
    main()
