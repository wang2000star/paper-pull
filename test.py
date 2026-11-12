import requests
from bs4 import BeautifulSoup
r = requests.get("https://eprint.iacr.org/2026/001",
                 headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
soup = BeautifulSoup(r.text, "html.parser")
print("== citation meta 标签 ==")
for m in soup.select('meta[name^="citation_"]'):
    print(" ", m.get("name"), "=>", m.get("content"))
print("== 标题的下一个兄弟元素 ==")
h3 = soup.find("h3")
sib = h3.find_next_sibling() if h3 else None
print(sib.prettify()[:400] if sib else "(无)")
