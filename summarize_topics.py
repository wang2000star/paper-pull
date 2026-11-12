#!/usr/bin/env python3
"""Merge ePrint/IACR venue records and rank specific research directions.

Direction labels are transparent high-precision keyword rules, intended as a
reviewable first-pass taxonomy rather than a substitute for expert screening.
"""
import csv, re, unicodedata
from collections import Counter, defaultdict
from pathlib import Path

YEARS = range(2022, 2027)
RULES = [
    ("BGV/BFV bootstrapping improvements", r"\b(bgv|bfv)\b.{0,70}\bbootstrapp|\bbootstrapp.{0,70}\b(bgv|bfv)\b"),
    ("CKKS bootstrapping improvements", r"\bckks\b.{0,90}\bbootstrapp|\bbootstrapp.{0,90}\bckks\b"),
    ("TFHE bootstrapping improvements", r"\btfhe\b.{0,90}\bbootstrapp|\bbootstrapp.{0,90}\btfhe\b"),
    ("FHE key switching and modulus switching", r"\b(homomorphic encryption|\bfhe\b|\bbgv\b|\bbfv\b|\bckks\b|\btfhe\b).{0,100}\b(key switch|modulus switch|rescal|relineariz)"),
    ("FHE implementation and hardware acceleration", r"\b(fhe|homomorphic encryption|ckks|bgv|bfv|tfhe)\b.{0,100}\b(hardware|gpu|fpga|accelerat|implement|risc-v|riscv)"),
    ("Lattice KEM attacks and security analysis", r"\b(lattice|ml-kem|kyber|saber|hqc|frodo|kem).{0,100}\b(attack|cryptanal|side.channel|fault|security analysis|key recovery)"),
    ("Dilithium/ML-DSA signature construction and optimization", r"\b(dilithium|ml-dsa)\b.{0,100}\b(signature|signing|implementation|optimization|efficient|masking)"),
    ("Falcon signature construction and implementation", r"\bfalcon\b.{0,100}\b(signature|signing|implementation|optimization|efficient|masking)"),
    ("Other lattice signature construction and efficiency", r"\blattice.based signature|\blattice signatures?\b"),
    ("Lattice reduction and PQC security estimation", r"\b(lwe|sieving|lattice reduction|bkz|svp|sieving|cryptographic estimation).{0,100}\b(quantum|cost|complexity|security|reduction|estimat)"),
    ("Zero-knowledge folding and recursive proof systems", r"\b(folding scheme|folding schemes|recursive proof|recursive snark|incrementally verifiable|ivc|accumulation scheme)"),
    ("zkSNARK lookup arguments and arithmetization", r"\b(lookup argument|lookup table|plonk|snark|zk.?snark).{0,100}\b(lookup|arithmetization|constraint system|sumcheck)"),
    ("Zero-knowledge proof aggregation and succinctness", r"\b(snark|zk.?proof|zero.knowledge proof|proof system).{0,100}\b(aggregate|aggregation|succinct|prover time|proof size)"),
    ("MPC preprocessing and communication efficiency", r"\b(multiparty computation|multi.party computation|\bmpc\b).{0,100}\b(preprocess|preprocessing|communication|round|offline phase|online phase)"),
    ("Malicious-secure MPC protocol construction", r"\b(multiparty computation|multi.party computation|\bmpc\b).{0,100}\b(malicious|dishonest majority|active security|abort)"),
    ("Private set intersection protocols", r"\b(private set intersection|\bpsi\b)\b"),
    ("Threshold and distributed signature protocols", r"\b(threshold signature|threshold signatures|distributed signing|threshold signing|multi.signature|multisignature)\b"),
    ("Anonymous credentials and blind signatures", r"\b(anonymous credential|anonymous credentials|blind signature|blind signatures|anonymous token)\b"),
    ("Differential and linear cryptanalysis of block ciphers", r"\b(differential|linear|integral|algebraic) cryptanal|\b(round|key recovery) attack.{0,60}\b(aes|des|cipher|sbox|arx)\b"),
    ("Power and electromagnetic attacks on post-quantum implementations", r"\b(ml-kem|ml-dsa|kyber|dilithium|falcon|hqc|post.quantum)\b.{0,100}\b(power analysis|electromagnetic|side.channel|leakage)\b|\b(power analysis|electromagnetic|side.channel|leakage)\b.{0,100}\b(ml-kem|ml-dsa|kyber|dilithium|falcon|hqc|post.quantum)\b"),
    ("Cache and timing attacks on cryptographic implementations", r"\b(cache|timing) (attack|attacks|leakage|side.channel)\b|\b(cache|timing) side.channel\b"),
    ("Masking and leakage-resistant implementation techniques", r"\b(masking|masked implementation|leakage.resilient)\b.{0,90}\b(side.channel|power|implementation|cryptographic)\b"),
    ("Fault injection against cryptographic implementations", r"\b(fault injection|fault attack|fault attacks|fault analysis)\b"),
    ("Post-quantum cryptography implementation and leakage", r"\b(post.quantum|ml-kem|ml-dsa|kyber|dilithium|falcon|hqc)\b.{0,100}\b(implementation|leakage|constant.time)"),
    ("Universal composability proofs for multiparty protocols", r"\b(mpc|multiparty computation|multi.party computation)\b.{0,100}\b(universally composable|universal composability|uc security|composability)\b"),
    ("Verifiable computation and SNARK-based applications", r"\b(verifiable computation|zk.?snark|snark).{0,100}\b(machine learning|blockchain|rollup|application|outsourc|delegat)"),
]

def norm_title(s):
    s = unicodedata.normalize("NFKD", s or "").casefold()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()

def read_csv(path):
    p = Path(path)
    if not p.exists(): return []
    with p.open(encoding="utf-8-sig", newline="") as f: return list(csv.DictReader(f))

def eprint_year(row):
    m = re.search(r"eprint\.iacr\.org/(\d{4})/", row.get("PDF链接", ""))
    return m.group(1) if m else ""

def main():
    eprints = read_csv("papers_5y.csv")
    venue = read_csv("iacr_publications.csv")
    papers, rwc_talks = {}, []
    for r in eprints:
        title = r.get("标题", "").strip()
        y = eprint_year(r)
        if not title or y not in map(str, YEARS): continue
        k = norm_title(title)
        papers[k] = {"title": title, "year": y, "authors": r.get("作者", ""), "abstract": r.get("摘要", ""),
                     "keywords": r.get("关键词", ""), "venues": "ePrint", "eprint_url": r.get("PDF链接", ""), "venue_url": ""}
    for r in venue:
        title = r.get("title", "").strip(); y = r.get("year", "")
        if not title or y not in map(str, YEARS): continue
        k = norm_title(title)
        if "talk" in (r.get("publication_type", "").lower()) and k not in papers:
            rwc_talks.append(r)
            continue
        if k in papers:
            old = papers[k]
            old["venues"] = old["venues"] + "; " + r.get("venue", "")
            old["venue_url"] = r.get("url", "")
            if r.get("abstract") and not old["abstract"]: old["abstract"] = r["abstract"]
        else:
            papers[k] = {"title": title, "year": y, "authors": r.get("authors", ""), "abstract": r.get("abstract", ""),
                         "keywords": "", "venues": r.get("venue", ""), "eprint_url": "", "venue_url": r.get("url", "")}
    counts = Counter(); tagged = []
    for p in papers.values():
        text = " ".join([p["title"], p["abstract"], p["keywords"]]).casefold()
        topics = [name for name, pattern in RULES if re.search(pattern, text, re.I)]
        p["directions"] = "; ".join(topics)
        p["hot_topic_directions"] = ""
        for t in topics: counts[(t, p["year"])] += 1
        tagged.append(p)
    hot = {t for (t, y), n in counts.items() if n > 10}
    ranked = []
    for name, _ in RULES:
        row = {"research_direction": name}
        for y in YEARS: row[str(y)] = counts[(name, str(y))]
        row["total_2022_2026"] = sum(row[str(y)] for y in YEARS)
        row["hot_years_gt_10"] = ", ".join(str(y) for y in YEARS if row[str(y)] > 10)
        row["hot_topic"] = "yes" if row["hot_years_gt_10"] else "no"
        ranked.append(row)
    ranked.sort(key=lambda r: (-r["total_2022_2026"], r["research_direction"]))
    for p in tagged:
        p["hot_topic_directions"] = "; ".join(t for t in p["directions"].split("; ") if t in hot)
    with open("papers_classified.csv", "w", newline="", encoding="utf-8-sig") as f:
        cols = ["title", "year", "authors", "venues", "directions", "hot_topic_directions", "abstract", "keywords", "eprint_url", "venue_url"]
        w=csv.DictWriter(f,fieldnames=cols); w.writeheader(); w.writerows(sorted(tagged,key=lambda p:(p["year"],p["title"].casefold())))
    with open("rwc_program.csv", "w", newline="", encoding="utf-8-sig") as f:
        cols = ["title", "authors", "year", "url", "source"]
        w=csv.DictWriter(f,fieldnames=cols); w.writeheader()
        for r in sorted(rwc_talks, key=lambda x:(x.get("year", ""), x.get("title", "").casefold())):
            w.writerow({k:r.get(k, "") for k in cols})
    with open("topics_ranked.csv", "w", newline="", encoding="utf-8-sig") as f:
        cols = ["rank", "research_direction", *map(str, YEARS), "total_2022_2026", "hot_years_gt_10", "hot_topic"]
        w=csv.DictWriter(f,fieldnames=cols); w.writeheader()
        for i,r in enumerate(ranked,1): w.writerow({"rank":i,**r})
    print(f"ePrint records: {len(eprints)}; IACR venue records: {len(venue)}; unique title records: {len(tagged)}")
    print(f"Tagged records: {sum(bool(p['directions']) for p in tagged)}; unmatched records remain blank")
    print("Top research directions:")
    for r in ranked[:15]: print(f"{r['total_2022_2026']:>4} {r['research_direction']} | hot years: {r['hot_years_gt_10'] or '-'}")

if __name__ == "__main__": main()
