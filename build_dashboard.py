#!/usr/bin/env python3
"""Build the self-contained browser dashboard from the collected CSV files."""
import csv
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

YEARS = [str(y) for y in range(2022, 2027)]
VENUES = ["ASIACRYPT", "CiC", "CRYPTO", "EUROCRYPT", "JoC", "PKC", "RWC", "TCC", "TCHES", "ToSC"]
JOURNALS = ["CiC", "JoC", "TCHES", "ToSC"]
CONFERENCES = ["ASIACRYPT", "CRYPTO", "EUROCRYPT", "PKC", "TCC"]
COLORS = ["#4f46e5", "#0891b2", "#e11d48", "#ca8a04", "#7c3aed", "#059669", "#ea580c", "#2563eb", "#be123c", "#0f766e"]


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main():
    papers = read_csv("papers_classified.csv")
    ranked = read_csv("topics_ranked.csv")
    outlets = read_csv("iacr_publications.csv")
    topics = []
    for row in ranked:
        topics.append({
            "name": row["research_direction"],
            "counts": [int(row[y] or 0) for y in YEARS],
            "total": int(row["total_2022_2026"] or 0),
            "hotYears": [y for y in YEARS if y in row["hot_years_gt_10"].split(", ")],
        })
    topics.sort(key=lambda x: (-x["total"], x["name"]))

    annual_total = Counter(p["year"] for p in papers)
    annual_tagged = Counter(p["year"] for p in papers if p.get("directions", "").strip())
    topic_venue = defaultdict(Counter)
    annual_outlet = defaultdict(Counter)
    outlet_topics = defaultdict(Counter)
    rwc_counts = Counter()
    for row in outlets:
        venue, year = row.get("venue", ""), row.get("year", "")
        if year not in YEARS:
            continue
        if row.get("publication_type", "").startswith("accepted conference talk"):
            rwc_counts[year] += 1
            continue
        if venue in VENUES:
            annual_outlet[venue][year] += 1

    paper_samples = defaultdict(list)
    for p in papers:
        year = p.get("year", "")
        direction_names = [x.strip() for x in p.get("directions", "").split(";") if x.strip()]
        outlet_names = {x.strip() for x in p.get("venues", "").split(";") if x.strip()}
        for topic in direction_names:
            for venue in outlet_names:
                if venue in VENUES and venue != "RWC":
                    topic_venue[topic][venue] += 1
                    outlet_topics[venue][topic] += 1
            if len(paper_samples[topic]) < 6:
                url = p.get("eprint_url") or p.get("venue_url") or ""
                paper_samples[topic].append({"title": p.get("title", ""), "year": year, "url": url})

    data = {
        "years": YEARS,
        "generated": date.today().isoformat(),
        "venues": VENUES,
        "journals": JOURNALS,
        "conferences": CONFERENCES,
        "colors": COLORS,
        "topics": topics,
        "paperCount": len(papers),
        "taggedCount": sum(annual_tagged.values()),
        "annualTotal": [annual_total[y] for y in YEARS],
        "annualTagged": [annual_tagged[y] for y in YEARS],
        "annualCoverage": [round(100 * annual_tagged[y] / annual_total[y], 1) if annual_total[y] else 0 for y in YEARS],
        "outletCounts": {v: [annual_outlet[v][y] for y in YEARS] for v in VENUES if v != "RWC"},
        "rwcTalks": [rwc_counts[y] for y in YEARS],
        "topicVenue": {t: {v: topic_venue[t][v] for v in VENUES if v != "RWC"} for t in [x["name"] for x in topics]},
        "outletTopics": {v: dict(outlet_topics[v]) for v in VENUES if v != "RWC"},
        "samples": dict(paper_samples),
    }
    template = Path("dashboard_template.html").read_text(encoding="utf-8")
    # Prevent embedded data from terminating the JSON script element.
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    Path("index.html").write_text(template.replace("__DASHBOARD_DATA__", encoded), encoding="utf-8")
    print(f"Built index.html: {len(papers):,} unique records, {len(topics)} directions, {len(outlets):,} venue/program records")


if __name__ == "__main__":
    main()
