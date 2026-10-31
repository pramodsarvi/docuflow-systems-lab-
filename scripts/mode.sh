#!/usr/bin/env bash
# Switch the shared-GPU cluster between serving queries and maximum ingest.
#   scripts/mode.sh ingest    pause queries (API answers 503), stop the agent model, give its GPU share to page reading:
#                             model-vlm up to 3 replicas, worker-vlm up to 48, worker-io up to 8, no query back-off
#   scripts/mode.sh query     restore the default: agent model up, queries open, model-vlm 1..2, worker-vlm 0..24
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
  query)
    kubectl -n $NS scale deploy/model-llm --replicas=1
    kubectl -n $NS patch scaledobject model-vlm --type merge -p '{"spec":{"maxReplicaCount":2}}'
    kubectl -n $NS patch scaledobject worker-vlm --type merge -p '{"spec":{"maxReplicaCount":24}}'
    kubectl -n $NS patch scaledobject worker-io --type merge -p '{"spec":{"maxReplicaCount":3}}'
    kubectl -n $NS patch configmap docuflow-config --type merge -p '{"data":{"QUERY_PRESSURE_INFLIGHT":"24"}}'
    kubectl -n $NS rollout status deploy/model-llm --timeout=600s
    curl -fsS -XPOST "$API/workloads/queries" -H 'content-type: application/json' -d '{"paused":false}'; echo
    echo "query mode: agent model up, queries open" ;;
  *) echo "usage: $0 ingest|query"; exit 1 ;;
esac
