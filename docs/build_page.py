"""Render one markdown document as a standalone page in the same style as the blog:
    python docs/build_page.py docs/design.md docs/design.html "docuflow System Design"
Reuses the stylesheet from docs/blog/index.html (build that first with docs/blog/build.py)."""
import html
import re
import sys

import markdown

src, out, title = sys.argv[1], sys.argv[2], sys.argv[3]
style = re.search(r"<style>(.*?)</style>", open("docs/blog/index.html").read(), re.S).group(1)
text = open(src).read()
h1, body = text.split("\n", 1)
status = body.strip().split("\n\n", 1)[0]
body = body.strip().split("\n\n", 1)[1]
h = markdown.markdown(body, extensions=["tables", "fenced_code", "sane_lists", "toc"])
toc = re.findall(r'<h2 id="([^"]+)">(.*?)</h2>', h)
h = re.sub(r"<table>", '<div class="scroll"><table>', h).replace("</table>", "</table></div>")
h = re.sub(r"<pre>", '<div class="scroll code"><pre>', h).replace("</pre>", "</pre></div>")
h = h.replace('href="blog/README.md"', 'href="https://github.com/pramodsarvi/docuflow-systems-lab-/tree/main/docs/blog"')
h = re.sub(r'<h2 id="([^"]+)">', r'</section><section id="\1"><h2 class="parttitle" id="h-\1">', h)
h = "<section>" + h + "</section>"
nav = "".join(f'<li><a href="#{i}"><span class="n">{n.split(".")[0]}</span>{re.sub(r"^[0-9]+[.] ", "", n)}</a></li>' for i, n in toc)
page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>{style}
main > section {{ padding-top: 28px; }} section + section {{ margin-top: 28px; }}
h4 {{ font: 700 19px/1.3 var(--font-display); margin: 30px 0 8px; }}
</style></head><body>
<header class="hero"><p class="kicker">Design reference · docuflow</p>
<h1>{html.escape(re.sub(r"^# ", "", h1))}</h1>
<p class="lede">{html.escape(re.sub(r"[`*]", "", status).replace("](blog/README.md)", "").replace("[", ""))}</p>
<div class="meta"><span>14 decision records</span><span>SLOs with measured values</span><span>capacity model</span><span>roadmap</span></div></header>
<div class="wrap"><nav class="toc" aria-label="Sections"><div class="inner"><p>Contents</p><ol>{nav}</ol></div></nav>
<main>{h}</main></div></body></html>"""
open(out, "w").write(page)
print("wrote", out, len(page))
