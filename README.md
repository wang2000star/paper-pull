# paper-pull: IACR corpus

This project fetches papers from the IACR ePrint Archive and IACR-affiliated conference and journal sources. The current snapshot covers publication years 2017–2026 inclusive; 2026 is year-to-date.

## Data layout

Large datasets live in `data/<name>/<year>.csv`. Each shard is a standalone
UTF-8 CSV with a BOM and the original header. ePrint years come from the ID in
`PDF链接`; publications and classified records use their `year` column. Rows
without a usable year go into `_unknown.csv`.

| Directory | Contents |
|---|---|
| `data/papers/` | Landing-page crawl metadata, including keywords |
| `data/papers_bulk/` | Raw OAI-PMH metadata |
| `data/papers_merged/` | Merged ePrint corpus (bulk + crawl) |
| `data/iacr_publications/` | IACR venue and RWC program metadata |
| `data/papers_classified/` | Existing topic classification snapshot |

Corpus names describe their role rather than a fixed time window, so they
remain valid as coverage grows beyond 2026.

`rwc_program.csv`, `topics_ranked.csv`, and the Markdown/HTML reports remain
small files at the repository root. The five corpora above replace overlapping
snapshots without changing retained CSV content. Raw OAI metadata also retains
earlier ePrint IDs returned for the requested modification dates. Existing Git
history remains intact.

## Refresh the fetched data

```bash
python -m pip install requests beautifulsoup4
python3 fetch_oai_bulk.py --from 2017-01-01 --until 2026-11-11 --out-dir data/papers_bulk --state oai_state_bulk.txt --restart
python3 collect_iacr_publications.py --out-dir data/iacr_publications
python3 merge_papers.py --bulk data/papers_bulk --crawl data/papers --out-dir data/papers_merged --start-year 2017 --end-year 2026
```

The merge writes only `data/papers_merged/`, using `data/papers/` as its default
keyword source. If multiple `--crawl` sources are supplied, later sources take
precedence for a matching ID. OAI fields take precedence over crawl fields
except for keywords. Unchanged shard contents are left untouched.

To refresh one year while preserving all other years:

```bash
python3 fetch_oai_bulk.py --year 2026 --from 2026-01-01 --until 2026-11-11 --out-dir data/papers_bulk --state oai_state_2026.txt --restart
# Optional keyword enrichment: supply the specific new IDs or an ID file.
python3 fetch_iacr.py 2026/1385 --data-dir data/papers
python3 collect_iacr_publications.py --year 2026 --out-dir data/iacr_publications
python3 merge_papers.py --year 2026
```

OAI `--from`/`--until` select modification dates; `--year` filters the ePrint
ID's publication year. For an older publication year, keep `--until` at the
current date to include later revisions. The dates above are explicit examples;
advance the end date when refreshing. `--restart --year` clears only the selected
year in the output directory; `--restart` without `--year` clears all year
shards there. An interrupted OAI fetch resumes with the same command after
removing `--restart`. Keep its output and checkpoint together and reuse the
same dates, year and output path. Checkpoints store that query context; old
token-only checkpoints require a restart. Replayed records are skipped by PDF
URL. Separate output directories or years should use separate state files.

`fetch_iacr.py` defaults to `--data-dir data/papers` and skips IDs already in
those shards. `run_pull.sh` uses the same layout (`DATA_DIR` overrides it).
Both fetchers retain an explicit `--out FILE` option for legacy flat output.
The merge accepts shard directories or flat CSV inputs and `--out FILE` for a
legacy flat export. Its default year range remains 2017–2026; adjust
`--start-year`/`--end-year` as coverage grows. See each script's `--help` for all flags.

The local analysis tools also accept shards:

```bash
python3 summarize_topics.py --eprints data/papers_merged --publications data/iacr_publications --out-dir data/papers_classified
python3 build_dashboard.py --papers data/papers_classified --publications data/iacr_publications
```

The summarizer reads the merged corpus and applies its existing 2022–2026
analysis window. Run it only when intentionally regenerating classifications.
`summarize_topics.py --reports-dir DIR` redirects the small CSV reports;
`build_dashboard.py --out FILE` redirects the HTML. Keep ongoing topic
classification, rankings, and report generation in the separate `crypto-hot`
project when using that analysis workflow.

## Flat exports for crypto-hot

Build the two handoff inputs locally; `dist/` is ignored by Git:

```bash
python3 tools/shards.py concat data/papers_merged --out dist/papers_10y.csv  # intentional legacy filename for crypto-hot compatibility
python3 tools/shards.py concat data/iacr_publications --out dist/iacr_publications.csv
```

Without `--out`, `concat` writes the CSV to stdout. Exports contain one header
and a UTF-8 BOM. Shards are read in filename order; original row order across
years is not retained.

To migrate another flat snapshot and verify it before removing the original:

```bash
python3 tools/shards.py split snapshot.csv data/snapshot
python3 tools/shards.py verify snapshot.csv data/snapshot
```

`split` replaces the destination dataset, including removing obsolete year
shards. `verify` prints each shard's row count, file count, and total, checks
headers/BOMs/year placement, and compares exact multisets of data rows while
ignoring repeated headers. Duplicate data rows are preserved.

Offline regression checks: `python3 -B -m unittest discover -s tests -v`.

## Keyword search on GitHub Pages

`docs/index.html` is a separate, responsive search page for ePrint preprints
and IACR conferences and journals: CRYPTO, EUROCRYPT, ASIACRYPT, PKC, TCC,
TCHES, ToSC, JoC, CiC and RWC. The root `index.html` remains the CryptoScope
dashboard. Search runs in the browser with AND terms, quoted phrases,
year/category/source/venue filters, sorting, and shareable query URLs.
ePrint titles open the original **abstract landing page**. Publication titles
use DOI or OJS article landing pages, with proceedings index pages as the
fallback. Index links carry an explicit note; a title remains plain text if
no safe page exists. Every result link uses HTTPS and excludes direct downloads.
No PDFs are hosted or downloaded by this page.

After refreshing the corpus, regenerate and commit the search metadata:

```bash
python3 tools/resolve_links.py
python3 tools/build_search_index.py
```

The resolver stores title/year matches in `data/link_landing.json` (under
1,000,000 bytes). It queries [OpenAlex](https://openalex.org/), then
[Semantic Scholar](https://www.semanticscholar.org/product/api) on a miss or
temporary failure. Matching removes LaTeX macros and punctuation, and accepts
title similarity ≥ 0.90 or token-set Jaccard similarity ≥ 0.85, with publication
years within ±1. OpenAlex requests include the contact address in both the
`mailto` query parameter and User-Agent, at least 0.35 seconds apart; Semantic
Scholar requests are at least 0.6 seconds apart. Confident matches use DOI
landing URLs and record their resolver in `r`; other rows use their
`source` proceedings index, or an empty URL when no safe source exists. The
current inputs have 2,775 PDF links requiring lookup (the 2,771 archive links counted
on `www.iacr.org` or relative paths, plus four on other IACR hosts); six ePrint
PDF URLs convert directly to abstract pages without a lookup.

The cache is resumable and written atomically after each row. To upgrade
existing index fallbacks without losing DOI matches, run:

```bash
python3 tools/resolve_links.py --retry-index
python3 tools/build_search_index.py
```

Each source retries a throttled request up to six total attempts, with delays
of 2, 4, 8, 16 and 32 seconds, extended by `Retry-After` when present (seconds
or HTTP date). A source stops for that run only after eight consecutive
distinct queries are throttled; retrying one title cannot trip this threshold,
and a non-429 response resets the streak. Server delays longer than 60 seconds
defer that source until the specified deadline while the other source continues.
The report persists these deadlines across restarts, so retrying cannot bypass
a server-requested cooldown.

`dist/link_resolution_report.json` (ignored by Git; override with `--report`)
records attempted/resolved counts per source, HTTP requests and 429s, pending
rows, and retry deadlines. Incomplete runs exit with status 2 and retain safe
cache entries; interrupted runs save progress and exit 130. Pending lookups are
never reported as completed misses. Retry with the same command after the
recorded delay. The legacy `--retry-fallbacks` flag retries both index and
unmatched entries. Existing `OPENALEX_API_KEY` and `SEMANTIC_SCHOLAR_API_KEY`
environment variables are supported and kept separate between the services.

The builder reads `data/papers_merged/` and `data/iacr_publications/` and writes
`docs/data/<year>.json` plus a small `manifest.json`. It keeps one result per
normalized title (casefolded, LaTeX formatting removed, alphanumerics and
collapsed spaces). ePrint wins as the canonical source; the most recent
publication supplies a venue badge and an “also published at” label/link.
For ties, journals take precedence. Duplicate publication title/venue/year
records are collapsed, and duplicate titles across venues also become one
result. Repeated ePrint titles use the newest ePrint year.

The current inputs contain 17,563 ePrint rows and 5,943 publication rows;
4,436 duplicates collapse to **19,070 searchable titles**: 17,556 ePrint,
582 conference (including RWC talks), and 932 journal records. Those source
counts describe canonical search results; ePrint entries may also carry an
official publication link. The source dropdown selects the canonical source;
the venue dropdown selects its displayed venue family. Categories apply only
to ePrint records. URL parameters `q`, `year`, `cat`, `src`, and `venue` combine
freely; `src` accepts `eprint`, `conference`, `journal`, or `all`.

The JSON payload is limited to 7,000,000 bytes in total and 1,500,000 bytes per
file. **No abstract snippets are generated, fetched, or searched**;
`snippetLength` is always zero. Records contain compact title/author/year/
category/keyword/link fields plus `s` (source), `v` (venue), `lk` (link kind:
`eprint`, `doi`, `ojs`, `index`, or `none`), and optional `al`/`au`/`alk`
(also-published label/link/kind). Link priority is ePrint landing page, row or
resolved DOI, OJS article, then source index. Unsafe and relative links are
excluded, and OJS galley/download URLs normalize to their article page. The
builder rejects any PDF reference in the complete JSON payload before writing
files and reports primary and also-published link-kind counts. Year files
load in parallel on the first interaction; subsequent searches need no
network connection. The builder reports input/indexed source counts, dedupe
reduction, per-year record counts, and UTF-8 byte sizes.

To enable publishing in [GitHub's Pages settings](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site):

1. Open `wang2000star/paper-pull` on GitHub and select **Settings**.
2. Select **Pages** in the settings sidebar.
3. Under **Build and deployment → Source**, choose **Deploy from a branch**.
4. Choose branch **main** and folder **/docs**, then click **Save**.
5. Wait for the Pages deployment to complete and open the URL shown there
   (normally `https://wang2000star.github.io/paper-pull/`).

The hosted search works only after Pages is enabled and deployed. For local
use, run a static server instead of opening the HTML through `file://`:

```bash
python3 -m http.server 8000 --directory docs
# Open http://localhost:8000/ ; stop with Ctrl-C.
```

The page uses only its inline CSS/JavaScript and local JSON, with no external
fonts, libraries, trackers, or runtime build step. `docs/.nojekyll` keeps the
static payload available as-is.
