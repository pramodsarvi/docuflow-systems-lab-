#!/usr/bin/env bash
# Switch the shared-GPU cluster between serving queries and maximum ingest.
#   scripts/mode.sh ingest    pause queries (API answers 503), stop the agent model, give its GPU share to page reading:
#                             model-vlm up to 3 replicas with 3 GiB KV / 24 slots each, worker-vlm up to 72, worker-io up to 8, no query back-off
#   scripts/mode.sh query     restore the default: agent model up, queries open, model-vlm 1..2, worker-vlm 0..24
#   scripts/mode.sh mac-llm   keep ingest-priority scaling, but answer queries with an agent model on a Mac (Ollama via
#                             a reverse SSH tunnel; see k8s/extras/mac-llm.yaml). MAC_LLM_MODEL defaults to qwen3:4b-instruct
# Changes are live patches; `make deploy` also restores the defaults.
set -euo pipefail
API=${API:-http://docuflow.localtest.me}
NS=docuflow

# Replace one flag's value in a model server's vLLM args (live patch; `make deploy` restores the manifest values).
set_vllm_arg() {  # deploy flag value
  kubectl -n $NS get deploy "$1" -o json | python3 -c "
import json, sys
d = json.load(sys.stdin); a = d['spec']['template']['spec']['containers'][0]['args']
i = a.index('$2'); a[i + 1] = '$3'
print(json.dumps([{'op': 'replace', 'path': '/spec/template/spec/containers/0/args', 'value': a}]))" \
  | xargs -0 -I{} kubectl -n $NS patch deploy "$1" --type json -p {}
}
case "${1:-}" in
  ingest)
    curl -fsS -XPOST "$API/workloads/queries" -H 'content-type: application/json' -d '{"paused":true}'; echo
    kubectl -n $NS scale deploy/model-llm --replicas=0
    kubectl -n $NS patch scaledobject model-vlm --type merge -p '{"spec":{"maxReplicaCount":3}}'
    # the agent model's ~17 GB goes to page reading: bigger KV cache and batch per page reader, more workers to fill it
    set_vllm_arg model-vlm --kv-cache-memory-bytes 3221225472
    set_vllm_arg model-vlm --gpu-memory-utilization 0.29
    set_vllm_arg model-vlm --max-num-seqs 24
    kubectl -n $NS patch scaledobject worker-vlm --type merge -p '{"spec":{"maxReplicaCount":72}}'
    kubectl -n $NS patch scaledobject worker-io --type merge -p '{"spec":{"maxReplicaCount":8}}'     # keep downloads ahead of the GPU
    kubectl -n $NS patch configmap docuflow-config --type merge -p '{"data":{"QUERY_PRESSURE_INFLIGHT":"0"}}'
    echo "ingest-priority mode: queries paused, agent model stopped, page reading up to 3 vLLM replicas x 24 slots / 72 workers" ;;
  mac-llm)
    # relay: the SSH tunnel listens on the server's loopback; expose it on the kind network only (not the public IP)
    command -v socat >/dev/null || { echo "install socat first"; exit 1; }
    systemctl is-active --quiet docuflow-mac-llm || systemd-run --unit docuflow-mac-llm -p Restart=always \
      /usr/bin/socat TCP-LISTEN:11434,bind=172.18.0.1,reuseaddr,fork TCP:127.0.0.1:11434
    curl -fsS -m 5 http://127.0.0.1:11434/api/tags >/dev/null || { echo "no Ollama tunnel on 127.0.0.1:11434 (ssh -N -R 11434:localhost:11434 ...)"; exit 1; }
    kubectl apply -f "$(dirname "$0")/../k8s/extras/mac-llm.yaml"
    kubectl -n $NS patch configmap docuflow-config --type merge -p "{\"data\":{\"LLM_BASE_URL\":\"http://mac-llm.$NS.svc.cluster.local:11434/v1\",\"LLM_MODEL\":\"${MAC_LLM_MODEL:-qwen3:4b-instruct}\"}}"
    kubectl -n $NS rollout restart deploy/api && kubectl -n $NS rollout status deploy/api --timeout=300s
    curl -fsS -XPOST "$API/workloads/queries" -H 'content-type: application/json' -d '{"paused":false}'; echo
    echo "queries open, answered by the Mac; the GPU stays on ingest" ;;
  query)
    kubectl -n $NS patch configmap docuflow-config --type merge -p '{"data":{"LLM_BASE_URL":"http://model-llm:8000/v1","LLM_MODEL":"standard-model"}}'
    kubectl -n $NS scale deploy/model-llm --replicas=1
    kubectl -n $NS patch scaledobject model-vlm --type merge -p '{"spec":{"maxReplicaCount":2}}'
    set_vllm_arg model-vlm --max-num-seqs 16
    set_vllm_arg model-vlm --gpu-memory-utilization 0.25
    set_vllm_arg model-vlm --kv-cache-memory-bytes 1610612736
    kubectl -n $NS patch scaledobject worker-vlm --type merge -p '{"spec":{"maxReplicaCount":24}}'
    kubectl -n $NS patch scaledobject worker-io --type merge -p '{"spec":{"maxReplicaCount":3}}'
    kubectl -n $NS patch configmap docuflow-config --type merge -p '{"data":{"QUERY_PRESSURE_INFLIGHT":"24"}}'
    kubectl -n $NS rollout status deploy/model-llm --timeout=600s
    kubectl -n $NS rollout restart deploy/api && kubectl -n $NS rollout status deploy/api --timeout=300s
    curl -fsS -XPOST "$API/workloads/queries" -H 'content-type: application/json' -d '{"paused":false}'; echo
    echo "query mode: agent model up, queries open" ;;
  *) echo "usage: $0 ingest|query|mac-llm"; exit 1 ;;
esac
