#!/usr/bin/env python3
"""Resolve archival links via OpenAlex/Semantic Scholar, preserving safe fallbacks."""
import argparse
import json
import os
import re
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests

if __package__:
    from .build_search_index import ROOT, clean_text, doi_url, landing_url, link_key, normalized_title, ojs_url, safe_url
    from .shards import iter_dict_rows
else:
    from build_search_index import ROOT, clean_text, doi_url, landing_url, link_key, normalized_title, ojs_url, safe_url
    from shards import iter_dict_rows

CONTACT = "wangfangzhen23@mails.ucas.ac.cn"
USER_AGENT = f"paper-pull-link-resolver/2.0 (mailto={CONTACT})"
CACHE_BUDGET = 1_000_000
MAX_ATTEMPTS = 6
RATE_LIMIT_STREAK = 8
MAX_INLINE_WAIT = 60  # Longer Retry-After values defer this source, without ignoring the deadline.
DEFAULT_API_BUDGET = 60


class LookupUnavailable(RuntimeError):
    pass


class RequestBudget:
    """A shared cap on HTTP attempts, including retries, across both providers."""
    def __init__(self, limit):
        if limit < 0:
            raise ValueError("API request budget must be nonnegative")
        self.limit = limit
        self.used = 0

    @property
    def available(self):
        return self.used < self.limit

    def consume(self):
        if not self.available:
            raise LookupUnavailable("API request budget exhausted")
        self.used += 1


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


def query_title(value):
    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^]]*\])?", " ", clean_text(value))
    return " ".join(re.sub(r"[$}{]", " ", text).split())


def choose_doi(row, hits):
    title = normalized_title(query_title(row["title"]))
    tokens = set(title.split())
    candidates = []
    if not tokens:
        return ""
    for hit in hits:
        year = hit.get("publication_year", hit.get("year"))
        url = doi_url(hit.get("doi") or (hit.get("externalIds") or {}).get("DOI", ""))
        if not isinstance(year, int) or abs(year - int(row["year"])) > 1 or not url:
            continue
        candidate = normalized_title(query_title(hit.get("title", "")))
        other = set(candidate.split())
        similarity = SequenceMatcher(None, title, candidate, autojunk=False).ratio()
        jaccard = len(tokens & other) / len(tokens | other)
        if similarity >= .90 or jaccard >= .85:
            candidates.append((max(similarity, jaccard), similarity, jaccard, -abs(year - int(row["year"])), url))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    best = candidates[0]
    if len({item[-1] for item in candidates if item[:-1] == best[:-1]}) != 1:
        return ""  # Equally good but conflicting DOIs are not confident matches.
    return best[-1]


def retry_after(response):
    value = response.headers.get("Retry-After", "")
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0


class Provider:
    def __init__(self, name, *, session=None, delay=.35, retry_at=0, budget=None):
        self.name = name
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.delay = max(.35 if name == "openalex" else .6, delay)
        self.last_request = 0
        self.retry_at = retry_at
        self.limited_keys = set()
        self.disabled = False
        self.attempted = set()
        self.stats = Counter()
        self.budget = budget

    @property
    def available(self):
        return not self.disabled and time.time() >= self.retry_at and (self.budget is None or self.budget.available)

    def lookup(self, row):
        if not self.available:
            raise LookupUnavailable(f"{self.name} is deferred or rate-limited")
        title = query_title(row["title"])
        headers = {}
        if self.name == "openalex":
            endpoint = "https://api.openalex.org/works"
            params = {"filter":"title.search:" + title, "per-page":5,
                      "select":"doi,title,publication_year", "mailto":CONTACT}
            if os.environ.get("OPENALEX_API_KEY"):
                headers["Authorization"] = "Bearer " + os.environ["OPENALEX_API_KEY"]
        else:
            endpoint = "https://api.semanticscholar.org/graph/v1/paper/search"
            params = {"query":title, "limit":5, "fields":"title,year,externalIds"}
            if os.environ.get("SEMANTIC_SCHOLAR_API_KEY"):
                headers["x-api-key"] = os.environ["SEMANTIC_SCHOLAR_API_KEY"]
        key = link_key(row)
        last_error = "lookup unavailable"
        for attempt in range(MAX_ATTEMPTS):
            if self.budget is not None:
                self.budget.consume()
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            self.attempted.add(key)
            self.stats["httpRequests"] += 1
            wait = 2 ** (attempt + 1)
            try:
                response = self.session.get(endpoint, params=params, headers=headers, timeout=30)
            except requests.RequestException as error:
                last_error = type(error).__name__
                self.stats["transportErrors"] += 1
            else:
                status = response.status_code
                last_error = f"HTTP {status}"
                if status == 429:
                    self.stats["http429"] += 1
                    self.limited_keys.add(key)  # Retries of one query count only once.
                    server_wait = retry_after(response)
                    wait = max(wait, server_wait)
                    if server_wait > MAX_INLINE_WAIT:
                        self.retry_at = time.time() + server_wait
                        print(f"DEFER {self.name}: HTTP 429, Retry-After {server_wait:.0f}s; continuing with other source", flush=True)
                        raise LookupUnavailable(f"{self.name}: Retry-After until {self.retry_at:.0f}")
                    if attempt + 1 == MAX_ATTEMPTS and server_wait:
                        time.sleep(server_wait)  # The next title must respect the last response too.
                else:
                    self.limited_keys.clear()  # Any non-429 response breaks the consecutive streak.
                    if status < 400:
                        hits = response.json().get("results" if self.name == "openalex" else "data", [])
                        url = choose_doi(row, hits)
                        self.stats["resolved" if url else "misses"] += 1
                        return url
                    if status < 500:
                        self.stats["httpErrors"] += 1
                        raise LookupUnavailable(f"{self.name}: HTTP {status}")
            if attempt + 1 < MAX_ATTEMPTS:
                print(f"RETRY {self.name}: {last_error}; attempt {attempt + 2}/{MAX_ATTEMPTS} in {wait:.1f}s", flush=True)
                time.sleep(wait)
        self.stats["exhaustedRequests"] += 1
        if len(self.limited_keys) >= RATE_LIMIT_STREAK:
            self.disabled = True
            print(f"RATE LIMIT {self.name}: {len(self.limited_keys)} consecutive distinct queries throttled; saving resumable progress", flush=True)
        raise LookupUnavailable(f"{self.name}: {last_error} after {MAX_ATTEMPTS} attempts")

    def report(self):
        return {"attempted":len(self.attempted), "resolved":self.stats["resolved"],
                "misses":self.stats["misses"], "httpRequests":self.stats["httpRequests"],
                "http429":self.stats["http429"], "exhaustedRequests":self.stats["exhaustedRequests"],
                "httpErrors":self.stats["httpErrors"], "transportErrors":self.stats["transportErrors"],
                "rateLimitedStreak":len(self.limited_keys), "disabled":self.disabled, "retryAt":self.retry_at}


def valid_entry(entry):
    if not isinstance(entry, dict) or entry.get("k") not in {"doi", "index", "none"}:
        return False
    if entry["k"] == "none":
        return entry.get("u") == ""
    return bool(safe_url(entry.get("u", ""))) and (entry["k"] != "doi" or
        bool(doi_url(entry.get("u", ""))) or entry.get("r") == "dblp")


def dblp_key(row):
    return f"{normalized_title(query_title(row['title']))}|{int(row['year'])}"


def dblp_link(row, index):
    hit = index.get(dblp_key(row), {})
    if hit.get("y") != int(row["year"]):
        return ""
    aliases = {"tches":"ches", "tosc":"fse"}
    venue = clean_text(row.get("venue", "")).casefold()
    matched_venue = hit.get("v", "").casefold()
    if venue and aliases.get(venue, venue) != aliases.get(matched_venue, matched_venue):
        return ""
    return doi_url(hit.get("doi", "")) or safe_url(hit.get("ee", ""))


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


def resolve_links(source, cache_path, *, delay=.35, retry_index=False, retry_fallbacks=False,
                  providers=None, report_path=None, dblp_path=None, max_api_requests=DEFAULT_API_BUDGET):
    cache_path = Path(cache_path)
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    previous = {}
    if report_path and Path(report_path).exists():
        previous = json.loads(Path(report_path).read_text(encoding="utf-8")).get("sources", {})
    providers = providers if providers is not None else [
        Provider(name, delay=delay, retry_at=previous.get(name, {}).get("retryAt", 0))
        for name in ("openalex", "semantic_scholar")]
    budget = RequestBudget(max_api_requests)
    for provider in providers:
        provider.budget = budget
    rows = [row for row in iter_dict_rows(source) if needs_resolution(row)]
    dblp = json.loads(Path(dblp_path).read_text(encoding="utf-8")) if dblp_path else {}
    dblp_matches, dblp_doi_matches, dblp_resolved = 0, 0, 0
    # Finish the entire local join before any provider availability or request-budget check.
    for row in rows:
        url = dblp_link(row, dblp)
        if url:
            dblp_matches += 1
            dblp_doi_matches += url.startswith("https://doi.org/")
            key = link_key(row)
            if not valid_entry(cache.get(key)) or cache[key]["k"] != "doi":
                cache[key] = {"u":url, "k":"doi", "r":"dblp"}
                dblp_resolved += 1
    save_cache(cache_path, cache)
    print(f"DBLP exact title/year join: {dblp_matches:,}/{len(rows):,} matches; {dblp_resolved:,} cache upgrades", flush=True)
    eligible = {}
    for row in rows:
        key = link_key(row)
        entry = cache.get(key)
        if not valid_entry(entry) or (retry_index and entry["k"] == "index") or (retry_fallbacks and entry["k"] != "doi"):
            eligible.setdefault(key, row)
    processed, pending, resolved, errors = 0, 0, 0, 0
    interrupted = False
    try:
        for key, row in eligible.items():
            if not any(provider.available for provider in providers):
                print("STOP: API budget exhausted or sources deferred/rate-limited; remaining rows are pending, not completed misses", flush=True)
                break
            url, resolved_by, complete = "", "", True
            for provider in providers:
                try:
                    url = provider.lookup(row)
                except (LookupUnavailable, requests.RequestException, ValueError) as error:
                    complete = False
                    errors += 1
                    print(f"UNAVAILABLE {provider.name}, row {processed + 1}: {error}", flush=True)
                    continue
                if url:
                    resolved_by = provider.name
                    break
            if url:
                cache[key] = {"u":url, "k":"doi", "r":resolved_by}
                resolved += 1
                print(f"MATCH {resolved_by}, row {processed + 1}: {url}", flush=True)
            elif not valid_entry(cache.get(key)):
                fallback = safe_url(row.get("source", ""))
                cache[key] = {"u":fallback, "k":"index"} if fallback else {"u":"", "k":"none"}
            if not url and not complete:
                pending += 1
            save_cache(cache_path, cache)
            processed += 1
            if processed % 100 == 0 or processed == len(eligible):
                print(f"Processed {processed:,}/{len(eligible):,}; new DOIs {resolved:,}; pending {pending:,}", flush=True)
    except KeyboardInterrupt:
        interrupted = True
        print("INTERRUPTED: saving cache and pending counts", flush=True)
    save_cache(cache_path, cache)
    unprocessed = len(eligible) - processed
    counts = Counter(cache[link_key(row)]["k"] if valid_entry(cache.get(link_key(row))) else "none" for row in rows)
    report = {"rows":len(rows), "eligible":len(eligible), "processed":processed,
              "attempted":len(set().union(*(p.attempted for p in providers))), "resolved":resolved + dblp_resolved,
              "dblpMatched":dblp_matches, "dblpResolved":dblp_resolved,
              "dblpDoiMatched":dblp_doi_matches,
              "apiBudget":budget.limit, "apiRequests":budget.used,
              "doi":counts["doi"], "index":counts["index"], "unmatched":counts["none"],
              "pending":pending + unprocessed, "unprocessed":unprocessed,
              "complete":not (pending or unprocessed or interrupted), "interrupted":interrupted,
              "cacheEntries":len(cache), "cacheBytes":cache_path.stat().st_size,
              "lookupErrors":errors, "sources":{p.name:p.report() for p in providers},
              "updatedAt":datetime.now(timezone.utc).isoformat()}
    if report_path:
        save_cache(Path(report_path), report)
    print("Resolution summary: " + json.dumps(report, sort_keys=True), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/iacr_publications")
    parser.add_argument("--cache", type=Path, default=ROOT / "data/link_landing.json")
    parser.add_argument("--dblp-index", type=Path, default=ROOT / "data/dblp_dois.json", help="local DBLP title/year landing index; build it first")
    parser.add_argument("--max-api-requests", type=int, default=DEFAULT_API_BUDGET, help="shared HTTP attempt cap including retries; 0 runs only the local join")
    parser.add_argument("--delay", type=float, default=.35, help="request pacing: minimum 0.35s OpenAlex, 0.6s Semantic Scholar")
    parser.add_argument("--retry-index", action="store_true", help="retry cached index fallbacks; preserve existing DOI matches")
    parser.add_argument("--retry-fallbacks", action="store_true", help="retry both index and unmatched entries (legacy flag)")
    parser.add_argument("--report", type=Path, default=ROOT / "dist/link_resolution_report.json", help="resumable provider deadlines and per-source counts (ignored by Git)")
    args = parser.parse_args()
    report = resolve_links(args.source, args.cache, delay=args.delay, retry_index=args.retry_index,
                           retry_fallbacks=args.retry_fallbacks, report_path=args.report,
                           dblp_path=args.dblp_index, max_api_requests=args.max_api_requests)
    raise SystemExit(130 if report["interrupted"] else 0 if report["complete"] else 2)


if __name__ == "__main__":
    main()
