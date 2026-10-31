# Part 6. Stress testing: method and results

## The load generator

`scripts/stress.py` produces both kinds of load at once and samples the cluster while it runs:

1. **Ingest:** one `POST /ingest` of N pages in `vlm` mode, so the vision model really reads every page. The
   `synthetic` dataset generates forms locally, with known field values.
2. **Queries:** C closed-loop clients, each sending a question, waiting for the answer, and repeating. Questions are
   generated from the synthetic forms ("Who signed document INV-1005?"), so answers can be checked for correctness,
   not just for HTTP 200. `--query-steps 8,24,48` ramps the client count every `--step-seconds`.
3. **Sampling,** every 10 s:
   - replicas (ready/desired) for every Deployment
   - pending pods
   - RabbitMQ depth per lane
   - jobs queued/running per lane
   - pages indexed
   - queries in flight
   - GPU utilisation and memory
4. **Scale-down phase:** with `--observe-after`, it stops all load and keeps sampling, so the report also shows KEDA
   scaling back down.
5. **Report:** a markdown file with ingest throughput, query latency per load step (p50/p95/max), error rate,
   answer correctness, peak replicas, and the full timeline.

```bash
python scripts/stress.py --api http://docuflow.localtest.me --dataset synthetic --pages 2000 --offset 2000 \
    --query-steps 8,24,48 --step-seconds 300 --observe-after 420 --out docs/stress-report.md
```

Grafana shows the same story live: the docuflow dashboard has panels for KEDA replicas, RabbitMQ tokens per lane,
vLLM running/waiting, KV-cache use and GPU utilisation.

## Run 1: 800 pages + 12 query clients

The first full run, before any of the fixes in Part 7 (report: `docs/stress-report-run1.md`):

| Metric | Value |
|---|---|
| pages indexed | 800 in 1,419 s, **34 pages/min** |
| first page queryable | after **10 s** |
| queries answered | **1,635, zero errors** |
| query latency during ingest | p50 9.6 s, p95 16.6 s, max 25.2 s |
| peak replicas | worker-vlm **0 → 12**, worker-io 0 → 3 → 0, api 1 → 3, model-vlm **1 → 2** |

The scale-out from zero, from the sampled timeline:

| t (s) | vlm tokens | worker-vlm | api | model-vlm |
|---|---|---|---|---|
| 10 | 339 | 1 | 1 | 1 |
| 21 | 795 | 4 | 3 | 1 |
| 31 | 790 | 6 | 3 | 1 |
| ~180 | 718 | 10 → 12 | 3 | 1 → 2 |

Two findings from this run:
- **Only 17% of answers were correct.** Extraction was perfect; retrieval wasn't. Document codes like "INV-1005"
  were tokenised as `inv` + `-1005` (a negative number), and the full-text branch returned 30 arbitrary matches.
  Fixing that, plus an agent change, raised RAGAS exact match to **97.5%**. That's not an autoscaling bug, but stress
  testing with checkable answers is what surfaced it.
- **model-vlm never scaled** until its threshold was lowered (Part 7, failure 2).

## Run 2: 2,000 pages, query ramp 8 → 24 → 48

Run 2 surfaced three of the scaling bugs live:

| Moment | Observation | Fix | Effect |
|---|---|---|---|
| t ≈ 300 s | 12 vlm workers up, **only 8 working** | raise in-app caps above KEDA max | ingest **39.7 → 56.5 pages/min** |
| t ≈ 500 s | vLLM replica A: 16 running + 6 waiting; replica B: **2 running** | no HTTP keep-alive to model servers | load split 16 / 12 |
| every `kubectl apply` | worker-vlm 24 → 1, model-vlm 2 → 1 | remove `replicas:` from KEDA-managed Deployments | counts survive applies |

## Query latency under load

All queries are stored in Postgres, so latency per 5-minute window comes straight from the database:

| Load | Queries / 5 min | p50 | p95 | p99 | max |
|---|---|---|---|---|---|
| light (no ingest) | 65 | **3.3 s** | 4.3 s | 5.3 s | 5.7 s |
| 8 clients + ingest | 237 | 5.5 s | 10.2 s | 13.2 s | 14.4 s |
| 24 clients + ingest | 538 | 8.1 s | 12.4 s | 18.7 s | 26.1 s |
| 48 clients + ingest | 630 | 17.4 s | 22.4 s | 42.2 s | 46.8 s |
| 96 clients + 2 ingests | 684 | **25.5 s** | **42.7 s** | **60.7 s** | 75.1 s |

Throughput flattened at **126–137 queries/min** from 48 clients upward, while latency kept climbing: a textbook
saturation curve. Measured at 96 clients:

| Component | State |
|---|---|
| GPU | 100% compute, 309 / 350 W |
| model-llm (agent) | **31 / 32 slots running, 64 waiting**, KV cache 81% |
| model-vlm x2 | 10 and 13 running, 0 waiting |
| api x8 | **~5 millicores CPU each** |

KEDA had scaled the API to its maximum of 8, which did nothing, because the API was waiting on the agent model.
The changes that targeted the actual bottleneck:
- run the first search before the first LLM call, saving one of ~2.2 LLM round trips per query
- give the agent model 64 batch slots and an 8 GiB KV cache
- make page reading back off to 8 concurrent while 24+ queries are in flight
- scale the agent model on its own queue in prod

Still to do: the before/after comparison at the same load. Part 8 gives the command to reproduce it.

## Error rate

Across every run, **no query failed** (no 5xx, no timeouts below 600 s). Under overload the system degrades by queueing,
not by dropping requests. Every job that failed was retried through the delay queues. The only persistent failure in
~16,000 pages was a single page that failed all retries and was parked as `dead`.

## Ingest mode: the whole GPU for ingest

The final experiment was to ingest every dataset as fast as possible, about 16,000 pages: synthetic forms, IAM
handwriting lines, XFUND, DocVQA and FUNSD. `scripts/mode.sh ingest` does four things:
- pauses queries (`POST /workloads/queries {"paused": true}`; the API answers 503 with `Retry-After`)
- scales the agent model to zero, releasing its ~17 GB of GPU memory
- raises the KEDA maxima: model-vlm to 3, worker-vlm to 48, worker-io to 8
- turns off the query back-off

| Configuration | Ingest rate |
|---|---|
| run 1 (cap of 8, single vLLM replica effective) | 34 pages/min |
| run 2 after the cap fix | 56.5 pages/min |
| ingest mode, 3 vLLM replicas, 48 workers, 8 fetchers | **~170 pages/min** (865 pages per 5 min) |

The page mix matters as much as the configuration. Under this load, mean VLM read time per page was:

| Dataset | Page type | Mean read time |
|---|---|---|
| IAM | one handwritten line | **5.0 s** |
| synthetic | generated form | 19.6 s |
| DocVQA | document page | 37.1 s |
| FUNSD | scanned form | 41.4 s |
| XFUND | multilingual form, ~2,480 px | **52.8 s** |

A "pages per minute" number means little without the page mix: a 10x difference in work per page separates IAM from
XFUND.
