# DocuFlow Refactored: Three Scaling Strategies for Three Different Problems

**Date:** November 2026 | **Status:** Shipped  
**TL;DR:** We separated vLLM servers from the job queue and gave them their own scaling signal (latency), created a fixed-size evaluation pool so it never blocks user queries, and kept the ingest pipeline queue-driven. Each tier now scales on what actually matters to it.

---

## The Problem We Started With

When we first built the job queue, everything was simple: workers poll a queue, KEDA watches the queue depth, and scales pods up when work arrives. It works great for **throughput-driven workloads** (like page fetching). But we had a problem:

**The vision model server (vLLM) was being scaled by the wrong metric.**

Here's what was happening:

1. User submits 1,000 pages to ingest
2. RabbitMQ queue fills up with 1,000 extract tokens
3. KEDA sees the queue and scales worker-vlm pods from 1 → 20
4. Meanwhile, a user asks a question
5. **The query has to wait in line behind the ingest jobs** 😱

Why? Because everything was competing for GPU time based on queue depth. The system couldn't distinguish between "we have lots of work" (good) and "work is taking a long time" (bad). It scaled on speculation, not reality.

---

## The Insight: Three Different Scaling Problems

We realized the system has **three fundamentally different scaling requirements**:

### 1. **Throughput-Driven: Ingest Pipeline** (fetch → extract → embed)
- *Question:* "How many pages are waiting to be processed?"
- *Metric:* Queue depth (work tokens waiting)
- *Scaling:* KEDA watches RabbitMQ queue length
- *Goal:* Maximize pages/minute while respecting resource limits

### 2. **Latency-Driven: Model Servers** (vLLM GPU pods)
- *Question:* "How long are requests taking to complete?"
- *Metric:* Request latency p95/p99 (actual measured time)
- *Scaling:* KEDA watches response time in Postgres
- *Goal:* Keep response time under SLO (5s for extract, 3s for embed)

### 3. **Background Work: Evaluation** (RAGAS)
- *Question:* "Do we need evaluation running all the time?"
- *Metric:* None (fixed capacity)
- *Scaling:* No scaling—always 2 pods
- *Goal:* Continuously drain eval queue without blocking users

---

## The Old Architecture

```
┌─────────────────────────────────────────────────────────┐
│ Everything scales on RabbitMQ queue depth               │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  RabbitMQ docuflow.vlm                                 │
│  (extract tokens: 1000 waiting)                        │
│        ↓                                                │
│  KEDA: Scale worker-vlm 1 → 20 pods                   │
│        ↓                                                │
│  worker-vlm pods call vLLM (GPU)                      │
│        ↓                                                │
│  vLLM batches requests, processes them                │
│        ↓                                                │
│  ❌ Problem: Worker scaling doesn't match GPU capacity │
│  ❌ Problem: Queries compete with ingest on queue      │
│  ❌ Problem: No signal about actual latency            │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**The flaw:** We scaled worker pods based on queue depth, but the GPU server was the bottleneck. 20 worker pods calling the same 1 GPU server doesn't help—they just queue up inside vLLM (failure 13 from the design doc).

---

## The New Architecture

### Layer 1: Ingest Pipeline (Queue-Driven)

```
POST /ingest (100 pages)
  ↓
Postgres: INSERT 5 fetch jobs
  ↓
RabbitMQ: Publish 5 tokens to docuflow.io
  ↓
KEDA: Watches queue depth every 5 seconds
  ├─ Queue has 5 tokens
  ├─ Scale worker-io: 0 → 1 pod
  └─ Worker claims job, processes batch
```

**Logic:** Queue depth = available work. Scale workers to drain it. This is perfect for **throughput optimization**.

### Layer 2: Model Servers (Latency-Driven)

```
worker-vlm calls: HTTP POST /v1/chat/completions
  ├─ Latency: 4200 ms
  ├─ Record: INSERT model_latencies (stage='extract', endpoint='model-vlm', latency_ms=4200, ts=now)
  └─ After 10 requests: average = 4100 ms
      ↓
Postgres: model_latencies table has 10 recent entries
  ↓
KEDA: Queries every 15 seconds
  ├─ SELECT PERCENTILE_CONT(0.95) ... FROM model_latencies
  ├─ Result: p95 = 4800 ms (under 5s SLO ✓)
  ├─ Decision: Keep current scale (2 GPU replicas)
  ↓
[Later: Request latency spikes to 6500 ms]
  ↓
KEDA: p95 = 6200 ms (over 5s SLO ✗)
  ├─ Decision: Scale model-vlm 2 → 4 replicas
  ├─ More GPU capacity available
  ├─ New batch slots = more concurrency
  └─ Latency drops back to 3200 ms ✓
```

**Logic:** Measure actual request time. Scale GPU when response time exceeds SLO. This is **SLO-driven scaling**.

### Layer 3: Evaluation (Fixed Capacity)

```
POST /eval?dataset=acme
  ├─ Find: 50 recent queries without eval
  ├─ Enqueue: 50 eval jobs to docuflow.ragas
  ├─ Publish: 50 tokens to RabbitMQ
  └─ Return: {"eval_jobs_queued": 50}

RabbitMQ docuflow.ragas: 50 tokens waiting
  ├─ 2 fixed worker-ragas pods
  ├─ Each runs 1 eval job at a time (5-20 min)
  └─ Queue steadily drains (no autoscaling)

Why fixed size?
├─ Evaluation is background work (no SLA)
├─ Doesn't need to be fast, just needs to happen
├─ Constant consumption prevents queue buildup
└─ 2 pods = 10-20 evals/day (plenty)
```

**Logic:** Some work doesn't need to scale. Fixed capacity is simpler and avoids unnecessary complexity.

---

## How the Metrics Flow Works

This is the cool part. Let me trace one request all the way through:

### Scenario: Extract Job at Peak Load

**Time 0:00 – Worker starts extract job**
```
worker-vlm pod claims extract job
  → job.id = 1234, doc_id = "acme/inv-005.jpg"
  → image = 2.5MB scanned document
  → Call: model-vlm:8000/v1/chat/completions
```

**Time 0:04 – Response arrives**
```
vLLM returns regions (text, bbox, kind)
  → Worker records: INSERT model_latencies 
       (stage='extract', endpoint='model-vlm', latency_ms=4120, ts=now)
  → Worker chunks the regions
  → Worker inserts chunks into Postgres
  → Worker enqueues embed job
  → Worker marks job done
```

**Time 0:15 – KEDA checks latency**
```
KEDA runs its 15-second poll:
  SELECT PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)
  FROM model_latencies 
  WHERE stage='extract' AND endpoint='model-vlm' 
    AND ts > now() - interval '2 minutes'
  
Result: p95 = 4850 ms (over 5000 ms SLO threshold ✗)

HPA adjusts:
  Previous replicas: 2
  New replicas: 4  (scaled up by 2)
  Kubernetes pulls new GPU node, starts vLLM container
```

**Time 0:20 – New replicas are ready**
```
vLLM container starts, loads model weights (~8GB)
  → First request to new replica
  → vLLM initializes batch slots
  → GPU memory reserved
  → Ready to serve
```

**Time 0:25+ – Queries process faster**
```
Requests now spread across 4 replicas instead of 2
  → More batch slots available
  → Less queueing inside vLLM
  → Latency drops: 4850 ms → 2300 ms
  → SLO comfortably met ✓
```

**Time 5:00 – KEDA checks again**
```
Previous 2-minute window shows: p95 = 2100 ms
Decision: Can scale down safely
Transition: 4 replicas → 3 replicas (one pod at a time, 60s apart)
```

---

## The Engineering Benefits

### 1. **Separation of Concerns**

| Component | Scales On | Why It Matters |
|-----------|-----------|----------------|
| **worker-io** | Queue depth | Throughput—more work = more workers |
| **worker-vlm** | Queue depth | Throughput—drain queued jobs |
| **worker-embed** | Queue depth | Throughput—drain queued jobs |
| **model-vlm** | Latency p95 | SLO—keep response time acceptable |
| **model-embed** | Latency p95 | SLO—keep response time acceptable |
| **worker-ragas** | (fixed 2) | Background—doesn't need to scale |

Each tier has one reason to exist and scales accordingly. No confusion.

### 2. **Query Latency Isolation**

Before: Ingest queue depth → scales workers → workers hammer GPU → query latency spikes  
After: Ingest queue depth → scales workers (separate vLLM endpoints) + latency → scales GPU independently

**Result:** Ingest doesn't block queries anymore. They compete for GPU, but fairly—based on actual demand (latency), not speculation.

### 3. **Cost Efficiency**

Before: 1000-token queue → scale to 20 worker pods (even if GPU is only 20% busy)  
After: 1000-token queue → 8 worker pods + GPU scales based on actual load

**Result:** We don't oversupply worker pods anymore. GPU is the constraint, and it scales to match.

### 4. **Easier Debugging**

If extract latency is high:
- Check: `SELECT p95 FROM model_latencies WHERE stage='extract'`
- Clear signal: Is the GPU overloaded? Yes → scale it up.

No guessing, no "why is this slow?" hunts. The metric tells you exactly what's happening.

---

## The Implementation: What We Changed

### Database Schema
```sql
-- New table: tracks every vLLM request time
CREATE TABLE model_latencies (
  stage text,           -- 'extract' or 'embed'
  endpoint text,        -- 'model-vlm' or 'model-embed'
  latency_ms int,
  ts timestamptz
);

-- New table: stores evaluation results
CREATE TABLE eval_results (
  job_id bigint,
  dataset text,
  metrics jsonb         -- {"faithfulness": 0.92, ...}
);
```

### Worker Changes
```python
# extract_handler
t0 = time.perf_counter()
regions, secs = extract.read_page(image_bytes)
latency_ms = (time.perf_counter() - t0) * 1000

# Record it for KEDA
llm.record_latency("extract", "model-vlm", latency_ms)
```

### KEDA ScaledObjects
```yaml
# model-vlm scales on extract latency
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: model-vlm-latency
spec:
  scaleTargetRef:
    name: model-vlm
    kind: StatefulSet
  minReplicaCount: 1
  maxReplicaCount: 8
  triggers:
    - type: postgresql
      metadata:
        query: |
          SELECT PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)
          FROM model_latencies 
          WHERE stage='extract' AND endpoint='model-vlm'
          AND ts > now() - interval '2 minutes'
        targetQueryValue: "5000"      # 5 second SLO
```

### New eval Lane
```python
# jobs.py
LANE_OF = {
  "fetch": "io",
  "extract": "vlm",
  "embed": "embed",
  "eval": "ragas"  # ← new
}
```

```yaml
# workers.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: worker-ragas
spec:
  replicas: 2  # Fixed—never scales
  template:
    spec:
      containers:
        - command: ["python", "-m", "docuflow", "worker", "ragas"]
```

---

## Testing It Out

### 1. Start a small ingest
```bash
curl -X POST http://localhost:8000/ingest \
  -H 'Content-Type: application/json' \
  -d '{"dataset": "synthetic", "limit": 100}'
```

### 2. Watch queue depth
```bash
watch 'curl -s http://localhost:8000/metrics | grep model_latencies'
```

### 3. Check KEDA decisions
```bash
kubectl get hpa
kubectl describe hpa model-vlm-latency
```

### 4. Trigger evaluation
```bash
curl -X POST http://localhost:8000/eval?dataset=synthetic
```

### 5. Monitor eval jobs
```bash
kubectl logs -f deployment/worker-ragas --tail=20
```

---

## What's Next?

1. **Prometheus integration** – Export model_latencies as Prometheus metrics for the dashboard
2. **Per-endpoint routing** – Route workers to vLLM replicas based on load distribution
3. **Dynamic SLO adjustment** – Adjust thresholds based on time of day (busier = looser SLO)
4. **Full RAGAS metrics** – Add faithfulness, context precision, context recall scoring

---

## Summary

We took a system that scaled everything on queue depth and split it into three tiers:

1. **Workers** scale on queue depth (throughput-optimized)
2. **GPUs** scale on latency (SLO-optimized)
3. **Evaluation** runs at fixed capacity (background-optimized)

Each tier now scales on what actually matters to it. The result: better query latency, more efficient resource usage, and a system that's much easier to reason about.

---

**Questions?** Check out `ARCHITECTURE_DIAGRAM.md` for detailed flow diagrams, or `REFACTOR_SUMMARY.md` for a technical breakdown of all the code changes.
