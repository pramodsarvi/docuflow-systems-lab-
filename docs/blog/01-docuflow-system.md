# DocuFlow: What It Is & How It Works

**Document ingestion + RAG on Kubernetes. Read scanned documents with vision, make them searchable, answer questions.**

---

## System Architecture

```mermaid
graph TB
    subgraph "API Layer"
        INGRESS["Ingress<br/>example.com"]
        API1["api-1"]
        API2["api-2"]
        SVC_API["Service api<br/>:8791"]
    end
    
    subgraph "Ingest Pipeline (Parallel Workers)"
        WIO["worker-io<br/>Fetch pages"]
        WVLM["worker-vlm<br/>Read pages<br/>(vision model)"]
        WEMBED["worker-embed<br/>Embed chunks"]
    end
    
    subgraph "LLM Gateway Stack (Intelligent Routing)"
        LITE["LiteLLM Proxy<br/>:4000<br/>Keys, rate limits,<br/>cost tracking"]
        LLMD["llm-d Gateway<br/>:8000<br/>Pod routing,<br/>queue-aware"]
    end
    
    subgraph "GPU Servers (Auto-scaled)"
        MVLM["vLLM Pods<br/>Vision model<br/>(Qwen2.5-VL-3B)"]
        MEMBED["vLLM Pods<br/>Embed model<br/>(Qwen3-Embedding)"]
        MAGENT["vLLM Pods<br/>Agent model<br/>(Qwen3-4B)"]
    end
    
    subgraph "Storage"
        PG["Postgres + pgvector<br/>Jobs, chunks, vectors,<br/>latencies"]
        RMQ["RabbitMQ<br/>Work tokens<br/>4 queues: io, vlm,<br/>embed, eval"]
    end
    
    INGRESS -->|routes| SVC_API
    SVC_API --> API1 & API2
    
    API1 & API2 -->|1. Create jobs| PG
    API1 & API2 -->|2. Publish tokens| RMQ
    
    RMQ -->|io tokens| WIO
    RMQ -->|vlm tokens| WVLM
    RMQ -->|embed tokens| WEMBED
    
    WIO -->|store pages| PG
    WVLM -->|extract text| LITE
    LITE -->|forward| LLMD
    LLMD -->|route to pod| MVLM
    MVLM -->|record latency| PG
    
    WEMBED -->|embed chunks| LITE
    LITE -->|forward| LLMD
    LLMD -->|route to pod| MEMBED
    MEMBED -->|record latency| PG
    
    API1 & API2 -->|query with agent| LITE
    LITE -->|forward| LLMD
    LLMD -->|route to pod| MAGENT
    MAGENT -->|record latency| PG
    
    style LITE fill:#ff9800
    style LLMD fill:#4caf50
    style MVLM fill:#2196f3
    style MEMBED fill:#2196f3
    style MAGENT fill:#2196f3
```

---

## Three Workloads, One GPU

**Problem:** Reading 10,000 scanned documents while serving live queries on a single GPU.

**Solution:** Three independent auto-scaling strategies.

```mermaid
graph LR
    subgraph "Ingest Pipeline"
        Q1["RabbitMQ Queue:<br/>io, vlm, embed<br/>tokens"]
        W1["KEDA scales on<br/>queue depth"]
        subgraph Workers
            W1A["worker-io<br/>worker-vlm<br/>worker-embed"]
        end
        W1 -.-> W1A
    end
    
    subgraph "GPU Servers"
        Q2["Model Latency<br/>p95 from Postgres<br/>last 2 minutes"]
        W2["KEDA scales on<br/>latency p95 > 5s"]
        subgraph Models
            W2A["vLLM pod 1<br/>vLLM pod 2<br/>vLLM pod N"]
        end
        W2 -.-> W2A
    end
    
    subgraph "Evaluation"
        Q3["Fixed pool<br/>2 workers"]
        W3A["worker-ragas-1<br/>worker-ragas-2"]
        Q3 -.-> W3A
    end
    
    style Q1 fill:#ff5722
    style Q2 fill:#ff9800
    style Q3 fill:#9c27b0
```

---

## Data Flow: One Request

```mermaid
sequenceDiagram
    participant API
    participant PG as Postgres
    participant RMQ as RabbitMQ
    participant W as worker-vlm
    participant LITE as LiteLLM
    participant LLMD as llm-d
    participant MVLM as vLLM pod

    API->>PG: Create job (PDF, page range)
    API->>RMQ: Publish token to "vlm" queue
    
    W->>RMQ: Claim token
    W->>PG: Fetch job details
    W->>LITE: POST /chat/completions (with page image)
    
    LITE->>LITE: ✓ Validate key<br/>✓ Check rate limit<br/>✓ Log request
    LITE->>LLMD: Forward (transparent)
    
    LLMD->>LLMD: Poll /metrics on pods<br/>Score: queue + cache<br/>→ pick least busy
    LLMD->>MVLM: Route to pod
    
    MVLM->>MVLM: Vision inference<br/>Extract text
    MVLM-->>LLMD: Response
    LLMD-->>LITE: Response
    LITE->>PG: Record latency, cost
    LITE-->>W: Response
    
    W->>PG: Store extracted text
    W->>RMQ: Return token
```

---

## Scaling in Action

```mermaid
graph TB
    subgraph "Ingest starts: 1000 pages queued"
        T0["Queue depth: 1000<br/>KEDA sees: 'scale workers'"]
        T0A["Launches:<br/>8 worker-vlm pods<br/>4 worker-embed pods<br/>2 worker-io pods"]
    end
    
    subgraph "Pages being read"
        T1["Model latency p95: 2.5s<br/>KEDA sees: 'GPU has room'"]
        T1A["Keeps vLLM at:<br/>2 replicas<br/>(2 models per GPU)"]
    end
    
    subgraph "Ingest slows, queries spike"
        T2["Queue depth: 50<br/>Query latency p95: 8s<br/>KEDA sees: 'Scale GPU'"]
        T2A["Scales GPU to:<br/>4 vLLM replicas<br/>Ingest pauses (yield)<br/>Queries get GPU time"]
    end
    
    subgraph "Idle"
        T3["Queue: 0<br/>No queries"]
        T3A["After 2 min cooldown:<br/>Scale to zero<br/>Save all GPU memory"]
    end
    
    T0 -->|5 min| T1
    T1 -->|10 min| T2
    T2 -->|15 min| T3
    
    style T0A fill:#4caf50
    style T1A fill:#2196f3
    style T2A fill:#ff9800
    style T3A fill:#9c27b0
```

---

## Request Routing: LiteLLM + llm-d

Workers send all requests through **two intelligent layers:**

```mermaid
graph LR
    W["worker"]
    LITE["LiteLLM<br/>:4000"]
    LLMD["llm-d<br/>:8000"]
    P1["Pod 1<br/>queue=2"]
    P2["Pod 2<br/>queue=8"]
    
    W -->|POST<br/>+ API key| LITE
    LITE -->|✓ Key valid<br/>✓ Rate OK<br/>✓ Log| LLMD
    LLMD -->|Metrics poll<br/>Score: 2 vs 8| P1
    LLMD -->|"Route to<br/>least busy"| P1
    P1 -->|Response| LLMD
    LLMD -->|Response| LITE
    LITE -->|Cost + latency<br/>recorded| W
    
    style LITE fill:#ff9800
    style LLMD fill:#4caf50
    style P1 fill:#c8e6c9
    style P2 fill:#ffebee
```

**Layer 1 (LiteLLM):** API keys, rate limits, cost tracking, logging  
**Layer 2 (llm-d):** Pod discovery, queue-aware routing, health checks  

---

## The Three Pipelines

### 1. Ingest: Read & Index Documents
```
PDF → io worker (fetch pages)
      ↓
   vlm worker (vision: read text)
      ↓
   embed worker (embeddings)
      ↓
   Postgres (chunks + vectors)
```
**Scales on:** RabbitMQ queue depth (0 to 16+ pods)  
**Speed:** 100-200 pages/min on one GPU

### 2. Query: Agent + Search
```
User question → api (search) → vLLM agent
                     ↓
            pgvector (retrieve chunks)
                     ↓
                Agent loop
                     ↓
            Answer + sources
```
**Scales on:** Query latency p95 (1 to 8 GPU pods)  
**Latency:** <15s p95 during ingest

### 3. Eval: Quality Metrics
```
Ingest results → worker-ragas (RAGAS eval)
                      ↓
              Postgres (eval_results)
```
**Fixed:** 2 workers (not auto-scaled)  
**Metrics:** Faithfulness, relevancy, context precision

---

## Tech Stack

| Layer | Tech | Why |
|-------|------|-----|
| **API** | Python FastAPI | Simple, Async I/O |
| **Ingest Queue** | RabbitMQ | Work tokens (KEDA scales on it) |
| **Job State** | Postgres + pgvector | Source of truth |
| **Vision & LLM** | vLLM (Qwen models) | Fast inference, batching |
| **Routing** | LiteLLM + llm-d | App policy + K8s intelligence |
| **Scaling** | KEDA | Reacts to queue depth & latency |
| **Container Orch** | Kubernetes | Pod scaling, networking, monitoring |

---

## Performance: What to Expect

| Metric | Value | Notes |
|--------|-------|-------|
| Ingest speed | 100-200 pages/min | Single GPU, parallel workers |
| Extract latency p95 | 2-5 sec | Per page (varies by page size) |
| Query latency p95 | <15 sec | During ingest (scales GPU if higher) |
| Cost per page | $0.01-0.05 | Depends on page size + model |
| Eval latency | 30-60 sec | Per query (RAGAS) |
| Max concurrent docs | 10,000+ | Limited by Postgres, not GPU |

---

## Next: Deployment & Operations

See [Part 2: Operate & Scale](02-deploy-monitor-gateway.md) for:
- Kubernetes manifests (all-in-one YAML)
- SQL monitoring queries
- Troubleshooting & scaling guidelines
- Cost optimization

---

**Files:**
- Architecture: `k8s/base/llm-stack-combined.yaml` (complete stack)
- Deployment: `k8s/APPLY_LITELLM_STACK.md` (step-by-step)
- Setup guide: `docs/LITELLM_LLMD_SETUP.md` (detailed config)
