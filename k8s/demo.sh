#!/bin/sh
# Burst of ingest work, then sample scaling every few seconds. Needs `make k8s-ui` running in another terminal.
PY=.venv/bin/python
export DOCUFLOW_API=${DOCUFLOW_API:-http://127.0.0.1:8792}
$PY -m docuflow ingest funsd -n 20 >/dev/null
$PY -m docuflow ingest xfund -n 20 >/dev/null
$PY -m docuflow ingest iam -n 20 >/dev/null
echo "queued 60 pages; sampling (t = seconds)"
t0=$(date +%s); asked=0
while :; do
  now=$(( $(date +%s) - t0 ))
  q=$(docker exec docuflow-db true 2>/dev/null; kubectl -n docuflow exec sts/db -- psql -U docuflow -Atc "select count(*) filter (where status='queued' and lane='vlm'), count(*) filter (where status='running' and lane='vlm') from jobs" 2>/dev/null)
  ready=$(kubectl -n docuflow get deploy worker-vlm -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
  want=$(kubectl -n docuflow get deploy worker-vlm -o jsonpath='{.spec.replicas}' 2>/dev/null)
  pend=$(kubectl -n docuflow get pods -l app=worker-vlm --field-selector=status.phase=Pending --no-headers 2>/dev/null | wc -l | tr -d ' ')
  echo "t=${now}s vlm jobs(queued|running)=$q  worker-vlm desired=${want:-0} ready=${ready:-0} pending-for-gpu=$pend"
  if [ $now -gt 40 ] && [ $asked -eq 0 ]; then
    asked=1; echo "--- asking a question mid-ingest"; $PY -m docuflow ask "Sample Person" | head -3; fi
  [ "${q%%|*}" = "0" ] && [ "${q##*|}" = "0" ] && [ $now -gt 20 ] && break
  [ $now -gt 600 ] && break
  sleep 8
done
$PY -m docuflow status
