"""Build docs/blog/index.html from the markdown parts:  python docs/blog/build.py docs/blog docs/blog/index.html  (needs `pip install markdown`)"""
import html
import re
import sys

import markdown

SRC = sys.argv[1]          # docs/blog
OUT = sys.argv[2]
parts = ["README.md", "01-system.md", "02-hpa-and-keda.md", "03-queue-design.md", "04-scaledobjects.md",
         "05-gpus.md", "06-stress-tests.md", "07-what-broke.md", "08-recreate.md"]
ids = ["summary", "part-1", "part-2", "part-3", "part-4", "part-5", "part-6", "part-7", "part-8"]


def md(text):
    return markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])


sections, toc = [], []
for f, sid in zip(parts, ids, strict=True):
    text = open(f"{SRC}/{f}").read()
    if f == "README.md":
        text = text.split("## The parts")[0] + "\n## Environment" + text.split("## Environment", 1)[1].split("\n", 1)[1]
        title_line, body = text.split("\n", 1)
        lede = re.search(r"\*(.+?)\*", body, re.S).group(1)
        body = body.replace(f"*{lede}*", "", 1)
        h = md(body)
        sections.append(f'<section id="{sid}" class="part">{h}</section>')
        continue
    title_line, body = text.split("\n", 1)
    m = re.match(r"# Part (\d)\. (.+)", title_line)
    num, title = m.group(1), m.group(2)
    toc.append((sid, num, title))
    h = md(body)
    sections.append(f'<section id="{sid}" class="part"><p class="partno">Part {num}</p><h2 class="parttitle">{html.escape(title)}</h2>{h}</section>')

body_html = "\n".join(sections)
# Links between parts -> in-page anchors
for f, sid in zip(parts, ids, strict=True):
    body_html = body_html.replace(f'href="{f}"', f'href="#{sid}"')
# demote headings inside parts: h2 -> h3 (part title is the h2)
body_html = re.sub(r"<h2>(.*?)</h2>", r"<h3>\1</h3>", body_html)
body_html = body_html.replace('<h3>Summary</h3>', '<h2 class="parttitle">Summary</h2>', 1)
body_html = body_html.replace('<h3>Environment used for every number in this write-up</h3>', '<h3>Environment used for every number in this write-up</h3>', 1)
# wrap tables and code for horizontal scroll on phones
body_html = re.sub(r"<table>", '<div class="scroll"><table>', body_html).replace("</table>", "</table></div>")
body_html = re.sub(r"<pre>", '<div class="scroll code"><pre>', body_html).replace("</pre>", "</pre></div>")


def latency_chart():
    data = [("light", 3.3, 4.3), ("8 + ingest", 5.5, 10.2), ("24 + ingest", 8.1, 12.4),
            ("48 + ingest", 17.4, 22.4), ("96 + 2 ingests", 25.5, 42.7)]
    W, H, L, B, T = 640, 300, 44, 54, 18
    ymax = 45
    pw = (W - L - 12) / len(data)
    y = lambda v: T + (H - T - B) * (1 - v / ymax)
    g = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Query latency p50 and p95 by load step">']
    for v in range(0, 46, 10):
        g.append(f'<line class="grid" x1="{L}" x2="{W-8}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                 f'<text class="axis" x="{L-8}" y="{y(v)+4:.1f}" text-anchor="end">{v}s</text>')
    for i, (lab, p50, p95) in enumerate(data):
        x0 = L + i * pw + pw * 0.16
        bw = pw * 0.32
        g.append(f'<rect class="bar-a" x="{x0:.1f}" y="{y(p50):.1f}" width="{bw:.1f}" height="{y(0)-y(p50):.1f}" rx="2"/>')
        g.append(f'<rect class="bar-b" x="{x0+bw+3:.1f}" y="{y(p95):.1f}" width="{bw:.1f}" height="{y(0)-y(p95):.1f}" rx="2"/>')
        g.append(f'<text class="val" x="{x0+bw/2:.1f}" y="{y(p50)-5:.1f}" text-anchor="middle">{p50:g}</text>')
        g.append(f'<text class="val" x="{x0+bw*1.5+3:.1f}" y="{y(p95)-5:.1f}" text-anchor="middle">{p95:g}</text>')
        g.append(f'<text class="axis" x="{L+i*pw+pw/2:.1f}" y="{H-B+18}" text-anchor="middle">{lab}</text>')
    g.append(f'<text class="axis" x="{L+(W-L)/2:.1f}" y="{H-8}" text-anchor="middle">concurrent query clients</text>')
    g.append("</svg>")
    return ('<figure class="chart"><figcaption><b>Query latency by load step.</b> '
            '<span class="key a">p50</span> <span class="key b">p95</span> Throughput flattened at ~130 queries/min '
            'from 48 clients up; only latency kept growing.</figcaption>' + "".join(g) + "</figure>")


def ingest_chart():
    data = [("Run 1: cap of 8, one effective vLLM replica", 34), ("Run 2: in-app cap raised", 56.5),
            ("Ingest mode: 3 vLLM replicas, 48 workers", 170)]
    W, rowh, L = 640, 46, 300
    H = rowh * len(data) + 26
    xmax = 180
    x = lambda v: L + (W - L - 50) * v / xmax
    g = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Ingest rate by configuration">']
    for v in (0, 50, 100, 150):
        g.append(f'<line class="grid" x1="{x(v):.1f}" x2="{x(v):.1f}" y1="6" y2="{H-20}"/>'
                 f'<text class="axis" x="{x(v):.1f}" y="{H-4}" text-anchor="middle">{v}</text>')
    for i, (lab, v) in enumerate(data):
        yy = 10 + i * rowh
        cls = "bar-a" if i == len(data) - 1 else "bar-c"
        g.append(f'<text class="label" x="{L-10}" y="{yy+19}" text-anchor="end">{html.escape(lab)}</text>')
        g.append(f'<rect class="{cls}" x="{L}" y="{yy+4}" width="{x(v)-L:.1f}" height="22" rx="2"/>')
        g.append(f'<text class="val" x="{x(v)+6:.1f}" y="{yy+20}">{v:g}/min</text>')
    g.append("</svg>")
    return ('<figure class="chart"><figcaption><b>Ingest rate, same single L40S.</b> Pages indexed per minute as the '
            'scaling bugs were fixed and the GPU was dedicated to ingest.</figcaption>' + "".join(g) + "</figure>")


body_html = body_html.replace("<h3>Query latency under load</h3>", "<h3>Query latency under load</h3>" + latency_chart(), 1)
body_html = body_html.replace("<p>The page mix matters as much as the configuration.", ingest_chart() + "<p>The page mix matters as much as the configuration.", 1)

toc_html = "".join(f'<li><a href="#{sid}"><span class="n">{n}</span>{html.escape(t)}</a></li>' for sid, n, t in toc)

page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scaling on Queue Depth</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root {{
  /* Layout: one reading column (~68ch) with a sticky part index on wide screens; tables/code scroll in place. */
  --paper: #f6f7f9;
  --ink: #15181e;
  --muted: #5b6372;
  --rule: #dde1e8;
  --panel: #ffffff;
  --accent: #1f4fd1;
  --accent-2: #e2892b;
  --code-bg: #eef1f6;
  --font-display: "Bricolage Grotesque", "Segoe UI", system-ui, sans-serif;
  --font-body: "Source Serif 4", Georgia, "Times New Roman", serif;
  --font-mono: "JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
  color-scheme: light;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --paper: #101318; --ink: #e7eaf0; --muted: #9aa3b2; --rule: #262c36; --panel: #161a21;
    --accent: #8fb0ff; --accent-2: #f0a75a; --code-bg: #1a1f28; color-scheme: dark;
  }}
}}
:root[data-theme="dark"] {{
  --paper: #101318; --ink: #e7eaf0; --muted: #9aa3b2; --rule: #262c36; --panel: #161a21;
  --accent: #8fb0ff; --accent-2: #f0a75a; --code-bg: #1a1f28; color-scheme: dark;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; }}
body {{ background: var(--paper); color: var(--ink); font: 400 18px/1.62 var(--font-body); -webkit-font-smoothing: antialiased; }}
.wrap {{ display: grid; grid-template-columns: minmax(0, 1fr); gap: 48px; max-width: 1120px; margin: 0 auto; padding: 0 20px 96px; }}
@media (min-width: 1000px) {{ .wrap {{ grid-template-columns: 240px minmax(0, 1fr); }} }}
header.hero {{ max-width: 1120px; margin: 0 auto; padding: 56px 20px 28px; border-bottom: 1px solid var(--rule); }}
.kicker {{ font: 600 13px/1 var(--font-mono); letter-spacing: .08em; text-transform: uppercase; color: var(--accent); margin: 0 0 18px; }}
h1 {{ font: 700 clamp(34px, 5.4vw, 60px)/1.04 var(--font-display); letter-spacing: -.02em; margin: 0 0 18px; max-width: 18ch; text-wrap: balance; }}
.lede {{ font-size: 21px; color: var(--muted); max-width: 60ch; margin: 0; font-style: italic; }}
.meta {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 24px; }}
.meta span {{ font: 400 13px/1 var(--font-mono); border: 1px solid var(--rule); background: var(--panel); padding: 7px 10px; border-radius: 4px; color: var(--muted); }}
nav.toc {{ position: relative; }}
@media (min-width: 1000px) {{ nav.toc .inner {{ position: sticky; top: 24px; }} }}
nav.toc .inner {{ padding-top: 36px; }}
nav.toc p {{ font: 600 12px/1 var(--font-mono); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); margin: 0 0 12px; }}
nav.toc ol {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 2px; }}
nav.toc a {{ display: flex; gap: 10px; padding: 7px 8px; border-radius: 4px; color: var(--ink); text-decoration: none; font: 500 15px/1.3 var(--font-display); }}
nav.toc a:hover, nav.toc a:focus-visible {{ background: var(--panel); outline: none; box-shadow: inset 2px 0 0 var(--accent); }}
nav.toc .n {{ font: 600 13px/1.5 var(--font-mono); color: var(--accent); min-width: 1.2em; }}
main {{ min-width: 0; }}
main > section {{ max-width: 68ch; padding-top: 36px; }}
.partno {{ font: 600 13px/1 var(--font-mono); letter-spacing: .08em; text-transform: uppercase; color: var(--accent); margin: 28px 0 10px; }}
.parttitle {{ font: 700 clamp(28px, 3.6vw, 38px)/1.12 var(--font-display); letter-spacing: -.015em; margin: 0 0 18px; text-wrap: balance; }}
section + section {{ border-top: 1px solid var(--rule); margin-top: 40px; }}
h3 {{ font: 700 23px/1.25 var(--font-display); margin: 38px 0 10px; text-wrap: balance; }}
p, ul, ol {{ margin: 0 0 16px; }}
li {{ margin: 4px 0; }}
strong {{ font-weight: 600; }}
a {{ color: var(--accent); text-underline-offset: 3px; }}
a:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
code {{ font: 400 .84em/1.4 var(--font-mono); background: var(--code-bg); padding: .1em .35em; border-radius: 3px; }}
.scroll {{ overflow-x: auto; margin: 0 0 20px; max-width: 100%; }}
.code {{ background: var(--code-bg); border-radius: 6px; border-left: 3px solid var(--accent); }}
pre {{ margin: 0; padding: 14px 16px; font: 400 13.5px/1.55 var(--font-mono); }}
pre code {{ background: none; padding: 0; font-size: inherit; }}
table {{ border-collapse: collapse; font: 400 15px/1.45 var(--font-body); min-width: 100%; font-variant-numeric: tabular-nums; }}
th {{ text-align: left; font: 600 13px/1.3 var(--font-mono); text-transform: uppercase; letter-spacing: .04em; color: var(--muted); border-bottom: 2px solid var(--ink); padding: 8px 12px 8px 0; white-space: nowrap; }}
td {{ border-bottom: 1px solid var(--rule); padding: 8px 12px 8px 0; vertical-align: top; }}
td code {{ white-space: nowrap; }}
figure.chart {{ margin: 8px 0 26px; padding: 16px 16px 8px; background: var(--panel); border: 1px solid var(--rule); border-radius: 6px; }}
figure.chart svg {{ width: 100%; height: auto; display: block; }}
figcaption {{ font: 400 15px/1.45 var(--font-body); color: var(--muted); margin-bottom: 10px; }}
figcaption b {{ color: var(--ink); font-weight: 600; }}
.key {{ font: 600 12px/1 var(--font-mono); padding: 3px 7px; border-radius: 3px; color: var(--paper); }}
.key.a {{ background: var(--accent); }} .key.b {{ background: var(--accent-2); }}
svg .grid {{ stroke: var(--rule); stroke-width: 1; }}
svg .axis {{ fill: var(--muted); font: 400 12px var(--font-mono); }}
svg .label {{ fill: var(--ink); font: 500 13px var(--font-display); }}
svg .val {{ fill: var(--ink); font: 600 12px var(--font-mono); }}
svg .bar-a {{ fill: var(--accent); }} svg .bar-b {{ fill: var(--accent-2); }} svg .bar-c {{ fill: var(--muted); opacity: .55; }}
footer {{ max-width: 68ch; color: var(--muted); font-size: 15px; margin-top: 56px; border-top: 1px solid var(--rule); padding-top: 18px; }}
@media (max-width: 420px) {{ body {{ font-size: 17px; }} .lede {{ font-size: 18px; }} }}
@media (prefers-reduced-motion: reduce) {{ * {{ scroll-behavior: auto !important; }} }}
html {{ scroll-behavior: smooth; }}
</style>
</head>
<body>
<header class="hero">
  <p class="kicker">Field report · KEDA · HPA · RabbitMQ · vLLM</p>
  <h1>Scaling on queue depth with KEDA, HPA and one GPU</h1>
  <p class="lede">{html.escape(lede.strip())}</p>
  <div class="meta"><span>1× NVIDIA L40S 46 GB</span><span>kind · Kubernetes 1.31</span><span>KEDA 2.15</span><span>vLLM 0.13</span><span>~16,000 pages</span></div>
</header>
<div class="wrap">
  <nav class="toc" aria-label="Parts"><div class="inner"><p>Contents</p><ol><li><a href="#summary"><span class="n">·</span>Summary</a></li>{toc_html}</ol></div></nav>
  <main>
{body_html}
<footer>Source, manifests and scripts: <a href="https://github.com/pramodsarvi/docuflow-systems-lab-">github.com/pramodsarvi/docuflow-systems-lab-</a> (docs/blog has this write-up as markdown). All measurements were taken on the environment listed in the summary.</footer>
  </main>
</div>
</body>
</html>
"""
open(OUT, "w").write(page)
print("wrote", OUT, len(page), "bytes")
