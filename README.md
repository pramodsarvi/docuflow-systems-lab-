# docuflow

Ingest scanned documents that mix **printed and handwritten text**, and answer questions about them with an
**agentic RAG** loop, while ingestion keeps running. Everything runs locally: Ollama for the models, Postgres + pgvector
for storage, search and the job queue.

| Dataset | Source on the Hub | What the pipeline gets |
|---|---|---|
| FUNSD | `nielsr/funsd-layoutlmv3` | scanned forms, printed labels + filled-in answers |
| XFUND | `nnul/xfund-multilingual` | forms in DE/ES/FR/IT/JA/PT/ZH |
| DocVQA | `lmms-lab/DocVQA` | document pages with question/answer pairs (used for QA evals) |
| IAM | `Teklia/IAM-line` | handwritten **line images** only (the Hub copy has no full forms or printed prompts) |

## How it works

```
 POST /ingest ──► fetch (io lane) ──► extract (vlm lane) ──► embed (embed lane) ──► pgvector + tsvector
   per dataset     stream a slice,      vision model reads the     embed chunks,        queryable as soon as
                   store page, enqueue  page: text + printed/      mark page            each page lands
                   one extract per page handwritten + role         indexed
 POST /query ──► agent loop: search / get_page / read_page_image / corpus_stats / answer(citations)
```

- **Parallel ingest and query.** Each stage is a job in a Postgres queue (`FOR UPDATE SKIP LOCKED`). Pages are
  enqueued as they are fetched and indexed as soon as they are embedded, so a query run mid-ingest sees whatever is
  indexed so far. The agent is told the corpus is still being ingested.
- **Workload management** (`docuflow/jobs.py`), all live-adjustable without restarting:
  - per-lane concurrency limit (`io`, `vlm`, `embed`), enforced across processes under an advisory lock
  - per-dataset cap in the vlm lane plus fair-share ordering, so one big dataset cannot starve the others
  - priorities; downstream stages outrank upstream ones so pages finish and become queryable sooner
  - pause/resume a lane or a dataset
  - **the vlm lane yields while queries are in flight**, so interactive questions are not stuck behind page reading
  - retries with exponential backoff, a dead-letter state (`retry-dead`), lease expiry for crashed workers,
    orphan recovery on startup, and the supervisor restarts any process that dies
- **Printed vs handwritten.** The vision model labels every region `printed` or `handwritten` and gives it a role
  (label, value, signature…). Each chunk line keeps that tag, so the agent can filter (`kind=handwritten`) and cite.
- **Agentic RAG** (`docuflow/agent.py`). A JSON-action loop on a small LLM: search (hybrid vector + full-text, RRF),
  rewrite and retry, read a whole page, **look at the page image** with the vision model when extracted text is
  unclear, then answer with citations. A guard refuses to answer before the first search. The full trace is returned.

## Run it

```bash
make setup db models        # venv, pgvector on :5434, pull the three Ollama models
make up                     # API on http://127.0.0.1:8791 (dashboard at /) + workers      (terminal 1)
make ingest-all             # queue all four datasets at once                              (terminal 2)
make status                 # or: python -m docuflow watch
make ask Q="Who is the fax cover sheet addressed to?"   # works while ingestion is running
python -m docuflow pause datasets xfund      # workload control
python -m docuflow lane vlm 1                # set lane concurrency
make eval QA=12             # writes evals/report.md
make test
```

Models are configurable: `VLM_MODEL` (default `gemma4:e2b-mlx`), `LLM_MODEL` (`qwen3:4b-instruct`), `EMBED_MODEL`
(`qwen3-embedding:0.6b`). `--mode gt` on `ingest` skips the vision model and uses the dataset's own transcription,
which is useful for fast tests.

API: `POST /ingest`, `POST /query`, `GET /search`, `GET /workloads`, `POST /workloads/{lane,pause,resume,retry-dead}`,
`GET /jobs`, `GET /stats`, `GET /documents/{id}/image`.

## Kubernetes (fake GPUs, KEDA + HPA)

Runs on the existing `kind` cluster from the llm-d project (3 workers x 3 fake `nvidia.com/gpu`, KEDA, metrics-server,
the `tenant-*` PriorityClasses). Plain Kustomize manifests in `k8s/`, no Helm.

```bash
make k8s-up        # build image, load into kind, deploy db + api + sim-model + workers + scalers
make k8s-key KEY=...   # optional: require X-API-Key (also: DOCUFLOW_API_KEY env locally)
make k8s-ui        # port-forward the API to :8792 (leave running)
make k8s-demo      # ingest burst, prints worker-vlm replicas / GPU-pending samples, asks a question mid-run
make k8s-status    # pods, ScaledObjects, HPA, node GPU capacity
make k8s-real      # swap the simulator for real Ollama on the Mac host
```

| Piece | Scaling |
|---|---|
| `worker-vlm` (requests 1 fake GPU, `tenant-batch` priority) | **KEDA** Postgres scaler on queued+running vlm jobs, 0 to 6 replicas, ~2 jobs per worker. Scales to zero when idle. |
| `worker-io`, `worker-embed` (no GPU) | KEDA, 1 to 2 replicas |
| `api` (`tenant-standard`) | plain **HPA** on CPU, 1 to 3 replicas |

Only 3 of the 6 fake GPUs are free next to the llm-d tenants, so KEDA asking for more vlm workers leaves the extra pods
`Pending` until GPUs free up, and a scaled-up `critical` tenant preempts them (ingest is the lowest priority). Workers
get 180s to finish their current job on scale-down; anything lost is requeued by lease expiry.

`sim-model` is a stand-in for Ollama with fixed latency and **synthetic output** (it cannot read images), so the demo
shows scheduling and scaling, not extraction quality. In-app lane limits are set above the replica caps because the pod
count is the real limit on Kubernetes.

## Production on GPUs (AKS)

`k8s/overlays/gpu` is the production variant for the AKS GPU cluster built by `infra/aks/up.sh` (GPU Operator, vLLM
model servers, LiteLLM, KEDA, Prometheus/DCGM):

| Concern | How |
|---|---|
| GPU usage | The GPUs belong to the vLLM servers (`models/vlm.prod.values.yaml`: Qwen2.5-VL-7B, KEDA-scaled 1 to 3 on in-flight requests; `embed`; the agent model). docuflow's workers are clients and request no GPU, so more workers means more concurrent requests that vLLM batches. |
| Model access | `LLM_BACKEND=openai`, all roles through **LiteLLM** (`vlm-model`, `standard-model`, `embed-model`): per-tenant key, budget, rate limit. Retries with backoff on 429/5xx. |
| Secrets | DSN, LiteLLM key and API key in a Secret (`make gpu-docuflow-secrets`), never in manifests. |
| API | `X-API-Key` auth, 2 to 6 replicas on CPU HPA, spread across nodes, PodDisruptionBudget, `/ready` and `/health` probes. |
| Pods | non-root, read-only root filesystem, no capabilities, resource requests/limits, 180s graceful stop. |
| Data | Postgres on a premium disk with a PDB and a NetworkPolicy (only docuflow and KEDA can connect). For real production use a managed Postgres with pgvector and put its URL in `DOCUFLOW_DSN`. |
| Observability | `GET /metrics` (queue depth and age per lane, documents by status, query latency, in-flight queries), scraped via pod annotations. |

```bash
make gpu-docuflow-image ACR=<registry>          # linux/amd64 build + push
export DOCUFLOW_DSN=... LLM_API_KEY=<litellm key> DOCUFLOW_API_KEY=<choose one>
make gpu-docuflow-secrets
make gpu-docuflow-up ACR=<registry>
```

**Status: not yet run on a real GPU cluster.** `az` is not installed on the dev machine and no subscription was used.
What has been checked: the overlay renders and passes a server-side dry run, the OpenAI-protocol backend was run against
real local models (embeddings, agent, and the vision model reading a FUNSD page) and against the simulator in tests, and the
scaling logic ran on the fake-GPU kind cluster. Expect to tune on first contact with real hardware: vLLM model names and
flags, `maxNumSeqs`, worker counts, and `targetQueryValue`.

## Limits worth knowing

- Page images are stored in Postgres (`bytea`, downscaled JPEG, roughly 100-300 KB each). Fine to hundreds of thousands of pages; beyond that move them to object storage.
- Everything shares one machine (local mode). With three models resident, a query takes tens of seconds, and more while pages are being
  read. Results are in `evals/report.md`. Raise `PROCS_VLM`/lane limits on a bigger GPU.
- The datasets do not label printed vs handwritten. That label comes from a small vision model and is unverified.
- The API is open unless `DOCUFLOW_API_KEY` is set (`/health` and the dashboard page stay open). Set it before exposing the API.
- Ports: the API uses 8791 because 8000 and 8765 are commonly taken; change with `python -m docuflow up --port`.
