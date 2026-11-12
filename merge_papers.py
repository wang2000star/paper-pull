#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_papers.py —— 合并 ePrint 元数据源，产出指定年份范围的数据集

数据来源：
  data/papers_bulk/  OAI-PMH 官方批量收割
  data/papers/       逐篇抓取的关键词来源

规则：
  标题/作者/摘要/分类 → 优先 OAI 批量数据，缺失时回退逐篇抓取数据
  关键词             → 仅逐篇抓取提供，随抓取进度推进而逐步补齐（未抓到的为空）

可反复运行：抓取有新进展后重新执行本脚本即可刷新输出。
用法：python merge_papers.py [--year 2026] [--bulk data/papers_bulk]
      [--crawl data/papers] [--out-dir data/papers_merged]
      [--start-year 2017] [--end-year 2026]
"""
import argparse, csv, re
from pathlib import Path
from tools.shards import iter_dict_rows, write_shards

HEADER = ["标题", "作者", "摘要", "关键词", "PDF链接", "分类"]

def pid_of(row):
    m = re.search(r"/(\d{4}/\d+)\.pdf", row.get("PDF链接", "") or "")
    return m.group(1) if m else None

def load(path):
    d = {}
    p = Path(path)
    if not p.exists() or (p.is_dir() and not any(p.glob("*.csv"))):
        print(f"提示：{path} 不存在，已跳过")
        return d
    for r in iter_dict_rows(p):
        pid = pid_of(r)
        if pid and pid not in d:   # 首次出现优先，天然去重
            d[pid] = r
    return d

def main():
    ap = argparse.ArgumentParser(description="合并OAI批量数据与逐篇抓取数据")
    ap.add_argument("--bulk", default="data/papers_bulk", help="OAI目录或旧CSV")
    ap.add_argument("--crawl", nargs="+", default=["data/papers"],
                    help="关键词目录或旧CSV，后面的源优先")
    output = ap.add_mutually_exclusive_group()
    output.add_argument("--out-dir", default="data/papers_merged", help="合并输出目录")
    output.add_argument("--out", help="兼容旧流程的单CSV输出")
    ap.add_argument("--year", type=int, help="只刷新此年份，保留其他年份分片")
    ap.add_argument("--missing", default="missing_ids.txt")
    ap.add_argument("--start-year", type=int, default=2017)
    ap.add_argument("--end-year", type=int, default=2026)
    args = ap.parse_args()
    if not 1000 <= args.start_year <= args.end_year <= 9999:
        ap.error("--start-year/--end-year must be ordered four-digit years")
    if args.year is not None and not args.start_year <= args.year <= args.end_year:
        ap.error("--year must be inside --start-year/--end-year")

    bulk, crawl = load(args.bulk), {}
    for source in args.crawl:
        crawl.update(load(source))
    print(f"载入：OAI批量 {len(bulk)} 篇，逐篇抓取 {len(crawl)} 篇")

    # Use the observed union of paper IDs; do not rely on manually maintained
    # per-year ID ceilings, which silently dropped newer and older records.
    merged = {**crawl, **bulk}
    ids = sorted((pid for pid in merged
                  if args.start_year <= int(pid.split("/")[0]) <= args.end_year
                  and (args.year is None or int(pid.split("/")[0]) == args.year)),
                 key=lambda pid: (int(pid.split("/")[0]), int(pid.split("/")[1])))

    stat = {"总ID": 0, "双源": 0, "仅OAI": 0, "仅抓取": 0, "关键词已填": 0, "缺失": 0}
    per_year, missing = {}, []
    output_rows = []
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
        output_rows.append({
            "标题": pick("标题"),
            "作者": pick("作者"),
            "摘要": pick("摘要"),
            "关键词": kw,
            "PDF链接": f"https://eprint.iacr.org/{pid}.pdf",
            "分类": pick("分类"),
        })
        per_year[y] = per_year.get(y, 0) + 1

    years = [args.year] if args.year is not None else range(args.start_year, args.end_year + 1)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=HEADER, lineterminator="\n")
            writer.writeheader()
            writer.writerows(output_rows)
    else:
        write_shards(args.out_dir, HEADER, output_rows, years=years)
    print(f"输出：{args.out or args.out_dir}（{sum(per_year.values())} 篇）")
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
