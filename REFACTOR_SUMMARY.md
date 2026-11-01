# DocuFlow Architecture Refactor: vLLM REST APIs + Ingestion Queue + RAGAS

## Overview
Refactored the system to separate concerns:
- **Ingestion pipeline:** RabbitMQ queues with KEDA scaling on queue depth
- **vLLM servers:** REST APIs with KEDA scaling on request latency (p95/p99)
- **RAGAS evaluation:** Fixed-size queue with constant pod count

## Code Changes

### 1. Database Schema (docuflow/db.py)
**Added two new tables:**
- `model_latencies`: Records vLLM request latency for each stage/endpoint
  - Columns: id, stage (extract/embed), endpoint (model-vlm/model-embed), latency_ms, in_flight_requests, ts
  - Index: (stage, endpoint, ts DESC) for fast queries over time window
- `eval_results`: Stores RAGAS evaluation results
  - Columns: id, job_id, dataset, query_id, metrics (JSONB), ts

### 2. Latency Recording (docuflow/llm.py)
**Added function:**
- `record_latency(stage, endpoint, latency_ms, in_flight_requests=None)`
  - Fire-and-forget recording into model_latencies table
  - Used by extract and embed handlers to track request times

### 3. Extract Handler (docuflow/extract.py)
**Modified:**
- `read_page()` now records latency after each vLLM call
- Calls: `llm.record_latency("extract", "model-vlm", latency_ms)`

### 4. Embed Handler (docuflow/pipeline.py)
**Modified:**
- `run_embed()` now records latency after embedding
- Calls: `llm.record_latency("embed", "model-embed", latency_ms)`

**Added:**
- `run_eval()` handler for RAGAS evaluation jobs
- Stores metrics in eval_results table

### 5. Job Queue Management (docuflow/jobs.py)
**Added:**
- `"eval": "ragas"` mapping to LANE_OF
- `"eval": 0` to STAGE_PRIORITY
- Updated snapshot() function to include "ragas" lane

### 6. RabbitMQ Broker (docuflow/broker.py)
**Added:**
- `"ragas"` to LANES list
- Queue: `docuflow.ragas` (created automatically by _channel())
- Delay queue: `docuflow.ragas.delay` for retry backoff

### 7. API Endpoint (docuflow/api.py)
**Added:**
- `POST /eval` endpoint
- Triggers service.start_eval(dataset) to queue evaluation jobs

### 8. Service Layer (docuflow/service.py)
**Added:**
- `start_eval(dataset=None)` function
  - Finds recent queries without eval jobs
  - Creates eval job per query
  - Publishes tokens to docuflow.ragas queue

## Kubernetes Configuration

### 1. Worker Deployment (k8s/base/workers.yaml)
**Added:**
- `worker-ragas` Deployment
  - 2 fixed replicas (never scales)
  - Runs: `python -m docuflow worker ragas`
  - Resources: 200m CPU, 512Mi memory (limits: 1Gi)
  - Metrics scraping on port 9100

### 2. KEDA Scaling (k8s/base/keda.yaml)
**Added:**
- `model-vlm-latency` ScaledObject
  - Scales vLLM model-vlm based on extract p95 latency
  - Min 1, max 8 replicas
  - Threshold: scale up if p95 > 5 seconds, scale down if < 2 seconds
  - PostgreSQL query: `PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)`

- `model-embed-latency` ScaledObject
  - Scales vLLM model-embed based on embed p95 latency
  - Min 1, max 4 replicas
  - Threshold: scale up if p95 > 3 seconds, scale down if < 1.5 seconds

## New Architecture Flow

```
POST /ingest
  ├─ Create fetch jobs in Postgres
  └─ Publish tokens to docuflow.io
     ├─ KEDA scales worker-io on queue depth
     └─ worker-io → fetch_handler
        ├─ Download images
        └─ Enqueue extract jobs
           ├─ Publish tokens to docuflow.vlm
           ├─ KEDA scales worker-vlm on queue depth
           └─ worker-vlm → extract_handler
              ├─ HTTP POST to model-vlm:8000/v1/chat/completions
              ├─ Record latency in model_latencies
              ├─ KEDA scales model-vlm on p95 latency (PostgreSQL query)
              └─ Enqueue embed jobs
                 ├─ Publish tokens to docuflow.embed
                 ├─ KEDA scales worker-embed on queue depth
                 └─ worker-embed → embed_handler
                    ├─ HTTP POST to model-embed:8001/v1/embeddings
                    ├─ Record latency in model_latencies
                    └─ KEDA scales model-embed on p95 latency

POST /eval (NEW)
  ├─ Find recent queries without eval
  ├─ Create eval jobs in Postgres
  └─ Publish tokens to docuflow.ragas
     └─ worker-ragas (2 fixed pods) → eval_handler
        ├─ Run RAGAS pipeline
        └─ Store metrics in eval_results
```

## Key Differences from Previous Design

| Aspect | Old | New |
|--------|-----|-----|
| **Extract Queue** | RabbitMQ held jobs; workers consumed directly | RabbitMQ holds tokens; workers call vLLM via HTTP |
| **vLLM Scaling** | KEDA on queue depth (indirect) | KEDA on latency p95 (direct SLO-driven) |
| **Model Calls** | Workers queued, KEDA watched queue | Workers call REST API, latency metrics drive scaling |
| **Evaluation** | Not separated | Dedicated fixed queue (2 pods always) |
| **Metrics** | Queue depth only | Queue depth + latency per stage/endpoint |

## Scaling Signals

- **worker-io:** RabbitMQ docuflow.io queue depth
- **worker-vlm:** RabbitMQ docuflow.vlm queue depth
- **worker-embed:** RabbitMQ docuflow.embed queue depth
- **worker-ragas:** Fixed 2 replicas (no scaling)
- **model-vlm:** PostgreSQL latency p95 (extract stage)
- **model-embed:** PostgreSQL latency p95 (embed stage)

## Testing

1. **Unit tests:** `python3 -m pytest tests/test_pipeline.py -xvs`
2. **Database migrations:** Already run via `db.init_schema()` on worker/API startup
3. **Integration test:**
   ```bash
   # Terminal 1: Start services (docker-compose up -d rabbitmq postgres)
   # Terminal 2: 
   curl -X POST http://localhost:8000/ingest -H 'Content-Type: application/json' \
     -d '{"dataset": "synthetic", "limit": 10}'
   
   # Monitor latencies:
   curl http://localhost:8000/metrics | grep model_latencies
   ```

## Future Enhancements

1. **Prometheus integration:** Export model_latencies as Prometheus metrics for external monitoring
2. **Dynamic SLO adjustment:** Adjust latency thresholds based on time of day or load patterns
3. **RAGAS evaluation expansion:** Add full RAGAS metrics (faithfulness, context precision, etc.)
4. **Per-endpoint routing:** Route workers to specific vLLM replicas based on load distribution

## Deployment Checklist

- [ ] Run `db.init_schema()` to create new tables (automatic on worker/API start)
- [ ] Update `k8s/base/kustomization.yaml` to include worker-ragas
- [ ] Apply `k8s/base/workers.yaml` (includes worker-ragas)
- [ ] Apply `k8s/base/keda.yaml` (includes model-vlm/embed latency scalers)
- [ ] Verify KEDA ScaledObjects: `kubectl get scaledobjects`
- [ ] Check worker metrics: `kubectl logs -f deployment/worker-ragas`
- [ ] Monitor latencies: `curl http://<api>/metrics | grep model_latencies`
