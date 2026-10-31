# docuflow

Ingest scanned documents that mix **printed and handwritten text**, and answer questions about them with an **agentic
RAG** loop **while ingestion keeps running**. It runs on Kubernetes with real GPUs: vLLM containers serve the models, a
RabbitMQ work queue drives **KEDA** autoscaling, and Postgres + pgvector handle storage, search and job state.

```
                 ┌──────────── ingress-nginx (docuflow.localtest.me) ────────────┐
                 ▼                                                                │
   POST /ingest, /query ──► api (KEDA: CPU + queries in flight, 1..4) ────────────┘
          │                    │ agent loop: search / get_page / read_page_image / answer(citations)
          │ jobs (Postgres)    │
          │ + 1 token per job  ▼
          ▼             ┌─────────────┐   vLLM containers on the GPU node (nvidia.com/gpu, time-sliced)
   RabbitMQ queues ───► │ worker-io   │   ┌──────────────────────────────────────────────────────────┐
   docuflow.io          │ worker-vlm  │──►│ model-vlm   Qwen2.5-VL (page reading)   KEDA 1..N on vLLM │
   docuflow.vlm         │ worker-embed│──►│ model-llm   Qwen3-4B-Instruct (agent)                    │
   docuflow.embed       └─────────────┘   │ model-embed Qwen3-Embedding-0.6B (1024-d)                 │
     ▲  KEDA rabbitmq scaler: 0..N each    └──────────────────────────────────────────────────────────┘
     └─ retries via docuflow.<lane>.delay (TTL dead-letter)        Prometheus + Grafana + DCGM, alerts
```

| Dataset | Source | What the pipeline gets |
|---|---|---|
| synthetic | generated in-process | forms with printed labels + handwriting-style values, known answers (demos, CI, stress tests) |
| FUNSD | `nielsr/funsd-layoutlmv3` | scanned forms, printed labels + filled-in answers |
| XFUND | `nnul/xfund-multilingual` | forms in DE/ES/FR/IT/JA/PT/ZH |
| DocVQA | `lmms-lab/DocVQA` | document pages with question/answer pairs (QA evals) |
| IAM | `Teklia/IAM-line` | handwritten line images |

## Quick start: Kubernetes on an NVIDIA GPU machine

Needs Docker with the NVIDIA Container Toolkit, plus `kind`, `kubectl`, `helm` and `kustomize`.

```bash
# one-time host setup (Docker must hand GPUs to kind nodes)
sudo nvidia-ctk runtime configure --runtime=docker --set-as-default
sudo nvidia-ctk config --set accept-nvidia-visible-devices-as-volume-mounts=true --in-place && sudo systemctl restart docker

make cluster-up     # kind (GPU node + CPU node), NVIDIA device plugin w/ time-slicing, KEDA, metrics-server,
                    # ingress-nginx, kube-prometheus-stack + DCGM exporter
make deploy         # build + load the image, deploy k8s/overlays/local-gpu, wait for the vLLM servers
make smoke          # ingest 6 pages (real VLM reading) + an agentic query with a checked citation
make stress         # 800-page ingest + 12 concurrent query clients, sampling autoscaling -> docs/stress-report.md
make k8s-status     # pods, ScaledObjects, HPAs, GPUs, RabbitMQ queues
```

The API is at <http://docuflow.localtest.me> (dashboard at `/`), and Grafana is at <http://grafana.localtest.me>
(admin/docuflow, dashboard "docuflow"). CPU-only machines (and CI) use the model simulator instead:
`GPU=0 MONITORING=0 make cluster-up && make deploy OVERLAY=ci-sim`.

## How it scales

| Component | Scaler | Signal | Range (local / prod) |
|---|---|---|---|
| `worker-io`, `worker-vlm`, `worker-embed` | KEDA **rabbitmq** + postgresql | tokens waiting on `docuflow.<lane>` (backlog); jobs running (keeps a busy pod alive) | 0..3 / 0..12 / 0..3, scale to zero |
| `api` | KEDA cpu (the HPA metric) + postgresql | CPU 60%, interactive queries in flight (4 per pod) | 1..4 / 2..6 |
| `model-vlm` (vLLM container) | KEDA **prometheus** | `vllm:num_requests_running + waiting` (4 per replica locally, 16 in prod) | 1..2 / 1..4 |
| `model-llm`, `model-embed` | fixed | one vLLM server each, batching all clients | 1 |

**Why RabbitMQ and Postgres together.** RabbitMQ carries one token per runnable job. KEDA reads queue length from it, and
workers take a token before claiming work. Postgres keeps the job itself: its state, lease, retries, and the
workload rules applied at claim time:
- priority, with downstream stages first so pages become queryable sooner
- per-dataset fair share and caps
- pausing a lane or dataset (its tokens are dropped, so its pods scale away; on resume the reconciler restores them)
- dead-lettering after retries

Failed jobs go back through `docuflow.<lane>.delay`, whose per-message TTL implements exponential backoff. If a token is
ever lost (a publish after commit fails, the broker restarts), workers rebuild the queue from Postgres under an
advisory lock. Workers that die mid-job are covered by lease expiry.

**GPU sharing.** The device plugin advertises the L40S as 6 time-sliced `nvidia.com/gpu`. Each vLLM server gets a fixed
`--kv-cache-memory-bytes`: vLLM's memory profiling miscounts when several servers start on one GPU at the same time.
Measured: vlm (3B) + llm (4B) + embed (0.6B) use 27 GB of the 46 GB, which leaves room for a second vlm replica.
docuflow's workers request no GPU. They are HTTP clients, and vLLM batches their concurrent requests.

## Stress test

`scripts/stress.py` queues a large ingest and runs N closed-loop query clients for the whole duration. Every 10 s it
records replicas, pending pods, RabbitMQ depth, job counts, pages indexed, queries in flight, and GPU utilisation and
memory. The report covers ingest throughput, query latency during ingest (p50/p95/max), error rate, answer correctness
(synthetic pages have known answers) and the full scaling timeline. The latest run is in
[`docs/stress-report.md`](docs/stress-report.md).

## Evaluation (RAGAS)

`make quality-eval` scores what is indexed without any LLM: extraction accuracy and handwriting against ground truth,
and retrieval hit@1/hit@5 across the whole knowledge base (latest: [`evals/quality-report.md`](evals/quality-report.md);
hit@1 99.0% on 11,560 pages).

`make ragas N=40 DOCVQA=40` asks questions with known answers through the live agent (`POST /query`). It then scores them with
RAGAS, judged by the cluster's own vLLM servers (`standard-model` as judge, `embed-model` for embeddings), so no
external API is involved. Contexts are what the agent actually observed in its trace, so the context metrics grade its
real retrieval. The latest report is in [`evals/ragas-report.md`](evals/ragas-report.md).

| metric | v0.1 retrieval | current |
|---|---|---|
| exact match (reference value in the answer) | 0.900* | **0.975** |
| faithfulness | 0.956 | **0.975** |
| answer relevancy | 0.450 | **0.808** |
| context precision / recall | 0.900 / 0.900 | **0.975 / 0.975** |

\* measured after the tokenisation fix. Before it, only 17% of stress-test answers were correct: "INV-1005" was indexed as
`inv` + `-1005` (a negative number), and the full-text branch took 30 arbitrary matches. The judge is a 4B model, so
compare runs made with the same judge rather than reading the absolute values. `evals/run_eval.py` still covers
extraction CER/F1 against the Hub datasets' ground truth and DocVQA exact-match QA.

## System design

[`docs/design.md`](docs/design.md) is the reference: requirements and service levels, the architecture as built and
the target architecture, data flows, a capacity model from measurements, and 14 decision records (accepted and
proposed), each citing the evidence behind it, plus the roadmap.

## Kubernetes command reference

[`docs/kubernetes-commands.md`](docs/kubernetes-commands.md) lists every `kind`, `kubectl`, `helm` and `kustomize`
command used to build, deploy, scale, inspect, debug and recover the system. Each entry covers what it does, why it's
used here, what to look for in the output, and the gotchas we hit.

## Challenges and lessons

Building and stress-testing this system surfaced 14 failures. Most sat around the autoscaler, not in it:
- app limits KEDA couldn't see
- thresholds that couldn't be reached
- per-connection load balancing
- `kubectl apply` resetting replica counts
- rolling updates deadlocking on GPU memory
- vLLM memory profiling on a shared GPU
- scaling a tier that wasn't the bottleneck
- truncated model output discarding whole pages
- outages downgrading pages
- a memory budget that turned out not to be the bottleneck

Each is written up with symptom, cause and fix in [`docs/blog/07-what-broke.md`](docs/blog/07-what-broke.md). The full
write-up (system design, HPA vs KEDA, queue design, every ScaledObject, GPU sharing, stress results, how to recreate
it) starts at [`docs/blog/README.md`](docs/blog/README.md).

## Repository layout

```
docuflow/            app: api, agent, worker, jobs (queue rules), broker (RabbitMQ tokens), pipeline, sources, llm client
deploy/kind/         kind cluster with GPU passthrough      deploy/helm-values/   device plugin, Prometheus stack, DCGM
k8s/base/            namespace, priority classes, Postgres, RabbitMQ, api, workers, KEDA, gc CronJob, PDBs, NetworkPolicies
k8s/components/      models (vLLM + KEDA) · simulator · monitoring (monitors, alerts, Grafana dashboard) · ingress
k8s/overlays/        local-gpu (this machine) · ci-sim (CPU-only/CI) · prod (whole GPUs, TLS, backups, external secrets)
scripts/             cluster-up.sh · smoke.sh · stress.py          evals/   ragas_eval.py · run_eval.py · reports
.github/workflows/   lint + tests (Postgres + RabbitMQ services), manifest validation, image build, kind e2e
```

## Production (`k8s/overlays/prod`)

This overlay targets a GPU cluster (AKS/EKS/GKE) with the GPU Operator, KEDA, kube-prometheus-stack, ingress-nginx and
cert-manager installed.

```bash
make prod-image REGISTRY=myregistry.azurecr.io TAG=v0.2.0
export POSTGRES_PASSWORD=... RABBITMQ_PASSWORD=... DOCUFLOW_API_KEY=...   # optional: DOCUFLOW_DSN / RABBITMQ_URL for managed services
make prod-secrets
make prod-deploy REGISTRY=myregistry.azurecr.io TAG=v0.2.0 HOST=docuflow.example.com
```

Compared with local: the 7B vision model runs on whole GPUs, and KEDA scales it 1..4 (the GPU node pool's cluster
autoscaler adds nodes). The API runs 2..6 replicas behind TLS ingress, and up to 48 page-reading workers run. Postgres
uses premium disks and gets nightly `pg_dump` backups. The model cache lives on RWX file storage, and secrets are
created out of band. For production HA, use a managed Postgres with pgvector and a RabbitMQ cluster (quorum queues);
both are just URLs in `docuflow-secrets`.

**Hardening everywhere:**
- pods run as non-root with a read-only root filesystem, no capabilities, seccomp `RuntimeDefault`, and no service-account token
- PriorityClasses (`docuflow-interactive` outranks `docuflow-batch`; batch never preempts anything)
- PodDisruptionBudgets and NetworkPolicies (only docuflow pods and KEDA can reach Postgres and RabbitMQ)
- liveness/readiness/startup probes everywhere; workers expose `/health` and `/metrics` on :9100
- the API requires `X-API-Key` when `DOCUFLOW_API_KEY` is set
- an hourly retention CronJob (`python -m docuflow gc`)

**Alerts:**
- API down
- model server down
- dead jobs
- stale backlog
- job failure rate above 20%
- vLLM queueing
- GPU memory above 97%

## Local development without Kubernetes

```bash
make setup db       # venv; Postgres+pgvector :5434 and RabbitMQ :5673 in Docker
make test           # 22 tests: queue rules, broker tokens (real RabbitMQ), API, worker health, synthetic data
make stack          # or the whole system in docker compose (model simulator, api :8791, workers)
make up             # or: supervisor mode with Ollama on the host (models: VLM_MODEL, LLM_MODEL, EMBED_MODEL)
```

Workers poll Postgres directly when `RABBITMQ_URL` is unset. API: `POST /ingest`, `POST /query`, `GET /search`,
`GET /documents`, `GET /workloads`, `POST /workloads/{lane,pause,resume,retry-dead}`, `GET /jobs`, `GET /stats`,
`GET /metrics`, `GET /documents/{id}/image`.

## Limits worth knowing

- The `synthetic` dataset makes the stress test reproducible offline with checkable answers, but it is easier than real
  scans. Use `--dataset funsd` (or xfund/docvqa) for real pages. FUNSD has only 50 test pages.
- On one GPU, page reading is compute-bound. A second time-sliced vLLM replica adds concurrency, not FLOPs, so the
  speed-up is small. Real horizontal throughput comes from more GPUs (prod overlay).
- Page images are stored in Postgres (`bytea`, ~100-300 KB each). Move them to object storage beyond a few hundred thousand pages.
- The printed/handwritten label comes from the vision model and is unverified.
