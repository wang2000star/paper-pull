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
