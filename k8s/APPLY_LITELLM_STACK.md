# Applying the LiteLLM + llm-d Stack to Your Cluster

**One-command setup** to replace the current configuration with the new two-layer gateway.

---

## Prerequisites
- Kubernetes 1.28+
- Existing DocuFlow deployment with `docuflow-config` ConfigMap
- llm-d controller installed (or install with: `helm install llm-d ...`)

---

## Step 1: Deploy the Gateway Stack
```bash
kubectl apply -f k8s/base/llm-stack-combined.yaml
```

This creates:
- LiteLLM Proxy (2 replicas on port 4000)
- llm-d Gateway API (port 8000)
- vLLM StatefulSet with metrics enabled
- KEDA scaling based on latency
- New ConfigMap: `docuflow-config-litellm`

---

## Step 2: Update Worker Deployments

**Option A: Patch in-place** (recommended for existing clusters)
```bash
# Update workers to use the new LiteLLM endpoint ConfigMap
kubectl patch deployment worker-vlm -p '
{
  "spec": {
    "template": {
      "spec": {
        "containers": [
          {
            "name": "worker",
            "envFrom": [
              {"configMapRef": {"name": "docuflow-config-litellm"}},
              {"secretRef": {"name": "docuflow-secrets"}}
            ]
          }
        ]
      }
    }
  }
}'

kubectl patch deployment worker-embed -p '
{
  "spec": {
    "template": {
      "spec": {
        "containers": [
          {
            "name": "worker",
            "envFrom": [
              {"configMapRef": {"name": "docuflow-config-litellm"}},
              {"secretRef": {"name": "docuflow-secrets"}}
            ]
          }
        ]
      }
    }
  }
}'
```

**Option B: Manual YAML edit**

Edit `k8s/base/workers.yaml`, in each worker Deployment (worker-vlm, worker-embed):

```yaml
# BEFORE:
envFrom:
  - configMapRef: {name: docuflow-config}
  - secretRef: {name: docuflow-secrets}

# AFTER:
envFrom:
  - configMapRef: {name: docuflow-config-litellm}
  - secretRef: {name: docuflow-secrets}
```

Then:
```bash
kubectl apply -f k8s/base/workers.yaml
```

---

## Step 3: Update API Server (Optional)

If your API also needs to call models:

```bash
kubectl patch deployment api -p '
{
  "spec": {
    "template": {
      "spec": {
        "containers": [
          {
            "name": "api",
            "envFrom": [
              {"configMapRef": {"name": "docuflow-config-litellm"}},
              {"secretRef": {"name": "docuflow-secrets"}}
            ]
          }
        ]
      }
    }
  }
}'
```

---

## Step 4: Verify Everything is Connected

```bash
# Check pods are running
kubectl get pods -l app=litellm-proxy
kubectl get pods -l app=model-vlm
kubectl get gateway
kubectl get httproute

# Check workers can reach LiteLLM
kubectl logs deployment/worker-vlm | grep "POST.*litellm"

# Check LiteLLM is routing through llm-d
kubectl logs deployment/litellm-proxy | grep "forwarding\|routing"
```

---

## Step 5: Monitor the Transition

```bash
# Watch LiteLLM logs
kubectl logs -f deployment/litellm-proxy

# Watch llm-d controller
kubectl logs -f deployment/llm-d-controller -n kube-system

# Check vLLM metrics
kubectl exec -it model-vlm-0 -- curl localhost:8000/metrics | grep vllm
```

---

## Rollback (if needed)

If something breaks, revert to the original:

```bash
# Restore original config to workers
kubectl patch deployment worker-vlm -p '
{
  "spec": {
    "template": {
      "spec": {
        "containers": [
          {
            "name": "worker",
            "envFrom": [
              {"configMapRef": {"name": "docuflow-config"}},
              {"secretRef": {"name": "docuflow-secrets"}}
            ]
          }
        ]
      }
    }
  }
}'

kubectl patch deployment worker-embed -p '
{
  "spec": {
    "template": {
      "spec": {
        "containers": [
          {
            "name": "worker",
            "envFrom": [
              {"configMapRef": {"name": "docuflow-config"}},
              {"secretRef": {"name": "docuflow-secrets"}}
            ]
          }
        ]
      }
    }
  }
}'

# Remove the gateway stack
kubectl delete -f k8s/base/llm-stack-combined.yaml
```

---

## Verifying the Connection Works

### Test request through LiteLLM:
```bash
# Port-forward to LiteLLM
kubectl port-forward svc/litellm-proxy 4000:4000 &

# Send a test request
curl -X POST http://localhost:4000/v1/models \
  -H "Authorization: Bearer none" \
  -H "Content-Type: application/json"
```

Expected response: List of available models from llm-d

### Trace a request through both layers:
```bash
# Terminal 1: Watch LiteLLM logs
kubectl logs -f deployment/litellm-proxy

# Terminal 2: Watch llm-d logs
kubectl logs -f deployment/llm-d-controller -n kube-system

# Terminal 3: Send a request
curl -X POST http://localhost:4000/v1/models

# You should see:
# [LiteLLM logs] Request received, rate limit OK
# [llm-d logs] Routing to model-vlm-0 (queue=2)
```

---

## Configuration Summary

| Component | Endpoint | Purpose |
|-----------|----------|---------|
| Workers | `litellm-proxy:4000` | Entry point (via env vars) |
| LiteLLM | `:4000` | Rate limits, keys, logging |
| llm-d Gateway | `:8000` | Pod routing, metrics |
| vLLM pods | `:8000 + /metrics` | Actual inference |

---

## Next: Tuning

Once running:
1. Check `docs/LITELLM_LLMD_SETUP.md` for monitoring and SQL queries
2. Adjust rate limits in the LiteLLM ConfigMap as needed
3. Monitor pod distribution with llm-d routing logs
4. Verify KEDA is scaling GPUs on latency

---

For detailed architecture explanation, see: `docs/blog/03-gateway-api-routing.md`
