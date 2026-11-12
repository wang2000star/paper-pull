#!/usr/bin/env python3
"""Resolve archival paper links through OpenAlex; cache safe page fallbacks."""
import argparse
import json
import os
import tempfile
import time
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

import requests

if __package__:
    from .build_search_index import ROOT, clean_text, doi_url, landing_url, link_key, normalized_title, ojs_url, safe_url
    from .shards import iter_dict_rows
else:
    from build_search_index import ROOT, clean_text, doi_url, landing_url, link_key, normalized_title, ojs_url, safe_url
    from shards import iter_dict_rows

USER_AGENT = "paper-pull-link-resolver/1.0 (mailto=wangfangzhen23@mails.ucas.ac.cn)"
CACHE_BUDGET = 1_000_000


class QuotaExhausted(RuntimeError):
    pass


def needs_resolution(row):
    raw = row.get("url", "")
    if ".pdf" not in raw.casefold() or ojs_url(raw):
        return False
    if urlsplit(raw).hostname == "eprint.iacr.org":
        try:
            landing_url(raw)
            return False
        except ValueError:
            pass
    return True


def choose_doi(row, hits):
    title = normalized_title(clean_text(row["title"]))
    candidates = []
    for hit in hits:
        year = hit.get("publication_year")
        url = doi_url(hit.get("doi", ""))
        if not isinstance(year, int) or abs(year - int(row["year"])) > 1 or not url:
            continue
        candidate = normalized_title(clean_text(hit.get("title", "")))
        similarity = SequenceMatcher(None, title, candidate, autojunk=False).ratio()
        if similarity >= .92:
            candidates.append((similarity, -abs(year - int(row["year"])), url))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    best = candidates[0]
    if len({item[2] for item in candidates if item[:2] == best[:2]}) != 1:
        return ""  # Equally good but conflicting DOIs are not confident matches.
    return best[2]


def lookup(session, row, delay, last_request):
    for attempt in range(3):
        time.sleep(max(0, delay - (time.monotonic() - last_request[0])))
        last_request[0] = time.monotonic()
        response = session.get("https://api.openalex.org/works", params={
            "filter": "title.search:" + clean_text(row["title"]),
            "per-page": 5, "select": "doi,title,publication_year",
        }, timeout=30)
        if response.status_code == 429:
            body = response.json()
            if body.get("dailyRemainingUsd") == 0 or "insufficient budget" in body.get("message", "").casefold():
                raise QuotaExhausted("OpenAlex shared daily budget exhausted; no further API requests in this run")
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < 2:
                retry_after = response.headers.get("Retry-After", "")
                time.sleep(min(30, float(retry_after)) if retry_after.isdigit() else 2 ** (attempt + 1))
                continue
        response.raise_for_status()
        return choose_doi(row, response.json().get("results", []))
    return ""


def valid_entry(entry):
    if not isinstance(entry, dict) or entry.get("k") not in {"doi", "index", "none"}:
        return False
    if entry["k"] == "none":
        return entry.get("u") == ""
    return bool(safe_url(entry.get("u", ""))) and (entry["k"] != "doi" or entry["u"].startswith("https://doi.org/"))


def save_cache(path, cache):
    data = (json.dumps(cache, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) >= CACHE_BUDGET:
        raise ValueError(f"Link cache exceeds {CACHE_BUDGET:,} byte budget: {len(data):,}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".link-landing-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def resolve_links(source, cache_path, *, delay=.5, retry_fallbacks=False, session=None):
    cache_path = Path(cache_path)
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    rows = [row for row in iter_dict_rows(source) if needs_resolution(row)]
    session = session or requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    if os.environ.get("OPENALEX_API_KEY"):
        session.headers["Authorization"] = "Bearer " + os.environ["OPENALEX_API_KEY"]
    counts, seen = Counter(), set()
    quota_exhausted = False
    last_request = [0.0]
    errors = 0
    for i, row in enumerate(rows, 1):
        key = link_key(row)
        cached = cache.get(key)
        if key not in seen and (not valid_entry(cached) or (retry_fallbacks and cached["k"] != "doi")):
            url = ""
            if not quota_exhausted:
                try:
                    url = lookup(session, row, max(.5, delay), last_request)
                except QuotaExhausted as error:
                    quota_exhausted = True
                    errors += 1
                    print(f"API LIMIT: {error}. Writing index-page fallbacks; retry with --retry-fallbacks when access is available.", flush=True)
                except (requests.RequestException, ValueError) as error:
                    errors += 1
                    print(f"Lookup unavailable ({type(error).__name__}) for row {i}; preserving safe fallback", flush=True)
            fallback = safe_url(row.get("source", ""))
            cache[key] = {"u": url, "k": "doi"} if url else (
                {"u": fallback, "k": "index"} if fallback else {"u": "", "k": "none"})
            save_cache(cache_path, cache)
        seen.add(key)
        counts[cache[key]["k"]] += 1
        if i % 100 == 0 or i == len(rows):
            print(f"Processed {i:,}/{len(rows):,}: DOI {counts['doi']:,}; index {counts['index']:,}; unmatched {counts['none']:,}", flush=True)
    save_cache(cache_path, cache)
    report = {"rows": len(rows), "doi": counts["doi"], "index": counts["index"], "unmatched": counts["none"],
              "cacheEntries": len(cache), "cacheBytes": cache_path.stat().st_size,
              "quotaExhausted": quota_exhausted, "lookupErrors": errors}
    print("Resolution summary: " + json.dumps(report, sort_keys=True), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/iacr_publications")
    parser.add_argument("--cache", type=Path, default=ROOT / "data/link_landing.json")
    parser.add_argument("--delay", type=float, default=.5, help="minimum seconds between API calls (at least 0.5)")
    parser.add_argument("--retry-fallbacks", action="store_true", help="retry cached index/unmatched entries, e.g. after quota resets")
    args = parser.parse_args()
    resolve_links(args.source, args.cache, delay=args.delay, retry_fallbacks=args.retry_fallbacks)


if __name__ == "__main__":
    main()
