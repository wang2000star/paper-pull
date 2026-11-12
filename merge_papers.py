#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_papers.py —— 合并 ePrint 元数据源，产出指定年份范围的数据集

数据来源：
  papers_bulk.csv  OAI-PMH 官方批量收割（标题/作者/摘要/分类，速度快，无关键词）
  papers.csv       fetch_iacr.py 逐篇抓取落地页（含关键词，速度慢，受 Cloudflare 限流）

规则：
  标题/作者/摘要/分类 → 优先 OAI 批量数据，缺失时回退逐篇抓取数据
  关键词             → 仅逐篇抓取提供，随抓取进度推进而逐步补齐（未抓到的为空）

可反复运行：抓取有新进展后重新执行本脚本即可刷新输出。
用法：python merge_papers.py [--bulk papers_bulk_10y.csv] [--crawl papers_5y.csv]
      [--out papers_10y.csv] [--start-year 2017] [--end-year 2026]
"""
import argparse, csv, re
from pathlib import Path

HEADER = ["标题", "作者", "摘要", "关键词", "PDF链接", "分类"]

def pid_of(row):
    m = re.search(r"/(\d{4}/\d+)\.pdf", row.get("PDF链接", "") or "")
    return m.group(1) if m else None

def load(path):
    d = {}
    p = Path(path)
    if not p.exists():
        print(f"提示：{path} 不存在，已跳过")
        return d
    with open(p, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            pid = pid_of(r)
            if pid and pid not in d:   # 首次出现优先，天然去重
                d[pid] = r
    return d

def main():
    ap = argparse.ArgumentParser(description="合并OAI批量数据与逐篇抓取数据")
    ap.add_argument("--bulk", default="papers_bulk_10y.csv")
    ap.add_argument("--crawl", default="papers_5y.csv")
    ap.add_argument("--out", default="papers_10y.csv")
    ap.add_argument("--missing", default="missing_ids.txt")
    ap.add_argument("--start-year", type=int, default=2017)
    ap.add_argument("--end-year", type=int, default=2026)
    args = ap.parse_args()

    bulk, crawl = load(args.bulk), load(args.crawl)
    print(f"载入：OAI批量 {len(bulk)} 篇，逐篇抓取 {len(crawl)} 篇")

    # Use the observed union of paper IDs; do not rely on manually maintained
    # per-year ID ceilings, which silently dropped newer and older records.
    merged = {**crawl, **bulk}
    ids = sorted((pid for pid in merged
                  if args.start_year <= int(pid.split("/")[0]) <= args.end_year),
                 key=lambda pid: (int(pid.split("/")[0]), int(pid.split("/")[1])))

    stat = {"总ID": 0, "双源": 0, "仅OAI": 0, "仅抓取": 0, "关键词已填": 0, "缺失": 0}
    per_year, missing = {}, []
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, lineterminator="\n")
        w.writeheader()
        for pid in ids:
            y = pid.split("/")[0]
            stat["总ID"] += 1
            b, c = bulk.get(pid), crawl.get(pid)
            if b and c: stat["双源"] += 1
            elif b:     stat["仅OAI"] += 1
            else:       stat["仅抓取"] += 1

            def pick(key, primary=b, fallback=c):
                if primary and (primary.get(key) or "").strip():
                    return primary[key].strip()
                return (fallback.get(key, "").strip() if fallback else "")

            kw = (c.get("关键词", "").strip() if c else "")
            if kw: stat["关键词已填"] += 1
            w.writerow({
                "标题": pick("标题"),
                "作者": pick("作者"),
                "摘要": pick("摘要"),
                "关键词": kw,
                "PDF链接": f"https://eprint.iacr.org/{pid}.pdf",
                "分类": pick("分类"),
            })
            per_year[y] = per_year.get(y, 0) + 1

    print(f"输出：{args.out}（{sum(per_year.values())} 篇）")
    for y in sorted(per_year):
        print(f"  {y}: {per_year[y]:>5} 篇")
    print(f"合并统计：双源 {stat['双源']}，仅OAI {stat['仅OAI']}，仅抓取 {stat['仅抓取']}，"
          f"缺失 {stat['缺失']}")
    print(f"关键词已填 {stat['关键词已填']} 篇"
          f"（{stat['关键词已填'] * 100.0 / max(1, stat['总ID'] - stat['缺失']):.1f}%）；"
          f"其余未获得关键词（抓取尚未覆盖，或 ePrint 页面本身无关键词字段）")
    if missing:
        Path(args.missing).write_text("\n".join(missing) + "\n", encoding="utf-8")
        print(f"缺失ID（约需逐篇抓取）已写入 {args.missing}")

if __name__ == "__main__":
    main()
