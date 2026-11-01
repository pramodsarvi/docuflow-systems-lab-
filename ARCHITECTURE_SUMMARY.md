# Complete Architecture Refactor – Delivery Summary

## What Was Delivered

### 1. **Code Implementation** (11 files changed)
✅ Database schema for latency tracking and evaluation  
✅ Latency recording in extract/embed handlers  
✅ New RAGAS evaluation lane and API endpoint  
✅ KEDA scalers for vLLM based on latency  
✅ Fixed-capacity worker-ragas deployment  

**Commits:**
- `cc545f5` – Refactor: vLLM REST APIs + Ingestion Queue + RAGAS Fixed Deployment
- `eb5d0aa` – Docs: Architecture diagrams and human-friendly blog post

### 2. **Visual Documentation** (Mermaid Diagrams)
✅ System overview with all components  
✅ Ingest flow sequence diagram  
✅ Query flow sequence diagram  
✅ Evaluation flow sequence diagram  
✅ Component responsibilities table  
✅ Latency metrics feedback loop  
✅ Retry & backoff logic diagram  

**File:** `docs/ARCHITECTURE_DIAGRAM.md`

### 3. **Human-Friendly Blog Post**
✅ Problem statement (why the refactor was needed)  
✅ Three scaling strategies explained  
✅ Before/after architecture comparison  
✅ Detailed metrics flow walkthrough  
✅ Engineering benefits analysis  
✅ Implementation summary with code examples  
✅ Testing instructions  

**File:** `docs/blog/10-architecture-refactor.md`

---

## Key Architectural Changes

### Before: Everything Scaled on Queue Depth
```
RabbitMQ queue depth → KEDA scales workers → workers call GPU → GPU bottleneck
```

### After: Three Different Scaling Signals
```
┌─ Ingestion Workers (io, vlm, embed)
│  └─ Scales on: RabbitMQ queue depth
│     Why: Throughput-driven (pages/min)
│
├─ GPU Servers (model-vlm, model-embed)
│  └─ Scales on: Request latency p95/p99
│     Why: Latency-driven (SLO enforcement)
│
└─ Evaluation (worker-ragas)
   └─ Scales on: Fixed 2 pods
      Why: Background work (no SLA)
```

---

## Component Layout

```
┌─────────────────────────────────────────────────────────────┐
│                        API (FastAPI)                        │
│                   Port 8000 / Stateless                     │
│             KEDA: CPU + Queries in flight                   │
│                    Min 1 / Max 8 pods                       │
└─────────────────────────────────────────────────────────────┘

┌──────────────────────┬──────────────────────┬──────────────────┐
│   Ingest Pipeline    │   Ingest Pipeline    │  Ingest Pipeline │
├──────────────────────┼──────────────────────┼──────────────────┤
│   worker-io (0-3)    │   worker-vlm (0-24)  │ worker-embed (0-3)│
│   Fetch pages        │   Extract text       │   Embed chunks   │
│   KEDA: io queue     │   KEDA: vlm queue    │  KEDA: embed qe  │
└──────────────────────┴──────────────────────┴──────────────────┘

┌──────────────────────┬──────────────────────┐
│   GPU Tier (Scale on │   GPU Tier (Scale on │
│   Extract Latency)   │   Embed Latency)     │
├──────────────────────┼──────────────────────┤
│   model-vlm (1-8)    │  model-embed (1-4)   │
│   Qwen2.5-VL-3B      │  Qwen3-Embed-0.6B    │
│   p95 threshold: 5s  │  p95 threshold: 3s   │
│   KEDA: Postgres     │  KEDA: Postgres      │
└──────────────────────┴──────────────────────┘

┌──────────────────────────────────────────────┐
│     Evaluation (Fixed 2 pods / No scaling)   │
├──────────────────────────────────────────────┤
│   worker-ragas                               │
│   Runs RAGAS eval pipeline                   │
│   Background: 5-20 min per eval              │
└──────────────────────────────────────────────┘

┌──────────────────────────────────────────────┐
│     Data & Job State (Postgres)              │
├──────────────────────────────────────────────┤
│   - documents (pages + images)               │
│   - chunks (text + vectors)                  │
│   - jobs (state, lease, retries)             │
│   - queries (logs + traces)                  │
│   - model_latencies (metrics for KEDA)       │
│   - eval_results (RAGAS scores)              │
└──────────────────────────────────────────────┘

┌──────────────────────────────────────────────┐
│     Work Queue (RabbitMQ)                    │
├──────────────────────────────────────────────┤
│   - docuflow.io (fetch tokens)               │
│   - docuflow.vlm (extract tokens)            │
│   - docuflow.embed (embed tokens)            │
│   - docuflow.ragas (eval tokens)             │
│   - *.delay queues (retry backoff)           │
└──────────────────────────────────────────────┘
```

---

## Scaling Behavior Reference

### Ingest Load = 1000 Pages

```
Time 0:00
  POST /ingest (limit=1000)
  API: INSERT 50 fetch jobs (20 pages each)
  API: PUBLISH 50 tokens to docuflow.io
  
Time 0:15 (KEDA check interval)
  Queue depth = 50 tokens
  Decision: worker-io scale 0 → 1 pod
  
Time 0:30
  Queue depth = 35 tokens
  Fetch job finishes → enqueues extract
  
Time 1:00
  Extract queue depth = 150 tokens
  Decision: worker-vlm scale 0 → 3 pods
  model-vlm: latency p95 = 4200ms (under 5s)
  
Time 3:00
  Extract queue depth = 300 tokens
  worker-vlm latency p95 = 6100ms (over 5s threshold)
  Decision: model-vlm scale 1 → 2 replicas (GPU pods)
  
Time 5:00
  GPU capacity increased
  Latency p95 = 2800ms (back under threshold)
  Decision: model-vlm scale stays at 2
  worker-vlm continues processing
  
Time 10:00+
  All pages extracted, moving to embed
  embed queue depth grows
  worker-embed scales, model-embed scales on latency
  
Time 20:00
  Embed queue empties
  Last page marked indexed
  All pages searchable ✓
```

---

## Testing Checklist

- [ ] Database migrations run (automatic on startup)
- [ ] `docuflow.ragas` queue created in RabbitMQ
- [ ] KEDA ScaledObjects deployed and active
- [ ] worker-ragas pods running (2 fixed replicas)
- [ ] Latency metrics recorded in Postgres:
  - [ ] Extract: `SELECT * FROM model_latencies WHERE stage='extract'`
  - [ ] Embed: `SELECT * FROM model_latencies WHERE stage='embed'`
- [ ] KEDA scaling decisions logged:
  - [ ] `kubectl logs -n keda deployment/keda-operator`
- [ ] Manual eval job creation works:
  - [ ] `POST /eval?dataset=synthetic`
  - [ ] `SELECT * FROM jobs WHERE stage='eval'`
- [ ] worker-ragas processes eval jobs:
  - [ ] `kubectl logs -f deployment/worker-ragas`

---

## Files Changed Summary

| File | Changes | Purpose |
|------|---------|---------|
| `docuflow/db.py` | +13 lines | Added model_latencies & eval_results tables |
| `docuflow/llm.py` | +16 lines | Added record_latency() function |
| `docuflow/extract.py` | +3 lines | Record extract latency after vLLM call |
| `docuflow/pipeline.py` | +21 lines | Record embed latency + new eval handler |
| `docuflow/broker.py` | +1 line | Added "ragas" to LANES |
| `docuflow/jobs.py` | +3 lines | Added "eval" job mapping + snapshot fix |
| `docuflow/api.py` | +7 lines | Added POST /eval endpoint |
| `docuflow/service.py` | +16 lines | Added start_eval() function |
| `k8s/base/workers.yaml` | +30 lines | Added worker-ragas deployment |
| `k8s/base/keda.yaml` | +42 lines | Added model-vlm & model-embed latency scalers |
| `REFACTOR_SUMMARY.md` | 250 lines | Technical summary of changes |
| `docs/ARCHITECTURE_DIAGRAM.md` | 800 lines | Mermaid diagrams (7 diagrams) |
| `docs/blog/10-architecture-refactor.md` | 500 lines | Human-friendly blog post |
| `docs/blog/README.md` | +3 lines | Updated index |

**Total:** 14 files, 1,700+ lines of code & docs

---

## Key Insights for Team

### 1. Why Three Different Scaling Strategies?

**Throughput-driven (Ingest Queue):**
- More work in queue = scale up workers
- Simple, predictable, works well for batch processing
- Metric: queue depth

**Latency-driven (GPU Servers):**
- Requests getting slow = add GPU capacity
- Directly enforces SLO (response time)
- Metric: p95 latency from measurements

**Fixed Capacity (Evaluation):**
- Background work doesn't need to scale
- Fixed capacity prevents queue overflow
- Simpler than dynamic scaling for non-critical work

### 2. Data Flow Recap

```
Ingest: fetch → extract → embed → indexed (searchable)
  ├─ Each stage runs in separate worker lane
  ├─ RabbitMQ tokens coordinate work distribution
  ├─ Postgres stores job state & latency metrics
  └─ KEDA watches different signals for each tier

Query: question → search → agent loop → answer
  ├─ Runs in API pod (concurrent, stateless)
  ├─ Uses search (pgvector) + LLM calls
  └─ Competes with ingest for GPU (fair: latency-driven)

Eval: recent queries → RAGAS pipeline → scores
  ├─ Runs in worker-ragas (background)
  ├─ 2 fixed pods, no urgency
  └─ Never blocks ingest or query
```

### 3. Monitoring Strategy

**For Latency-Driven Scaling:**
```sql
-- Check current p95 latency
SELECT PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)
FROM model_latencies 
WHERE stage='extract' AND endpoint='model-vlm' 
  AND ts > now() - interval '2 minutes';
```

**For Queue-Driven Scaling:**
```bash
# Check queue depth
rabbitmqctl list_queues name messages | grep docuflow
```

**For KEDA Decisions:**
```bash
# Watch HPA scaling
kubectl get hpa -w

# Check KEDA logs
kubectl logs -n keda deployment/keda-operator
```

---

## Next Steps

1. **Deploy to staging:**
   - `kubectl apply -f k8s/base/`
   - Verify KEDA ScaledObjects are active
   - Monitor metrics in first 24 hours

2. **Add dashboards:**
   - Prometheus: latency metrics per stage/endpoint
   - Grafana: vLLM replica count + latency SLO line
   - Alerts: when p95 exceeds SLO for 2+ minutes

3. **Load testing:**
   - Run stress test with ingest + queries
   - Verify KEDA scaling decisions
   - Measure query latency under ingest load

4. **Team training:**
   - Share blog post with team
   - Walk through architecture diagrams
   - Practice debugging scaling issues

---

## Documentation

**For Reference:**
- `docs/ARCHITECTURE_DIAGRAM.md` – Visual system overview
- `docs/blog/10-architecture-refactor.md` – Why we did it
- `REFACTOR_SUMMARY.md` – Technical implementation details
- `ARCHITECTURE_SUMMARY.md` – This file (high-level overview)

**For Operations:**
- KEDA ScaledObjects in `k8s/base/keda.yaml`
- Worker deployments in `k8s/base/workers.yaml`
- SQL schema in `docuflow/db.py` (SCHEMA constant)

---

## Done! ✅

The refactored architecture is:
- **Implemented** in code
- **Deployed** in Kubernetes configs
- **Documented** with diagrams and blog post
- **Ready to test** with your datasets

Start with a small ingest test and watch the scaling decisions happen in real-time. Good luck! 🚀
