# Part 1. The system and why it scales on queues

## What docuflow does

docuflow ingests scanned documents that mix printed text and handwriting, then answers questions about them. Ingest
and querying happen at the same time: a page becomes searchable seconds after it is read, and a question asked
mid-ingest sees whatever has been indexed so far.

A page moves through three stages, each a job in its own **lane**:

| Lane | Stage | What it does | Cost |
|---|---|---|---|
| `io` | fetch | stream a slice of 20 pages from a dataset, store each image, enqueue one extract job per page | network, CPU |
| `vlm` | extract | the vision model reads the page: every region's text, tagged `printed`/`handwritten` and a role | **GPU, 3 to 25 s** |
| `embed` | embed | embed the page's chunks, mark the page `indexed` | GPU, under 0.1 s |

Questions go to an **agent loop**: a small LLM chooses actions (`search`, `get_page`, `read_page_image`, `answer`)
over hybrid vector + full-text search until it can answer with citations.

```
 POST /ingest ─► fetch (io lane) ─► extract (vlm lane) ─► embed (embed lane) ─► pgvector + tsvector
 POST /query  ─► api ─► agent loop ─► model-llm (+ search, + model-vlm for page re-reads)

  ┌─ RabbitMQ ──────────────┐     ┌─ GPU node (1x L40S, time-sliced) ──────────────┐
  │ docuflow.io     tokens  │     │ model-vlm    Qwen2.5-VL-3B     KEDA 1..N        │
  │ docuflow.vlm    tokens  │──►  │ model-llm    Qwen3-4B          agent            │
  │ docuflow.embed  tokens  │     │ model-embed  Qwen3-Embedding   KEDA 1..N        │
  └─────────────────────────┘     └─────────────────────────────────────────────────┘
        ▲ KEDA reads queue length          ▲ KEDA reads vLLM metrics via Prometheus
        │                                  │
  worker-io / worker-vlm / worker-embed  (CPU pods, 0..N each)    api (1..8)
```

Every component is a container on Kubernetes:
- the API
- the workers, one Deployment per lane
- three vLLM servers, one per model
- RabbitMQ
- Postgres

The workers don't request a GPU. They're HTTP clients of the vLLM servers, and vLLM batches their concurrent requests
on the GPU.

## Three workloads, three shapes of load

The system serves three very different kinds of demand, and each needs its own scaling signal:

1. **Batch ingest** arrives in bursts: one `POST /ingest` can enqueue 5,000 pages in a second. The demand is the
   *backlog*: how many pages are waiting. CPU on the worker pods says almost nothing about it, because a worker
   spends nearly all its time waiting on a model server.
2. **Interactive queries** arrive continuously and care about latency. The demand is *concurrency*: how many questions
   are being answered right now.
3. **Model inference** is shared by both. The demand is what the model server itself reports: requests running in its
   batch, and requests waiting for a slot.

## Why not scale on CPU

The classic Horizontal Pod Autoscaler scales on CPU or memory utilisation. For this system that signal is wrong in
both directions:

- A page-reading worker sends one request to vLLM and waits 3 to 25 s for the answer. Its CPU is near zero whether 5
  pages are queued or 5,000.
- An API pod running 12 agent loops is also near zero: during the stress test, 8 API pods each used **5 millicores**
  while 96 queries were in flight.
- With no work, CPU-based HPA still keeps `minReplicas` (at least 1) running. A queue-based scaler can go to **zero**.

Queue depth measures the actual demand, it's known *before* any pod starts working, and it reaches zero when the
work is done. That's the signal KEDA is built around, and Part 2 describes how it plugs into the HPA.

## Where the GPU fits

There is one GPU. Every replica of every model server gets a time-slice of it and a fixed share of its 46 GB of
memory. So autoscaling here redistributes a fixed budget between:
- page reading (ingest)
- the agent model (query latency)
- embeddings (both)

That constraint drives several of the failures in Part 7 and the "ingest mode" at the end of Part 6.
