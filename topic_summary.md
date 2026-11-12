# IACR papers: collection and topic ranking

## Scope

Period: 2022–2026 year-to-date (collection run on 2026-09-26), matching the existing ePrint dataset. The ten IACR venues are ASIACRYPT, CiC, CRYPTO, EUROCRYPT, JoC, PKC, RWC, TCC, TCHES, and ToSC. CHES is normalized to TCHES; FSE is normalized to ToSC. The ePrint Archive is included as the additional source.

## Topic method

Titles, abstracts, and keywords are merged by normalized title to avoid obvious ePrint/proceedings duplicates. Direction labels use explicit keyword rules in `summarize_topics.py`; a paper can receive multiple labels. `topics_ranked.csv` sorts directions by five-year total and reports the count for each year. A direction is hot in any year with strictly more than 10 papers; the exact hot years are listed per direction.

These are machine-assisted candidate counts, not a manually validated final taxonomy. Some venue records lack abstracts, and false positives or missed matches are possible; expert review is needed before treating the ranking as definitive. 2026 is year-to-date.

## Collection coverage

ePrint records: 10346. IACR venue and RWC program records: 3662. Title-deduplicated paper records in the classification table: 11438. The rule set assigned at least one direction to 2936 records (25.7%); the remaining papers stay in `papers_classified.csv` with a blank direction label.

| Venue | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---:|---:|---:|---:|---:|
| ASIACRYPT | 97 | 105 | 133 | 143 | 0 |
| CiC | 0 | 0 | 103 | 142 | 113 |
| CRYPTO | 96 | 125 | 174 | 156 | 189 |
| EUROCRYPT | 85 | 110 | 122 | 123 | 142 |
| JoC | 28 | 41 | 39 | 36 | 30 |
| PKC | 39 | 50 | 62 | 60 | 65 |
| RWC | 0 | 0 | 0 | 43 | 0 |
| TCC | 60 | 68 | 68 | 70 | 0 |
| TCHES | 83 | 78 | 101 | 111 | 149 |
| ToSC | 48 | 44 | 44 | 58 | 29 |

RWC entries are accepted talk titles, not proceedings papers; they are kept in `rwc_program.csv` and excluded from paper topic counts unless the title also appears independently as a paper in ePrint. The RWC program pages for 2022–2024 and the accepted-talk page for 2026 did not expose a list during collection. ASIACRYPT 2026 and TCC 2026 had not yet held their 2026 meetings by the collection date.

## Ranked research directions

| Rank | Research direction | 2022 | 2023 | 2024 | 2025 | 2026* | Five-year total | Hot years (>10) |
|---:|---|---:|---:|---:|---:|---:|---:|---|
| 1 | Lattice KEM attacks and security analysis | 56 | 62 | 63 | 72 | 63 | 316 | 2022, 2023, 2024, 2025, 2026 |
| 2 | Lattice reduction and PQC security estimation | 36 | 49 | 54 | 72 | 70 | 281 | 2022, 2023, 2024, 2025, 2026 |
| 3 | Post-quantum cryptography implementation and leakage | 38 | 41 | 51 | 75 | 59 | 264 | 2022, 2023, 2024, 2025, 2026 |
| 4 | Differential and linear cryptanalysis of block ciphers | 42 | 45 | 49 | 55 | 63 | 254 | 2022, 2023, 2024, 2025, 2026 |
| 5 | Anonymous credentials and blind signatures | 31 | 42 | 38 | 68 | 64 | 243 | 2022, 2023, 2024, 2025, 2026 |
| 6 | Threshold and distributed signature protocols | 30 | 40 | 47 | 58 | 67 | 242 | 2022, 2023, 2024, 2025, 2026 |
| 7 | Power and electromagnetic attacks on post-quantum implementations | 43 | 36 | 39 | 58 | 53 | 229 | 2022, 2023, 2024, 2025, 2026 |
| 8 | Fault injection against cryptographic implementations | 36 | 43 | 40 | 42 | 37 | 198 | 2022, 2023, 2024, 2025, 2026 |
| 9 | MPC preprocessing and communication efficiency | 31 | 30 | 33 | 50 | 42 | 186 | 2022, 2023, 2024, 2025, 2026 |
| 10 | Private set intersection protocols | 25 | 27 | 47 | 42 | 35 | 176 | 2022, 2023, 2024, 2025, 2026 |
| 11 | Masking and leakage-resistant implementation techniques | 38 | 32 | 25 | 27 | 33 | 155 | 2022, 2023, 2024, 2025, 2026 |
| 12 | Zero-knowledge proof aggregation and succinctness | 20 | 16 | 43 | 33 | 33 | 145 | 2022, 2023, 2024, 2025, 2026 |
| 13 | FHE implementation and hardware acceleration | 19 | 36 | 34 | 29 | 26 | 144 | 2022, 2023, 2024, 2025, 2026 |
| 14 | Dilithium/ML-DSA signature construction and optimization | 20 | 22 | 23 | 37 | 39 | 141 | 2022, 2023, 2024, 2025, 2026 |
| 15 | Zero-knowledge folding and recursive proof systems | 10 | 17 | 39 | 33 | 27 | 126 | 2023, 2024, 2025, 2026 |
| 16 | Malicious-secure MPC protocol construction | 21 | 22 | 19 | 25 | 17 | 104 | 2022, 2023, 2024, 2025, 2026 |
| 17 | zkSNARK lookup arguments and arithmetization | 14 | 20 | 18 | 20 | 24 | 96 | 2022, 2023, 2024, 2025, 2026 |
| 18 | Other lattice signature construction and efficiency | 14 | 18 | 16 | 24 | 20 | 92 | 2022, 2023, 2024, 2025, 2026 |
| 19 | Falcon signature construction and implementation | 12 | 15 | 15 | 24 | 24 | 90 | 2022, 2023, 2024, 2025, 2026 |
| 20 | TFHE bootstrapping improvements | 13 | 13 | 16 | 33 | 13 | 88 | 2022, 2023, 2024, 2025, 2026 |
| 21 | Verifiable computation and SNARK-based applications | 6 | 11 | 26 | 27 | 10 | 80 | 2023, 2024, 2025 |
| 22 | Cache and timing attacks on cryptographic implementations | 13 | 13 | 13 | 12 | 14 | 65 | 2022, 2023, 2024, 2025, 2026 |
| 23 | CKKS bootstrapping improvements | 4 | 4 | 10 | 17 | 24 | 59 | 2025, 2026 |
| 24 | BGV/BFV bootstrapping improvements | 4 | 5 | 14 | 10 | 6 | 39 | 2024 |
| 25 | FHE key switching and modulus switching | 2 | 5 | 4 | 6 | 6 | 23 | — |
| 26 | Universal composability proofs for multiparty protocols | 2 | 0 | 1 | 6 | 3 | 12 | — |

* 2026 year-to-date through September 26, 2026. Counts combine ePrint papers with collected proceedings and journal articles after title-based deduplication. RWC talks are excluded unless independently present as papers.
