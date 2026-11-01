# DocuFlow Complete Architecture Diagram

## System Overview

```mermaid
graph TB
    subgraph "Client Layer"
        USER["👤 Users"]
        API_CLIENT["API Clients"]
    end

    subgraph "API & Control"
        INGRESS["Ingress<br/>TLS/Auth"]
        API["api<br/>FastAPI<br/>Port 8000"]
        DASHBOARD["📊 Dashboard"]
    end

    subgraph "Job Queue"
        PG_JOBS["📋 Postgres<br/>Jobs Table<br/>Source of Truth"]
        RMQ["🐰 RabbitMQ<br/>Work Tokens"]
        RMQ_IO["docuflow.io<br/>fetch tokens"]
        RMQ_VLM["docuflow.vlm<br/>extract tokens"]
        RMQ_EMBED["docuflow.embed<br/>embed tokens"]
        RMQ_RAGAS["docuflow.ragas<br/>eval tokens"]
    end

    subgraph "Ingestion Workers (CPU)"
        WIO["worker-io<br/>0-3 pods<br/>KEDA: queue depth"]
        WVLM["worker-vlm<br/>0-24 pods<br/>KEDA: queue depth"]
        WEMBED["worker-embed<br/>0-3 pods<br/>KEDA: queue depth"]
    end

    subgraph "Model Servers (GPU)"
        MVLM["model-vlm<br/>vLLM StatefulSet<br/>1-8 replicas<br/>🔴 KEDA: latency p95"]
        MEMBED["model-embed<br/>vLLM StatefulSet<br/>1-4 replicas<br/>🔴 KEDA: latency p95"]
    end

    subgraph "Evaluation (Fixed)"
        WRAGAS["worker-ragas<br/>2 fixed pods<br/>No scaling"]
    end

    subgraph "Data Storage"
        PG["🗄️ Postgres<br/>- documents<br/>- chunks<br/>- vectors<br/>- images<br/>- queries<br/>- model_latencies<br/>- eval_results"]
        PGVECTOR["pgvector<br/>HNSW Index"]
    end

    subgraph "Monitoring & Scaling"
        PROM["Prometheus<br/>Scrapes metrics"]
        GRAFANA["Grafana<br/>Dashboards"]
        KEDA["KEDA<br/>Autoscaler<br/>Watches:<br/>- Queue depth<br/>- Latency metrics<br/>- Running jobs"]
    end

    USER -->|HTTP| INGRESS
    API_CLIENT -->|HTTP| INGRESS
    INGRESS -->|/ingest, /query, /eval| API
    API -->|reads/writes| PG_JOBS
    API -->|publishes tokens| RMQ
    DASHBOARD -->|queries| API

    API -->|Creates fetch jobs| PG_JOBS
    PG_JOBS -->|tokens| RMQ_IO
    RMQ_IO -->|KEDA scales| WIO
    WIO -->|claims job| PG_JOBS
    WIO -->|stores images| PG
    WIO -->|enqueues extract| PG_JOBS
    PG_JOBS -->|tokens| RMQ_VLM

    RMQ_VLM -->|KEDA scales| WVLM
    WVLM -->|claims job| PG_JOBS
    WVLM -->|HTTP REST| MVLM
    MVLM -->|vLLM API| MVLM
    WVLM -->|records latency| PG
    WVLM -->|enqueues embed| PG_JOBS
    PG_JOBS -->|tokens| RMQ_EMBED

    RMQ_EMBED -->|KEDA scales| WEMBED
    WEMBED -->|claims job| PG_JOBS
    WEMBED -->|HTTP REST| MEMBED
    MEMBED -->|vLLM API| MEMBED
    WEMBED -->|records latency| PG
    WEMBED -->|marks indexed| PG

    PG -->|model_latencies| KEDA
    KEDA -->|scales| MVLM
    KEDA -->|scales| MEMBED
    KEDA -->|scales workers| WIO
    KEDA -->|scales workers| WVLM
    KEDA -->|scales workers| WEMBED

    PG -->|chunks + vectors| PGVECTOR
    API -->|hybrid search| PGVECTOR
    API -->|search + agent loop| API

    API -->|Creates eval jobs| PG_JOBS
    PG_JOBS -->|tokens| RMQ_RAGAS
    RMQ_RAGAS -->|dequeues| WRAGAS
    WRAGAS -->|claims job| PG_JOBS
    WRAGAS -->|HTTP REST| MVLM
    WRAGAS -->|HTTP REST| MEMBED
    WRAGAS -->|stores results| PG

    PROM -->|scrapes| WIO
    PROM -->|scrapes| WVLM
    PROM -->|scrapes| WEMBED
    PROM -->|scrapes| WRAGAS
    PROM -->|scrapes| API
    PROM -->|scrapes| MVLM
    PROM -->|scrapes| MEMBED
    GRAFANA -->|reads| PROM

    style API fill:#4a90e2
    style MVLM fill:#f5a623
    style MEMBED fill:#f5a623
    style PG fill:#7ed321
    style RMQ fill:#d0021b
    style KEDA fill:#ff6b6b
    style WIO fill:#417505
    style WVLM fill:#417505
    style WEMBED fill:#417505
    style WRAGAS fill:#417505
```

## Data Flow Sequences

### 1. Ingest Flow

```mermaid
sequenceDiagram
    participant User
    participant API
    participant Postgres as Postgres<br/>Jobs
    participant RabbitMQ
    participant WorkerIO as worker-io
    participant WorkerVLM as worker-vlm
    participant VLM as model-vlm<br/>vLLM
    participant WorkerEmbed as worker-embed
    participant Embed as model-embed<br/>vLLM

    User->>API: POST /ingest<br/>(dataset, limit)
    API->>Postgres: INSERT fetch jobs<br/>(20 pages each)
    API->>RabbitMQ: publish N tokens<br/>to docuflow.io
    
    RabbitMQ-->>WorkerIO: tokens available
    WorkerIO->>Postgres: claim fetch job<br/>(FOR UPDATE SKIP LOCKED)
    WorkerIO->>Postgres: UPDATE status='running'
    
    WorkerIO->>WorkerIO: fetch_handler()<br/>download images
    WorkerIO->>Postgres: INSERT documents<br/>store image bytea
    WorkerIO->>Postgres: INSERT extract jobs
    WorkerIO->>RabbitMQ: publish 1 token<br/>per page to docuflow.vlm
    WorkerIO->>Postgres: UPDATE status='done'

    RabbitMQ-->>WorkerVLM: tokens available
    WorkerVLM->>Postgres: claim extract job
    WorkerVLM->>Postgres: UPDATE status='running'
    
    WorkerVLM->>VLM: HTTP POST<br/>/v1/chat/completions
    VLM->>VLM: vision model<br/>reads page
    VLM-->>WorkerVLM: regions (text, bbox, kind)
    
    WorkerVLM->>Postgres: INSERT model_latencies<br/>(latency_ms)
    WorkerVLM->>Postgres: INSERT chunks<br/>(text per region)
    WorkerVLM->>Postgres: INSERT embed jobs
    WorkerVLM->>RabbitMQ: publish token<br/>to docuflow.embed
    WorkerVLM->>Postgres: UPDATE status='done'

    RabbitMQ-->>WorkerEmbed: tokens available
    WorkerEmbed->>Postgres: claim embed job
    WorkerEmbed->>Postgres: UPDATE status='running'
    
    WorkerEmbed->>Embed: HTTP POST<br/>/v1/embeddings
    Embed->>Embed: embedding model<br/>vectorizes chunks
    Embed-->>WorkerEmbed: vector embeddings
    
    WorkerEmbed->>Postgres: INSERT model_latencies
    WorkerEmbed->>Postgres: UPDATE chunks SET embedding=vector
    WorkerEmbed->>Postgres: UPDATE documents SET indexed=true
    WorkerEmbed->>Postgres: UPDATE status='done'

    Note over Postgres: Page is now searchable!
```

### 2. Query Flow

```mermaid
sequenceDiagram
    participant User
    participant API
    participant Search as Search<br/>pgvector
    participant LLM as model-llm<br/>vLLM
    participant Postgres
    participant Cache as Response Cache

    User->>API: POST /query<br/>(question)
    API->>Postgres: INSERT inflight<br/>(beat=now)
    
    API->>Search: hybrid search<br/>(vector + full-text)
    Search-->>API: top-k chunks<br/>(with doc_id, excerpt)
    
    API->>Postgres: INSERT INTO queries<br/>(question, trace=pending)
    
    loop Agent Loop (max 4 steps)
        API->>LLM: HTTP POST /v1/chat/completions<br/>context + question
        LLM-->>API: thought + action<br/>(search/read_page/answer)
        
        alt action == "search"
            API->>Search: refine search<br/>(follow-up query)
        else action == "read_page"
            API->>Search: full page text
        else action == "answer"
            break
        end
    end
    
    API->>LLM: Generate final answer<br/>with citations
    LLM-->>API: answer + citations
    API->>Postgres: UPDATE queries<br/>(answer, citations, trace)
    API->>Postgres: DELETE inflight
    
    API-->>User: answer + citations<br/>+ trace

    Note over API,LLM: Total latency SLO: p95 ≤ 15s<br/>during ingest
```

### 3. Evaluation (RAGAS) Flow

```mermaid
sequenceDiagram
    participant User
    participant API
    participant Postgres
    participant RabbitMQ
    participant WorkerRAGAS as worker-ragas
    participant VLM as model-vlm<br/>vLLM
    participant Embed as model-embed<br/>vLLM

    User->>API: POST /eval?dataset=acme
    API->>Postgres: SELECT recent queries<br/>WHERE NOT evaluated
    API->>Postgres: INSERT eval jobs
    API->>RabbitMQ: publish N tokens<br/>to docuflow.ragas
    
    RabbitMQ-->>WorkerRAGAS: tokens available<br/>(2 fixed pods)
    
    loop For each eval job
        WorkerRAGAS->>Postgres: claim eval job
        WorkerRAGAS->>Postgres: SELECT query result<br/>answer + contexts
        
        WorkerRAGAS->>VLM: Faithfulness judge<br/>answer vs contexts
        VLM-->>WorkerRAGAS: score (0-1)
        
        WorkerRAGAS->>Embed: Context precision<br/>relevant contexts ranked?
        Embed-->>WorkerRAGAS: score (0-1)
        
        WorkerRAGAS->>VLM: Context recall<br/>contexts cover question?
        VLM-->>WorkerRAGAS: score (0-1)
        
        WorkerRAGAS->>Postgres: INSERT eval_results<br/>(metrics JSON)
        WorkerRAGAS->>Postgres: UPDATE status='done'
        
        Note over WorkerRAGAS: 5-20 min per eval<br/>background work
    end
    
    API-->>User: eval_jobs_queued: N
```

## Component Details

### API Pod
- **Role:** HTTP endpoint, query agent, job creation
- **Scaling:** KEDA on CPU + queries in flight (min 1, max 8)
- **Responsibilities:**
  - `POST /ingest` → creates fetch jobs, publishes tokens
  - `POST /query` → runs agent loop with search + LLM
  - `POST /eval` → creates eval jobs, publishes tokens
  - Hybrid search (vector + full-text)

### worker-io
- **Role:** Fetch pages from dataset source
- **Scaling:** KEDA on `docuflow.io` queue depth (min 0, max 3)
- **Resources:** 200m CPU, 512Mi memory
- **Job:** fetch → downloads images, stores in Postgres, enqueues extract
- **Throughput:** ~20 pages per job, 1-2 jobs per pod

### worker-vlm
- **Role:** Page reading with vision model
- **Scaling:** KEDA on `docuflow.vlm` queue depth (min 0, max 24)
- **Resources:** 100m CPU, 256Mi memory
- **Job:** extract → calls model-vlm REST API, records latency, enqueues embed
- **Latency:** Recorded → drives model-vlm GPU scaling
- **Throughput:** ~1 page per 40-80s (depends on page complexity)

### worker-embed
- **Role:** Vectorize chunks
- **Scaling:** KEDA on `docuflow.embed` queue depth (min 0, max 3)
- **Resources:** 100m CPU, 256Mi memory
- **Job:** embed → calls model-embed REST API, records latency, marks page indexed
- **Latency:** Recorded → drives model-embed GPU scaling
- **Throughput:** ~10-100 chunks per second (batched)

### worker-ragas
- **Role:** Background evaluation
- **Scaling:** **FIXED 2 pods** (never scales)
- **Resources:** 200m CPU, 512Mi memory
- **Job:** eval → runs RAGAS pipeline, stores metrics
- **Duration:** 5-20 minutes per query evaluation
- **Note:** Never blocks user queries (separate lane)

### model-vlm (vLLM)
- **Model:** Qwen2.5-VL-3B (vision model)
- **Port:** 8000
- **API:** OpenAI-compatible `/v1/chat/completions`
- **Scaling:** KEDA on latency p95 (min 1, max 8)
  - **Threshold:** scale up if p95 > 5 seconds
  - **Activation:** scale down if p95 < 2 seconds
- **GPU Memory:** ~8GB per replica on L40S
- **Concurrency:** 16 requests per replica (batch slots)

### model-embed (vLLM)
- **Model:** Qwen3-Embedding-0.6B
- **Port:** 8001
- **API:** OpenAI-compatible `/v1/embeddings`
- **Scaling:** KEDA on latency p95 (min 1, max 4)
  - **Threshold:** scale up if p95 > 3 seconds
  - **Activation:** scale down if p95 < 1.5 seconds
- **GPU Memory:** ~2GB per replica on L40S
- **Concurrency:** 32+ requests per replica (lightweight)

### Postgres
- **Role:** Source of truth for all state
- **Tables:**
  - `jobs` → queue state (status, lease, retries)
  - `documents` → pages (id, dataset, image, status)
  - `chunks` → text regions with vectors
  - `queries` → question/answer logs with traces
  - `model_latencies` → latency metrics for KEDA
  - `eval_results` → RAGAS evaluation scores
- **Scaling:** Single node (HA possible with replicas)
- **Pool:** Min 1, max 8 connections per worker

### RabbitMQ
- **Role:** Work token queue (NOT job store)
- **Queues:**
  - `docuflow.io` → fetch tokens
  - `docuflow.vlm` → extract tokens
  - `docuflow.embed` → embed tokens
  - `docuflow.ragas` → eval tokens
- **Delay Queues:** `*.delay` for retry backoff
- **Persistence:** Durable queues (survive restart)
- **Reconciliation:** Workers rebuild tokens from Postgres if lost

## Latency Metrics & KEDA Decisions

```mermaid
graph LR
    subgraph "Latency Recording"
        L1["extract handler<br/>calls model-vlm"]
        L2["records latency<br/>model_latencies table"]
        L3["p95 = PERCENTILE_CONT<br/>over 2-min window"]
    end

    subgraph "KEDA Decision"
        K1["KEDA queries Postgres<br/>every 15 seconds"]
        K2["Reads p95_latency<br/>from model_latencies"]
        K3["Compare vs threshold<br/>5000ms"]
        K4["Scale decision:<br/>+2 pods or -1 pod"]
    end

    subgraph "GPU Scaling"
        S1["model-vlm replicas<br/>increase from 2→4"]
        S2["vLLM batch slots<br/>increase available"]
        S3["latency drops<br/>requests finish faster"]
    end

    L1 -->|measures| L2
    L2 -->|aggregates| L3
    L3 -->|provides signal to| K1
    K1 -->|fetches| K2
    K2 -->|compares| K3
    K3 -->|triggers| K4
    K4 -->|scales| S1
    S1 -->|increases| S2
    S2 -->|causes| S3
    S3 -->|feeds back to| L3

    style L1 fill:#417505
    style L2 fill:#7ed321
    style L3 fill:#7ed321
    style K1 fill:#ff6b6b
    style K2 fill:#7ed321
    style K3 fill:#ff6b6b
    style K4 fill:#ff6b6b
    style S1 fill:#f5a623
    style S2 fill:#f5a623
    style S3 fill:#f5a623
```

## Retry & Backoff Logic

```mermaid
graph TD
    A["Job execution<br/>in worker"] -->|Success| B["UPDATE status='done'"]
    A -->|Transient Error<br/>model unavailable| C["defer_job"]
    A -->|Permanent Error<br/>bad data| D["fail_job"]

    C -->|Set run_after| E["run_after = now() + backoff"]
    C -->|Refund attempt| F["attempts = attempts - 1"]
    C -->|Publish to delay queue| G["docuflow.&lt;lane&gt;.delay<br/>with TTL"]
    
    D -->|Increment attempt| H["attempts = attempts + 1"]
    D -->|Check max| I{attempts >=<br/>max_attempts?}
    I -->|No| J["set run_after +<br/>exponential backoff<br/>min(300, 5*2^attempt)"]
    I -->|Yes| K["UPDATE status='dead'"]
    
    J -->|Publish to delay queue| G
    G -->|After TTL expires| L["Dead-letter back to<br/>docuflow.&lt;lane&gt;"]
    L -->|Next worker| A
    
    K -->|Operator must| M["POST /workloads/retry-dead"]
    M -->|Manually requeue| A

    B -->|Page indexed| N["✅ Searchable!"]
    
    style C fill:#ff9800
    style D fill:#f44336
    style K fill:#f44336
    style N fill:#4caf50
    style G fill:#ffeb3b
```
