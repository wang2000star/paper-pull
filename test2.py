src = open("fetch_iacr.py", encoding="utf-8").read()
old = 'authors  = text_of("p.fst-italic")'
new = ('authors  = "; ".join(m.get("content", "").strip()\n'
       '                for m in soup.select(\'meta[name="citation_author"]\')) \\\n'
       '               or text_of("p.fst-italic")')
assert old in src, "未找到目标行，文件可能已被改动"
open("fetch_iacr.py", "w", encoding="utf-8").write(src.replace(old, new))
print("补丁已写入")
