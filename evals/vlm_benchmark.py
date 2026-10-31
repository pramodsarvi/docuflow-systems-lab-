"""Benchmark a page-reading model server: speed and quality on the same ground-truth pages.

  python evals/vlm_benchmark.py --vlm-url http://127.0.0.1:18101/v1 --model vlm-model --label bf16 -n 50 -c 24
  python evals/vlm_benchmark.py --vlm-url http://127.0.0.1:18102/v1 --model vlm-fp8  --label fp8  -n 50 -c 24
  python evals/vlm_benchmark.py --compare evals/vlm-bench-bf16.json evals/vlm-bench-fp8.json

Pages come from the knowledge base (`GET /eval/extractions`, images from `GET /documents/{id}/image`) and are the same for
every run with the same seed. Each page is read with docuflow's own prompt and schema (extract.read_page), so the numbers
reflect production behaviour. For a fair speed comparison run each model with the GPU otherwise idle.
"""
import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from common import cer, pct, token_f1  # noqa: E402

from docuflow import config, extract  # noqa: E402


def pages(api, h, datasets, n, seed):
    out = []
    for ds in datasets:
        rows = httpx.get(f"{api}/eval/extractions", params={"dataset": ds, "n": n, "seed": seed}, headers=h, timeout=120).json()
        for r in rows:
            img = httpx.get(f"{api}/documents/{r['id']}/image", headers=h, timeout=60).content
            out.append({"id": r["id"], "dataset": ds, "gt": r["gt_text"], "image": img})
    return out


def read(p):
    t = time.perf_counter()
    try:
        regions, _ = extract.read_page(p["image"])
        text = "\n".join(r["text"] for r in regions)
        kinds = [r["kind"] for r in regions]
        err = None
    except Exception as e:
        text, kinds, err = "", [], f"{type(e).__name__}: {str(e)[:120]}"
    return {"id": p["id"], "dataset": p["dataset"], "seconds": time.perf_counter() - t, "cer": cer(p["gt"], text),
            "f1": token_f1(p["gt"], text), "kinds": kinds, "error": err}


def summarise(res, wall, label, concurrency):
    by = {}
    for ds in sorted({r["dataset"] for r in res}):
        rs = [r for r in res if r["dataset"] == ds]
        ks = [k for r in rs for k in r["kinds"]]
        by[ds] = dict(pages=len(rs), cer_median=statistics.median(r["cer"] for r in rs), cer_mean=statistics.mean(r["cer"] for r in rs),
                      f1=statistics.mean(r["f1"] for r in rs), missed=sum(r["cer"] >= 0.9 for r in rs) / len(rs),
                      hw_label=sum(k == "handwritten" for k in ks) / max(1, len(ks)), errors=sum(bool(r["error"]) for r in rs),
                      p50_s=pct([r["seconds"] for r in rs], 50))
    return {"label": label, "concurrency": concurrency, "pages": len(res), "wall_s": wall,
            "pages_per_min": len(res) / wall * 60, "by_dataset": by}


def compare(paths):
    runs = [json.load(open(p)) for p in paths]
    L = ["| metric | " + " | ".join(r["label"] for r in runs) + " |", "|---|" + "---|" * len(runs)]
    L.append("| pages/min (c=%s) | " % runs[0]["concurrency"] + " | ".join(f"{r['pages_per_min']:.1f}" for r in runs) + " |")
    for ds in runs[0]["by_dataset"]:
        for key, name, fmt in (("cer_median", "CER median", "{:.1%}"), ("f1", "token F1", "{:.3f}"), ("missed", "missed", "{:.1%}"),
                               ("hw_label", "labelled handwritten", "{:.0%}"), ("p50_s", "read p50", "{:.1f}s")):
            L.append(f"| {ds}: {name} | " + " | ".join(fmt.format(r["by_dataset"].get(ds, {}).get(key, float('nan'))) for r in runs) + " |")
    print("\n".join(L))
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("DOCUFLOW_API", "http://docuflow.localtest.me"))
    ap.add_argument("--vlm-url", help="OpenAI-compatible base URL of the page reader (ending in /v1)")
    ap.add_argument("--model", default="vlm-model")
    ap.add_argument("--label", default="run")
    ap.add_argument("--datasets", default="iam,funsd,xfund,synthetic")
    ap.add_argument("-n", type=int, default=50, help="pages per dataset")
    ap.add_argument("-c", "--concurrency", type=int, default=24)
    ap.add_argument("--seed", type=float, default=0.42)
    ap.add_argument("--compare", nargs="+")
    a = ap.parse_args()
    if a.compare:
        return compare(a.compare)
    config.BACKEND = "openai"
    config.ENDPOINTS["vlm"] = (a.vlm_url.rstrip("/"), "none")
    config.VLM_MODEL = a.model
    h = {"x-api-key": os.environ.get("DOCUFLOW_API_KEY", "")}
    ps = pages(a.api.rstrip("/"), h, a.datasets.split(","), a.n, a.seed)
    print(f"{a.label}: {len(ps)} pages, concurrency {a.concurrency}", flush=True)
    t0 = time.perf_counter()
    with ThreadPoolExecutor(a.concurrency) as ex:
        res = list(ex.map(read, ps))
    s = summarise(res, time.perf_counter() - t0, a.label, a.concurrency)
    out = os.path.join(os.path.dirname(__file__), f"vlm-bench-{a.label}.json")
    json.dump({**s, "results": res}, open(out, "w"), indent=1, default=str)
    print(json.dumps({k: v for k, v in s.items() if k != "by_dataset"}, indent=1))
    for ds, m in s["by_dataset"].items():
        print(f"  {ds:10} CER med {m['cer_median']:.1%}  F1 {m['f1']:.3f}  missed {m['missed']:.1%}  hw-label {m['hw_label']:.0%}  "
              f"errors {m['errors']}  p50 {m['p50_s']:.1f}s")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
