#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_oai_bulk.py —— 通过 IACR ePrint 官方 OAI-PMH 接口“批量收割”全部元数据
（标题 / 作者 / 摘要 / 分类；官方接口不含“关键词”，关键词由 fetch_iacr.py 逐篇补齐）

相比逐篇抓落地页（每篇 1 次请求，受 Cloudflare 限流约 20-30 次/分钟），
OAI-PMH 每页返回 100 条记录，近5年约 10k 篇只需 ~105 次请求（几分钟）。

用法：
  python fetch_oai_bulk.py                        # 默认收割 2017-01-01 至今
  python fetch_oai_bulk.py --from 2017-01-01 --until 2026-09-27
  python fetch_oai_bulk.py --restart              # 忽略断点，从头重新收割

特性：429/5xx 自动退避；断点续传（resumptionToken 存于 oai_state.txt，逐页追加写 CSV）
"""
import argparse, csv, json, re, time
import xml.etree.ElementTree as ET
from pathlib import Path
import requests

BASE = "https://eprint.iacr.org"
OAI = BASE + "/oai"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
CSV_HEADER = ["标题", "作者", "摘要", "关键词", "PDF链接", "分类"]
NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

def get(session, params, delay, max_delay, retries=6):
    """带 429/5xx 指数退避的 GET，返回解析后的 XML 根节点"""
    for attempt in range(retries + 1):
        try:
            r = session.get(OAI, params=params, headers=HEADERS, timeout=180)
        except requests.RequestException as e:
            if attempt == retries:
                raise
            print(f"  [网络错误 {e.__class__.__name__}] 重试 {attempt + 1}/{retries}", flush=True)
            time.sleep(min(max_delay, 5 * (2 ** attempt)))
            continue
        if r.status_code == 200:
            return ET.fromstring(r.content)
        wait = (int(r.headers["Retry-After"]) if (r.headers.get("Retry-After") or "").isdigit()
                else min(300, 10 * (2 ** attempt)))
        if attempt == retries:
            r.raise_for_status()
        print(f"  [{r.status_code} 退避{wait}s]", end=" ", flush=True)
        time.sleep(wait)
    raise RuntimeError("unreachable")

def parse_page(root):
    """解析一页 ListRecords，返回 (记录列表, resumptionToken)"""
    rows = []
    for rec in root.findall(".//oai:record", NS):
        hdr = rec.find("oai:header", NS)
        if hdr is not None and hdr.get("status") == "deleted":
            continue
        ident = (hdr.findtext("oai:identifier", default="", namespaces=NS) if hdr is not None else "")
        m = re.search(r"(\d{4})/(\d+)$", ident)
        if not m:
            continue
        pid = f"{m.group(1)}/{m.group(2)}"
        title = rec.findtext(".//dc:title", default="", namespaces=NS) or ""
        authors = "; ".join(x.text.strip() for x in rec.findall(".//dc:creator", NS) if x.text)
        abstract = rec.findtext(".//dc:description", default="", namespaces=NS) or ""
        cats = [s.text.strip() for s in rec.findall(".//dc:subject", NS) if s.text]
        rows.append({
            "标题": re.sub(r"\s+", " ", title).strip(),
            "作者": re.sub(r"\s+", " ", authors).strip(),
            "摘要": re.sub(r"\s+", " ", abstract).strip(),
            "关键词": "",
            "PDF链接": f"{BASE}/{pid}.pdf",
            "分类": "; ".join(cats),
        })
    tok = root.findtext(".//oai:resumptionToken", default="", namespaces=NS) or ""
    return rows, tok.strip()

def main():
    ap = argparse.ArgumentParser(description="ePrint OAI-PMH 批量元数据收割")
    ap.add_argument("--from", dest="frm", default="2017-01-01", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--until", dest="until", default=time.strftime("%Y-%m-%d"), help="结束日期 YYYY-MM-DD")
    ap.add_argument("--out", default="papers_bulk.csv", help="输出CSV")
    ap.add_argument("--state", default="oai_state.txt", help="断点(resumptionToken)文件")
    ap.add_argument("--delay", type=float, default=1.5, help="每页请求间隔秒数")
    ap.add_argument("--max-delay", type=float, default=60.0, help="限流退避上限秒数")
    ap.add_argument("--restart", action="store_true", help="忽略断点，从头收割")
    args = ap.parse_args()

    state = Path(args.state)
    session = requests.Session()
    token = ""
    if state.exists() and not args.restart:
        token = state.read_text(encoding="utf-8").strip()
        if token:
            print(f"从断点继续：token={token[:40]}...")

    new_file = not Path(args.out).exists() or args.restart
    if args.restart and Path(args.out).exists():
        Path(args.out).unlink()
    fh = open(args.out, "a", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
    if new_file:
        writer.writeheader()

    page = done = total = 0
    while True:
        params = ({"verb": "ListRecords", "resumptionToken": token} if token else
                  {"verb": "ListRecords", "metadataPrefix": "oai_dc",
                   "from": args.frm, "until": args.until})
        root = get(session, params, args.delay, args.max_delay)
        err = root.find(".//oai:error", NS)
        if err is not None:
            print(f"OAI 错误：{err.get('code')} {err.text}")
            break
        rows, token = parse_page(root)
        for row in rows:
            if row["标题"]:
                writer.writerow(row)
                done += 1
        fh.flush()
        page += 1
        size = root.find(".//oai:resumptionToken", NS)
        total = (size.get("completeListSize") if size is not None and size.get("completeListSize")
                 else total)
        print(f"第 {page} 页：+{len(rows)} 条，累计 {done}"
              + (f"/{total}" if total else ""), flush=True)
        if not token:
            if state.exists():
                state.unlink()
            break
        state.write_text(token, encoding="utf-8")
        time.sleep(args.delay)

    fh.close()
    print(f"\n完成：共 {done} 条记录（{page} 页）→ {args.out}")

if __name__ == "__main__":
    main()
