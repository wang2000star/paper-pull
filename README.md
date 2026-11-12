# paper-pull: IACR decade corpus

This project fetches papers from the IACR ePrint Archive and IACR-affiliated conference and journal sources. The current snapshot covers publication years 2017–2026 inclusive; 2026 is year-to-date.

## Refresh the fetched data

```bash
python -m pip install requests beautifulsoup4
python fetch_oai_bulk.py --from 2017-01-01 --until 2026-09-27 --out papers_bulk_10y.csv --state oai_state_10y.txt --restart
python collect_iacr_publications.py
python merge_papers.py --bulk papers_bulk_10y.csv --crawl papers_5y.csv --out papers_10y.csv --start-year 2017 --end-year 2026
```

`papers_10y.csv` and `iacr_publications.csv` are the handoff inputs consumed by the separate `crypto-hot` analysis project. Keep topic classification, rankings, and report generation in `crypto-hot`.

The OAI-PMH fetch is resumable: retain the checkpoint and its output file together until the fetch completes. The local `papers_bulk_10y.csv` is the raw OAI metadata snapshot; `papers_5y.csv` contributes keyword fields where available.
