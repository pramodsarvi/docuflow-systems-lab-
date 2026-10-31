# Part 9. Evaluation: RAGAS and the layers beneath it

The first stress run answered 1,635 queries with **zero errors**, and **only 17% of the answers were right**.
HTTP status codes and latency percentiles say nothing about whether a RAG system works. This part describes the four
evaluation layers used here, from the cheapest and most specific to the most end-to-end. Each layer answers a
different question, and a failure in a lower layer explains a bad score in a higher one.

| Layer | Tool | Question it answers | Needs an LLM |
|---|---|---|---|
| 1. Extraction | `evals/quality_eval.py` | Did the vision model read the page correctly? | no |
| 2. Retrieval | `evals/quality_eval.py` | Does search return the right page? | no (embeddings only) |
| 3. End-to-end RAG | `evals/ragas_eval.py` (RAGAS) | Is the agent's answer correct, grounded and relevant? | yes (judge) |
| 4. Model choice | `evals/vlm_benchmark.py` | Is a faster page reader as good as the current one? | the model under test |

## Layer 1: extraction

FUNSD, XFUND, IAM and the synthetic forms ship a ground-truth transcription. The eval compares the vision model's
reading of each indexed page with it:

- **Character error rate (CER):** Levenshtein distance divided by reference length, on lower-cased text without
  punctuation. On forms it also penalises reading order (left column then right, or row by row), so it overstates
  errors there.
- **Token F1:** order-independent word overlap. This is the better content measure on forms.
- **Missed (CER ≥ 90%):** the model returned nothing useful for the page.
- **Handwriting label recall:** IAM lines are all handwritten, so the share of regions labelled `handwritten` is the
  recall of the printed/handwritten classifier.

Pages indexed from fallback text (Part 7, failures 11–12) are excluded from the scores and counted separately.

Baseline on the full knowledge base (Qwen2.5-VL-3B, 500 pages per dataset):

| dataset | CER median | token F1 | near-perfect (CER < 5%) | missed | labelled handwritten |
|---|---|---|---|---|---|
| IAM (handwritten lines) | **0.0%** | 0.881 | 73% | **4.0%** | **67%** |
| FUNSD (scanned forms) | 25.8% | 0.825 | 4% | 0.0% | 1% |
| XFUND (7-language forms) | 37.0% | 0.774 | 1% | 0.4% | 1% |
| synthetic forms | 0.0% | 0.999 | 100% | 0.0% | 0% |

The model **reads** handwriting well: the median IAM line is perfect. It **classifies** handwriting poorly. A third
of IAM lines and almost all handwritten entries on forms are labelled `printed`, so filtering search by
`kind=handwritten` misses most handwriting. The 4% of missed lines are cases where the model returned only `...`.

## Layer 2: retrieval

Retrieval is measured without the agent. For questions that name a synthetic document number ("Which city is listed
on PUR-1106?"), does a corpus-wide hybrid search return that page first (hit@1) or in the top 5 (hit@5)?

| when | corpus | hit@1 | hit@5 |
|---|---|---|---|
| before the tokenisation fix | ~1,600 pages | (answer accuracy 17%) | |
| after the fix | 11,560 pages | **99.0%** | **100%** |

Search latency was 90 ms p50 and 108 ms p95 at 11,560 pages, with 1024-dimension vectors under HNSW plus a GIN
full-text index.

The bug behind the 17% (Part 6): Postgres read "PUR-1106" as `pur` plus the negative number `-1106`, so the document
number never matched, and the full-text branch returned 30 arbitrary rows. A fusion step that merges vector and
keyword results can't recover from a branch that returns noise. Measuring retrieval on its own is what pinned the
problem on search rather than on the model.

## Layer 3: RAGAS

### What it measures

RAGAS scores a question, the answer, the contexts the system retrieved and, for some metrics, a reference answer,
using an LLM as judge:

| metric | question it answers | how |
|---|---|---|
| **faithfulness** | Is every claim in the answer supported by the retrieved contexts? (hallucination) | the judge splits the answer into claims and checks each against the contexts |
| **answer relevancy** | Does the answer address the question? | the judge writes questions the answer would fit; their embeddings are compared with the real question |
| **context precision** | Are the useful contexts ranked above the useless ones? | the judge marks each context useful or not for the reference answer, weighted by rank |
| **context recall** | Do the contexts contain what the reference answer needs? | the judge checks each reference statement against the contexts |

Two simpler metrics sit alongside them: **exact match** (the reference value, such as the signer's name, appears in
the answer) and **cited the source page**.

### How it's set up here

- **Questions go through the real agent** (`POST /query`), so the eval runs exactly what users run: the same
  retrieval, prompts and step limit.
- **Contexts are taken from the agent's trace:** every search result, page and image re-read it actually observed.
  The context metrics therefore grade the agent's own retrieval, not a separate retriever written for the eval.
- **The judge is the cluster's own agent model** (`standard-model`, Qwen3-4B on vLLM), and embeddings come from
  `embed-model`. No external API is called, and nothing leaves the cluster.
- **Two question sets:**
  - *Synthetic forms, corpus-wide:* "Who signed document INV-1005?" against the whole knowledge base, with exact
    references from the generated fields.
  - *DocVQA, page-scoped:* the dataset's own human-written questions ("What is the date on this letter?"). These are
    page-specific and ambiguous across 14,600 pages, so each is asked with a document scope (`doc_id` on
    `/query`). That measures reading and answering from the right page, and doesn't penalise questions that are
    unanswerable across the corpus.
- **Pinned dependencies.** The newest RAGAS release fails at import with the current `langchain_community`. The
  working set is pinned in `requirements-eval.txt` (RAGAS 0.2.15, LangChain 0.3) and kept in its own virtualenv,
  away from the app.

```bash
make ragas N=40                                             # synthetic, corpus-wide
.venv-eval/bin/python evals/ragas_eval.py -n 40 --docvqa 40 # + DocVQA, page-scoped; per-set table in the report
```

### Results so far (40 synthetic questions)

| metric | after the retrieval fix | after the agent changes |
|---|---|---|
| exact match | 0.900 | **0.975** |
| faithfulness | 0.956 | **0.975** |
| answer relevancy | 0.450 | **0.808** |
| context precision | 0.900 | **0.975** |
| context recall | 0.900 | **0.975** |
| mean agent steps / latency | 2.0 / 3.3 s | 2.2 / 3.5 s |

The agent changes between the two columns came straight from reading the low-scoring rows:

- **Answer relevancy 0.45 was mostly a format problem.** Correct but terse answers such as "Fatima Berg" make the
  judge generate "Who is Fatima Berg?", which embeds far from "Who signed INV-1570?". The agent now answers in one
  sentence that restates the subject ("INV-1570 was signed by Fatima Berg."), which is also clearer for a person
  reading it.
- **All misses were "not found" after a single search** for a page that was indexed. A guard now makes the agent try
  one rewritten search, for example just the document number, before it may declare something missing.

### Caveats

- **The judge is a 4B model.** Its scores are noisier than a frontier judge's would be. Compare runs made with the
  same judge; don't compare absolute values with numbers from other setups.
- **The judge is the agent model itself,** so it can share the agent's blind spots. Exact match is the guard
  against that: it needs no judge.
- **The synthetic set is easy by design.** Its value is exact references and corpus-wide retrieval at scale. The
  DocVQA set covers real documents with human questions.

## Layer 4: choosing a faster page reader

The page reader is the throughput bottleneck (Part 5): memory bandwidth on the GPU is 80–99% used. An FP8 version of
the same model moves about half the bytes per token, so it could be substantially faster. It could also read worse.
`evals/vlm_benchmark.py` decides that with the same ground-truth pages, the production prompt and schema, and a fixed
concurrency, and reports per dataset:
- pages per minute
- CER and token F1
- missed lines
- handwriting label recall
- p50 read time

The method matters:
- **Quality** can be compared at any time, because it doesn't depend on load.
- **Speed** must be compared with each model alone on an otherwise idle GPU. A speed test next to a saturated
  ingest measures contention, not the model.
- A replacement is accepted only if quality holds on every dataset, IAM handwriting in particular.

## When to run what

| change | run |
|---|---|
| search, chunking, tokenisation | layer 2, then RAGAS |
| agent prompt or loop | RAGAS (both sets) |
| page-reading model, prompt or output limit | layer 1 + layer 4, then RAGAS on DocVQA |
| embedding model | layer 2 + RAGAS (and re-embed everything first) |
| infrastructure only (scaling, KEDA, nodes) | stress test; evals unchanged unless outputs changed |
