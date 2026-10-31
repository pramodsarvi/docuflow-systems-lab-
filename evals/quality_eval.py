"""Quality of what is indexed, without any LLM: extraction vs ground truth, handwriting, and retrieval.

  python evals/quality_eval.py --api http://docuflow.localtest.me --out evals/quality-report.md

Extraction  every dataset that ships a transcription (IAM, FUNSD, XFUND, synthetic): character error rate (CER) and
            order-independent token F1 of the vision model's reading against it. Pages indexed from fallback text are
            excluded from the scores and counted separately.
Handwriting IAM lines are all handwritten: share read near-perfectly (CER < 5%), share missed entirely (CER >= 90%), and
            how often the model labelled the regions `handwritten` (= label recall).
Retrieval   corpus-wide search for synthetic document numbers ("Who signed document INV-1005?"): is the source page the
            first hit (hit@1) or in the top 5 (hit@5)? Measures search alone, without the agent.
"""
import argparse
import os
import random
import statistics
import sys
import time

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from common import cer, pct, token_f1  # noqa: E402

from docuflow import sources  # noqa: E402


def extraction(api, h, datasets, n):
    out = {}
    for ds in datasets:
        rows = httpx.get(f"{api}/eval/extractions", params={"dataset": ds, "n": n}, headers=h, timeout=120).json()
        read = [r for r in rows if not r["used_fallback"] and r["text"] is not None]
        if not read:
            continue
        c = [cer(r["gt_text"], r["text"]) for r in read]
        f = [token_f1(r["gt_text"], r["text"]) for r in read]
        kinds = [k for r in read for k in (r["kinds"] or [])]
        out[ds] = dict(pages=len(read), fallback=len(rows) - len(read), cer_mean=statistics.mean(c), cer_median=statistics.median(c),
                       f1=statistics.mean(f), near_perfect=sum(x < 0.05 for x in c) / len(c), missed=sum(x >= 0.9 for x in c) / len(c),
                       hw_label=sum(k == "handwritten" for k in kinds) / max(1, len(kinds)),
                       read_s=statistics.mean(r["seconds"] for r in read if r["seconds"]) if any(r["seconds"] for r in read) else 0)
    return out


def retrieval(api, h, n, seed):
    docs = httpx.get(f"{api}/documents", params={"dataset": "synthetic", "status": "indexed", "limit": 1000}, headers=h, timeout=60).json()
    idx = [int(d["id"].rsplit("/", 1)[1]) for d in docs if d["id"].startswith("synthetic/gen/")]
    r = random.Random(seed)
    hit1 = hit5 = 0
    lat = []
    sample = r.sample(idx, min(n, len(idx)))
    for i in sample:
        f = dict(line.split(": ", 1) for line in sources.synthetic_page(i)[1].splitlines()[1:])
        q = r.choice([f"Who signed document {f['Number']}?", f"What is the amount on {f['Number']}?",
                      f"Which city is listed on {f['Number']}?"])
        t = time.perf_counter()
        hits = httpx.get(f"{api}/search", params={"q": q, "k": 5}, headers=h, timeout=60).json()
        lat.append(time.perf_counter() - t)
        ids = [x["doc_id"] for x in hits]
        hit1 += bool(ids) and ids[0] == f"synthetic/gen/{i}"
        hit5 += f"synthetic/gen/{i}" in ids
    return dict(n=len(sample), hit1=hit1 / max(1, len(sample)), hit5=hit5 / max(1, len(sample)),
                p50_ms=pct(lat, 50) * 1000, p95_ms=pct(lat, 95) * 1000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("DOCUFLOW_API", "http://docuflow.localtest.me"))
    ap.add_argument("--datasets", default="iam,funsd,xfund,synthetic")
    ap.add_argument("-n", type=int, default=500, help="pages per dataset for extraction")
    ap.add_argument("--retrieval-n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "quality-report.md"))
    a = ap.parse_args()
    h = {"x-api-key": os.environ.get("DOCUFLOW_API_KEY", "")}
    api = a.api.rstrip("/")
    stats = httpx.get(f"{api}/workloads", headers=h, timeout=60).json()
    ext = extraction(api, h, a.datasets.split(","), a.n)
    ret = retrieval(api, h, a.retrieval_n, a.seed)
    total = sum(p["total"] for p in stats["progress"])
    indexed = sum(p["indexed"] for p in stats["progress"])
    L = [f"# docuflow quality eval ({time.strftime('%Y-%m-%d %H:%M')})", "",
         f"Knowledge base: {indexed:,} of {total:,} fetched pages indexed. No LLM involved in these numbers.", "",
         "## Extraction (vision-model reading vs dataset ground truth)", "",
         "| dataset | pages | CER median | CER mean | token F1 | near-perfect (CER<5%) | missed (CER>=90%) | regions labelled handwritten | fallback pages |",
         "|---|---|---|---|---|---|---|---|---|"]
    for ds, m in ext.items():
        L.append(f"| {ds} | {m['pages']} | {m['cer_median']:.1%} | {m['cer_mean']:.1%} | {m['f1']:.3f} | {m['near_perfect']:.0%} | "
                 f"{m['missed']:.1%} | {m['hw_label']:.0%} | {m['fallback']} |")
    if "iam" in ext:
        m = ext["iam"]
        L += ["", "## Handwriting (IAM: every line is handwritten)", "",
              f"- read near-perfectly: **{m['near_perfect']:.0%}** of lines; missed entirely: **{m['missed']:.1%}**",
              f"- handwriting label recall: **{m['hw_label']:.0%}** of regions labelled `handwritten` (should be ~100%)",
              f"- CER median {m['cer_median']:.1%}, mean {m['cer_mean']:.1%}; token F1 {m['f1']:.3f}"]
    L += ["", "## Retrieval (search only, corpus-wide)", "",
          f"{ret['n']} questions naming a synthetic document number, against the whole knowledge base.", "",
          "| hit@1 | hit@5 | search p50 | search p95 |", "|---|---|---|---|",
          f"| {ret['hit1']:.1%} | {ret['hit5']:.1%} | {ret['p50_ms']:.0f} ms | {ret['p95_ms']:.0f} ms |", "",
          "CER is measured on lower-cased, punctuation-free text; on forms it also penalises reading order, so token F1 is "
          "the better content measure there."]
    open(a.out, "w").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
