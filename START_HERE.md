# DocuFlow – Start Here 🚀

**Welcome!** This guide helps you find what you need in the DocuFlow project.

---

## 📚 Reading Paths

### For New Team Members (30 minutes)
1. **Read:** `docs/blog/00-introduction.md` ← **START HERE**
   - What DocuFlow is and why it matters
   - The three pipelines (ingest, query, eval)
   - Key results from stress testing
   
2. **See:** `docs/ARCHITECTURE_DIAGRAM.md` (scroll to system overview)
   - Visual overview of all components
   - How everything connects
   
3. **Deep dive:** `docs/blog/10-architecture-refactor.md`
   - Why we designed it this way
   - Three scaling strategies explained
   - How metrics flow through the system

### For Operators & DevOps (20 minutes)
1. **Reference:** `ARCHITECTURE_SUMMARY.md`
   - Component layout
   - Scaling behavior
   - Testing checklist
   
2. **Deploy:** 
   - `k8s/base/workers.yaml` – Deploy worker-ragas
   - `k8s/base/keda.yaml` – Deploy KEDA scalers
   
3. **Monitor:**
   - Check latency metrics: `SELECT * FROM model_latencies`
   - Check KEDA HPA: `kubectl get hpa -w`
   - See logs: `kubectl logs -f deployment/worker-vlm`

### For Engineers (1-2 hours)
1. **Technical details:** `REFACTOR_SUMMARY.md`
   - All code changes listed
   - Database schema
   - KEDA configuration
   
2. **Flows:** `docs/ARCHITECTURE_DIAGRAM.md`
   - Ingest sequence diagram
   - Query sequence diagram
   - Eval sequence diagram
   
3. **Full blog series:** `docs/blog/`
   - Posts 01-09: Original 9-part series on scaling
   - Post 10: Architecture refactor
   
4. **Code:** Read the implementation
   - `docuflow/db.py` – Schema
   - `docuflow/llm.py` – Latency recording
   - `docuflow/pipeline.py` – Job handlers
   - `k8s/base/keda.yaml` – Scaling config

### For Stakeholders (15 minutes)
1. **Executive summary:** `docs/blog/00-introduction.md` (Quick Start section)
   - What it does in one paragraph
   - Key capabilities
   - Stress test results
   
2. **Why it matters:** `docs/blog/10-architecture-refactor.md` (The Problem section)
   - Before/after comparison
   - Cost efficiency improvements
   
3. **Next steps:** `ARCHITECTURE_SUMMARY.md` (Next Steps section)

---

## 🗂️ File Organization

```
docuflow-systems-lab/
├── 📄 START_HERE.md ← YOU ARE HERE
├── 📄 ARCHITECTURE_SUMMARY.md ← Quick ref
├── 📄 REFACTOR_SUMMARY.md ← Tech details
│
├── docs/
│   ├── ARCHITECTURE_DIAGRAM.md ← 8 diagrams
│   └── blog/
│       ├── 00-introduction.md ← Entry point
│       ├── 01-system.md through 09-evaluation.md ← Original series
│       └── 10-architecture-refactor.md ← Latest design
│
├── docuflow/
│   ├── db.py ← Schema (model_latencies table)
│   ├── llm.py ← Latency recording
│   ├── pipeline.py ← Handlers (eval_handler added)
│   └── ...
│
└── k8s/base/
    ├── workers.yaml ← worker-ragas added
    ├── keda.yaml ← Latency scalers added
    └── ...
```

---

## 🎯 Quick Navigation

### "What is DocuFlow?"
→ `docs/blog/00-introduction.md`

### "How does it work?"
→ `docs/ARCHITECTURE_DIAGRAM.md` (system overview diagram)

### "Why did we design it this way?"
→ `docs/blog/10-architecture-refactor.md`

### "What code changed?"
→ `REFACTOR_SUMMARY.md` (files changed table)

### "How do I deploy it?"
→ `ARCHITECTURE_SUMMARY.md` (deployment checklist)

### "How do I monitor it?"
→ `ARCHITECTURE_SUMMARY.md` (monitoring strategy)

### "What's the database schema?"
→ `docuflow/db.py` (SCHEMA constant)

### "How does the ingest pipeline work?"
→ `docs/ARCHITECTURE_DIAGRAM.md` (ingest sequence diagram)

### "How does the query agent work?"
→ `docs/ARCHITECTURE_DIAGRAM.md` (query sequence diagram)

### "How does evaluation work?"
→ `docs/ARCHITECTURE_DIAGRAM.md` (evaluation sequence diagram)

### "How does KEDA scaling work?"
→ `docs/blog/10-architecture-refactor.md` (metrics flow section)

### "What are the stress test results?"
→ `docs/blog/00-introduction.md` (key results section)

### "How do I build this from scratch?"
→ `docs/blog/08-recreate.md` (from original series)

---

## 📋 Key Takeaways

### System in One Sentence
DocuFlow reads stacked scanned documents with a vision model, makes them searchable, and answers questions about them—while handling ingest and queries simultaneously.

### Architecture in One Sentence
Three tiers that scale on three different metrics: worker pods on queue depth (throughput), GPU servers on latency (SLO), and evaluation at fixed capacity (background).

### What Changed
- vLLM moved from being queue-driven to REST APIs
- GPU autoscaling moved from queue depth to latency p95/p99
- Evaluation moved to a separate fixed-capacity queue
- Each tier now scales on what actually matters to it

---

## ✅ Quality Checklist

Before using this in production:
- [ ] Read the intro blog post (understand what it does)
- [ ] Review the architecture diagram (understand how it works)
- [ ] Review the refactor blog post (understand why it's designed this way)
- [ ] Check the testing checklist (`ARCHITECTURE_SUMMARY.md`)
- [ ] Deploy to staging
- [ ] Run stress test with real dataset
- [ ] Monitor metrics for 24 hours
- [ ] Check scaling behavior under load

---

## 🤔 Questions?

| Question | Answer Location |
|----------|-----------------|
| What's the overall architecture? | `docs/ARCHITECTURE_DIAGRAM.md` |
| How does worker-vlm work? | `docs/ARCHITECTURE_DIAGRAM.md` → Component Details |
| Why is vLLM a REST API now? | `docs/blog/10-architecture-refactor.md` |
| How does KEDA scale the GPU? | `docs/blog/10-architecture-refactor.md` → Latency Metrics section |
| What tables did you add? | `REFACTOR_SUMMARY.md` → Database Schema |
| How do I deploy worker-ragas? | `k8s/base/workers.yaml` |
| How do I monitor latency? | `ARCHITECTURE_SUMMARY.md` → Monitoring Strategy |
| What are the stress test results? | `docs/blog/00-introduction.md` → Key Results |

---

## 🚀 Next Steps

**1. First 10 minutes:**
- [ ] Read: `docs/blog/00-introduction.md`
- [ ] Skim: `docs/ARCHITECTURE_DIAGRAM.md` (just look at the diagrams)

**2. Next 20 minutes:**
- [ ] Read: `docs/blog/10-architecture-refactor.md`
- [ ] Understand: Three scaling strategies

**3. Before deploying:**
- [ ] Review: `ARCHITECTURE_SUMMARY.md` (testing checklist)
- [ ] Review: `REFACTOR_SUMMARY.md` (what changed)

**4. Deploy & monitor:**
- [ ] Apply: `k8s/base/workers.yaml` + `k8s/base/keda.yaml`
- [ ] Test: Ingest 100 pages, query, watch scaling
- [ ] Monitor: Model latencies + KEDA HPA

---

**Ready?** Start with `docs/blog/00-introduction.md` →

Good luck! 🎉
