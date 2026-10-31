"""RAGAS evaluation of the agentic RAG, judged by the cluster's own models (no external API).

  make ragas N=40                       # port-forwards the vLLM services and writes evals/ragas-report.md
  python evals/ragas_eval.py --api http://docuflow.localtest.me --llm-url http://127.0.0.1:18002/v1 \
      --embed-url http://127.0.0.1:18000/v1 -n 40

Questions come from indexed synthetic pages with known answers ("Who signed document INV-1005?", amount, city), so
the reference is exact. (For DocVQA's own question/answer pairs use evals/run_eval.py.)
Each question goes through the real `POST /query` agent loop. Contexts are what the agent actually observed (search
hits, full pages, image re-reads), taken from the returned trace, so the context metrics grade the agent's retrieval and
not a separate retriever.

Metrics (RAGAS 0.2):
  faithfulness           is every claim in the answer supported by the retrieved contexts?   (hallucination)
  answer_relevancy       does the answer address the question?                               (needs embeddings)
  context_precision      are the relevant contexts ranked above irrelevant ones?             (vs reference)
  context_recall         do the contexts contain what the reference answer needs?            (vs reference)
plus exact match: the reference value appears in the answer.

The judge is a 4B model, so absolute scores are noisier than with a frontier judge; compare runs with the same judge.
Run it with the eval dependencies (pip install -r requirements-eval.txt), which pin a RAGAS/LangChain set that works.
"""
import argparse
import json
import os
import random
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from docuflow import sources  # noqa: E402

METRIC_NAMES = ["faithfulness", "answer_relevancy", "llm_context_precision_with_reference", "context_recall"]


def synthetic_items(api, headers, n, seed):
    """Questions about synthetic pages that are actually indexed."""
    docs = httpx.get(f"{api}/documents", params={"dataset": "synthetic", "status": "indexed", "limit": 1000},
                     headers=headers, timeout=60).json()
    idx = sorted(int(d["id"].rsplit("/", 1)[1]) for d in docs if d["id"].startswith("synthetic/gen/"))
    r = random.Random(seed)
    items = []
    for i in r.sample(idx, min(n, len(idx))):
        f = dict(line.split(": ", 1) for line in sources.synthetic_page(i)[1].splitlines()[1:])
        kind = r.choice(["signer", "amount", "city"])
        if kind == "signer":
            q, gold, ref = f"Who signed document {f['Number']}?", f["Signed by"], f"Document {f['Number']} was signed by {f['Signed by']}."
        elif kind == "amount":
            q, gold, ref = (f"What is the amount on {f['Number']}?", f["Amount"].split()[0],
                            f"The amount on {f['Number']} is {f['Amount']}.")
        else:
            q, gold, ref = f"Which city is listed on {f['Number']}?", f["City"], f"The city listed on {f['Number']} is {f['City']}."
        items.append(dict(question=q, gold=[gold], reference=ref, doc_id=f"synthetic/gen/{i}", source="synthetic"))
    return items


def ask(api, headers, item, max_steps):
    t0 = time.time()
    r = httpx.post(f"{api}/query", json={"question": item["question"], "max_steps": max_steps}, headers=headers, timeout=600)
    r.raise_for_status()
    j = r.json()
    contexts = [t["observation"] for t in j["trace"]
                if t.get("action") in ("search", "get_page", "read_page_image") and t.get("observation")
                and t["observation"] != "No results."]
    norm = lambda s: re.sub(r"[^\w]", "", s.lower())
    return {**item, "answer": j["answer"], "citations": j["citations"], "contexts": contexts or ["(no context retrieved)"],
            "steps": j["steps"], "seconds": round(time.time() - t0, 2),
            "exact_match": any(norm(g) in norm(j["answer"]) for g in item["gold"]),
            "cited_source": item["doc_id"] in j["citations"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("DOCUFLOW_API", "http://docuflow.localtest.me"))
    ap.add_argument("--llm-url", default="http://127.0.0.1:18002/v1", help="OpenAI-compatible judge endpoint")
    ap.add_argument("--llm-model", default="standard-model")
    ap.add_argument("--embed-url", default="http://127.0.0.1:18000/v1")
    ap.add_argument("--embed-model", default="embed-model")
    ap.add_argument("-n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--max-steps", type=int, default=4)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "ragas-report.md"))
    a = ap.parse_args()
    headers = {"x-api-key": os.environ.get("DOCUFLOW_API_KEY", "")}
    api = a.api.rstrip("/")

    items = synthetic_items(api, headers, a.n, a.seed)
    if not items:
        sys.exit("no indexed pages with known answers: ingest some synthetic pages first")
    print(f"asking {len(items)} questions through the agent ...", flush=True)
    with ThreadPoolExecutor(a.concurrency) as ex:
        rows = list(ex.map(lambda it: ask(api, headers, it, a.max_steps), items))

    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    from ragas import EvaluationDataset, RunConfig, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import Faithfulness, LLMContextPrecisionWithReference, LLMContextRecall, ResponseRelevancy

    judge = LangchainLLMWrapper(ChatOpenAI(base_url=a.llm_url, api_key="none", model=a.llm_model, temperature=0,
                                           max_tokens=2048, timeout=300))
    emb = LangchainEmbeddingsWrapper(OpenAIEmbeddings(base_url=a.embed_url, api_key="none", model=a.embed_model,
                                                      check_embedding_ctx_length=False))
    ds = EvaluationDataset.from_list([{"user_input": r["question"], "response": r["answer"],
                                       "retrieved_contexts": [c[:3000] for c in r["contexts"]], "reference": r["reference"]}
                                      for r in rows])
    print("scoring with RAGAS (judge: %s) ..." % a.llm_model, flush=True)
    res = evaluate(ds, metrics=[Faithfulness(), ResponseRelevancy(), LLMContextPrecisionWithReference(), LLMContextRecall()],
                   llm=judge, embeddings=emb, run_config=RunConfig(max_workers=a.concurrency, timeout=300, max_retries=3),
                   show_progress=False, raise_exceptions=False)
    df = res.to_pandas()
    for r, (_, s) in zip(rows, df.iterrows(), strict=True):
        r["scores"] = {m: (None if s.get(m) is None or s.get(m) != s.get(m) else round(float(s[m]), 3)) for m in METRIC_NAMES}
    report(a, rows)


def report(a, rows):
    def mean(m):
        xs = [r["scores"][m] for r in rows if r["scores"].get(m) is not None]
        return (statistics.mean(xs), len(xs)) if xs else (float("nan"), 0)
    names = {"faithfulness": "faithfulness", "answer_relevancy": "answer relevancy",
             "llm_context_precision_with_reference": "context precision", "context_recall": "context recall"}
    lines = [f"# docuflow RAGAS evaluation ({time.strftime('%Y-%m-%d %H:%M')})", "",
             f"{len(rows)} questions through the live agent (`POST /query`, max {a.max_steps} steps). "
             f"Judge: `{a.llm_model}`, embeddings: `{a.embed_model}` (the cluster's own vLLM servers).", "",
             "| metric | score | scored |", "|---|---|---|"]
    for m in METRIC_NAMES:
        v, n = mean(m)
        lines.append(f"| {names[m]} | {v:.3f} | {n}/{len(rows)} |")
    lines += [f"| exact match (reference value in answer) | {sum(r['exact_match'] for r in rows) / len(rows):.3f} | {len(rows)} |",
              f"| cited the source page | {sum(r['cited_source'] for r in rows) / len(rows):.3f} | {len(rows)} |",
              f"| mean agent steps / latency | {statistics.mean(r['steps'] for r in rows):.1f} / {statistics.mean(r['seconds'] for r in rows):.1f}s | |",
              "", "## Per question", "",
              "| ok | question | reference | answer | faith | relev | c-prec | c-rec |", "|---|---|---|---|---|---|---|---|"]
    f = lambda x: "-" if x is None else f"{x:.2f}"
    for r in rows:
        s = r["scores"]
        lines.append(f"| {'✓' if r['exact_match'] else '✗'} | {r['question']} | {r['gold'][0]} | {r['answer'][:80].replace('|', '/')} | "
                     f"{f(s['faithfulness'])} | {f(s['answer_relevancy'])} | {f(s['llm_context_precision_with_reference'])} | {f(s['context_recall'])} |")
    open(a.out, "w").write("\n".join(lines) + "\n")
    json.dump(rows, open(a.out.replace(".md", ".json"), "w"), indent=1, default=str)
    print("\n".join(lines[:14]))
    print(f"\nreport: {a.out}")


if __name__ == "__main__":
    main()
