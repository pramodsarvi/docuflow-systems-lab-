# Intelligent Routing: Layered Stack (LiteLLM + llm-d)

**Architecture:** Two-layer gateway system
- **Layer 1 (App):** LiteLLM handles keys, rate limits, cost tracking, logging
- **Layer 2 (K8s):** llm-d handles pod routing based on queue depth & KV cache
- **Result:** Smart routing + operational features in one system

This combines the best of both worlds.

---

## Combined Architecture: LiteLLM + llm-d

```mermaid
graph TB
    subgraph "Workers"
        W1["worker-vlm-1"]
        W2["worker-vlm-2"]
    end
    
    subgraph "Layer 1: Application Gateway (LiteLLM)"
        LITE["LiteLLM Proxy<br/>:4000<br/>Rate limits<br/>Keys<br/>Cost tracking<br/>Logging"]
    end
    
    subgraph "Layer 2: Kubernetes Gateway (llm-d)"
        LLMD["llm-d Controller<br/>:8000<br/>Pod discovery<br/>Queue-aware routing<br/>Health checks"]
    end
    
    subgraph "Kubernetes"
        SVC["Service: model-vlm<br/>discovered by Gateway API"]
        P1["model-vlm-0<br/>vLLM + /metrics"]
        P2["model-vlm-1<br/>vLLM + /metrics"]
    end
    
    W1 & W2 -->|request| LITE
    LITE -->|"forwards<br/>no modifications"| LLMD
    LLMD -->|"routes to<br/>best pod"| P1 & P2
    
    LLMD -->|"polls /metrics<br/>every 5s"| P1 & P2
    
    style LITE fill:#ff9800
    style LLMD fill:#4caf50
```

### Why Both?

**LiteLLM handles:**
- ✓ API key validation and management
- ✓ Rate limiting per key/user
- ✓ Cost tracking and budgets
- ✓ Request/response logging
- ✓ Fallback model handling
- ✓ Unified interface (workers don't see backend details)

**llm-d handles:**
- ✓ Pod discovery (automatic from K8s Services)
- ✓ Health checking
- ✓ Intelligent routing (least queue depth, most KV cache)
- ✓ Load shedding (503 when full)
- ✓ Integration with KEDA (pod scaling)

**Together:**
- Workers see one endpoint: `litellm-proxy:4000`
- LiteLLM enforces policy (keys, limits)
- llm-d ensures requests go to the best pod
- No duplication, no conflicts

---

## How Requests Flow

```mermaid
sequenceDiagram
    participant W as worker-vlm
    participant LITE as LiteLLM<br/>:4000
    participant LLMD as llm-d<br/>:8000
    participant P1 as vLLM-0<br/>queue=2
    participant P2 as vLLM-1<br/>queue=8

    W->>LITE: POST /v1/chat/completions<br/>with API key
    LITE->>LITE: ✓ Validate key<br/>✓ Check rate limit<br/>✓ Log request
    
    LITE->>LLMD: POST /v1/chat/completions<br/>(transparent forward)
    
    loop Every 5 seconds
        LLMD->>P1: GET /metrics
        P1-->>LLMD: queue=2, cache=40%
        
        LLMD->>P2: GET /metrics
        P2-->>LLMD: queue=8, cache=85%
    end
    
    LLMD->>LLMD: Route: score=queue+cache<br/>P1=2.4, P2=8.85<br/>→ use P1
    
    LLMD->>P1: Forward request
    P1-->>LLMD: Response
    
    LITE->>LITE: Log response<br/>Track cost<br/>Record latency
    
    LITE-->>W: Response
```

---

## Option Comparison

If you need ONLY routing → **llm-d Gateway API alone**  
If you need ONLY keys/limits → **LiteLLM alone**  
If you need BOTH → **LiteLLM + llm-d (recommended)**

```
╔════════════════════════╦════════════════╦════════════════╦═══════════════════╗
║ Feature                ║ llm-d Only     ║ LiteLLM Only   ║ Both (Combined)   ║
╠════════════════════════╬════════════════╬════════════════╬═══════════════════╣
║ Routing awareness      ║ queue/cache    ║ round-robin    ║ queue/cache       ║
║ API keys               ║ ❌             ║ ✓              ║ ✓                 ║
║ Rate limiting          ║ ❌             ║ ✓              ║ ✓                 ║
║ Cost tracking          ║ ❌             ║ ✓              ║ ✓                 ║
║ Health checking        ║ ✓              ║ ❌             ║ ✓                 ║
║ Pod awareness          ║ ✓              ║ ❌             ║ ✓                 ║
║ Load shedding          ║ ✓              ║ ❌             ║ ✓                 ║
║ Per-request overhead   ║ <1ms           ║ 2-5ms          ║ ~2-5ms            ║
║ Setup complexity       ║ Medium         ║ Medium         ║ Medium            ║
║ Best for               ║ Pure K8s       ║ Multi-cloud    ║ K8s + operations  ║
╚════════════════════════╩════════════════╩════════════════╩═══════════════════╝
```

---

## LiteLLM Configuration for Combined Stack

```yaml
# LiteLLM receives requests from workers
# LiteLLM routes to llm-d Gateway (not directly to pods)

model_list:
  - model_name: "extract"
    litellm_params:
      model: "openai/Qwen2.5-VL-3B"
      api_base: "http://llm-d-gateway:8000/v1"  # ← Points to llm-d
      api_key: "unused"

rate_limit_models:
  - model: "extract"
    requests_per_second: 100

cost_tracking:
  enabled: true
  database_url: "postgresql://litellm:pass@postgres:5432/litellm"

logging:
  type: "json"
  database_url: "postgresql://litellm:pass@postgres:5432/litellm"
```

## llm-d Configuration for Combined Stack

```yaml
# llm-d receives requests from LiteLLM
# llm-d routes to individual vLLM pods based on metrics

apiVersion: gateway.networking.k8s.io/v1beta1
kind: GatewayClass
metadata:
  name: llm-d
spec:
  controllerName: "llm.ai/llm-d"

---
apiVersion: gateway.networking.k8s.io/v1beta1
kind: Gateway
metadata:
  name: llm-d-gateway
spec:
  gatewayClassName: llm-d
  listeners:
  - name: http
    port: 8000
    protocol: HTTP

---
apiVersion: llm.ai/v1alpha1
kind: LLMServicePolicy
metadata:
  name: vllm-routing-policy
spec:
  targetRef:
    kind: Service
    name: model-vlm
  
  routing:
    strategy: "least_pending_requests"
    metrics:
      - type: "vllm_queue_depth"
      - type: "vllm_kv_cache_utilization"
```

---

## Deployment: Step by Step

### 1. Install LiteLLM
```bash
# Deploy LiteLLM Proxy (2 replicas for HA)
kubectl apply -f k8s/base/litellm-deployment.yaml

# Verify
kubectl get deploy litellm-proxy
kubectl get svc litellm-proxy
```

### 2. Install llm-d Controller
```bash
# Install K8s Gateway API (if not present)
kubectl apply -k github.com/kubernetes-sigs/gateway-api/config/crd

# Install llm-d controller
helm repo add llm-d https://charts.llm-d.io
helm install llm-d llm-d/llm-d-controller --namespace kube-system
```

### 3. Deploy Gateway Resources
```bash
# Deploy Gateway API resources + llm-d configuration
kubectl apply -f k8s/base/llm-stack-combined.yaml

# Verify everything is connected
kubectl get gatewayclass
kubectl get gateway llm-d-gateway
kubectl get httproute
kubectl get llmservicepolicy
```

### 4. Update Workers
```python
# OLD: Direct to vLLM
response = httpx.post("http://model-vlm:8000/v1/chat/completions", ...)

# NEW: Through LiteLLM (which routes through llm-d)
response = httpx.post("http://litellm-proxy:4000/v1/chat/completions", ...)
```

---

## Monitoring the Stack

### Check LiteLLM metrics
```bash
# View LiteLLM logs (keys, rate limits, cost)
kubectl logs -f deployment/litellm-proxy

# Query cost tracking (if using Postgres backend)
psql -c "SELECT model, user_id, SUM(cost) FROM litellm_logs GROUP BY model, user_id"
```

### Check llm-d routing
```bash
# View llm-d controller logs
kubectl logs -f deployment/llm-d-controller -n kube-system

# Check which pods are being routed to
kubectl get pods -l app=model-vlm
kubectl exec -it model-vlm-0 -- curl http://localhost:8000/metrics | grep queue_depth
```

### End-to-end request flow
```bash
# 1. Send request through LiteLLM
curl -X POST http://litellm-proxy:4000/v1/chat/completions \
  -H "Authorization: Bearer key-123" \
  -d '{"model": "extract", "messages": [...]}'

# 2. Check LiteLLM logs for key validation + rate limit
kubectl logs deployment/litellm-proxy | tail -20

# 3. Check which pod llm-d routed to
kubectl logs deployment/llm-d-controller -n kube-system | tail -20

# 4. Verify response came from the right pod
curl http://model-vlm-0:8000/v1/models
```

---

## Troubleshooting

| Problem | Check |
|---------|-------|
| "401 Unauthorized" | LiteLLM key validation. Check `kubectl logs litellm-proxy` |
| "429 Too Many Requests" | Rate limit hit in LiteLLM. Check configured RPS limits |
| "503 Service Unavailable" | llm-d couldn't find available pods. Check `kubectl get pods -l app=model-vlm` |
| "High latency" | Check llm-d controller logs: `kubectl logs deployment/llm-d-controller -n kube-system` |
| "Requests going to busy pod" | llm-d metrics scraping failed. Verify `/metrics` endpoint on vLLM pods |

---

## Files Provided

```
k8s/base/llm-stack-combined.yaml
  ├─ LiteLLM ConfigMap: rate limits, cost tracking, logging config
  ├─ LiteLLM Deployment: 2 replicas, HA proxy
  ├─ LiteLLM Service: :4000
  ├─ GatewayClass: llm-d controller selection
  ├─ Gateway: Listens on :8000, routes to vLLM services
  ├─ HTTPRoute: Path-based routing rules
  ├─ LLMServicePolicy: Queue-aware routing strategy
  ├─ vLLMMonitor: Metrics scraping config
  ├─ vLLM StatefulSet: With /metrics enabled
  ├─ KEDA ScaledObject: GPU scaling on latency
  └─ Secrets: Database credentials
```

---

## Summary

```
Layer 1 (LiteLLM):     Keys, rate limits, cost, logging
            ↓
Layer 2 (llm-d):       Pod discovery, health checks, intelligent routing
            ↓
Actual inference:      vLLM pods handle requests
```

**Result:**
- ✓ Workers have unified endpoint (`litellm-proxy:4000`)
- ✓ App-level policies enforced (keys, budgets, logging)
- ✓ K8s-level intelligence (pod-aware routing, load shedding)
- ✓ No duplication or conflicts
- ✓ Latency: +2-5ms from LiteLLM + <1ms from llm-d = ~3-6ms overhead (acceptable)

This is the recommended production setup for DocuFlow on Kubernetes.
