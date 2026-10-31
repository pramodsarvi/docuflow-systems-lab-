"""Evaluate extraction quality, agentic-RAG answer accuracy, and query latency during vs outside ingestion.

  python evals/run_eval.py --qa 12          # writes evals/report.md
Run it while an ingest is in flight to populate the "during ingest" latency row.
"""
import argparse
import re
import statistics
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from docuflow import agent, db  # noqa: E402


def norm(s):
    return re.sub(r"[^\w\s]", "", (s or "").lower()).split()


def cer(ref, hyp):
    a, b = " ".join(norm(ref)), " ".join(norm(hyp))
    if not a:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] / len(a)


def token_f1(ref, hyp):
    r, h = norm(ref), norm(hyp)
    if not r or not h:
        return 0.0
    common = sum(min(r.count(w), h.count(w)) for w in set(r))
    if not common:
        return 0.0
    p, rc = common / len(h), common / len(r)
    return 2 * p * rc / (p + rc)


def extraction_quality():
    with db.pool().connection() as c:
        rows = c.execute("""SELECT d.dataset, d.gt_text, e.text, e.seconds, e.used_fallback, e.regions
                            FROM documents d JOIN extractions e ON e.doc_id=d.id WHERE d.gt_text IS NOT NULL AND e.model <> 'gt'""").fetchall()
    out = {}
    for ds in sorted({r["dataset"] for r in rows}):
        rs = [r for r in rows if r["dataset"] == ds]
        kinds = [x["kind"] for r in rs for x in r["regions"]]
        out[ds] = dict(n=len(rs), cer=statistics.mean(cer(r["gt_text"], r["text"]) for r in rs),
                       f1=statistics.mean(token_f1(r["gt_text"], r["text"]) for r in rs),
                       sec=statistics.mean(r["seconds"] for r in rs), fallbacks=sum(r["used_fallback"] for r in rs),
                       hw=sum(k == "handwritten" for k in kinds) / max(1, len(kinds)))
    return out


def correct(answer, gts):
    a = " ".join(norm(answer))
    return any(" ".join(norm(g)) and (" ".join(norm(g)) in a) for g in gts)


def qa_eval(n):
    with db.pool().connection() as c:
        docs = c.execute("SELECT id, gt_qa FROM documents WHERE dataset='docvqa' AND status='indexed' AND gt_qa IS NOT NULL").fetchall()
    items = [(d["id"], q) for d in docs for q in d["gt_qa"]][:n]
    res = []
    for doc_id, q in items:
        r = agent.run(q["question"], dataset="docvqa")
        res.append(dict(q=q["question"], gold=q["answers"], pred=r["answer"], ok=correct(r["answer"], q["answers"]),
                        cited=doc_id in r["citations"], steps=r["steps"], sec=r["seconds"], ingest=r["during_ingest"]))
        print(("PASS " if res[-1]["ok"] else "FAIL ") + q["question"][:70], "->", r["answer"][:60], flush=True)
    return res


def latency():
    with db.pool().connection() as c:
        return c.execute("""SELECT during_ingest, count(*) n, avg(seconds) mean, percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds) p50,
                            max(seconds) mx FROM queries GROUP BY 1 ORDER BY 1""").fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--qa", type=int, default=12)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "report.md"))
    a = ap.parse_args()
    ext, qa = extraction_quality(), qa_eval(a.qa)
    lines = ["# docuflow eval report\n", "## Page reading (vision model vs dataset ground truth)\n",
             "| dataset | pages | CER | token F1 | s/page | handwritten share | GT fallbacks |", "|---|---|---|---|---|---|---|"]
    for ds, m in ext.items():
        lines.append(f"| {ds} | {m['n']} | {m['cer']:.2f} | {m['f1']:.2f} | {m['sec']:.1f} | {m['hw']:.0%} | {m['fallbacks']} |")
    if qa:
        acc = sum(r["ok"] for r in qa) / len(qa)
        lines += ["\n## Agentic RAG on DocVQA (answer contains a gold answer)\n",
                  f"- questions: {len(qa)}, accuracy: **{acc:.0%}**, cited the source page: {sum(r['cited'] for r in qa) / len(qa):.0%}",
                  f"- mean steps: {statistics.mean(r['steps'] for r in qa):.1f}\n", "| result | question | gold | predicted |", "|---|---|---|---|"]
        lines += [f"| {'ok' if r['ok'] else 'miss'} | {r['q'][:60]} | {r['gold'][0][:30]} | {r['pred'][:60].replace('|', '/')} |" for r in qa]
    lines += ["\n## Query latency (seconds)\n", "| during ingest | queries | mean | p50 | max |", "|---|---|---|---|---|"]
    lines += [f"| {r['during_ingest']} | {r['n']} | {r['mean']:.1f} | {r['p50']:.1f} | {r['mx']:.1f} |" for r in latency()]
    open(a.out, "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
