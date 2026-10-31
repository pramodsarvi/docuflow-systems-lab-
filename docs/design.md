# docuflow system design

Status: living document. Every decision below cites the measurement or failure that motivated it. Decisions marked
**accepted** are implemented; **proposed** decisions are the target design and the roadmap (section 8). The
experiment write-up that produced the evidence is in [`docs/blog/`](blog/README.md).

## 1. Purpose and scope

docuflow turns scanned documents (printed text and handwriting) into a searchable knowledge base and answers
questions about them with an agent that cites its sources. Ingest and querying run at the same time: a page becomes
searchable minutes after it arrives, and questions asked mid-ingest see everything indexed so far.

**In scope:**
- batch ingest of document collections
- interactive question answering with citations
- autoscaling on Kubernetes with GPU model serving
- operating it: observability, recovery, evaluation

**Out of scope for now:**
- multi-tenancy
- per-document access control
- streaming answers
- document formats beyond page images

## 2. Requirements

### Functional

| ID | Requirement |
|---|---|
| F1 | Ingest a collection by name and size; read every page with a vision model into text regions tagged printed/handwritten |
| F2 | Index each page for hybrid search (vectors + full text) as soon as it is read |
| F3 | Answer a question with citations to the pages used; optionally scoped to a dataset or a single document |
| F4 | Expose progress, backlog and every job's state; allow pause/resume, retry, and re-read of degraded pages |

### Service levels (proposed targets)

| ID | Target | Measured today (1x L40S) |
|---|---|---|
| S1 | Query p95 ≤ 5 s with no ingest running | p95 4.3 s (light load) ✓ |
| S2 | Query p95 ≤ 15 s while ingest runs, up to 24 concurrent users | p95 12.4 s at 24 clients ✓; 22 s at 48 ✗ |
| S3 | A fetched page is queryable within 15 min under normal load | p50 10 min with a 2,100-page backlog ✓ |
| S4 | No page is lost; no page is silently degraded | 0 lost in 14,614; 285 degraded before ADR-05, 0 after ✓ |
| S5 | Retrieval hit@1 ≥ 95% on identifier questions; RAGAS faithfulness ≥ 0.95 | hit@1 99.0%; faithfulness 0.975 ✓ |
| S6 | Scale from zero to first page in ≤ 30 s after an ingest request | 10 s ✓ |

### Constraints

- GPUs are the expensive, scarce resource. The design has to make one GPU useful and scale cleanly to many.
- Everything runs in containers on Kubernetes, and model weights are open (no external model API required).
- Recovery has to be possible by an operator with `kubectl` and the HTTP API alone.

## 3. Architecture as built

```
                     ingress (TLS in prod)
                            │
  clients ── POST /ingest, /query ──► api (KEDA: CPU + queries in flight)
                            │                      │ agent loop (search, page, image re-read, answer)
                            │ jobs + 1 token/job    ▼
                            ▼                 model-llm ─┐
   RabbitMQ  docuflow.{io,vlm,embed} (+ .delay)          │
        │ KEDA rabbitmq scaler                            │  vLLM servers, one per model role,
        ▼                                                 │  on GPU nodes (KEDA prometheus scaler
   worker-io ─► worker-vlm ─► worker-embed ──────────────►│  on vLLM running + waiting)
   (CPU pods, 0..N, one job each)        model-vlm, model-embed
        │
        ▼
   Postgres + pgvector: documents, page images, chunks (HNSW + GIN), jobs, control, queries
   Prometheus + Grafana + DCGM ─ dashboards and alerts on every tier
```

| Component | Responsibility | Scales on |
|---|---|---|
| api | ingest requests, agent loop, workload control, metrics | KEDA: CPU 60%, 4 queries in flight per pod |
| worker-io / vlm / embed | one pipeline stage per lane, one job at a time | KEDA: RabbitMQ lane depth + running jobs |
| model-vlm / model-llm / model-embed | vLLM inference per model role | KEDA: vLLM running + waiting requests |
| RabbitMQ | work tokens (KEDA input, wake-ups), delayed retries | n/a (single node) |
| Postgres | source of truth: jobs, documents, chunks, vectors, images | n/a (single node) |

## 4. Target architecture

```
                         ingress (TLS + OIDC)
                                │
  clients ─────────────────► api (2..N) ───────────────┐
                                │                       │
                         jobs (Postgres)                │ requests with role + priority
                                │                       ▼
                     work queue (decision ADR-04)   inference gateway (ADR-06)
                                │                   least-request / KV-aware routing,
                                ▼                   priorities, limits, tracing (ADR-13)
                     async workers (ADR-08)  ──────────►│
                     few pods, bounded concurrency      ├─► query pool  (GPU A): model-llm, model-embed
                     limit = model capacity             └─► ingest pool (GPU B): model-vlm  (ADR-07)
                                │                               ▲ scaled on backlog + vLLM load
                                ▼
   object storage: page images (ADR-09)     Postgres HA: jobs, chunks, vectors (ADR-09)
   OTel traces -> Lens · Prometheus/Grafana · evals in CI (ADR-11)
```

What changes, and why:

1. **An inference gateway sits between every client and every model server.** It routes per request (least-request,
   ideally KV-aware), carries priority, and is the single place for tracing, auth and limits.
2. **GPU pools are split by role**, so ingest bursts can't touch query latency. On a single GPU, ingest yields
   automatically when the query latency SLO is at risk (ADR-07).
3. **Workers become async, with bounded concurrency.** Their total concurrency is derived from model capacity, and
   the backlog scales the *model servers*.
4. **Images move to object storage, and Postgres gets HA.**

## 5. Data flows

**Ingest:**
1. `POST /ingest` → one `fetch` job per 20-page slice, committed, then one token per job.
2. **fetch:** stream the slice, store each image, enqueue one `extract` per page (enqueued as pages arrive).
3. **extract:** the vision model reads the page into regions (text, printed/handwritten, role). If output is cut off,
   completed regions are kept (failure 11). Then chunk, write chunks, enqueue `embed`.
4. **embed:** embed the chunks and mark the page `indexed`. It's searchable from this point.

Downstream stages have higher priority, so pages finish before new ones start.

**Query:**
1. `POST /query` registers the query in flight.
2. Search on the question (hybrid: vector + full text with an identifier boost, RRF fusion).
3. The agent loop decides: answer, search again, read a whole page, or have the vision model re-read the page image.
4. Answer with citations restricted to pages actually seen; record the query, trace and latency.

**Failure:**

| failure | handling |
|---|---|
| worker crash | lease expiry requeues the job |
| page problem (bad output) | up to 3 attempts with backoff; the last may fall back to dataset text; then `dead` |
| model server unavailable | deferred: attempt refunded, 15 s → 5 min backoff, never falls back (ADR-05) |
| lost token | the reconciler republishes from Postgres |

## 6. Capacity model

Measured on 1x L40S 46 GB, vLLM 0.13, Qwen2.5-VL-3B / Qwen3-4B / Qwen3-Embedding-0.6B.

**Page reading is GPU-memory-bandwidth bound.**
- At saturation: memory bandwidth 80–99%, clocks at max, ~300 of 350 W.
- Throughput ≈ concurrent reads × 60 / read seconds, until the GPU saturates. Beyond that, more slots only raise
  per-page latency (failure 13).

| page type | read time under load | pages/min at saturation |
|---|---|---|
| handwritten line (IAM) | 5 s | ~600+ (IAM-only mix) |
| generated form | 20–25 s | ~110–170 |
| scanned form / document (FUNSD, DocVQA, XFUND) | 40–80 s | ~40–70 |

**Queries are bounded by the agent model's batch slots.**
- On a shared GPU, throughput plateaued at ~130 queries/min, where 32 slots were full and 64 requests waited.
- Each query makes ~1.2 LLM calls after the up-front search (ADR-14), at 1–3 s each.

**Sizing rules:**

| Quantity | Rule |
|---|---|
| worker concurrency (total) | ≈ Σ model-vlm replicas × `--max-num-seqs` (more only queues inside vLLM) |
| model-vlm replicas | backlog-driven, bounded by GPUs available to the ingest pool |
| KV cache per server | the measured peak use at target concurrency + 25%; fixed via `--kv-cache-memory-bytes` on shared GPUs |
| API replicas | concurrency (4 per pod) for connection spreading; it's never the bottleneck |

## 7. Decision records

Each record: context → decision → evidence → consequences.

#### ADR-01: Scale on demand signals, not CPU (accepted)
**Context.** Workers and API pods spend nearly all their time waiting on model servers.
**Decision.** Scale every tier on its own demand signal: queue depth for workers, queries in flight for the API,
running + waiting requests for vLLM. Use KEDA, which drives a standard HPA and adds scale-to-zero.
**Evidence.** API pods used ~5 mCPU each with 96 queries in flight. Workers scaled 0 → 6 in 30 s on queue depth.
**Consequences.** Every scaler threshold has to be reachable from the upstream tier's maximum (failure 2). In-app
limits have to sit above KEDA maxima (failure 1). KEDA-managed Deployments must not set `replicas` (failure 4).

#### ADR-02: GPUs only in model servers; workers are CPU clients (accepted)
**Context.** GPU pods are slow to start (~1 min) and expensive; work arrives in bursts.
**Decision.** Only vLLM servers request GPUs. Workers are HTTP clients, and vLLM's continuous batching turns their
concurrency into GPU throughput.
**Evidence.** Workers scaled to 72 in seconds while model servers changed rarely. Throughput rose with worker
concurrency until the GPU saturated.
**Consequences.** Concurrency limits belong to the model tier (ADR-08). Load balancing to model servers matters
(ADR-06).

#### ADR-03: Postgres is the source of truth for jobs; stages are idempotent (accepted)
**Context.** Workload rules (priority, fair share, pause, per-dataset caps) and recovery need queryable state.
**Decision.** A jobs table claimed with `FOR UPDATE SKIP LOCKED`, leases and heartbeats, retries with exponential
backoff, `dead` after max attempts. Every stage is idempotent (upserts, delete-then-insert chunks).
**Evidence.** 14,614 pages through several worker restarts and model-server outages, with 0 lost.
**Consequences.** Postgres load grows with workers. Polling is acceptable to ~100 workers; beyond that, see ADR-04.

#### ADR-04: Work dispatch through RabbitMQ tokens (accepted; to revisit)
**Context.** Idle workers polling Postgres; KEDA needs a cheap backlog signal.
**Decision.** One RabbitMQ token per runnable job: published after commit, consumed before claiming, delayed
queues for backoff, and a reconciler that rebuilds tokens from Postgres.
**Evidence.** Correct under test against a real broker. KEDA scaled on queue depth reliably.
**Consequences / revisit.** It's two systems for one job, with a reconciler to maintain. **Criteria for keeping
it:** more than ~100 concurrent workers, several producer services, or a need for routing and fan-out. Otherwise
Postgres with `LISTEN/NOTIFY` and KEDA's Postgres scaler is simpler with the same behaviour.

#### ADR-05: An infrastructure outage is not a page failure (accepted)
**Context.** A one-minute model-server restart downgraded 264 pages to fallback text (failure 12).
**Decision.** The model client raises `ModelUnavailable` for connection errors, timeouts, 429 and 5xx. Workers
defer: the attempt is refunded, backoff runs 15 s → 5 min, and there's never a fallback. The API returns 503 with
`Retry-After`. `reread-fallbacks` repairs past damage.
**Evidence.** The next restart produced 590 deferrals, 0 dead and 0 downgraded pages.

#### ADR-06: An inference gateway in front of all model servers (proposed)
**Context.** Kubernetes Services balance per connection. Keep-alive pinned every worker to one replica (16+6
waiting vs 2), and random placement still left one replica with 24+11 while another had free slots. Tracing, auth
and limits have no single home.
**Options.**
1. kube-proxy IPVS with least-connection: simple, but connection-level only.
2. An Envoy-based gateway with least-request: request-level, with no model awareness.
3. Gateway API Inference Extension / llm-d: routes on queue depth and KV-cache state, model-aware.
4. LiteLLM: model routing, keys, budgets and limits, and Lens tracing integration.

**Decision (proposed).** Use a model-aware gateway (option 3) for routing, or LiteLLM (option 4) where per-client
keys, budgets and tracing matter more than KV-aware routing. Every model call from the API and workers goes through
it, and model roles become routes.
**Acceptance.** At equal load, the gap in batch occupancy between replicas is under 20%. Every request is traced.

#### ADR-07: Isolate GPU pools by role; on one GPU, ingest yields to an SLO (proposed)
**Context.** Time-slicing has no priority between processes, and the L40S has no MIG partitioning. Ingest and
queries competed: query p95 hit 42 s while ingest ran. The current answer is manual ingest/query modes.
**Decision (proposed).**
- **With two or more GPUs:** a query pool (model-llm, model-embed) and an ingest pool (model-vlm) on separate GPUs,
  each autoscaled on its own queue.
- **On one GPU:** automatic back-off. Page-reading concurrency is reduced when measured query p95 exceeds S2, and
  restored when it recovers. That replaces the manual modes with a feedback loop on the SLO, not on a fixed in-flight
  count.

**Acceptance.** S2 holds during a full-speed ingest. No manual mode switches.

#### ADR-08: Async workers with capacity-derived concurrency; scale model servers on the backlog (proposed)
**Context.** One job per pod meant 72 pods to keep 72 page reads in flight. Workers beyond model capacity only
lengthened vLLM queues (failures 1, 13).
**Decision (proposed).**
- Each worker pod runs N concurrent jobs (async I/O).
- Total worker concurrency = model-vlm replicas × batch slots, computed and published by the control plane.
- KEDA scales **model-vlm** on the backlog (bounded by the GPU pool) and the workers on model capacity.

**Acceptance.** The same throughput with ≤ 8 worker pods. Adding a model-vlm replica raises worker concurrency
automatically.

#### ADR-09: Page images in object storage; queue and vectors separated; HA Postgres (proposed)
**Context.** One single-node Postgres holds images (`bytea`), vectors, full text, jobs and query logs.
**Decision (proposed).**
- Images go to S3-compatible storage, referenced by key.
- Vectors and chunks go to a replicated or managed Postgres with pgvector.
- The job queue moves to a separate database, or to the broker if ADR-04 is revisited.

**Trigger.** Beyond ~100k pages, or any production use.

#### ADR-10: vLLM per model role, with fixed memory budgets on shared GPUs (accepted)
**Decision.**
- One vLLM server per role.
- On shared GPUs: a fixed `--kv-cache-memory-bytes` (startup profiling miscounts when servers start together,
  failure 8) and `Recreate` updates (rolling updates deadlock on GPU memory, failure 5).
- On whole GPUs: rolling updates and utilisation-based sizing.

**Evidence.** Stable co-location of 3–5 servers on one L40S.

#### ADR-11: Evaluation is a release gate (accepted; CI gate proposed)
**Context.** A load test with 0 errors had 17% correct answers.
**Decision.** Four eval layers:
1. extraction and handwriting vs ground truth
2. retrieval hit@k
3. RAGAS end-to-end (judge in-cluster)
4. model benchmarks for any model swap

Changes to search, the agent or a model need the matching layer to hold (Part 9 of the write-up has the mapping).
**Proposed.** Run layers 1–2 on every merge, and RAGAS nightly, with thresholds S5.

#### ADR-12: Authentication and transport security (proposed)
**Context.** An API key is the only auth. Grafana and Headlamp are exposed over plain HTTP.
**Decision (proposed).** TLS and OIDC at the ingress. Dashboards only behind SSO or a VPN. Per-client keys at the
gateway (ADR-06).

#### ADR-13: Tracing in OpenTelemetry with OpenInference attributes (accepted in code; backend proposed)
**Decision.** One trace per query: an AGENT root span, then LLM and TOOL spans per step, with the trace id returned
by `/query`. The backend (Lens, or any OTLP collector) is configured by endpoint only. With the gateway (ADR-06),
model calls from workers are traced too.

#### ADR-14: Hybrid retrieval with identifier boost; search before the first LLM call (accepted)
**Context.** Document codes were tokenised as negative numbers. The text branch took arbitrary rows. Near-identical
forms made vector ranking noise.
**Decision.**
- Normalise hyphenated codes in the full-text index.
- Rank the text branch properly.
- Add an identifier-match term to RRF fusion.
- Run the first search before the agent's first LLM call.
- Allow document-scoped questions.

**Evidence.** Answer accuracy 17% → 97.5%; hit@1 99.0% on 11,560 pages; one fewer LLM call per query.

## 8. Roadmap

| Order | Change | Decision | Done when |
|---|---|---|---|
| 1 | Inference gateway for all model traffic | ADR-06, ADR-13 | replicas balanced within 20%; all model calls traced |
| 2 | Async workers, capacity-derived limits, model-vlm on backlog | ADR-08 | same throughput with ≤ 8 worker pods |
| 3 | SLO-driven ingest back-off | ADR-07 | S2 holds during full ingest without mode switches |
| 4 | Eval gate in CI | ADR-11 | layers 1–2 per merge, RAGAS nightly |
| 5 | Object storage, HA Postgres, queue separation | ADR-09 | before production / ~100k pages |
| 6 | TLS + OIDC, dashboards behind SSO | ADR-12 | before any shared use |
| 7 | Revisit RabbitMQ vs Postgres-only dispatch | ADR-04 | decision recorded with load evidence |
| 8 | Faster page reader (FP8) if quality holds | ADR-10, ADR-11 | benchmark: equal quality per dataset, higher pages/min |

## 9. Open questions

- **Handwriting classification.** The model labels only 67% of handwritten lines as handwritten, and ~1% on forms.
  Options: a dedicated classifier per region, a larger reader, or dropping the label and relying on text. It decides
  whether `kind=handwritten` filtering is a feature or should be removed.
- **Page-scoped vs corpus questions.** Many real questions are about "this document". Should the API model
  documents and conversations explicitly, rather than only a `doc_id` scope?
- **Re-processing policy.** When the reader changes, which pages get re-read: none, degraded ones, or everything?
  Re-embedding is cheap (minutes); re-reading costs hours of GPU time.
