# LiteLLM + llm-d Combined Gateway Setup Guide

**Quick reference for deploying the two-layer intelligent routing stack.**

---

## Architecture at a Glance

```
┌─────────────────────────────────────────────────────────────┐
│                       Workers                               │
│                  (extract, embed, ragas)                    │
└────────────────────────────┬────────────────────────────────┘
                             │ POST :4000
┌────────────────────────────▼────────────────────────────────┐
│            LiteLLM Proxy (Layer 1 - App Policy)             │
│                                                             │
│  • API key validation                                       │
│  • Rate limiting per key/model                             │
│  • Cost tracking (logs to Postgres)                        │
│  • Request/response logging                                │
│  • 2 replicas (HA)                                         │
└────────────────────────────┬────────────────────────────────┘
                             │ POST :8000 (transparent)
┌────────────────────────────▼────────────────────────────────┐
│       llm-d Gateway (Layer 2 - K8s Intelligence)            │
│                                                             │
│  • Pod discovery from K8s Services                         │
│  • Polls /metrics every 5s                                 │
│  • Scores pods: queue_depth + (kv_cache / budget)         │
│  • Routes to lowest-score pod                              │
│  • Built-in health checks                                  │
└────────────────────────────┬────────────────────────────────┘
                    ┌────────┴────────┐
                    │                 │
         ┌──────────▼───────┐ ┌──────▼──────────┐
         │  vLLM Pod 0      │ │  vLLM Pod 1     │
         │  (extract)       │ │  (extract)      │
         │  queue=2         │ │  queue=8 ❌     │
         │  cache=40%       │ │  cache=85%      │
         └──────────────────┘ └─────────────────┘

Request routes to Pod 0 (lowest score: 2.4 vs 8.85)
```

---

## Prerequisites

- **Kubernetes 1.28+** (for Gateway API support)
- **KEDA 2.15+** (for autoscaling)
- **Postgres** (for LiteLLM cost/log backend)
- **RabbitMQ** (for job queue)
- **vLLM** image available

---

## Deployment: 5 Steps

### Step 1: Verify Gateway API is available
```bash
kubectl get crd gatewayclasses.gateway.networking.k8s.io
# Should return the CRD definition

# If not found, install:
kubectl apply -k github.com/kubernetes-sigs/gateway-api/config/crd?ref=v1.2.0
```

### Step 2: Install llm-d controller
```bash
# Using Helm (if available)
helm repo add llm-d https://charts.llm-d.io
helm install llm-d llm-d/llm-d-controller \
  --namespace kube-system \
  --create-namespace

# Verify
kubectl get deployment -n kube-system llm-d-controller
```

### Step 3: Deploy the combined stack
```bash
# Single command deploys:
# - LiteLLM ConfigMap (rate limits, cost tracking, logging)
# - LiteLLM Deployment (2 replicas)
# - LiteLLM Service (:4000)
# - GatewayClass (llm-d controller)
# - Gateway (:8000)
# - HTTPRoute (path routing)
# - LLMServicePolicy (intelligent routing config)
# - vLLM StatefulSet (with /metrics enabled)
# - KEDA ScaledObject (latency-based GPU scaling)
# - Postgres Secret (for LiteLLM backend)

kubectl apply -f k8s/base/llm-stack-combined.yaml
```

### Step 4: Verify everything is up
```bash
# Check deployments
kubectl get deploy litellm-proxy
kubectl get statefulset model-vlm

# Check Gateway API resources
kubectl get gatewayclass llm-d
kubectl get gateway llm-d-gateway
kubectl get httproute llm-routes

# Check llm-d policy
kubectl get llmservicepolicy vllm-routing-policy
```

### Step 5: Update worker configuration
```python
# In your worker code (extract.py, embed.py, etc)

# BEFORE:
LLM_URL = "http://model-vlm:8000"  # Direct to vLLM

# AFTER:
LLM_URL = "http://litellm-proxy:4000"  # Through LiteLLM
```

Then redeploy workers:
```bash
kubectl rollout restart deployment/worker-vlm
kubectl rollout restart deployment/worker-embed
```

---

## Testing the Setup

### Send a test request
```bash
# Direct request through LiteLLM
curl -X POST http://localhost:4000/v1/models \
  -H "Authorization: Bearer test-key"

# Port-forward if not local
kubectl port-forward svc/litellm-proxy 4000:4000 &
```

### Verify request flow
```bash
# 1. Check LiteLLM received and processed the request
kubectl logs deployment/litellm-proxy | grep "Authorization\|rate_limit"

# 2. Check llm-d routed the request to a pod
kubectl logs deployment/llm-d-controller -n kube-system | grep "routing\|pod"

# 3. Verify the vLLM pod processed it
kubectl logs statefulset/model-vlm | grep "GET /models"
```

### Check routing metrics
```bash
# See which pods are serving requests
kubectl exec -it model-vlm-0 -- curl localhost:8000/metrics | grep -E "queue_depth|kv_cache"

# Compare with other replicas
kubectl exec -it model-vlm-1 -- curl localhost:8000/metrics | grep -E "queue_depth|kv_cache"

# llm-d should route to the pod with lower queue_depth
```

---

## Configuration: Key Tuning Points

### LiteLLM Rate Limits
Edit `k8s/base/llm-stack-combined.yaml`, ConfigMap section:
```yaml
rate_limit_models:
  - model: "extract"
    requests_per_second: 100      # ← Adjust for your workload
  - model: "embed"
    requests_per_second: 200
```

### llm-d Routing Strategy
Edit LLMServicePolicy in the manifest:
```yaml
routing:
  strategy: "least_pending_requests"  # Options: round_robin, least_connections
  metrics:
    - type: "vllm_queue_depth"       # Primary metric
    - type: "vllm_kv_cache_utilization"  # Secondary
    - type: "vllm_batch_utilization"     # Tertiary
```

### vLLM Model & Batch Size
Edit StatefulSet args:
```yaml
args:
  - --served-model-name=Qwen2.5-VL-3B
  - --max-model-len=2048             # ← Adjust for model
  - --max-num-batched-tokens=8192    # ← GPU-dependent
  - --gpu-memory-utilization=0.95
```

### GPU Scaling Thresholds
Edit KEDA ScaledObject in the manifest:
```yaml
triggers:
  - type: postgresql
    metadata:
      targetQueryValue: "5000"  # Scale when p95 > 5000ms
      minReplicaCount: 1        # Always keep 1 GPU pod
      maxReplicaCount: 8        # Cap at 8
```

---

## Monitoring & Debugging

### View LiteLLM Logs (App-Level)
```bash
kubectl logs -f deployment/litellm-proxy
# Shows: API calls, rate limit hits, cost tracking, key validation
```

### View llm-d Logs (Routing-Level)
```bash
kubectl logs -f deployment/llm-d-controller -n kube-system
# Shows: Pod metrics, routing decisions, health checks
```

### View vLLM Logs (Inference-Level)
```bash
kubectl logs -f statefulset/model-vlm
# Shows: Request handling, batch stats, GPU utilization
```

### Check vLLM Metrics (Raw)
```bash
# Port-forward to a pod
kubectl port-forward model-vlm-0 8000:8000 &

# Query metrics endpoint
curl http://localhost:8000/metrics | grep vllm
# Shows: queue_depth, kv_cache_utilization, requests, latency
```

### SQL: Monitor Cost & Usage (LiteLLM Backend)
```sql
-- Total cost by model
SELECT model, SUM(cost) FROM litellm_logs GROUP BY model;

-- Rate limit usage
SELECT user_id, model, COUNT(*) as requests FROM litellm_logs 
WHERE timestamp > now() - interval '1 minute'
GROUP BY user_id, model;

-- Request latency percentiles
SELECT 
  model,
  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) as p95
FROM litellm_logs
WHERE timestamp > now() - interval '5 minutes'
GROUP BY model;
```

### SQL: Monitor GPU Scaling (DocuFlow)
```sql
-- Recent inference latencies
SELECT 
  stage,
  endpoint,
  COUNT(*) as requests,
  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) as p95,
  MAX(latency_ms) as max_latency
FROM model_latencies
WHERE ts > now() - interval '5 minutes'
GROUP BY stage, endpoint;

-- Signals to KEDA for scaling
SELECT 
  stage,
  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) as p95_latency
FROM model_latencies
WHERE ts > now() - interval '2 minutes'
GROUP BY stage;
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "401 Unauthorized" on every request | LiteLLM key validation failing | Check `kubectl logs litellm-proxy` for key validation errors |
| "429 Too Many Requests" | Rate limit hit | Adjust `requests_per_second` in LiteLLM ConfigMap |
| "503 Service Unavailable" | llm-d can't find available pods | Check `kubectl get pods -l app=model-vlm` — all pods full? |
| Requests always go to Pod 0 | llm-d metrics scraping failed | Check vLLM pod has `--enable-metrics=true` and `/metrics` responds |
| High latency | Load not distributed | Check `kubectl logs deployment/llm-d-controller -n kube-system` for routing issues |
| vLLM pods not scaling | KEDA not reading metrics | Check `kubectl describe hpa model-vlm-latency` for error messages |
| LiteLLM 503 errors | Backend (llm-d) unavailable | Verify Gateway is ready: `kubectl get gateway llm-d-gateway` |

---

## Performance Expectations

With this setup, you should see:

- **Request latency overhead:** ~3-6ms (2-5ms LiteLLM + <1ms llm-d)
- **Routing accuracy:** 99%+ requests route to the least-busy pod
- **Cost tracking:** All requests logged with model, user, tokens, cost
- **Rate limiting:** Enforced per API key, per model
- **Pod scaling:** Automatic within 30s of latency SLO breach

---

## Files Reference

```
k8s/base/llm-stack-combined.yaml
  ├─ ConfigMap: litellm-config (rate limits, cost, logging)
  ├─ Deployment: litellm-proxy (2 replicas)
  ├─ Service: litellm-proxy (port 4000)
  ├─ GatewayClass: llm-d
  ├─ Gateway: llm-d-gateway (port 8000)
  ├─ HTTPRoute: llm-routes
  ├─ LLMServicePolicy: vllm-routing-policy
  ├─ vLLMMonitor: vllm-metrics-monitor
  ├─ StatefulSet: model-vlm (vLLM)
  ├─ ScaledObject: model-vlm-latency (KEDA)
  └─ Secret: litellm-db

docs/blog/03-gateway-api-routing.md
  └─ Full explanation of combined architecture
```

---

## Next Steps

1. **Deploy & test** the combined stack locally
2. **Monitor** the routing decisions with the logs above
3. **Adjust rate limits** based on your workload patterns
4. **Scale GPUs** by watching the KEDA ScaledObject
5. **Track costs** using the LiteLLM SQL queries

For detailed architecture explanation, see: `docs/blog/03-gateway-api-routing.md`
