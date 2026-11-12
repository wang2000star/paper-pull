#!/usr/bin/env python3
"""Collect IACR proceedings and journal metadata for 2017--2026.

Primary sources: the IACR archive and IACR journal sites. Records are retained
with source URLs so coverage and possible ePrint duplicates can be audited.
"""
import csv
import argparse
import re
import time
import string
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

START_YEAR, END_YEAR = 2017, 2026
YEAR_RE = r"(201[7-9]|202[0-6])"
BASE = "https://www.iacr.org"
HEADER = ["title", "authors", "year", "venue", "publication_type", "abstract", "doi", "url", "source"]
S = requests.Session()
S.headers["User-Agent"] = "paper-pull/1.0"


def get(url):
    r = S.get(url, timeout=40)
    r.raise_for_status()
    return r


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def collect_archive():
    """Collect proceedings explicitly listed in IACR's archive index."""
    soup = BeautifulSoup(get(BASE + "/archive/").content, "html.parser")
    out = []
    for a in soup.select("a[href]"):
        label = clean(a.get_text(" ", strip=True))
        m = re.search(r"\b" + YEAR_RE + r"\b", label)
        href = urljoin(BASE + "/archive/", a["href"])
        if not m or not re.search(r"(?:crypto|eurocrypt|asiacrypt|pkc|tcc|fse|ches|africacrypt|latincrypt|indocrypt)", href, re.I):
            continue
        try:
            page = BeautifulSoup(get(href).content, "html.parser")
        except requests.RequestException as e:
            print(f"archive page skipped: {href}: {e}")
            continue
        venue = label
        for li in page.select("li"):
            title_link = li.find("a", href=re.compile(r"\.pdf(?:$|\?)", re.I))
            if not title_link:
                continue
            authors = [clean(x.get_text(" ", strip=True)) for x in li.find_all("a", href=re.compile(r"/author\.php"))]
            out.append({"title": clean(title_link.get_text(" ", strip=True)), "authors": "; ".join(authors),
                        "year": m.group(1), "venue": venue, "publication_type": "conference proceedings",
                        "abstract": "", "doi": "", "url": title_link["href"], "source": href})
        time.sleep(.15)
    return out


def collect_ojs(journal, archive_url, base_url, pubtype="journal"):
    """Read all issue/article records from an IACR OJS journal site."""
    issues = {}
    archive_page = archive_url
    visited = set()
    while archive_page and archive_page not in visited:
        visited.add(archive_page)
        archive = BeautifulSoup(get(archive_page).content, "html.parser")
        for a in archive.select('a[href*="/issue/view/"]'):
            y = re.search(r"\b" + YEAR_RE + r"\b", clean(a.get_text(" ", strip=True)))
            if y:
                issues[urljoin(archive_page, a["href"])] = y.group(1)
        next_link = archive.select_one(".cmp_pagination a.next")
        archive_page = urljoin(archive_page, next_link["href"]) if next_link else None
        if archive_page:
            time.sleep(.1)
    out = []
    for issue_url, year in issues.items():
        page = BeautifulSoup(get(issue_url).content, "html.parser")
        for entry in page.select(".obj_article_summary"):
            link = entry.select_one(".title a")
            if not link:
                continue
            authors = clean(entry.select_one(".authors").get_text("; ", strip=True)) if entry.select_one(".authors") else ""
            out.append({"title": clean(link.get_text(" ", strip=True)), "authors": authors,
                        "year": year, "venue": journal, "publication_type": pubtype, "abstract": "", "doi": "",
                        "url": urljoin(issue_url, link["href"]), "source": issue_url})
        time.sleep(.1)
    return out


def collect_crossref_joc():
    """Collect JoC issue contents from the journal's official Springer pages."""
    out = []
    root = "https://link.springer.com/journal/145/volumes-and-issues"
    for vol in range(30, 40):  # Vol. 30 (2017) through Vol. 39 (2026)
        year = str(vol + 1987)
        if not START_YEAR <= int(year) <= END_YEAR: continue
        for issue in range(1, 5):
            url = f"{root}/{vol}-{issue}"
            try: soup = BeautifulSoup(get(url).content, "html.parser")
            except requests.RequestException: continue
            for link in soup.select('a[href*="/article/"]'):
                title = clean(link.get_text(" ", strip=True))
                if not title: continue
                card = link.find_parent(class_=re.compile("app-card-open")) or link.parent
                authors = clean(card.select_one(".app-card-open__authors").get_text("; ", strip=True)) if card.select_one(".app-card-open__authors") else ""
                doi_m = re.search(r"/article/(10\.\d{4,9}/[^/?#]+)", link.get("href", ""))
                out.append({"title": title, "authors": authors, "year": year, "venue": "JoC",
                            "publication_type": "journal", "abstract": "", "doi": doi_m.group(1) if doi_m else "",
                            "url": urljoin("https://link.springer.com", link["href"]), "source": url})
            time.sleep(.1)
    return out


def collect_crossref_proceedings(years=None, venues=None):
    """Fill recent proceedings volumes missing from the lagging IACR HTML archive.

    Crossref metadata is filtered again against the actual proceedings-volume
    title; broad query matches are discarded.
    """
    series = [("CRYPTO", ["CRYPTO"]), ("EUROCRYPT", ["EUROCRYPT"]),
              ("ASIACRYPT", ["ASIACRYPT"]), ("PKC", ["PKC"])]
    if years is None: years = range(max(2024, START_YEAR), END_YEAR + 1)
    if venues is not None: series = [x for x in series if x[0] in set(venues)]
    out = []
    # IACR's archive currently supplies many older volumes. Query recent
    # proceedings volumes (2024 onward), where the archive listing lags.
    for year in years:
        for venue, tokens in series:
            phrase = (f"Advances in Cryptology {venue} {year}" if venue in ("CRYPTO", "EUROCRYPT", "ASIACRYPT")
                      else f"{venue} {year}")
            params = {"query.container-title": phrase, "filter": "type:book-chapter", "rows": 1000,
                      "select": "DOI,title,author,container-title,URL,published,published-print,published-online"}
            try:
                items = get("https://api.crossref.org/works?" + requests.compat.urlencode(params)).json()["message"]["items"]
            except requests.RequestException as e:
                print(f"proceedings lookup failed {venue} {year}: {e}")
                continue
            for x in items:
                containers = " | ".join(x.get("container-title", []))
                c = containers.upper()
                venue_match = all(re.search(r"\b" + re.escape(t) + r"\b", c) for t in tokens)
                year_match = bool(re.search(r"\b" + str(year) + r"\b", c))
                if not venue_match or not year_match:
                    continue
                title = clean((x.get("title") or [""])[0])
                if not title or title.lower().startswith("correction to:") or title.lower().startswith("erratum to:"):
                    continue
                authors = "; ".join(" ".join(filter(None, [a.get("given"), a.get("family")])) for a in x.get("author", []))
                out.append({"title": title, "authors": authors, "year": str(year), "venue": venue,
                            "publication_type": "conference proceedings", "abstract": clean(x.get("abstract", "")),
                            "doi": x.get("DOI", ""), "url": x.get("URL", ""),
                            "source": "Crossref metadata; proceedings title matched"})
            time.sleep(1.2)
    if venues is None or "TCC" in venues:
        out.extend(collect_crossref_tcc(years=years))
    return out


def collect_crossref_tcc(years):
    """TCC proceedings are indexed under the container title 'Theory of Cryptography'."""
    out=[]
    for year in years:
        if int(year) < 2024: continue
        params={"query.container-title":"Theory of Cryptography",
                "filter":f"from-pub-date:{year}-01-01,until-pub-date:{year}-12-31,type:book-chapter",
                "rows":1000,"select":"DOI,title,author,container-title,URL,published,published-print,published-online"}
        try: items=get("https://api.crossref.org/works?"+requests.compat.urlencode(params)).json()["message"]["items"]
        except requests.RequestException as e:
            print(f"TCC Crossref lookup failed {year}: {e}");continue
        for x in items:
            if not any(t.strip().lower()=="theory of cryptography" for t in x.get("container-title",[])): continue
            d=x.get("published") or x.get("published-online") or {}
            parts=d.get("date-parts",[[None]])[0]
            if not parts or str(parts[0])!=str(year):continue
            title=clean((x.get("title") or [""])[0])
            if not title:continue
            authors="; ".join(" ".join(filter(None,[a.get("given"),a.get("family")])) for a in x.get("author",[]))
            out.append({"title":title,"authors":authors,"year":str(year),"venue":"TCC",
                        "publication_type":"conference proceedings","abstract":"","doi":x.get("DOI",""),
                        "url":x.get("URL",""),"source":"Crossref; exact Theory of Cryptography container"})
        time.sleep(1)
    return out


def collect_springer_proceedings():
    """Collect 2024+ IACR conference proceedings from Springer book TOCs."""
    series = [("CRYPTO", "Advances in Cryptology CRYPTO"),
              ("EUROCRYPT", "Advances in Cryptology EUROCRYPT"),
              ("ASIACRYPT", "Advances in Cryptology ASIACRYPT"),
              ("PKC", "Public Key Cryptography PKC"),
              ("TCC", "Theory of Cryptography")]
    out, books = [], {}
    for year in range(START_YEAR, END_YEAR + 1):
        for venue, phrase in series:
            search = "https://link.springer.com/search?" + requests.compat.urlencode({"query": f"{phrase} {year}"})
            try: page = BeautifulSoup(get(search).content, "html.parser")
            except requests.RequestException as e:
                print(f"Springer search failed {venue} {year}: {e}"); continue
            for a in page.select('a[href*="/book/"]'):
                card = a.find_parent(class_=re.compile("app-card")) or a.parent
                label = clean(card.get_text(" ", strip=True))
                if not re.search(r"\b" + re.escape(venue) + r"\b", label, re.I) or not re.search(r"\b" + str(year) + r"\b", label):
                    continue
                href = urljoin("https://link.springer.com", a["href"])
                doi = re.search(r"/book/(10\.\d{4,9}/[^/?#]+)", href)
                if doi: books[(year, venue, doi.group(1))] = href
            time.sleep(.5)
    for (year, venue, book_doi), book_url in books.items():
        try: page = BeautifulSoup(get(book_url).content, "html.parser")
        except requests.RequestException as e:
            print(f"Springer book TOC failed {venue} {year}: {e}"); continue
        chapter_prefix = "/chapter/" + book_doi
        for a in page.select('a[href*="/chapter/"]'):
            href = a.get("href", "")
            if not href.startswith(chapter_prefix + "_"): continue
            title = clean(a.get_text(" ", strip=True))
            if not title: continue
            out.append({"title": title, "authors": "", "year": str(year), "venue": venue,
                        "publication_type": "conference proceedings", "abstract": "", "doi": "",
                        "url": urljoin("https://link.springer.com", href), "source": book_url})
        time.sleep(.3)
    return out


def collect_rwc():
    """Collect accepted RWC talk titles; RWC does not publish proceedings."""
    out = []
    for year in range(START_YEAR, END_YEAR + 1):
        talks_url = f"https://realworldcrypto.iacr.org/{year}/acceptedtalks.php"
        try:
            response = get(talks_url)
        except requests.RequestException:
            # Earlier RWC sites called this list acceptedpapers.php.
            talks_url = f"https://realworldcrypto.iacr.org/{year}/acceptedpapers.php"
            try: response = get(talks_url)
            except requests.RequestException as e:
                print(f"RWC program unavailable {year}: {e}"); continue
        page = BeautifulSoup(response.content, "html.parser")
        for li in page.select("li"):
            title = li.select_one(".paperTitle")
            if not title: continue
            authors = li.find("p")
            out.append({"title": clean(title.get_text(" ", strip=True)),
                        "authors": clean(authors.get_text(" ", strip=True)) if authors else "",
                        "year": str(year), "venue": "RWC", "publication_type": "accepted conference talk (no proceedings)",
                        "abstract": "", "doi": "", "url": talks_url, "source": talks_url})
    return out


def collect_cic():
    """Collect CiC records from the IACR journal's own search API.

    Union of single-character searches covers records even though this custom
    API has no browse-all endpoint. Deduplicate by its stable document id.
    """
    found = {}
    # Crossref is a second index of this exact journal and fills years that
    # are not surfaced by the custom API's bounded search results.
    for year in range(START_YEAR, END_YEAR + 1):
        params = {"filter": f"from-pub-date:{year}-01-01,until-pub-date:{year}-12-31",
                  "query.container-title": "IACR Communications in Cryptology", "rows": 1000}
        try:
            items = get("https://api.crossref.org/works?" + requests.compat.urlencode(params)).json()["message"]["items"]
        except requests.RequestException as e:
            print(f"CiC Crossref supplement unavailable {year}: {e}"); continue
        for item in items:
            if not any(t.strip().lower() == "iacr communications in cryptology" for t in item.get("container-title", [])): continue
            parts = (item.get("published-print") or item.get("published-online") or {}).get("date-parts", [[None]])[0]
            if not parts or str(parts[0]) != str(year): continue
            authors = "; ".join(" ".join(filter(None, [a.get("given"), a.get("family")])) for a in item.get("author", []))
            doi = item.get("DOI", "")
            found[doi] = {"title": clean((item.get("title") or [""])[0]), "authors": authors,
                          "year": str(year), "venue": "CiC", "publication_type": "journal",
                          "abstract": clean(item.get("abstract", "")), "doi": doi, "url": item.get("URL", ""),
                          "source": "Crossref; exact IACR Communications in Cryptology container"}
        time.sleep(1)
    for q in string.ascii_lowercase + string.digits:
        try:
            data = get("https://cic.iacr.org/api/search?" + requests.compat.urlencode({"q": q})).json()
        except (requests.RequestException, ValueError) as e:
            print(f"CiC search failed for {q!r}: {e}"); continue
        for item in data.get("results", []):
            pub = item.get("published", "")
            year = pub[:4]
            if not year.isdigit() or not START_YEAR <= int(year) <= END_YEAR: continue
            found[item.get("docid") or item.get("url")] = {
                "title": clean(item.get("title", "")), "authors": "; ".join(item.get("authors", [])),
                "year": year, "venue": "CiC", "publication_type": "journal",
                "abstract": clean(BeautifulSoup(item.get("abstract", ""), "html.parser").get_text(" ", strip=True)),
                "doi": "", "url": item.get("url", ""), "source": "https://cic.iacr.org/api/search"}
        time.sleep(.2)
    return list(found.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-crossref-proceedings", action="store_true", help="skip recent Springer/Crossref proceedings supplement")
    args = ap.parse_args()
    records = collect_archive()
    if not args.no_crossref_proceedings:
        records.extend(collect_crossref_proceedings())
    records.extend(collect_rwc())
    for args in [
        ("TCHES", "https://tches.iacr.org/index.php/TCHES/issue/archive", "https://tches.iacr.org/"),
        ("ToSC", "https://tosc.iacr.org/index.php/ToSC/en/issue/archive", "https://tosc.iacr.org/"),
    ]:
        try:
            records.extend(collect_ojs(*args))
        except requests.RequestException as e:
            print(f"journal skipped: {args[0]}: {e}")
    records.extend(collect_crossref_joc())
    records.extend(collect_cic())
    # Keep distinct publication records but eliminate repeated crawl URLs.
    unique = {}
    for r in records:
        if r["title"] and r["year"]:
            # Keep the project aligned to the ten venues in its venue scope.
            v = r["venue"].upper()
            if "CHES" in v: r["venue"] = "TCHES"
            elif "FSE" in v: r["venue"] = "ToSC"
            elif "ASIACRYPT" in v: r["venue"] = "ASIACRYPT"
            elif "EUROCRYPT" in v: r["venue"] = "EUROCRYPT"
            elif "PUBLIC KEY CRYPTOGRAPHY" in v or v == "PKC": r["venue"] = "PKC"
            elif "THEORY OF CRYPTOGRAPHY" in v or v == "TCC": r["venue"] = "TCC"
            elif v == "CRYPTO" or v.startswith("CRYPTO "): r["venue"] = "CRYPTO"
            unique.setdefault((r["year"], r["venue"], r["title"].casefold()), r)
    out = sorted(unique.values(), key=lambda r: (r["year"], r["venue"], r["title"].casefold()))
    with open("iacr_publications.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, lineterminator="\n"); w.writeheader(); w.writerows(out)
    print(f"Wrote {len(out)} records to iacr_publications.csv")
    from collections import Counter
    print("By venue/year:")
    for (venue, year), count in sorted(Counter((r["venue"], r["year"]) for r in out).items()):
        print(f"  {venue} {year}: {count}")


if __name__ == "__main__":
    main()
