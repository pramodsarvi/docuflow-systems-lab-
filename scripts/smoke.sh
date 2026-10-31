#!/usr/bin/env bash
# End-to-end check of a deployed docuflow: health, ingest through RabbitMQ + KEDA-scaled workers, query with citation.
#   scripts/smoke.sh [API]       (default http://docuflow.localtest.me)
set -euo pipefail
API=${1:-http://docuflow.localtest.me}
H=(-H "x-api-key: ${DOCUFLOW_API_KEY:-}" -H 'content-type: application/json')
OFFSET=$(( 100000 + RANDOM * 4 ))   # never-ingested range, so every run really reads pages
N=${N:-6}
MODE=${MODE:-vlm}
j() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }

echo "health: $(curl -fsS "$API/health")"
curl -fsS -XPOST "$API/ingest" "${H[@]}" -d "{\"dataset\":\"synthetic\",\"limit\":$N,\"offset\":$OFFSET,\"mode\":\"$MODE\"}" >/dev/null
echo "queued $N synthetic pages at offset $OFFSET (mode $MODE); waiting for workers to scale from zero and index them"
for i in $(seq 120); do
  IDS=$(python3 -c "print(','.join(f'synthetic/gen/{i}' for i in range($OFFSET, $OFFSET + $N)))")
  n=$(curl -fsS "$API/documents?ids=$IDS" "${H[@]}" 2>/dev/null | j "sum(x['status']=='indexed' for x in d)" || echo 0)
  [ "$n" -ge "$N" ] && break; sleep 5
done
[ "$n" -ge "$N" ] || { echo "FAIL: only $n/$N pages indexed after 10 minutes"; exit 1; }
echo "indexed $N pages after ~$((i * 5))s"
read -r NUM SIGNER < <(python3 - "$OFFSET" <<'PY'
import sys
sys.path.insert(0, ".")
from docuflow import sources
gt = dict(l.split(": ", 1) for l in sources.synthetic_page(int(sys.argv[1]))[1].splitlines()[1:])
print(gt["Number"], gt["Signed by"])
PY
)
R=$(curl -fsS -XPOST "$API/query" "${H[@]}" -d "{\"question\":\"Who signed document $NUM?\"}")
echo "Q: Who signed document $NUM?  (expected: $SIGNER)"
echo "A: $(echo "$R" | j "d['answer']")   citations: $(echo "$R" | j "d['citations']")"
if [ "$MODE" = vlm ] && ! echo "$R" | grep -qi "$SIGNER"; then echo "WARN: answer does not contain the expected name"; fi
echo "$R" | j "d['citations']" | grep -q "synthetic/gen/$OFFSET" || { echo "FAIL: expected citation synthetic/gen/$OFFSET"; exit 1; }
echo "smoke OK"
