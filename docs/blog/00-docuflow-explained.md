# DocuFlow: Document Ingestion & RAG at Scale

**What:** Vision model reads scanned documents → searchable knowledge base → agent answers questions with citations.  
**Why:** Ingest and queries run in parallel on a single GPU.  
**How:** Three pipelines, three scaling strategies, all on Kubernetes.

---

## System Architecture

```mermaid
graph TB
    subgraph "Client Tier"
        USER["Users"]
    end

    subgraph "API & Ingress"
        INGRESS["Ingress<br/>TLS/Auth"]
        API["api<br/>Stateless<br/>FastAPI Pods<br/>KEDA: CPU + In-Flight"]
    end

    subgraph "Job Queue"
        PG_JOBS["Postgres<br/>Jobs Table<br/>Source of Truth"]
        RMQ["RabbitMQ<br/>Work Tokens<br/>4 Queues"]
    end

    subgraph "Ingest Pipeline - CPU Workers"
        WIO["worker-io<br/>0-3 pods<br/>KEDA: io queue"]
        WVLM["worker-vlm<br/>0-24 pods<br/>KEDA: vlm queue"]
        WEMBED["worker-embed<br/>0-3 pods<br/>KEDA: embed queue"]
    end

    subgraph "Model Servers - GPU Pods"
        MVLM["model-vlm<br/>vLLM<br/>1-8 replicas<br/>🔴 KEDA: p95 latency"]
        MEMBED["model-embed<br/>vLLM<br/>1-4 replicas<br/>🔴 KEDA: p95 latency"]
    end

    subgraph "Evaluation - Fixed"
        WRAGAS["worker-ragas<br/>2 fixed pods"]
    end

    subgraph "Storage"
        PG["Postgres<br/>docs, chunks, vectors<br/>model_latencies"]
        PGVECTOR["pgvector<br/>Hybrid Search"]
    end

    USER -->|HTTP| INGRESS
    INGRESS --> API
    
    API -->|POST /ingest| PG_JOBS
    API -->|tokens| RMQ
    API -->|search| PGVECTOR
    
    RMQ -->|docuflow.io| WIO
    RMQ -->|docuflow.vlm| WVLM
    RMQ -->|docuflow.embed| WEMBED
    RMQ -->|docuflow.ragas| WRAGAS
    
    WIO -->|claim job| PG_JOBS
    WVLM -->|claim job| PG_JOBS
    WEMBED -->|claim job| PG_JOBS
    WRAGAS -->|claim job| PG_JOBS
    
    WVLM -->|HTTP /v1/chat| MVLM
    WEMBED -->|HTTP /v1/embed| MEMBED
    WRAGAS -->|HTTP| MVLM
    WRAGAS -->|HTTP| MEMBED
    
    WVLM -->|record latency| PG
    WEMBED -->|record latency| PG
    
    WIO -->|store| PG
    WVLM -->|store| PG
    WEMBED -->|store| PG
    WRAGAS -->|store| PG
    
    PG --> PGVECTOR

    style API fill:#4a90e2
    style MVLM fill:#f5a623
    style MEMBED fill:#f5a623
    style PG fill:#7ed321
    style RMQ fill:#d0021b
    style WIO fill:#417505
    style WVLM fill:#417505
    style WEMBED fill:#417505
    style WRAGAS fill:#417505
```

---

## Three Pipelines

### 1. Ingest Pipeline
```mermaid
graph LR
    A["POST /ingest<br/>100 pages"] -->|batch| B["fetch jobs<br/>RabbitMQ.io"]
    B -->|worker-io<br/>0-3 pods| C["Download<br/>Store images"]
    C -->|per page| D["extract jobs<br/>RabbitMQ.vlm"]
    D -->|worker-vlm<br/>0-24 pods| E["model-vlm<br/>vLLM pod"]
    E -->|REST API| F["Regions<br/>bbox + text"]
    F -->|chunk| G["embed jobs<br/>RabbitMQ.embed"]
    G -->|worker-embed<br/>0-3 pods| H["model-embed<br/>vLLM pod"]
    H -->|REST API| I["Vectors"]
    I -->|indexed| J["✓ Searchable"]
    
    style B fill:#d0021b
    style D fill:#d0021b
    style G fill:#d0021b
    style E fill:#f5a623
    style H fill:#f5a623
    style J fill:#4caf50
```

**Scaling:** KEDA watches RabbitMQ queue depth  
**Metric:** Pages queued = worker pods needed  

---

### 2. Query Pipeline
```mermaid
graph LR
    A["POST /query<br/>question"] -->|agent| B["search<br/>pgvector"]
    B -->|top-k| C["LLM thinks<br/>next action?"]
    C -->|search again| B
    C -->|read page| D["fetch full text"]
    C -->|unclear?| E["vision model<br/>re-read image"]
    C -->|answer| F["Generate<br/>+ citations"]
    F -->|response| G["Answer<br/>+ trace"]
    
    style A fill:#4a90e2
    style B fill:#7ed321
    style F fill:#4a90e2
    style G fill:#4caf50
```

**Scaling:** KEDA watches CPU + queries in-flight  
**Metric:** User load = API pods needed  

---

### 3. Evaluation Pipeline
```mermaid
graph LR
    A["POST /eval"] -->|queue| B["eval jobs<br/>RabbitMQ.ragas"]
    B -->|worker-ragas<br/>2 FIXED pods| C["RAGAS metrics"]
    C -->|faithfulness<br/>relevance| D["LLM judge"]
    D -->|scores| E["Store results"]
    
    style B fill:#d0021b
    style C fill:#ff9800
    style E fill:#4caf50
```

**Scaling:** Fixed 2 pods (no autoscaling)  
**Why:** Background work, no SLA  

---

## Three Scaling Strategies

```mermaid
graph TB
    subgraph "Throughput-Driven"
        A["Ingest Workers<br/>io, vlm, embed"]
        B["Metric: Queue Depth<br/>docuflow.io, .vlm, .embed"]
        C["Scaling: 0-24 pods<br/>More work = more workers"]
        A --> B --> C
    end

    subgraph "Latency-Driven"
        D["GPU Servers<br/>model-vlm, model-embed"]
        E["Metric: p95 Latency<br/>From Postgres metrics"]
        F["Scaling: 1-8 (vlm), 1-4 (embed)<br/>Slow requests = more GPUs"]
        D --> E --> F
    end

    subgraph "Fixed Capacity"
        G["Evaluation<br/>worker-ragas"]
        H["Metric: None<br/>Always running"]
        I["Scaling: 2 pods fixed<br/>Background work"]
        G --> H --> I
    end

    style A fill:#417505
    style D fill:#f5a623
    style G fill:#ff9800
```

---

## Latency-Driven GPU Scaling

```mermaid
sequenceDiagram
    participant Worker as worker-vlm
    participant GPU as model-vlm<br/>vLLM
    participant PG as Postgres<br/>model_latencies
    participant KEDA as KEDA<br/>HPA

    Worker->>GPU: POST /v1/chat/completions
    Note over Worker,GPU: t0 = now()
    GPU->>GPU: Batch inference
    GPU-->>Worker: regions (4.2 sec)
    
    Worker->>PG: INSERT latency_ms=4200
    
    Note over PG: Every 2 min window

    KEDA->>PG: PERCENTILE_CONT(0.95) latency
    PG-->>KEDA: p95 = 4800ms
    
    alt p95 > 5000ms
        KEDA->>KEDA: Scale up: 2→4 replicas
        Note over KEDA: More batch slots<br/>Lower latency
    else p95 < 2000ms
        KEDA->>KEDA: Scale down: 4→2 replicas
    end
```

---

## Data Model

```
📦 Postgres
├─ documents (id, dataset, image, status)
├─ chunks (doc_id, text, embedding vector)
├─ jobs (id, stage, lane, status, lease, attempts)
├─ model_latencies (stage, endpoint, latency_ms, ts) ← NEW
├─ eval_results (job_id, metrics) ← NEW
└─ pgvector indexes (HNSW + GIN)

🐰 RabbitMQ
├─ docuflow.io (fetch tokens)
├─ docuflow.vlm (extract tokens)
├─ docuflow.embed (embed tokens)
├─ docuflow.ragas (eval tokens) ← NEW
└─ *.delay (retry backoff)
```

---

## Key Insights

| Aspect | Insight |
|--------|---------|
| **Postgres** | Source of truth (jobs, latency, state) |
| **RabbitMQ** | Work tokens (not jobs themselves) |
| **REST APIs** | vLLM called via HTTP, not queue-based |
| **Latency Metrics** | Recorded by workers, read by KEDA |
| **Three Queues** | Ingest stages pipeline (io → vlm → embed) |
| **Three Scaling** | Queue depth + Latency + Fixed = right metric for each |

---

## Deploy & Test

```bash
# Deploy
kubectl apply -f k8s/base/workers.yaml  # includes worker-ragas
kubectl apply -f k8s/base/keda.yaml     # includes latency scalers

# Test
curl -X POST http://api:8000/ingest \
  -d '{"dataset":"synthetic","limit":100}'

# Monitor
kubectl get hpa -w                              # watch scaling
kubectl logs -f deployment/worker-vlm           # job logs
psql -c "SELECT * FROM model_latencies LIMIT1" # latency check
```

---

**Next:** Read blog post 01 for detailed flows and decisions.
