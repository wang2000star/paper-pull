#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_eprint.py —— IACR ePrint 论文元数据批量采集脚本
用法：
  pip install requests beautifulsoup4 pymupdf
  python fetch_eprint.py 2026/1385                 # 单篇
  python fetch_eprint.py 2026/1300-2026/1400       # 区间
  python fetch_eprint.py --latest 100              # 首页最新100篇
  python fetch_eprint.py --file ids.txt            # 从文件读ID，每行一个
  python fetch_eprint.py --mode pdf 2026/1385      # 下载PDF解析后立即删除
"""
import argparse, csv, re, time
from pathlib import Path
import requests
from bs4 import BeautifulSoup
BASE = "https://eprint.iacr.org"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
CSV_HEADER = ["标题", "作者", "摘要", "关键词", "PDF链接"]
# 运行期状态：被 Cloudflare 限流(429)时自动放大请求间隔
STATE = {"delay": 1.0, "hit429": False}

class _NotFound:
    """表示论文ID不存在(404)，与抓取失败区分开"""
    def __bool__(self): return False

NOT_FOUND = _NotFound()

def retry_wait(resp, attempt):
    """429/5xx 的退避时长：优先 Retry-After，否则指数退避"""
    ra = (resp.headers.get("Retry-After") or "").strip()
    if ra.isdigit():
        return min(600, int(ra))
    return min(300, 10 * (2 ** attempt))
# ---------- 论文ID ----------
def expand_ids(tokens):
    ids = []
    for tok in tokens:
        m = re.fullmatch(r"(\d{4})/(\d+)-\1/(\d+)", tok) \
            or re.fullmatch(r"(\d{4})/(\d+)-(\d+)", tok)
        if m:  # 区间，如 2026/1300-2026/1400 或 2026/1300-1400
            y, lo, hi = m.group(1), int(m.group(2)), int(m.group(3))
            ids += [f"{y}/{n:03d}" for n in range(lo, hi + 1)]
        else:
            ids.append(tok.strip())
    return ids
def latest_ids(session, n):
    """从首页抓最新 n 篇论文的 ID"""
    r = session.get(BASE + "/", headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    seen, out = set(), []
    for a in soup.select("a[href]"):
        m = re.fullmatch(r"/(\d{4}/\d+)/?", a.get("href", ""))
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(m.group(1))
    return out[:n]
# ---------- 落地页元数据解析（推荐，不下载PDF） ----------
def parse_landing(pid, session, retries=6):
    url = f"{BASE}/{pid}"
    r = None
    for attempt in range(retries + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
        except requests.RequestException:
            if attempt == retries:
                return None
            time.sleep(min(120, 5 * (2 ** attempt)))
            continue
        if r.status_code == 404:
            return NOT_FOUND  # 论文不存在
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == retries:
                return None
            wait = retry_wait(r, attempt)
            STATE["hit429"] = True
            print(f"[{r.status_code} 退避{wait:.0f}s]", end=" ", flush=True)
            time.sleep(wait)
            continue
        try:
            r.raise_for_status()
        except requests.RequestException:
            if attempt == retries:
                return None
            time.sleep(5)
            continue
        break
    if r is None:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    def text_of(sel):
        el = soup.select_one(sel)
        return el.get_text(" ", strip=True) if el else ""
    title    = text_of("h3.mb-3") or text_of("h1")
    authors  = "; ".join(m.get("content", "").strip()
                for m in soup.select('meta[name="citation_author"]')) \
               or text_of("p.fst-italic")
    abstract = text_of('p[style*="pre-wrap"]')
    keywords = [a.get_text(strip=True)
                for a in soup.select("a.badge.bg-secondary.keyword")]
    if not title:
        return None
    return {
        "标题": title,
        "作者": authors,
        "摘要": re.sub(r"\s+", " ", abstract),
        "关键词": "; ".join(keywords),
        "PDF链接": f"{BASE}/{pid}.pdf",
    }
# ---------- PDF 模式：下载 -> 解析 -> 删除 ----------
def download_pdf(pid, session, pdf_dir):
    try:
        r = session.get(f"{BASE}/{pid}.pdf", headers=HEADERS, timeout=120)
    except requests.RequestException:
        return None
    if r.status_code != 200 or not r.content.startswith(b"%PDF-"):
        return None  # 被拦截或不存在（校验 %PDF- 魔数）
    path = pdf_dir / f"{pid.replace('/', '-')}.pdf"
    path.write_bytes(r.content)
    return path
def parse_pdf(path):
    """从PDF首页启发式抽取元数据（可靠性低于落地页，仅作兜底）"""
    import fitz  # PyMuPDF
    doc = fitz.open(path)
    page = doc[0]
    spans = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for sp in line["spans"]:
                if sp["text"].strip():
                    spans.append((round(sp["size"], 1), sp["text"].strip()))
    title = ""
    if spans:  # 标题 = 首页前25个文本块中字号最大的那些
        biggest = max(s for s, _ in spans[:25])
        title = " ".join(t for s, t in spans[:25] if s == biggest)
    text = page.get_text()
    doc.close()
    m = re.search(r"Abstract[\s.:—-]*(.*?)(?=\bKeywords?\b|\bIntroduction\b|\bCCS\b)",
                  text, re.S | re.I)
    abstract = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    m = re.search(r"Keywords?[\s.:—-]*(.*?)(?=\n\s*\n|\bIntroduction\b|\bCCS\b|\bACM\b)",
                  text, re.S | re.I)
    kw = [k.strip() for k in re.split(r"[;,；，]", m.group(1)) if k.strip()] if m else []
    authors = ""
    if title:
        idx = text.find(title.split()[-1])
        m2 = re.search(r"Abstract", text, re.I)
        if idx >= 0 and m2 and m2.start() > idx:
            authors = " ".join(l.strip() for l in text[idx:m2.start()].splitlines()
                               if l.strip()).replace(title, "", 1).strip(" ,;")
    return {"标题": title, "作者": authors, "摘要": abstract, "关键词": "; ".join(kw)}
# ---------- 断点续传与写入 ----------
def load_done(out_csv):
    done = set()
    if Path(out_csv).exists():
        with open(out_csv, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                m = re.search(r"/(\d{4}/\d+)\.pdf", row.get("PDF链接", ""))
                if m:
                    done.add(m.group(1))
    return done
def append_row(out_csv, row):
    new_file = not Path(out_csv).exists()
    with open(out_csv, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        if new_file:
            w.writeheader()
        w.writerow(row)
def main():
    ap = argparse.ArgumentParser(description="IACR ePrint 论文元数据采集")
    ap.add_argument("ids", nargs="*", help="论文ID，如 2026/1385 或 2026/1300-2026/1400")
    ap.add_argument("--file", help="从文本文件读取ID（每行一个）")
    ap.add_argument("--latest", type=int, metavar="N", help="抓取首页最新N篇")
    ap.add_argument("--out", default="papers.csv", help="输出CSV路径")
    ap.add_argument("--mode", choices=["html", "pdf"], default="html",
                    help="html=只解析落地页(默认，不下载PDF)；pdf=下载PDF解析后删除")
    ap.add_argument("--pdf-dir", default="papers_tmp", help="PDF临时目录")
    ap.add_argument("--delay", type=float, default=1.0, help="两次请求间隔秒数")
    ap.add_argument("--max-delay", type=float, default=20.0, help="被限流时自适应间隔的上限秒数")
    ap.add_argument("--fail-log", default="failed_ids.txt", help="抓取失败的ID记录文件")
    ap.add_argument("--keep-pdf", action="store_true", help="pdf模式下保留PDF（默认删除）")
    args = ap.parse_args()
    tokens = list(args.ids)
    if args.file:
        tokens += [l.strip() for l in Path(args.file).read_text(encoding="utf-8").splitlines()
                   if l.strip() and not l.startswith("#")]
    session = requests.Session()
    if args.latest:
        tokens += latest_ids(session, args.latest)
    if not tokens:
        ap.error("请指定论文ID / --file / --latest 至少一项")
    ids = expand_ids(tokens)
    done = load_done(args.out)
    pdf_dir = Path(args.pdf_dir)
    if args.mode == "pdf":
        pdf_dir.mkdir(exist_ok=True)
    ok = fail = miss = skip = 0
    delay = args.delay
    STATE["delay"] = delay
    fail_fh = open(args.fail_log, "a", encoding="utf-8") if args.fail_log else None
    for i, pid in enumerate(ids, 1):
        if pid in done:
            skip += 1
            continue
        print(f"[{i}/{len(ids)}] {pid} ...", end=" ", flush=True)
        row = None
        if args.mode == "html":
            row = parse_landing(pid, session)
        else:
            path = download_pdf(pid, session, pdf_dir)
            if path:
                row = {**parse_pdf(path), "PDF链接": f"{BASE}/{pid}.pdf"}
                if not args.keep_pdf:
                    path.unlink()  # 解析完立即删除PDF
            if not row or not row.get("标题"):
                row = parse_landing(pid, session)  # PDF被拦/解析失败时回退落地页
        if row is NOT_FOUND:
            miss += 1
            print("SKIP(404 不存在)")
        elif row and row.get("标题"):
            append_row(args.out, row)
            done.add(pid)
            ok += 1
            print(f"OK  {row['标题'][:60]}")
        else:
            fail += 1
            print("FAIL")
            if fail_fh:
                fail_fh.write(pid + "\n")
                fail_fh.flush()
        # 自适应限速：被限流则放慢，长时间顺畅则逐步回到基准速度
        if STATE["hit429"]:
            delay = min(args.max_delay, max(delay * 1.5, 2.0))
            STATE["hit429"] = False
            print(f"    (限流：请求间隔 -> {delay:.1f}s)")
        elif delay > args.delay:
            delay = max(args.delay, delay * 0.95)
        time.sleep(delay)
    if fail_fh:
        fail_fh.close()
    print(f"\n完成：成功 {ok}，跳过(已存在) {skip}，404不存在 {miss}，失败 {fail}"
          f"，当前请求间隔 {delay:.1f}s\n结果写入 {args.out}"
          + (f"\n失败ID已记录到 {args.fail_log}" if fail else ""))
if __name__ == "__main__":
    main()
