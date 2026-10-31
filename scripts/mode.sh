#!/usr/bin/env bash
# Switch the shared-GPU cluster between serving queries and maximum ingest.
#   scripts/mode.sh ingest    pause queries (API answers 503), stop the agent model, give its GPU share to page reading:
#                             model-vlm up to 3 replicas, worker-vlm up to 48, worker-io up to 8, no query back-off
#   scripts/mode.sh query     restore the default: agent model up, queries open, model-vlm 1..2, worker-vlm 0..24
#   scripts/mode.sh mac-llm   keep ingest-priority scaling, but answer queries with an agent model on a Mac (Ollama via
#                             a reverse SSH tunnel; see k8s/extras/mac-llm.yaml). MAC_LLM_MODEL defaults to qwen3:4b-instruct
# Changes are live patches; `make deploy` also restores the defaults.
set -euo pipefail
API=${API:-http://docuflow.localtest.me}
NS=docuflow
case "${1:-}" in
  ingest)
    curl -fsS -XPOST "$API/workloads/queries" -H 'content-type: application/json' -d '{"paused":true}'; echo
    kubectl -n $NS scale deploy/model-llm --replicas=0
    kubectl -n $NS patch scaledobject model-vlm --type merge -p '{"spec":{"maxReplicaCount":3}}'
    kubectl -n $NS patch scaledobject worker-vlm --type merge -p '{"spec":{"maxReplicaCount":48}}'
    kubectl -n $NS patch scaledobject worker-io --type merge -p '{"spec":{"maxReplicaCount":8}}'     # keep downloads ahead of the GPU
    kubectl -n $NS patch configmap docuflow-config --type merge -p '{"data":{"QUERY_PRESSURE_INFLIGHT":"0"}}'
    echo "ingest-priority mode: queries paused, agent model stopped, page reading up to 3 vLLM replicas / 48 workers" ;;
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
    kubectl -n $NS patch scaledobject worker-vlm --type merge -p '{"spec":{"maxReplicaCount":24}}'
    kubectl -n $NS patch scaledobject worker-io --type merge -p '{"spec":{"maxReplicaCount":3}}'
    kubectl -n $NS patch configmap docuflow-config --type merge -p '{"data":{"QUERY_PRESSURE_INFLIGHT":"24"}}'
    kubectl -n $NS rollout status deploy/model-llm --timeout=600s
    kubectl -n $NS rollout restart deploy/api && kubectl -n $NS rollout status deploy/api --timeout=300s
    curl -fsS -XPOST "$API/workloads/queries" -H 'content-type: application/json' -d '{"paused":false}'; echo
    echo "query mode: agent model up, queries open" ;;
  *) echo "usage: $0 ingest|query|mac-llm"; exit 1 ;;
esac
