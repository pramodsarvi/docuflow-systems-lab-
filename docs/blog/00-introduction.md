# DocuFlow: A Document Ingestion and Question-Answering System

**What if you could upload a stack of scanned documents and instantly ask questions about them—while the system is still reading the rest of the stack?**

That's DocuFlow.

---

## What Is DocuFlow?

DocuFlow is a **document ingestion and retrieval-augmented generation (RAG) system** that turns stacks of scanned documents (printed text, handwritten notes, forms, invoices) into a searchable knowledge base. You can ask questions about the documents in natural language, and the system answers with citations to the exact pages it used.

Here's the magic part: **ingestion and querying happen simultaneously.** A page becomes searchable within minutes of being uploaded, and users can start asking questions while the system is still processing the rest of the batch.

### Simple Example

```
1. Upload 1000 scanned invoices
2. While the system is reading invoices 500-1000:
   - User asks: "Which invoices mention 'urgent repair'?"
   - System searches invoices 1-499 (already indexed)
   - Returns results with page citations
3. As more invoices finish processing:
   - They become searchable
   - Future queries include them automatically
```

---

## The Problem It Solves

### Traditional Approach (Bad)
1. Upload documents
2. Wait for entire batch to process (hours/days)
3. Then start querying
4. Re-processing takes the entire queue down

### DocuFlow Approach (Good)
1. Upload documents → start processing immediately
2. Query as soon as first pages are indexed
3. Ingest and queries run in parallel
4. System handles load spikes gracefully

### Why This Matters

- **Real-time insights:** Don't wait for batch processing to finish
- **User experience:** "Upload and search" instead of "upload and wait"
- **Cost efficiency:** One GPU shared between ingest and queries
- **Resilience:** If ingest slows down, queries don't suffer

---

## Core Capabilities

### 1. **Page Reading** 
Vision model reads scanned documents and extracts:
- Text regions (printed and handwritten)
- Region boundaries (bbox)
- Region classification (header, label, value, body, note, signature, table)
- Handles mixed printed/handwritten content

### 2. **Hybrid Search**
Find documents by:
- **Vector similarity** (semantic search: "How do I fix a broken widget?")
- **Full-text search** (exact keywords: "Invoice INV-1005")
- **Identifier matching** (document codes: "PROC-2024-001")
- **RRF fusion** (combines all three, ranks results)

### 3. **Agentic Question Answering**
Multi-step reasoning agent that:
- Searches for relevant passages
- Reads full pages when needed
- Re-reads pages with the vision model for unclear text
- Synthesizes an answer with citations
- Returns trace of its reasoning steps

### 4. **Live Evaluation (RAGAS)**
Background evaluation that measures:
- **Faithfulness:** Is the answer supported by the contexts?
- **Relevance:** Does the answer address the question?
- **Context precision:** Are relevant passages ranked above irrelevant ones?
- **Context recall:** Do the contexts contain what's needed to answer?

---

## How It Works: The Three Pipelines

### Pipeline 1: Ingest (Fetch → Extract → Embed)

```
POST /ingest (100 pages)
  ↓
[fetch jobs] Download images from dataset
  ↓ (per page)
[extract jobs] Vision model reads page → regions
  ├─ Record latency (used for GPU autoscaling)
  ├─ Chunk regions into semantic blocks
  └─ Enqueue embed jobs
  ↓ (per chunk)
[embed jobs] Embedding model vectorizes text
  ├─ Record latency
  └─ Mark page indexed (now searchable)
```

**Key insight:** Each stage produces work for the next stage, so pages become searchable progressively (page 1 indexed while pages 50-100 are still being read).

### Pipeline 2: Query (Agent Loop)

```
POST /query (question)
  ↓
[search] Vector + full-text hybrid search
  ↓
[agent loop] Ask LLM what to do next:
  ├─ "search again with refined query" → loop back
  ├─ "show me the full page" → fetch page
  ├─ "re-read this page image" → vision model
  └─ "I have the answer" → proceed
  ↓
[generate] LLM synthesizes answer with citations
  ↓
Response: {answer, citations, trace, latency}
```

**Key insight:** The agent "thinks" about the question and decides what information to retrieve. This beats keyword search for complex questions.

### Pipeline 3: Evaluation (RAGAS)

```
POST /eval (dataset name)
  ↓
[find] Recent queries without evaluation
  ↓ (per query)
[eval job] Run RAGAS metrics
  ├─ Faithfulness judge: Is answer grounded?
  ├─ Relevance judge: Does answer fit question?
  ├─ Context precision: Ranking correct?
  └─ Context recall: Covered everything?
  ↓
Store scores in eval_results table
```

**Key insight:** Evaluation runs in the background (2 fixed pods) and never blocks ingest or queries.

---

## The Kubernetes Architecture

Everything runs on Kubernetes with **three different autoscaling strategies:**

### Workers Scale on Queue Depth
```
Ingest queue backlog grows
  ↓
KEDA watches RabbitMQ queue
  ↓
More work = more worker pods (0→24)
```

**Why:** Throughput optimization. More pages waiting = spin up workers.

### GPUs Scale on Latency
```
Vision model response time spikes to 6 seconds
  ↓
KEDA watches latency p95 in Postgres
  ↓
Over 5-second SLO = more GPU replicas (1→8)
```

**Why:** SLO enforcement. Keep response time acceptable, not queue-driven.

### Evaluation Runs at Fixed Capacity
```
Evaluation queue has 500 jobs waiting
  ↓
2 fixed worker-ragas pods
  ↓
Queue drains at steady pace
```

**Why:** Background work. No SLA, no urgency, just steady progress.

---

## Tech Stack

| Layer | Technology | Role |
|-------|----------|------|
| **API** | FastAPI (Python) | HTTP endpoint, query agent, job creation |
| **Workers** | Python async | Fetch pages, call models, vectorize text |
| **Job Queue** | Postgres + RabbitMQ | Job state + work tokens |
| **Search** | Postgres + pgvector | Chunks with embeddings + full-text index |
| **Models** | vLLM (Qwen models) | Vision reading, LLM agent, embeddings |
| **GPU** | NVIDIA L40S | 46 GB shared via time-slicing |
| **Autoscaling** | KEDA + HPA | Scale workers on queue, GPUs on latency |
| **Monitoring** | Prometheus + Grafana | Metrics, dashboards, alerts |
| **Infrastructure** | Kubernetes | All components containerized |

---

## What Makes It Interesting

### 1. **Parallel Ingest and Query**
Most RAG systems are offline: ingest everything, then serve queries. DocuFlow lets them overlap, so users get results while the system is still processing.

### 2. **Three Independent Scaling Signals**
Usually you either scale on queue depth or CPU. DocuFlow uses:
- Queue depth for workers (throughput)
- Latency for GPUs (SLO)
- Fixed capacity for evaluation (background)

Each tier scales on what actually matters to it.

### 3. **Integrated Vision Model**
Handles mixed printed/handwritten documents, partial text, low-quality scans. Not just OCR—actual semantic understanding of document structure.

### 4. **Real Evaluation**
Runs RAGAS (Retrieval-Augmented Generation Assessment) on search and generation quality. Most systems skip this because it's expensive; DocuFlow runs it continuously.

### 5. **Observable Latency**
Every model call is timed and recorded. KEDA uses these metrics to make scaling decisions. This is feedback-driven autoscaling, not guesswork.

---

## The Blog Series

This repo contains a detailed 10-part write-up of building and scaling DocuFlow:

1. **[System & Why It Scales on Queues](01-system.md)** – Architecture overview
2. **[HPA and KEDA, Precisely](02-hpa-and-keda.md)** – How autoscaling works
3. **[Queue Design: RabbitMQ + Postgres](03-queue-design.md)** – Why both?
4. **[Every ScaledObject, Explained](04-scaledobjects.md)** – YAML with reasoning
5. **[GPUs: One L40S, Three vLLM Servers](05-gpus.md)** – GPU sharing & time-slicing
6. **[Stress Testing: Method & Results](06-stress-tests.md)** – Load testing breakdown
7. **[What Broke, and the Fixes](07-what-broke.md)** – 14 production issues & solutions
8. **[Recreate It](08-recreate.md)** – Commands to build from scratch
9. **[Evaluation: RAGAS and Quality Metrics](09-evaluation.md)** – How we measure correctness
10. **[Architecture Refactor: Three Scaling Strategies](10-architecture-refactor.md)** – Latest design

Each post is detailed and includes the actual measurements, code samples, and decision trade-offs.

---

## Quick Start

### See It In Action
```bash
# 1. Start the system
make up

# 2. Upload documents
curl -X POST http://localhost:8000/ingest \
  -H 'Content-Type: application/json' \
  -d '{"dataset": "synthetic", "limit": 100}'

# 3. While it's processing, ask a question
curl -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"question": "Who signed document INV-1005?"}'

# 4. Watch it scale
kubectl get hpa -w
kubectl logs -f deployment/worker-vlm
```

### Read the Details
- **Architecture:** See `docs/ARCHITECTURE_DIAGRAM.md` (7 Mermaid diagrams)
- **Why we designed it this way:** Read `docs/blog/10-architecture-refactor.md`
- **Full technical breakdown:** See `REFACTOR_SUMMARY.md`

---

## Key Results

We stress-tested the system under real load:

- **Throughput:** ~170 pages/min on a single L40S GPU (after optimizations)
- **Extraction quality:** 99% hit@1 on identifier questions
- **Answer quality:** 97.5% exact match, 0.975 faithfulness (RAGAS)
- **Query latency:** p95 ≤ 15 seconds even while ingesting 800 pages
- **Scalability:** Answered 1,635 queries with zero errors under load
- **Resilience:** 0 pages lost in 14,614-page ingest (100% durability)

---

## Who Should Care?

### Document-Heavy Organizations
- Insurance (claims, policies, applications)
- Finance (invoices, contracts, reports)
- Legal (briefs, discovery, case files)
- Healthcare (patient records, referrals, forms)
- Government (permits, records, applications)

Anyone dealing with stacks of scanned documents will recognize the problem DocuFlow solves.

### Engineers & Researchers
- Kubernetes autoscaling in practice
- Building systems where throughput and latency have different bottlenecks
- Vision model deployment at scale
- RAG system architecture
- Observability and feedback-driven scaling

### Companies Building on Open Models
- No external APIs (everything runs locally)
- Open weights for Qwen models
- Can run on consumer hardware (though we use GPU)
- Full control over data (privacy)

---

## What's Next?

The system is designed to handle:
- **Scale-up:** Easy to add more GPUs (split by role: ingest vs. query)
- **Multi-tenancy:** Route different datasets to different GPU pools
- **Specialized models:** Swap Qwen for domain-specific models
- **Fine-tuning:** Retrain on domain documents
- **Production hardening:** HA Postgres, cloud storage for images, distributed model serving

---

## Thanks

Built on top of:
- **Qwen models** (Qwen2.5-VL, Qwen3, Qwen3-Embedding)
- **vLLM** (continuous batching LLM serving)
- **pgvector** (vector search in Postgres)
- **KEDA** (Kubernetes event-driven autoscaling)
- **RabbitMQ** (work queue with backoff)
- **Kubernetes** (everything)

---

## Start Here

1. **New to the project?** Read this intro, then jump to `docs/blog/01-system.md`
2. **Want to understand the design?** Read `docs/ARCHITECTURE_DIAGRAM.md` (visual)
3. **Want to build it?** Follow `docs/blog/08-recreate.md`
4. **Want to understand scaling?** Read `docs/blog/10-architecture-refactor.md`
5. **Want to debug it?** Check `ARCHITECTURE_SUMMARY.md` (monitoring & troubleshooting)

---

**Questions?** Each blog post digs deep into one aspect. If something isn't clear, that post probably has the answer.

Let's go read scanned documents. 📄
