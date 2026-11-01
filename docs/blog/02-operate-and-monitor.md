# Operating DocuFlow: Monitor, Scale, Troubleshoot

**Deploy once, monitor everywhere.**

---

## Pod & Service Map

```mermaid
graph TB
    subgraph "API (Stateless, HPA)"
        API1["api-1"]
        API2["api-2"]
        SVC_API["Service api<br/>Port 8000"]
    end

    subgraph "Ingest Workers (HPA on queue)"
        WIO1["worker-io-1"]
        WIO2["worker-io-2"]
        WVLM1["worker-vlm-1"]
        WVLM2["worker-vlm-2"]
        WVLM3["worker-vlm-3"]
        WEMBED1["worker-embed-1"]
    end

    subgraph "GPU Servers (HPA on latency)"
        MVLM1["model-vlm-1<br/>GPU node"]
        MVLM2["model-vlm-2<br/>GPU node"]
        SVC_MVLM["Service model-vlm<br/>Port 8000"]
        
        MEMBED1["model-embed-1<br/>GPU node"]
        SVC_MEMBED["Service model-embed<br/>Port 8001"]
    end

    subgraph "Fixed Workers"
        RAGAS1["worker-ragas-1"]
        RAGAS2["worker-ragas-2"]
    end

    subgraph "Infrastructure"
        INGRESS["Ingress<br/>example.com"]
        PG["Postgres Service<br/>Port 5432"]
        RMQ["RabbitMQ Service<br/>Port 5672"]
        PROM["Prometheus<br/>Port 9090"]
    end

    INGRESS -->|routes| SVC_API
    SVC_API --> API1 & API2
    
    API1 & API2 -->|claim jobs| PG
    API1 & API2 -->|publish tokens| RMQ
    
    RMQ -->|io tokens| WIO1 & WIO2
    RMQ -->|vlm tokens| WVLM1 & WVLM2 & WVLM3
    RMQ -->|embed tokens| WEMBED1
    RMQ -->|ragas tokens| RAGAS1 & RAGAS2
    
    WVLM1 & WVLM2 & WVLM3 -->|HTTP| SVC_MVLM
    SVC_MVLM --> MVLM1 & MVLM2
    
    WEMBED1 -->|HTTP| SVC_MEMBED
    SVC_MEMBED --> MEMBED1
    
    RAGAS1 & RAGAS2 -->|HTTP| SVC_MVLM & SVC_MEMBED
    
    WIO1 & WIO2 & WVLM1 & WVLM2 & WVLM3 & WEMBED1 & RAGAS1 & RAGAS2 -->|record metrics| PG
    
    PROM -->|scrape :9100| WIO1 & WIO2 & WVLM1 & WVLM2 & WVLM3 & WEMBED1 & RAGAS1 & RAGAS2
    
    style SVC_API fill:#4a90e2
    style SVC_MVLM fill:#f5a623
    style SVC_MEMBED fill:#f5a623
    style PG fill:#7ed321
    style RMQ fill:#d0021b
```

---

## Monitoring Queries

### Queue Depth (Ingest Backlog)
```sql
-- What work is waiting?
SELECT lane, COUNT(*) as queued, COUNT(*) FILTER(WHERE status='running') as running
FROM jobs
WHERE status IN ('queued', 'running')
GROUP BY lane;

-- Expected output:
-- lane | queued | running
-- io   |     50 |       2
-- vlm  |    300 |       8
-- embed|    100 |       3
-- ragas|     10 |       0
```

### Worker Pod Count (Should match queue depth)
```bash
# Current replica counts
kubectl get deploy -l app.kubernetes.io/component=worker -o wide
kubectl get hpa -o wide

# Example:
# NAME             DESIRED   CURRENT   READY   AGE
# worker-io        2         2         2       2h
# worker-vlm       8         8         8       2h
# worker-embed     2         2         2       2h
# worker-ragas     2         2         2       2d
```

### Model Latency (GPU Health)
```sql
-- Extract model p95 latency (last 2 minutes)
SELECT 
  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) as p95_ms,
  AVG(latency_ms) as avg_ms,
  COUNT(*) as requests
FROM model_latencies
WHERE stage='extract' AND endpoint='model-vlm'
  AND ts > now() - interval '2 minutes';

-- Expected: p95 < 5000ms (5 sec SLO)

-- Embed model p95 latency
SELECT 
  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) as p95_ms,
  COUNT(*) as requests
FROM model_latencies
WHERE stage='embed' AND endpoint='model-embed'
  AND ts > now() - interval '2 minutes';

-- Expected: p95 < 3000ms (3 sec SLO)
```

### Query Performance
```sql
-- Interactive query latency
SELECT 
  COUNT(*) as queries_5min,
  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY seconds) as p95_latency,
  MAX(seconds) as max_latency
FROM queries
WHERE created_at > now() - interval '5 minutes';

-- Expected: p95 < 15s during ingest, <5s idle
```

### GPU Pod Status
```bash
# Are GPU pods healthy?
kubectl get pods -l app=model-vlm
kubectl logs -f deployment/model-vlm --tail=20

# Check GPU memory
kubectl exec -it model-vlm-0 -- nvidia-smi

# Check vLLM is serving requests
curl http://model-vlm:8000/v1/models
```

---

## Troubleshooting

### "Queries Are Slow"

```mermaid
graph TD
    A["Query p95 > 15s"] --> B{"Extract latency<br/>p95 > 5s?"}
    B -->|Yes| C["GPU bottleneck"]
    C --> D["Check KEDA:<br/>kubectl describe hpo<br/>model-vlm-latency"]
    D --> E{"Replicas ≥<br/>max?"}
    E -->|Yes| F["Out of GPU capacity<br/>Add GPU nodes"]
    E -->|No| G["KEDA should scale<br/>Check KEDA logs"]
    
    B -->|No| H["Search or LLM slow"]
    H --> I["Check API logs:<br/>kubectl logs api-1"]
    I --> J["Profile: search time<br/>vs LLM time"]
    
    style F fill:#ff9800
    style G fill:#ff9800
```

### "Ingest Is Slow"

```mermaid
graph TD
    A["Pages/min < target"] --> B{"Extract queue<br/>> 100?"}
    B -->|Yes| C["Too many pages"]
    C --> D["Check worker-vlm<br/>latency p95"]
    D --> E{"Latency ><br/>SLO?"}
    E -->|Yes| F["worker-vlm waiting on GPU"]
    F --> G["KEDA scales GPU<br/>or GPU full"]
    
    E -->|No| H["Add more worker-vlm pods"]
    H --> I["Or increase queue<br/>KEDA queueLength threshold"]
    
    B -->|No| J["Check worker pod logs<br/>for errors"]
    
    style G fill:#ff9800
    style I fill:#ff9800
```

### "KEDA Not Scaling"

```bash
# 1. Check KEDA operator is running
kubectl get pods -n keda

# 2. Check ScaledObjects exist
kubectl get scaledobjects
# Should show: model-vlm-latency, model-embed-latency, worker-vlm, etc.

# 3. Check KEDA logs for errors
kubectl logs -n keda deployment/keda-operator | grep -i error

# 4. Manually check KEDA metric source
# For PostgreSQL triggers:
psql -c "SELECT COUNT(*) FROM model_latencies WHERE ts > now() - interval '2 minutes'"

# For RabbitMQ triggers:
rabbitmqctl list_queues name messages

# 5. Check HPA status
kubectl describe hpa model-vlm-latency
# Look for: conditions, current/desired replicas, events
```

---

## Key Alerts

```yaml
# Prometheus alert rules to set up

- alert: VLMLatencyHighSLO
  expr: |
    avg(avg_over_time(model_latency_p95_ms{stage="extract"}[2m])) > 5000
  for: 2m
  annotations:
    summary: "Extract latency exceeds 5s SLO"

- alert: EmbedLatencyHighSLO
  expr: |
    avg(avg_over_time(model_latency_p95_ms{stage="embed"}[2m])) > 3000
  for: 2m
  annotations:
    summary: "Embed latency exceeds 3s SLO"

- alert: QueryLatencyHighSLO
  expr: |
    query_latency_p95_seconds > 15
  for: 5m
  annotations:
    summary: "Query p95 latency exceeds 15s"

- alert: WorkerPodsCrashing
  expr: |
    rate(kube_pod_container_status_restarts_total{pod=~"worker-.*"}[1h]) > 0.01
  annotations:
    summary: "Worker pods restarting frequently"

- alert: PostgresLag
  expr: |
    pg_replication_lag_seconds > 10
  annotations:
    summary: "Postgres replication lag > 10s"
```

---

## Scaling Guidelines

```
Ingest Queue Depth | Action
─────────────────────────────
0-10 tokens        | 1 worker pod (scale to 0 after 2m idle)
10-50              | 2-4 pods
50-200             | 4-8 pods
200-500            | 8-16 pods
500+               | 16+ pods (check GPU isn't bottleneck)

Model Latency p95  | Action
─────────────────────────────
< 2000ms           | Scale down GPU (after 5m low)
2000-5000ms        | Optimal zone
5000-7000ms        | Scale up GPU
7000ms+            | Check GPU memory, out of capacity
```

---

## Cost Optimization

```
High CPU, low GPU = Add more workers (cheap)
High latency, high GPU = Add GPU capacity (expensive)
Check:
  1. kubectl top nodes (GPU node utilization)
  2. NVIDIA SMI (per-GPU memory)
  3. model_latencies table (actual latency)
```

---

## Deployment Checklist

- [ ] Postgres initialized (auto on pod start)
- [ ] RabbitMQ queues declared (auto via broker.py)
- [ ] KEDA metrics (check PostgreSQL connectivity)
- [ ] Worker liveness probes passing (curl :9100/health)
- [ ] Model servers returning 200 on /health
- [ ] First ingest: 0 errors
- [ ] Query latency p95 < 15s with ingest running
- [ ] KEDA scaling pods up/down on signal changes

---

**Next:** See `docs/ARCHITECTURE_DIAGRAM.md` for detailed flows.
