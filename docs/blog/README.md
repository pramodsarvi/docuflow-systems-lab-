# Scaling on queue depth: KEDA, HPA and RabbitMQ in front of vLLM on one GPU

*A field report from building docuflow, a document-ingestion and agentic-RAG system, and stress-testing it until each
part of the autoscaling broke at least once.*

## Summary

docuflow reads scanned documents (printed and handwritten) with a vision model, indexes them in Postgres + pgvector,
and answers questions with an agent loop, while ingestion keeps running. Everything runs on Kubernetes: one machine,
one NVIDIA L40S (46 GB), a kind cluster, and three vLLM servers sharing that GPU.

Almost everything scales on **queue depth**, through KEDA, which turns each signal into a Kubernetes HPA:

| What scales | Signal | Scaler | Range |
|---|---|---|---|
| ingest workers (io, vlm, embed) | work tokens waiting in a RabbitMQ queue, plus jobs running | KEDA `rabbitmq` + `postgresql` | 0 to N, scale to zero |
| API | CPU, plus interactive queries in flight | KEDA `cpu` + `postgresql` | 1 to 8 |
| vLLM page reader and embedder | vLLM's own running + waiting requests | KEDA `prometheus` | 1 to 2 (local), 1 to 4 (prod) |

What the stress tests showed:

- **Queue-driven scale-out works, and is fast.** From zero, KEDA had the first page-reading worker up in 10 s and 6
  of them in 30 s. Under an 800-page burst with 12 query clients the system answered **1,635 queries with zero
  errors** while ingesting 34 pages/min.
- **Most of the bugs weren't in the autoscaler. They sat around it:** an in-app concurrency cap that left
  4 of 12 new pods idle, HTTP keep-alive pinning every worker to the first vLLM replica, `kubectl apply` resetting
  KEDA's replica counts, KEDA failing to resolve short DNS names, and rolling updates that deadlocked on GPU memory.
  Fixing them took ingest from **34 to ~170 pages/min** on the same GPU. Running a 14,600-page ingest then
  surfaced four more: truncated output discarding whole pages, an outage that downgraded pages instead of deferring
  them, a memory budget that turned out not to be the bottleneck, and uneven per-connection balancing.
- **Scaling the wrong tier does nothing.** KEDA scaled the API to 8 pods under 96 concurrent queries; each pod used
  5 millicores, because the bottleneck was the agent model's batch slots (32 running, 64 waiting). Latency only
  moved when the model server got more capacity.
- **On one GPU, the GPU is the ceiling.** Autoscaling redistributes a fixed budget. The last lever was choosing what
  the budget is for: an *ingest mode* that pauses queries and gives the whole GPU to page reading.

## The parts

1. [The system and why it scales on queues](01-system.md): architecture, the three workloads, why queue depth beats CPU
2. [HPA and KEDA, precisely](02-hpa-and-keda.md): what KEDA adds to the HPA, the replica formula, activation,
   cooldown, multiple triggers
3. [The queue: RabbitMQ tokens, Postgres truth](03-queue-design.md): why both, tokens, delayed retries, reconciliation
4. [Every ScaledObject, explained](04-scaledobjects.md): the actual YAML for workers, API and vLLM, and the reasoning
   behind each number
5. [GPUs: one L40S, three vLLM servers](05-gpus.md): passthrough into kind, time-slicing, KV-cache budgets
6. [Stress testing: method and results](06-stress-tests.md): the load generator, the runs, latency by load step
7. [What broke, and the fixes](07-what-broke.md): fourteen failures, each with symptom, cause and fix
8. [Recreate it](08-recreate.md): commands from a bare GPU machine to a stress report, plus the cost trade-offs

All code is in this repository: manifests in `k8s/`, cluster bootstrap in `scripts/cluster-up.sh`, the load generator
in `scripts/stress.py`.

## Environment used for every number in this write-up

| | |
|---|---|
| GPU | 1x NVIDIA L40S 46 GB, driver 570.133, CUDA 12.8 |
| Host | 60 vCPU, 200 GB RAM, Ubuntu 24.04 |
| Kubernetes | kind v0.24 (Kubernetes 1.31), 1 control plane + 1 GPU worker + 1 CPU worker |
| Autoscaling | KEDA 2.15.1, metrics-server 3.12.2 |
| GPU sharing | NVIDIA device plugin 0.17.0, time-slicing x6 |
| Model serving | vLLM v0.13.0 (CUDA 12.9 build) |
| Models | Qwen2.5-VL-3B-Instruct (page reading), Qwen3-4B-Instruct-2507 (agent), Qwen3-Embedding-0.6B |
| Queue / storage | RabbitMQ 3.13, Postgres 16 + pgvector |
| Monitoring | kube-prometheus-stack 65.5, DCGM exporter |
